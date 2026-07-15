#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
係② 商品あつめ — ③ v2_product へ保存＋発火（Mac実行・標準ライブラリのみ）

build_candidates.py（①除外/照合ゲート＋②多源コンセンサス＋AIリランク）を呼び、
結果を Supabase v2_product に保存する。書込は run.py と同じ「公開キー＋p_secret
トークン＋SECURITY DEFINER RPC」方式（全権キーは使わない）。

発火（どう動かすか）:
  - 担当した瞬間 = 1件だけ即取得:
      python3 product/product_fetch.py --theme "睡眠" --angle "高さ調整" --kw "枕 高さ調整" [--rerank]
  - 夜間の先読み = キューから未取得だけをN件:
      python3 product/product_fetch.py --queue product/fetch_queue.json --limit 10 [--rerank]

ピン留め（Amazon非決定性対策）:
  同一 catalog_date × テーマ × 切り口 は一度保存したら再取得しない（v2_product_has で判定）。
  作り直したい時だけ --force で入れ替え。

ガード:
  日次上限（product_usage.json の angles_fetched）と切り口間ウェイトで無料枠を超えない。

鍵/トークン:
  - 商品検索/AIキー: build_candidates.py が読む product/*.json（そのまま）
  - Supabase書込トークン: run.py と同じ sources/service_key.txt の1行（'batch'シークレット）
"""
import os, re, sys, json, time, argparse, datetime, subprocess
import urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SOURCES_DIR = os.path.join(ROOT, "sources")
BUILD = os.path.join(HERE, "build_candidates.py")
USAGE = os.path.join(HERE, "product_usage.json")

# Supabase（run.py と同じ公開キー。RLSで保護され埋め込み可）
# SupabaseプロジェクトURL。公開リポジトリにプロジェクトIDを直書きしないよう環境変数を優先。
# 未設定ならローカル(Mac)用の既定値を使う。※このURLはフロント公開分と同じでRLS＋トークン保護。
SB_URL = os.environ.get("SUPABASE_URL") or ""
SB_PUBLISHABLE = "sb_publishable_hbtP3WrNCJp0BUuBrDs4Ww_6x79K4uc"

# ガード既定値（慎重運用。必要なら調整）
DAILY_ANGLE_LIMIT = int(os.environ.get("DAILY_ANGLE_LIMIT", "40"))  # env で上書き可（公開先読みは300）
SLEEP_BETWEEN_ANGLES = 2.0  # 切り口間のウェイト（秒）


def today_str():
    # JST（Macのローカル時刻）基準の当日
    return datetime.date.today().isoformat()


# ---------- Supabase 書込トークン（run.py と同じ読み方） ----------
def read_batch_token():
    kp = os.path.join(SOURCES_DIR, "service_key.txt")
    if os.path.exists(kp):
        for line in open(kp, encoding="utf-8"):
            s = line.strip()
            if s and not s.startswith("#") and s != "ここにキーを貼る":
                return s
    cfg_path = os.path.join(SOURCES_DIR, "supabase_config.json")
    if os.path.exists(cfg_path):
        try:
            cfg = json.load(open(cfg_path, encoding="utf-8"))
            return cfg.get("batch_token") or cfg.get("service_key")
        except Exception:
            pass
    return None


def rpc(name, payload, timeout=25):
    body = json.dumps(payload).encode("utf-8")
    endpoint = SB_URL + "/rest/v1/rpc/" + name
    req = urllib.request.Request(endpoint, data=body, method="POST", headers={
        "apikey": SB_PUBLISHABLE,
        "Authorization": "Bearer " + SB_PUBLISHABLE,
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        txt = r.read().decode("utf-8")
        return json.loads(txt) if txt.strip() else None


def latest_catalog_date(token):
    """Supabaseの最新カタログ日を返す（取れなければ None）。"""
    try:
        c = rpc("v2_catalog_pull", {"p_secret": token})
        if c and c.get("date"):
            return c["date"]
    except Exception as e:
        print("  最新カタログ日の取得に失敗（今日の日付を使う）:", type(e).__name__, e)
    return None


# ---------- 日次上限ガード ----------
def usage_load():
    if os.path.exists(USAGE):
        try:
            u = json.load(open(USAGE, encoding="utf-8"))
            if u.get("date") == today_str():
                return int(u.get("angles_fetched", 0))
        except Exception:
            pass
    return 0


def usage_bump():
    n = usage_load() + 1
    try:
        json.dump({"date": today_str(), "angles_fetched": n},
                  open(USAGE, "w", encoding="utf-8"))
    except Exception as e:
        print("  使用量の記録失敗:", e)
    return n


# ---------- build_candidates.py を呼んで pool(JSON) を得る ----------
def clean_price(p):
    if not p:
        return None
    digits = re.sub(r"[^\d]", "", str(p))
    return int(digits) if digits else None


def build_pool(kw, rerank):
    """build_candidates.py を --json で呼び、最終プールの配列を返す。"""
    cmd = [sys.executable, BUILD, kw, "--json"]
    if rerank:
        cmd.append("--rerank")
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        print("  build_candidates がタイムアウト"); return None
    if out.returncode != 0:
        # die() の理由は stdout 末尾に出る（候補ゼロ / Amazon解決0件 など）
        reason = (out.stderr or "").strip()
        if not reason and out.stdout:
            lines = [l.strip() for l in out.stdout.splitlines() if l.strip()]
            reason = lines[-1] if lines else ""
        print("  この切り口は取得できず（%s）→ スキップ" % ((reason or "候補ゼロ/Amazon解決0件")[:120])); return None
    txt = out.stdout
    if "---- JSON ----" not in txt:
        print("  JSON出力が見つからない（キー未設定か候補ゼロ？）"); return None
    tail = txt.split("---- JSON ----", 1)[1]
    tail = tail.split("\n==== 見方", 1)[0].strip()
    try:
        return json.loads(tail)
    except Exception as e:
        print("  JSON解析失敗:", e); return None


def to_rows(pool):
    rows = []
    for i, it in enumerate(pool, 1):
        if not it.get("asin"):
            continue
        rows.append({
            "asin": it.get("asin"),
            "title": it.get("title"),
            "url": it.get("url"),
            "image_url": it.get("image_url"),
            "price": clean_price(it.get("price")),
            "brand": it.get("brand"),
            "star_rating": None,                       # Amazon API非提供。⑤ Edgeで後埋め
            "review_count": it.get("review_count"),
            "rank": i,
            "source": it.get("candidate_source") or "creators_api",
            "consensus": it.get("consensus"),
            "sources": "/".join(it.get("sources") or []),
            "verdict": it.get("verdict"),
            "match_reason": it.get("reason"),
            "ai_score": it.get("ai_score"),
            "ai_reason": it.get("ai_reason"),
        })
    return rows


# ---------- 1切り口を取得して保存 ----------
def fetch_and_save(token, catalog_date, theme, angle, kw, rerank, force):
    label = "%s / %s" % (theme, angle or "(テーマ単位)")
    # ピン留め判定
    if not force:
        try:
            n = rpc("v2_product_has", {
                "p_secret": token, "p_catalog_date": catalog_date,
                "p_theme": theme, "p_angle_title": angle or ""})
            if isinstance(n, int) and n > 0:
                print("  ⏭ 既に保存済み(%d件)なのでスキップ: %s" % (n, label))
                return "skip"
        except Exception as e:
            print("  has確認失敗（続行）:", e)
    # 日次上限
    used = usage_load()
    if used >= DAILY_ANGLE_LIMIT:
        print("  ⛔ 日次上限(%d切り口)に到達。今日はこれ以上取得しない。" % DAILY_ANGLE_LIMIT)
        return "limit"

    print("  ▶ 取得: %s（kw=%s）rerank=%s" % (label, kw, rerank))
    pool = build_pool(kw, rerank)
    if not pool:
        print("  × プールが空。保存せず。"); return "empty"
    rows = to_rows(pool)
    usage_bump()
    try:
        res = rpc("v2_upsert_product", {
            "p_secret": token, "p_catalog_date": catalog_date,
            "p_theme": theme, "p_angle_title": angle or "",
            "p_products": rows, "p_replace": bool(force)})
        if res == -1:
            print("  ⏭ 既存ありスキップ（force未指定）: %s" % label); return "skip"
        print("  ✅ 保存 %s 件: %s" % (res, label)); return "saved"
    except urllib.error.HTTPError as e:
        print("  保存失敗 HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:200]))
        return "error"
    except Exception as e:
        print("  保存失敗: %s: %s" % (type(e).__name__, e)); return "error"


def main():
    ap = argparse.ArgumentParser(description="v2_product へ商品を取得・保存（③）")
    ap.add_argument("--theme")
    ap.add_argument("--angle", default="")
    ap.add_argument("--kw")
    ap.add_argument("--queue", help="{theme,angle_title,kw} のJSON配列ファイル（夜間先読み用）")
    ap.add_argument("--limit", type=int, default=10, help="キューから取得する最大件数")
    ap.add_argument("--date", default=None,
                    help="catalog_date（既定=Supabaseの最新カタログ日／取れなければ今日）")
    ap.add_argument("--rerank", action="store_true", help="AIリランクも実行")
    ap.add_argument("--force", action="store_true", help="ピン留めを無視して入れ替え")
    args = ap.parse_args()

    token = read_batch_token()
    if not token:
        print("‼ 書込トークン未設定（sources/service_key.txt）。run.pyと同じ'batch'トークンを置く。")
        sys.exit(1)

    # 保存する catalog_date は「カタログの日付」に合わせる。
    # ※以前は今日固定だったため、カタログ生成が失敗/遅延した日に
    #   catalog(昨日) と product(今日) の日付がズレ、アプリ側が常に空に見える事故になった。
    if not args.date:
        args.date = latest_catalog_date(token) or today_str()

    print("=" * 60)
    print("v2_product 取得・保存  catalog_date=%s  rerank=%s  force=%s"
          % (args.date, args.rerank, args.force))
    print("本日の取得済み切り口数: %d / 上限%d" % (usage_load(), DAILY_ANGLE_LIMIT))
    print("=" * 60)

    jobs = []
    if args.queue:
        try:
            q = json.load(open(args.queue, encoding="utf-8"))
        except Exception as e:
            print("‼ キュー読込失敗:", e); sys.exit(1)
        for e in q:
            kw = e.get("kw") or e.get("angle_title") or e.get("theme")
            jobs.append((e.get("theme", ""), e.get("angle_title", ""), kw))
    elif args.theme and args.kw:
        jobs.append((args.theme, args.angle, args.kw))
    else:
        print("‼ 使い方: --theme/--kw（1件）か --queue（先読み）。--help 参照"); sys.exit(1)

    stats = {"saved": 0, "skip": 0, "empty": 0, "error": 0, "limit": 0}
    done_fetch = 0
    for theme, angle, kw in jobs:
        if args.queue and done_fetch >= args.limit:
            print("  上限 %d 件に達したので停止。" % args.limit); break
        r = fetch_and_save(token, args.date, theme, angle, kw, args.rerank, args.force)
        stats[r] = stats.get(r, 0) + 1
        if r == "limit":
            break
        if r == "saved":       # 実取得した時だけ間隔を空ける
            done_fetch += 1
            time.sleep(SLEEP_BETWEEN_ANGLES)

    print("\n" + "=" * 60)
    print("完了: 保存%d / スキップ%d / 空%d / エラー%d / 上限停止%d"
          % (stats["saved"], stats["skip"], stats["empty"], stats["error"], stats["limit"]))
    print("本日の取得済み切り口数: %d / 上限%d" % (usage_load(), DAILY_ANGLE_LIMIT))


if __name__ == "__main__":
    main()
