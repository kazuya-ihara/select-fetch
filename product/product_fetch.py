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
import socket as _socket; _socket.setdefaulttimeout(90)  # 保険：明示timeout無しの通信でも固まらない
from build_candidates import apply_intent_category_evidence
from quality_report import record_event

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
SLEEP_BETWEEN_ANGLES = 1.0  # 切り口間のウェイト（秒）
REL_MIN = 70                 # build_candidates.py の公開品質基準と合わせる
MIN_SAVE_COUNT = 3          # 0〜2件は候補収集失敗とみなし、既存結果を保持
# 「9件の旧結果」から、少数でも質の高い結果へ昇格させる時の追加条件。
# 通常保存・影テストの条件より厳しくし、明示的な --promote-quality の時だけ使う。
# 品質昇格は最低5件を必須にする。古いActions設定が4件を渡しても
# 作業用の合格条件が緩まないよう、コード側で下限を固定する。
PROMOTE_MIN_COUNT = max(5, int(os.environ.get("PROMOTE_MIN_COUNT", "5")))
PROMOTE_MIN_SCORE = int(os.environ.get("PROMOTE_MIN_SCORE", "80"))
PROMOTE_AVG_SCORE = int(os.environ.get("PROMOTE_AVG_SCORE", "85"))
QUALITY_REPORT_PATH = os.environ.get("QUALITY_REPORT_PATH", "").strip()

POOL_REASON_LABELS = {
    "config_missing": "AI設定がない",
    "search_empty": "検索結果が0件（Amazon関連性・評価・救済）",
    "ai_incomplete": "AI採点未完了",
    "ai_budget_exhausted": "AI共有予算の上限",
    "ai_http_429": "AI無料枠・レート制限（429）",
    "ai_http_5xx": "AI一時障害（5xx）",
    "ai_http_error": "AI通信エラー",
    "ai_network_error": "AI通信失敗",
    "ai_timeout": "AI通信タイムアウト",
    "ai_parse_error": "AI応答形式エラー",
    "ai_empty_response": "AI応答が空",
    "ai_filtered": "AI/品質フィルター後0件",
    "safety_filtered": "表示前の安全フィルターで除外",
    "candidate_format": "候補データの形式不正",
    "timeout": "取得処理タイムアウト",
    "build_error": "取得処理エラー",
    "unknown_empty": "候補プールが空（原因不明）",
}


def parse_candidate_diagnostic(text):
    """build_candidates.py が出す件数だけの内訳を安全に読み取る。"""
    match = re.search(r"^CANDIDATE_DIAGNOSTIC:(\{.*\})\s*$", text or "", re.MULTILINE)
    if not match:
        return None
    try:
        value = json.loads(match.group(1))
    except Exception:
        return None
    if not isinstance(value, dict):
        return None
    # 表示・ログに使うのは固定キーの整数だけ。商品情報や検索語は受け取らない。
    out = {}
    for key in ("search_candidates", "lane_or_brand_limited", "duplicate_removed",
                "variant_removed", "ai_low_score", "ai_unscored", "final_candidates"):
        number = value.get(key)
        if isinstance(number, int) and number >= 0:
            out[key] = number
    reason = value.get("empty_reason")
    if isinstance(reason, str) and re.fullmatch(r"[a-z_]{1,40}", reason):
        out["empty_reason"] = reason
    return out or None


def print_candidate_diagnostic(diag):
    """候補がどこで減ったかを、非エンジニア向けに1行で表示する。"""
    if not diag:
        return
    duplicates = (diag.get("duplicate_removed", 0)
                  + diag.get("variant_removed", 0))
    print("  候補内訳: 検索候補%d件 / 整理・ブランド上限%d件 / 重複・類似除外%d件"
          " / AI低評価%d件 / AI未採点%d件 / 最終候補%d件"
          % (diag.get("search_candidates", 0),
             diag.get("lane_or_brand_limited", 0),
             duplicates,
             diag.get("ai_low_score", 0),
             diag.get("ai_unscored", 0),
             diag.get("final_candidates", 0)))


def today_str():
    # JST（Macのローカル時刻）基準の当日
    return datetime.date.today().isoformat()


def quality_event(status, catalog_date, theme, angle, rows=None, reason="", mode=""):
    """品質レポート用の記録。失敗しても商品取得そのものは止めない。"""
    record_event(QUALITY_REPORT_PATH, status, catalog_date, theme, angle,
                 scores=[r.get("ai_score") for r in (rows or [])],
                 reason=reason, mode=mode)


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


