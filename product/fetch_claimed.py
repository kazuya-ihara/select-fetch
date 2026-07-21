#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
係② 対応済み切り口の商品取得（GitHub Actions／Mac どちらでも・標準ライブラリのみ）

アプリで「対応する」を押された切り口（v2_claim, kind=angle, status=claimed/done）を拾い、
その切り口の検索キーワード(kw)を最新カタログから引いて、まだ v2_product に無いものだけ
取得して保存する。product_fetch.py の処理を再利用。

カタログは Supabase の最新（v2_catalog_pull）を読む。取得できない時だけローカル
data/catalog/latest.json を使う。日付はカタログの date に合わせる（対応する を押した
カタログと同じ日付で処理＝ズレない）。

実行：python3 product/fetch_claimed.py [--rerank] [--limit N] [--force]
"""
import os, sys, json, time, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import product_fetch as pf   # rpc / read_batch_token / fetch_and_save を再利用

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LOCAL_CATALOG = os.path.join(ROOT, "data", "catalog", "latest.json")

# 何日前までの「対応済み切り口」を取得対象にするか（環境変数 CLAIM_DAYS で上書き可）
CLAIM_DAYS = int(os.environ.get("CLAIM_DAYS", "2"))


def load_catalog(token, date=None):
    """(date, payload) を返す。date 指定でその日のカタログ、無指定で最新。"""
    try:
        args = {"p_secret": token}
        if date:
            args["p_date"] = date
        c = pf.rpc("v2_catalog_pull", args)
        if c and c.get("payload"):
            return c.get("date"), c["payload"]
    except Exception as e:
        print("  カタログ(Supabase)取得に失敗、ローカルを試す:", type(e).__name__, e)
    if os.path.exists(LOCAL_CATALOG):
        d = json.load(open(LOCAL_CATALOG, encoding="utf-8"))
        return d.get("date"), d
    return None, None


def angle_map(payload):
    m = {}
    for theme, data in (payload.get("ANGLE_DATA") or {}).items():
        for a in (data.get("angles") or []):
            if a.get("t") and a.get("kw"):
                m[(theme, a["t"])] = {
                    "kw": a["kw"],
                    "components": a.get("c") or [],
                }
    return m


def main():
    ap = argparse.ArgumentParser(description="対応済み切り口の商品を取得")
    ap.add_argument("--rerank", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--shadow", action="store_true", help="保存せず新候補とAI評価だけを表示")
    args = ap.parse_args()

    # GitHubの手動実行は、誤操作で全切り口を一度に再取得しないよう安全側に制限する。
    # 定期実行(schedule)とMac実行には影響しない。必要なら環境変数で上限だけ調整できる。
    manual_run = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    run_limit = args.limit
    # 手動実行は影テストを標準にする。確認前に本番表示を入れ替えないため。
    run_shadow = args.shadow or manual_run
    run_force = args.force and not run_shadow
    if manual_run:
        manual_cap = int(os.environ.get("MANUAL_TEST_LIMIT", "3"))
        run_limit = min(args.limit, max(1, manual_cap))
        print("手動テストモード: 最大%d切り口・保存せず新候補を確認" % run_limit)
    elif args.shadow:
        print("影テストモード: 保存せず新候補を確認")

    token = pf.read_batch_token()
    if not token:
        print("‼ 書込トークン未設定（sources/service_key.txt / BATCH_TOKEN）"); sys.exit(1)

    date, payload = load_catalog(token)
    if not payload:
        print("‼ カタログが取得できませんでした（v2_catalog が空？先に run.py を実行）"); sys.exit(1)

    # 直近2日に絞る。※7日にすると、商品が取れない切り口（検索で0件になる切り口）を
    #   30分おきに何日も再取得し続けてAPI枠を浪費する。通常運用は当日中に対応→取得なので2日で足りる。
    #   過去分をまとめて取り込みたい時だけ CLAIM_DAYS を増やす。
    try:
        claims = pf.rpc("v2_claim_pull", {"p_secret": token, "p_days": CLAIM_DAYS}) or []
    except Exception as e:
        print("対応済み切り口の取得に失敗:", type(e).__name__, e); sys.exit(1)

    # 対応日ごとにその日のカタログを読み、kwを引く（切り口名は日々変わるので日付を合わせる）
    angle_cache = {}   # cdate -> {(theme, angle): {kw, components}}
    def angles_for(cdate):
        if cdate not in angle_cache:
            _, p = load_catalog(token, cdate) if cdate else (None, payload)
            angle_cache[cdate] = angle_map(p) if p else {}
        return angle_cache[cdate]

    # 直近7日の対応済み切り口を、その対応の catalog_date ごとに拾う（日付ズレに強い）
    targets, seen = [], set()
    for c in claims:
        cdate = c.get("catalog_date")
        theme = c.get("theme"); angle = c.get("angle_title") or ""
        key = (cdate, theme, angle)
        if key in seen:
            continue
        seen.add(key)
        spec = angles_for(cdate).get((theme, angle))
        targets.append((cdate, theme, angle, spec))

    print("=" * 60)
    print("最新カタログ=%s ／ 対応済み切り口=%d件（直近7日） ／ rerank=%s" % (date, len(targets), args.rerank))
    print("=" * 60)
    if not targets:
        print("対応済みの切り口がありません（アプリで『対応する』を押すと対象になります）。")
        return

    stats = {"saved": 0, "shadow": 0, "skip": 0, "empty": 0, "error": 0, "limit": 0, "nokw": 0}
    done_fetch = 0
    for cdate, theme, angle, spec in targets:
        if done_fetch >= run_limit:
            print("  上限 %d 件に達したので停止。" % run_limit); break
        kw = (spec or {}).get("kw")
        if not kw:
            print("  × kw不明（カタログに該当切り口なし）: %s / %s" % (theme, angle)); stats["nokw"] += 1; continue
        r = pf.fetch_and_save(token, cdate, theme, angle, kw, args.rerank, run_force,
                              components=(spec or {}).get("components") or [],
                              shadow=run_shadow)
        stats[r] = stats.get(r, 0) + 1
        if r == "limit":
            break
        if r in ("saved", "shadow"):
            done_fetch += 1
            time.sleep(pf.SLEEP_BETWEEN_ANGLES)

    print("\n" + "=" * 60)
    print("完了: 保存%d / 影テスト%d / スキップ%d / 空%d / エラー%d / kw不明%d"
          % (stats["saved"], stats["shadow"], stats["skip"], stats["empty"],
             stats["error"], stats["nokw"]))


if __name__ == "__main__":
    main()
