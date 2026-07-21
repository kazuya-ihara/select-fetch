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
正式反映：python3 product/fetch_claimed.py --rerank --promote-quality --confirm-promote
"""
import os, sys, json, time, argparse, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import product_fetch as pf   # rpc / read_batch_token / fetch_and_save を再利用

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LOCAL_CATALOG = os.path.join(ROOT, "data", "catalog", "latest.json")

# 何日前までの「対応済み切り口」を取得対象にするか（環境変数 CLAIM_DAYS で上書き可）
CLAIM_DAYS = int(os.environ.get("CLAIM_DAYS", "2"))
SNAPSHOT_SCHEMA_VERSION = 1
SNAPSHOT_MAX_AGE_HOURS = 72
TARGET_SPEC_ENV = "FETCH_TARGET"


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


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def write_shadow_snapshot(path, catalog_date, items):
    """影テストで実際に採用候補になった行だけを、機密なしで保存する。"""
    if not path:
        return
    payload = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "generated_at": _utc_now().isoformat(),
        "catalog_date": catalog_date,
        "items": items,
    }
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print("影テスト結果を保存しました（正式反映で再検索しません）: %s" % path)


def load_shadow_snapshot(path):
    """保存済み影テストを検証して、切り口キーから引ける形にする。

    壊れたファイル・古すぎる結果・重複キーは安全側に全体を無効にする。
    """
    if not path or not os.path.exists(path):
        print("‼ 影テスト結果がありません。正式反映を中止します（既存結果は変更しません）。")
        return None
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        if payload.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
            raise ValueError("未対応の形式")
        generated = datetime.datetime.fromisoformat(str(payload.get("generated_at", "")).replace("Z", "+00:00"))
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=datetime.timezone.utc)
        age = (_utc_now() - generated).total_seconds() / 3600
        if age < -1 or age > SNAPSHOT_MAX_AGE_HOURS:
            raise ValueError("有効期限切れ（%.1f時間前）" % age)
        items = payload.get("items")
        if not isinstance(items, list):
            raise ValueError("items が配列ではありません")
        out = {}
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("切り口データが不正です")
            key = (item.get("catalog_date"), item.get("theme"), item.get("angle") or "")
            rows = item.get("rows")
            if not key[0] or not key[1] or not isinstance(rows, list) or not rows:
                raise ValueError("切り口または商品行が不正です")
            if key in out:
                raise ValueError("同じ切り口が重複しています")
            out[key] = item
        return out
    except Exception as e:
        print("‼ 影テスト結果を検証できません。正式反映を中止します（%s）。" % e)
        return None


def main():
    ap = argparse.ArgumentParser(description="対応済み切り口の商品を取得")
    ap.add_argument("--rerank", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--shadow", action="store_true", help="保存せず新候補とAI評価だけを表示")
    ap.add_argument("--promote-quality", action="store_true",
                    help="高品質条件を満たす候補だけ既存結果を置き換える")
    ap.add_argument("--confirm-promote", action="store_true",
                    help="正式反映を明示的に確認したことを示す（必須）")
    ap.add_argument("--snapshot-out", help="影テスト結果の保存先（正式反映で再利用）")
    ap.add_argument("--snapshot-file", help="保存済み影テスト結果（再検索せず正式反映）")
    args = ap.parse_args()

    if args.promote_quality and not args.confirm_promote:
        print("‼ 正式反映には --confirm-promote が必要です。影テストだけなら --shadow を使ってください。")
        sys.exit(2)

    # GitHubの手動実行は、誤操作で全切り口を一度に再取得しないよう安全側に制限する。
    # 定期実行(schedule)とMac実行には影響しない。必要なら環境変数で上限だけ調整できる。
    manual_run = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    run_limit = args.limit
    # 手動実行は影テストを標準にする。確認前に本番表示を入れ替えないため。
    run_shadow = (args.shadow or manual_run) and not args.promote_quality
    run_force = args.force and not run_shadow
    run_promote = args.promote_quality
    if manual_run:
        manual_cap = int(os.environ.get("MANUAL_TEST_LIMIT", "3"))
        run_limit = min(args.limit, max(1, manual_cap))
        if run_promote:
            print("正式反映モード: 最大%d切り口・高品質条件を満たす時だけ置き換え" % run_limit)
        else:
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

    target_spec = os.environ.get(TARGET_SPEC_ENV, "").strip()
    if target_spec:
        if "|" not in target_spec:
            print("‼ 対象指定の形式が不正です（テーマ|切り口で指定してください）。既存データは変更しません。")
            return
        target_theme, target_angle = [part.strip() for part in target_spec.split("|", 1)]
        targets = [t for t in targets if t[1] == target_theme and t[2] == target_angle]
        print("対象指定: %s / %s" % (target_theme, target_angle))
        if not targets:
            print("指定された切り口が対応済み一覧にありません。既存データは変更しません。")
            return

    print("=" * 60)
    print("最新カタログ=%s ／ 対応済み切り口=%d件（直近7日） ／ rerank=%s" % (date, len(targets), args.rerank))
    print("=" * 60)
    if not targets:
        print("対応済みの切り口がありません（アプリで『対応する』を押すと対象になります）。")
        return

    stats = {"saved": 0, "promoted": 0, "shadow": 0, "skip": 0, "empty": 0, "error": 0, "limit": 0, "nokw": 0}
    done_fetch = 0
    shadow_capture = []
    snapshot = load_shadow_snapshot(args.snapshot_file) if run_promote else None
    if run_promote and snapshot is None:
        print("正式反映は行いません。先に同じ切り口の影テストを実行してください。")
        return
    for cdate, theme, angle, spec in targets:
        if done_fetch >= run_limit:
            print("  上限 %d 件に達したので停止。" % run_limit); break
        kw = (spec or {}).get("kw")
        if not kw:
            print("  × kw不明（カタログに該当切り口なし）: %s / %s" % (theme, angle))
            pf.quality_event("nokw", cdate, theme, angle,
                             reason="カタログに該当する検索語がない", mode="promote" if run_promote else "normal")
            stats["nokw"] += 1
            continue
        components = (spec or {}).get("components") or []
        snapshot_item = snapshot.get((cdate, theme, angle)) if snapshot is not None else None
        if run_promote:
            if snapshot_item is None:
                print("  ⏭ 影テスト結果にないため保留（既存結果は変更しません）: %s / %s" % (theme, angle))
                pf.quality_event("skip", cdate, theme, angle,
                                 reason="影テスト結果にないため正式反映しない", mode="promote")
                stats["skip"] += 1
                continue
            if snapshot_item.get("kw") != kw or snapshot_item.get("components") != components:
                print("  ⏭ 検索条件が影テスト時と違うため保留（既存結果は変更しません）: %s / %s"
                      % (theme, angle))
                pf.quality_event("skip", cdate, theme, angle,
                                 reason="影テスト時と検索条件が違うため保留", mode="promote")
                stats["skip"] += 1
                continue
            print("  ♻ 影テスト結果を再利用（再検索・AI再採点なし）: %s / %s" % (theme, angle))
            # 影テストで合格候補になった行だけを、同じ品質ゲートで正式保存する。
            existing_count = None
            try:
                existing_count = pf.rpc("v2_product_has", {
                    "p_secret": token, "p_catalog_date": cdate,
                    "p_theme": theme, "p_angle_title": angle or ""})
                if not isinstance(existing_count, int):
                    existing_count = None
            except Exception as e:
                print("  has確認失敗（この切り口は保留）: %s: %s" % (type(e).__name__, e))
                stats["error"] += 1
                continue
            r = pf.persist_rows(token, cdate, theme, angle, snapshot_item["rows"], args.rerank,
                                force=False, promote=True, existing_count=existing_count,
                                count_usage=False)
        else:
            r = pf.fetch_and_save(token, cdate, theme, angle, kw, args.rerank, run_force,
                                  components=components, shadow=run_shadow, promote=run_promote,
                                  shadow_capture=shadow_capture)
        stats[r] = stats.get(r, 0) + 1
        if r == "limit":
            break
        if r in ("saved", "promoted", "shadow"):
            done_fetch += 1
            time.sleep(pf.SLEEP_BETWEEN_ANGLES)

    if run_shadow:
        try:
            write_shadow_snapshot(args.snapshot_out, date, shadow_capture)
        except Exception as e:
            # 影テスト自体は表示できているため、保存失敗を明確に記録する。
            print("‼ 影テスト結果の保存に失敗しました（正式反映には使えません）: %s: %s"
                  % (type(e).__name__, e))

    print("\n" + "=" * 60)
    print("完了: 保存%d / 品質昇格%d / 影テスト%d / スキップ%d / 空%d / エラー%d / kw不明%d"
          % (stats["saved"], stats["promoted"], stats["shadow"], stats["skip"],
             stats["empty"], stats["error"], stats["nokw"]))


if __name__ == "__main__":
    main()