def build_pool(kw, rerank, theme="", angle="", components=None, return_reason=False):
    """検索kwと切り口意図を分離して build_candidates.py へ渡す。"""
    def finish(pool, reason=""):
        return (pool, reason) if return_reason else pool

    def reason_from_output(text):
        match = re.search(r"^QUALITY_REASON:([a-z_]+)\s*$", text or "", re.MULTILINE)
        reason = match.group(1) if match else "unknown_empty"
        if reason != "ai_incomplete":
            return reason
        # build_candidates は秘密情報を出さず、許可済み固定コードだけを返す。
        diag = re.search(
            r"^AI_DIAGNOSTIC:(config_missing|budget_exhausted|http_429|http_5xx|"
            r"http_error|network_error|timeout|parse_error|empty_response)\s*$",
            text or "", re.MULTILINE)
        if diag:
            return "ai_" + diag.group(1)
        return reason

    cmd = [sys.executable, BUILD, kw, "--json"]
    if theme:
        cmd += ["--theme", theme]
    if angle:
        cmd += ["--intent", angle]
    if components:
        cmd += ["--components-json", json.dumps(components, ensure_ascii=False)]
    if rerank:
        cmd.append("--rerank")
    # 子プロセス(build_candidates)が Gemini共有予算RPC を叩けるよう SUPABASE_URL を渡す。
    env = dict(os.environ)
    if SB_URL:
        env["SUPABASE_URL"] = SB_URL
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=env)
    except subprocess.TimeoutExpired:
        print("  build_candidates がタイムアウト"); return finish(None, "timeout")
    if out.returncode != 0:
        print("  この切り口は取得処理エラー（終了コード%d）→ スキップ" % out.returncode)
        return finish(None, "build_error")
    txt = out.stdout
    print_candidate_diagnostic(parse_candidate_diagnostic(txt))
    if "---- JSON ----" not in txt:
        print("  JSON出力が見つからない（候補結果の形式不正）"); return finish(None, "candidate_format")
    tail = txt.split("---- JSON ----", 1)[1]
    tail = tail.split("\n==== 見方", 1)[0].strip()
    try:
        rows = json.loads(tail)
        if not isinstance(rows, list):
            print("  候補結果が配列ではない（候補データの形式不正）")
            return finish(None, "candidate_format")
        # 子プロセスへテーマ情報が届かない経路や古い候補形式でも、保存直前に同じ安全弁を通す。
        # flat な title/features 形式にも対応するため、AIの低評価を人向け商品がすり抜けない。
        filtered = apply_intent_category_evidence(
            rows, theme=theme or kw, angle_title=angle or kw)
        if len(filtered) < len(rows):
            print("  ペット冷感カテゴリフィルタ（保存前）：人向け候補%d件を除外"
                  % (len(rows) - len(filtered)))
        if rows and not filtered:
            return finish([], "safety_filtered")
        if not filtered:
            return finish([], reason_from_output(txt))
        return finish(filtered, "")
    except Exception:
        print("  JSON解析失敗（候補データの形式不正）")
        return finish(None, "candidate_format")


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
            "features": it.get("features") or [],       # Amazonの特徴（bullet）。商品カードの説明に表示
        })
    return rows


def validate_new_result(rows, rerank):
    """新結果が明らかに劣化している時は、DBへ渡す前に止める。

    現行RPCは旧商品の詳細を返さないため、前回との完全比較ではなく、
    「3件以上・全件AI70点以上・ASIN重複なし」を上書きの必須条件とする。
    """
    rows = list(rows or [])
    if len(rows) < MIN_SAVE_COUNT:
        return False, "適格品%d件（0〜2件は再試行対象）" % len(rows)
    asins = [r.get("asin") for r in rows if r.get("asin")]
    if len(asins) != len(set(asins)):
        return False, "ASIN重複あり"
    if rerank:
        bad = [r for r in rows if r.get("ai_score") is None or r.get("ai_score") < REL_MIN]
        if bad:
            return False, "未採点またはAI%d点未満=%d件" % (REL_MIN, len(bad))
    return True, ""


def validate_quality_promotion(rows, existing_count, rerank):
    """既存の大きな結果を、少数の高品質結果へ置き換えてよいか判定する。

    これは通常保存より厳しい「昇格」専用ゲート。既存件数を下回ること自体は
    拒否理由にせず、AIスコアの下限・平均・重複だけで安全性を担保する。
    旧結果の件数しか取得できないため、既存商品とのスコア比較は行わない。
    """
    if existing_count is None:
        return False, "既存件数を確認できないため保留"
    ok, reason = validate_new_result(rows, rerank)
    if not ok:
        return False, reason
    if len(rows) < PROMOTE_MIN_COUNT:
        return False, "昇格は%d件以上（新結果=%d件）" % (PROMOTE_MIN_COUNT, len(rows))
    if not rerank:
        return False, "AIリランクなしの昇格は不可"
    scores = [r.get("ai_score") for r in rows]
    if any(not isinstance(s, (int, float)) for s in scores):
        return False, "AIスコア不明"
    min_score = min(scores)
    avg_score = sum(scores) / len(scores)
    if min_score < PROMOTE_MIN_SCORE:
        return False, "最低AI%d点未満（最低=%d）" % (PROMOTE_MIN_SCORE, min_score)
    if avg_score < PROMOTE_AVG_SCORE:
        return False, "平均AI%d点未満（平均=%.1f）" % (PROMOTE_AVG_SCORE, avg_score)
    if existing_count == 0:
        return True, "新規登録・平均AI%.1f点" % avg_score
    return True, "最低AI%d点・平均AI%.1f点" % (min_score, avg_score)


def persist_rows(token, catalog_date, theme, angle, rows, rerank,
                 force=False, promote=False, existing_count=None,
                 count_usage=True):
    """取得済みの行を品質確認して v2_product に保存する。

    影テスト結果の再利用時は、検索・AI評価を行わずこの関数だけを呼ぶ。
    count_usage=False にすると、過去の取得を正式反映するだけなので日次使用量を
    増やさない。既存の通常取得と同じ品質ゲート・上書きガードを通す。
    """
    label = "%s / %s" % (theme, angle or "(テーマ単位)")
    rows = list(rows or [])
    if existing_count is None:
        try:
            existing_count = rpc("v2_product_has", {
                "p_secret": token, "p_catalog_date": catalog_date,
                "p_theme": theme, "p_angle_title": angle or ""})
            if not isinstance(existing_count, int):
                existing_count = None
        except Exception as e:
            print("  has確認失敗（保存を保留）:", type(e).__name__, e)
            existing_count = None

    quality_ok, quality_reason = validate_new_result(rows, rerank)
    if not quality_ok:
        print("  ⚠ 新結果の品質ゲート不通過（%s）。保存せず既存データを保護: %s"
              % (quality_reason, label))
        quality_event("quality_hold", catalog_date, theme, angle, rows,
                      reason=quality_reason, mode="promote" if promote else "normal")
        return "empty"
    if promote:
        promote_ok, promote_reason = validate_quality_promotion(rows, existing_count, rerank)
        if not promote_ok:
            print("  ⚠ 昇格条件不通過（%s）。旧データを保持: %s"
                  % (promote_reason, label))
            quality_event("quality_hold", catalog_date, theme, angle, rows,
                          reason=promote_reason, mode="promote")
            return "empty"
        print("  ✅ 昇格条件を通過（%s）。旧結果を保存前に置き換えます: %s"
              % (promote_reason, label))

    # 手動forceでも、既存より少ない候補で置き換えない。
    if force and not promote and existing_count is not None and existing_count > len(rows):
        print("  ⚠ 既存%d件より新結果が少ない(%d件)ため上書きせず保護: %s"
              % (existing_count, len(rows), label))
        quality_event("quality_hold", catalog_date, theme, angle, rows,
                      reason="既存%d件より新結果が少ない" % existing_count, mode="force")
        return "empty"

    # 安全ガード：AIリランクを頼んだのに全件スコア無し＝Gemini無料枠切れ/失敗。
    # その結果で既存の“AIフィルタ済み”データを上書きしない。
    replace = bool(force or promote)
    ai_ran = any(r.get("ai_score") is not None for r in rows)
    if force and rerank and not ai_ran:
        print("  ⚠ AIリランク不発（枠切れ?）。既存のAI済みデータ保護のため上書きしない: %s" % label)
        replace = False
    if count_usage:
        usage_bump()
    try:
        res = rpc("v2_upsert_product", {
            "p_secret": token, "p_catalog_date": catalog_date,
            "p_theme": theme, "p_angle_title": angle or "",
            "p_products": rows, "p_replace": replace})
        if res == -1:
            print("  ⏭ 既存あり・上書きせずスキップ: %s" % label)
            quality_event("skip", catalog_date, theme, angle, rows,
                          reason="既存結果を保持", mode="promote" if promote else "normal")
            return "skip"
        print("  ✅ 保存 %s 件%s: %s"
              % (res, "（品質昇格）" if promote else "", label))
        quality_event("promoted" if promote else "saved", catalog_date, theme, angle, rows,
                      reason=("品質昇格" if promote else "保存完了"),
                      mode="promote" if promote else "normal")
        return "promoted" if promote else "saved"
    except urllib.error.HTTPError as e:
        print("  保存失敗 HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:200]))
        quality_event("error", catalog_date, theme, angle, rows,
                      reason="保存失敗 HTTP %s" % e.code, mode="promote" if promote else "normal")
        return "error"
    except Exception as e:
        print("  保存失敗: %s: %s" % (type(e).__name__, e))
        quality_event("error", catalog_date, theme, angle, rows,
                      reason="保存失敗: %s" % type(e).__name__, mode="promote" if promote else "normal")
        return "error"


# ---------- 1切り口を取得して保存 ----------
def fetch_and_save(token, catalog_date, theme, angle, kw, rerank, force,
                   components=None, shadow=False, promote=False,
                   shadow_capture=None):
    label = "%s / %s" % (theme, angle or "(テーマ単位)")
    # ピン留め判定と、force時の劣化防止に同じ件数を使う。
    # 既存件数を取得できない場合は、保存を止めずに従来どおり続行する。
    existing_count = None
    try:
        existing_count = rpc("v2_product_has", {
            "p_secret": token, "p_catalog_date": catalog_date,
            "p_theme": theme, "p_angle_title": angle or ""})
        if not isinstance(existing_count, int):
            existing_count = None
        elif not force and not shadow and not promote and existing_count > 0:
            print("  ⏭ 既に保存済み(%d件)なのでスキップ: %s" % (existing_count, label))
            quality_event("skip", catalog_date, theme, angle,
                          reason="既に保存済み(%d件)" % existing_count, mode="normal")
            return "skip"
    except Exception as e:
        print("  has確認失敗（続行）:", e)
    # 日次上限
    used = usage_load()
    if used >= DAILY_ANGLE_LIMIT:
        print("  ⛔ 日次上限(%d切り口)に到達。今日はこれ以上取得しない。" % DAILY_ANGLE_LIMIT)
        quality_event("limit", catalog_date, theme, angle,
                      reason="日次上限%d切り口" % DAILY_ANGLE_LIMIT, mode="shadow" if shadow else "normal")
        return "limit"

    print("  ▶ 取得: %s（kw=%s）rerank=%s" % (label, kw, rerank))
    pool_result = build_pool(
        kw, rerank, theme=theme, angle=angle, components=components, return_reason=True)
    # 既存のMac用テストや外部スクリプトが build_pool を差し替えても壊れないよう、
    # 従来どおりリストだけを返す実装も受け入れる。
    if isinstance(pool_result, tuple) and len(pool_result) == 2:
        pool, pool_reason = pool_result
    else:
        pool, pool_reason = pool_result, "unknown_empty"
    if not pool:
        pool_label = POOL_REASON_LABELS.get(pool_reason, POOL_REASON_LABELS["unknown_empty"])
        if pool_reason.startswith("ai_"):
            print("  AI診断: " + pool_label)
        print("  × プールが空。保存せず。")
        quality_event("empty", catalog_date, theme, angle,
                      reason=pool_label,
                      mode="shadow" if shadow else "normal")
        return "empty"
    rows = to_rows(pool)
    # 表示・保存用の最終形式へ変換した後にも再確認する。取得元ごとの形式差で
    # 人向け商品が候補プールをすり抜けても、v2_product へ渡す直前に止める。
    guarded_rows = apply_intent_category_evidence(
        rows, theme=theme or kw, angle_title=angle or kw)
    if len(guarded_rows) < len(rows):
        print("  ペット冷感カテゴリフィルタ（表示前）：人向け候補%d件を除外"
              % (len(rows) - len(guarded_rows)))
    rows = guarded_rows
    if not rows:
        print("  × 有効なASINが0件。保存せず。")
        quality_event("empty", catalog_date, theme, angle,
                      reason="有効なASINが0件", mode="shadow" if shadow else "normal")
        return "empty"
    quality_ok, quality_reason = validate_new_result(rows, rerank)
    if shadow:
        print("  ◇ 影テスト: 新候補%d件 / 既存%s件（DBには保存しません）"
              % (len(rows), existing_count if existing_count is not None else "不明"))
        if not quality_ok:
            print("  ◇ 品質ゲート判定: 保留（%s）" % quality_reason)
        promote_ok, promote_reason = validate_quality_promotion(rows, existing_count, rerank)
        print("  ◇ 正式反映候補: %s（%s）"
              % ("可" if promote_ok else "保留", promote_reason))
        quality_event("shadow", catalog_date, theme, angle, rows,
                      reason=(promote_reason if not promote_ok else "正式反映条件を通過"),
                      mode="shadow")
        for i, row in enumerate(rows, 1):
            print("    %d. AI=%s / %s / %s / ASIN=%s"
                  % (i, row.get("ai_score") if row.get("ai_score") is not None else "未採点",
                     row.get("brand") or "ブランド不明",
                     (row.get("title") or "商品名不明")[:90], row.get("asin")))
        if shadow_capture is not None:
            shadow_capture.append({
                "catalog_date": catalog_date,
                "theme": theme,
                "angle": angle or "",
                "kw": kw,
                "components": components or [],
                "rows": rows,
            })
        return "shadow"
    return persist_rows(token, catalog_date, theme, angle, rows, rerank,
                        force=force, promote=promote,
                        existing_count=existing_count, count_usage=True)


def main():
    ap = argparse.ArgumentParser(description="v2_product へ商品を取得・保存（③）")
    ap.add_argument("--theme")
    ap.add_argument("--angle", default="")
    ap.add_argument("--kw")
    ap.add_argument("--queue", help="{theme,angle_title,kw,components} のJSON配列ファイル（夜間先読み用）")
    ap.add_argument("--limit", type=int, default=10, help="キューから取得する最大件数")
    ap.add_argument("--date", default=None,
                    help="catalog_date（既定=Supabaseの最新カタログ日／取れなければ今日）")
    ap.add_argument("--rerank", action="store_true", help="AIリランクも実行")
    ap.add_argument("--force", action="store_true", help="ピン留めを無視して入れ替え")
    ap.add_argument("--shadow", action="store_true", help="保存せず新候補とAI評価だけを表示")
    ap.add_argument("--promote-quality", action="store_true",
                    help="明示確認済みの高品質候補だけ、既存結果を置き換える")
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
            jobs.append((e.get("theme", ""), e.get("angle_title", ""), kw,
                         e.get("components") or []))
    elif args.theme and args.kw:
        jobs.append((args.theme, args.angle, args.kw, []))
    else:
        print("‼ 使い方: --theme/--kw（1件）か --queue（先読み）。--help 参照"); sys.exit(1)

    stats = {"saved": 0, "promoted": 0, "shadow": 0,
             "skip": 0, "empty": 0, "error": 0, "limit": 0}
    done_fetch = 0
    for theme, angle, kw, components in jobs:
        if args.queue and done_fetch >= args.limit:
            print("  上限 %d 件に達したので停止。" % args.limit); break
        r = fetch_and_save(token, args.date, theme, angle, kw, args.rerank, args.force,
                           components=components, shadow=args.shadow,
                           promote=args.promote_quality)
        stats[r] = stats.get(r, 0) + 1
        if r == "limit":
            break
        if r in ("saved", "promoted", "shadow"):  # 影テストも実取得として数える
            done_fetch += 1
            time.sleep(SLEEP_BETWEEN_ANGLES)

    print("\n" + "=" * 60)
    print("完了: 保存%d / 品質昇格%d / 影テスト%d / スキップ%d / 空%d / エラー%d / 上限停止%d"
          % (stats["saved"], stats["promoted"], stats["shadow"], stats["skip"],
             stats["empty"], stats["error"], stats["limit"]))
    print("本日の取得済み切り口数: %d / 上限%d" % (usage_load(), DAILY_ANGLE_LIMIT))


if __name__ == "__main__":
    main()
