#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
係② 商品あつめ — タスク①「除外/重複ルール＋ブランド照合ゲート」検証版（一括）

パイプライン（1本で通す・検証重視の見やすい出力）:
  1. 候補取得   : 楽天市場API ＋ Yahoo!ショッピングAPI
  2. 除外       : (a) セット/まとめ買いタイトル除外（単品優先）
                  (b) 効果効能カテゴリ＋怪しい効能表現の backstop 除外（薬機法/景表法/PSE）
  3. Amazon解決 : Creators API searchItems で正規ASIN/URL/ブランド/価格
  4. 照合ゲート : Amazon結果と候補のブランド一致 → 採用/要確認/保留 の段階化
  5. 重複排除   : ASIN／親ASIN／正規化タイトルで名寄せ（カラバリ集約）

このスクリプトは「読むだけ・保存しない」。Supabase 保存や発火は後続タスク③で別ファイル。
標準ライブラリのみ。外部は Mac で実行して出力を貼る運用。

使い方（Macで実行）:
  cd "/Users/iharakazuya/Dropbox/Coworkテスト/Amazon_商品セレクト_改善ver"
  python3 product/build_candidates.py                      # 既定キーワード
  python3 product/build_candidates.py "枕 高さ調整"         # キーワード指定
  python3 product/build_candidates.py "ソロ キャンプ 軽量" --json   # 結果をJSONでも出す
  python3 product/build_candidates.py "枕 高さ調整" --rerank         # AIリランク(Gemini)も実行

鍵：product/shopping_api.json（楽天/Yahoo）と product/amazon_creators.json（Amazon）。
    AIリランク時のみ product/gemini_api.json（example をコピー）。--rerank 指定時だけ呼ぶ。
    値はチャットに貼らない。ファイルにだけ置く。
"""
import os, sys, re, json, time, base64, unicodedata, argparse, hashlib
import urllib.parse, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
SHOP = os.path.join(HERE, "shopping_api.json")
AMZ = os.path.join(HERE, "amazon_creators.json")

# 表示件数の上限。カードを3列×2段で見せられる6件を上限とし、
# 足りない時も不適合商品では埋めない。
TARGET_MAX = 6
# AI関連性の表示下限。中間点は「近いが主用途が別」の商品も含み得るため、
# 明確に適合した70点以上だけを表示する。これ未満は件数に関係なく表示しない。
REL_MIN = 70
# 検索で集める生候補の下限。除外・AI判定で落ちる分を見込む。
POOL_MIN = TARGET_MAX * 3
# 1ブランドの最大表示数。同ブランドの超過分で空きを埋めない。
MAX_PER_BRAND = 2
# Amazon照合する候補の上限。照合は1件ずつAPIを叩くので多いと激遅（先読み全体がタイムアウト）。
#   ※gather はAI変換クエリ→テーマ→修飾 の順に集めるので、先頭ほど「切り口の本命」。
#     レビュー数で並べ替えず gather 順のまま上位を残す＝本命(鍵/シート等)を維持しつつ高速化。
AMAZON_RESOLVE_MAX = 14

# 90点版: Amazonを関連性順と評価順の2系統で直接検索する。
# 通常は2呼び出しだけ。候補が極端に少ない時だけ別クエリを1回追加する。
AMAZON_LANE_COUNT = 8
AMAZON_DIRECT_MIN = 10
AI_RERANK_MAX = 18
MAX_PER_PRODUCT_TYPE = 2

# ---- レート配慮（無料枠を超えないための最小限のウェイト）----
SLEEP_BETWEEN_AMAZON = 0.6   # Amazon searchItems 連打の間隔（秒）


# ============================================================
# 0. 除外辞書（叩き台。KAZUYAさんが後で修正する前提）
# ============================================================
# (a) 業務用ロット/まとめ買い → 個人が1SKUを買える商品を優先
# 「科学実験セット」「浴衣3点セット」のような機能的に1商品のセットは除外しない。
SET_PATTERNS = [
    r"まとめ買い", r"まとめ売り", r"業務用ロット", r"ケース販売",
    # 「100枚 うちわ」「10本組」など、入/セットの語が省略された業務用数量も除外。
    r"[0-9０-９]{2,}\s*(?:個|本|枚|袋|箱|パック)(?:入|入り|セット|組|パック)?",
    r"[0-9０-９]+\s*(?:個|本|枚|袋|箱|パック|セット|組)入",
    # 「×N」はサイズ表記(35×50cm等)と衝突するため、数量単位が続く時だけ複数個とみなす
    r"×\s*[0-9０-９]+\s*(?:個|本|枚|袋|箱|パック|セット|組|set|pcs)",
    r"x\s*[0-9]{2,}\s*(?:個|本|set|pack)",
    r"[0-9０-９]{2,}pcs",
]

# (d) 効果効能に依存するカテゴリ（テーマ生成段階で除外の商品側 backstop）
#     ─ カテゴリを示す語。タイトルにあれば効果効能依存として除外。
EFFICACY_CATEGORY_WORDS = [
    # ダイエット/痩身
    "ダイエット", "痩身", "脂肪燃焼", "燃焼系", "糖質カット", "カロリーカット",
    "置き換えダイエット", "ファスティング",
    # 育毛/発毛/増毛
    "育毛", "発毛", "増毛", "薄毛", "抜け毛", "白髪", "AGA", "養毛",
    # 身長/成長
    "身長を伸ばす", "身長サプリ", "成長期サプリ", "背が伸びる",
    # バストアップ/豊胸
    "バストアップ", "豊胸", "美尻",
    # 美白/シミ等の医薬部外品的効能に寄りがちなもの（怪しい表現とセットで判断）
]

# ─ 怪しい効能表現（薬機法/景表法で NG になりやすい断定・誇大表現）
#   カテゴリ語が無くてもこれらが強く出ていれば backstop で弾く／フラグ。
EFFICACY_CLAIM_PATTERNS = [
    r"効果絶大", r"必ず(?:痩せ|効く|治る|生える)", r"1週間で\-?[0-9０-９]+\s*kg",
    r"飲むだけで(?:痩せ|やせ)", r"塗るだけで生える", r"シミが消える",
    r"[0-9０-９]+日で(?:-?[0-9０-９]+\s*kg|完治|治る)",
    r"医薬品(?:並み|レベル)", r"病気が治る", r"完治", r"癌|ガンに効く",
    r"WHO認可", r"厚労省認可(?:済)?",  # 誇大・虚偽になりやすい表現
]

# (e) 中華製ヒューリスティック（上流の弱いシグナル。最終判定は特商法チェック＝後続⑤）
#     ここでは「弱いフラグ」を立てるだけで除外はしない（誤除外を避ける）。
CHEAP_NONAME_HINTS = [
    r"高品質", r"日本語説明書", r"並行輸入", r"インポート",
]

SET_RE = [re.compile(p, re.I) for p in SET_PATTERNS]
CLAIM_RE = [re.compile(p, re.I) for p in EFFICACY_CLAIM_PATTERNS]
NONAME_RE = [re.compile(p, re.I) for p in CHEAP_NONAME_HINTS]


def die(m):
    print("‼ " + m)
    sys.exit(1)


def emit_empty(want_json, msg):
    """0件は異常ではなく正常な探索結果。die(=exit1)せず空プールを正常返しする。
    --json時は build_pool が読む契約(---- JSON ----)に合わせて空配列[]を出す。"""
    print("・" + msg + "（空プールを返します）")
    if want_json:
        print("\n---- JSON ----")
        print("[]")
    sys.exit(0)


def norm(s):
    """全角半角ゆらぎを吸収して小文字化（比較用）。"""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s))
    return s.lower().strip()


def normalize_components(components):
    """カタログの c=[[種別, 値], ...] を安全なリストへ正規化する。"""
    out = []
    for row in components or []:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        kind, value = str(row[0]).strip(), str(row[1]).strip()
        if kind and value:
            out.append([kind, value])
    return out


def build_intent_spec(theme, angle_title, search_kw, components=None):
    """検索語と選定意図を分離する。

    search_kw は外部EC検索専用。AIのクエリ展開・採点・キャッシュには、切り口タイトルと
    構造化要素を含む intent_text / intent_key を使い、同じkwを持つ別切り口の混線を防ぐ。
    """
    components = normalize_components(components)
    theme = (theme or "").strip()
    angle_title = (angle_title or search_kw or "").strip()
    search_kw = (search_kw or angle_title or theme).strip()
    grouped = {}
    for kind, value in components:
        grouped.setdefault(kind, []).append(value)

    labels = {
        "nayami": "最優先の悩み・目的",
        "buy": "買い軸",
        "attr": "対象属性",
        "scene": "利用シーン",
    }
    lines = ["テーマ: %s" % (theme or "未指定"), "切り口タイトル: %s" % angle_title]
    for kind in ("nayami", "buy", "attr", "scene"):
        values = grouped.get(kind) or []
        if values:
            lines.append("%s: %s" % (labels[kind], " / ".join(values)))
    lines.append("検索用キーワード（意図そのものではない）: %s" % search_kw)
    intent_text = "\n".join(lines)

    basis = json.dumps({
        "theme": theme,
        "angle_title": angle_title,
        "search_kw": search_kw,
        "components": components,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]
    intent_key = "%s|%s" % (digest, _norm_key(angle_title)[:80])
    return {
        "theme": theme,
        "angle_title": angle_title,
        "search_kw": search_kw,
        "components": components,
        "intent_text": intent_text,
        "intent_key": intent_key,
    }


# ============================================================
# 1. 候補取得（楽天 / Yahoo）— test_shopping_search.py と同ロジック
# ============================================================
def get_json(url, data=None, headers=None, timeout=30):
    h = {"User-Agent": "select-batch/1.0"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h,
                                 method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def dig(d, *path):
    for p in path:
        if isinstance(d, dict):
            d = d.get(p)
        elif isinstance(d, list) and isinstance(p, int) and -len(d) <= p < len(d):
            d = d[p]
        else:
            return None
    return d


def load_shop_conf():
    if not os.path.exists(SHOP):
        die("shopping_api.json が無い（example をコピーして作成）")
    d = json.load(open(SHOP, encoding="utf-8"))
    r = d.get("rakuten_application_id", "")
    ak = d.get("rakuten_access_key", "")
    y = d.get("yahoo_client_id", "")
    if not r or "＜" in r:
        die("rakuten_application_id 未入力")
    if not ak or "＜" in ak:
        die("rakuten_access_key 未入力（楽天は accessKey も必須）")
    if not y or "＜" in y:
        die("yahoo_client_id 未入力")
    return r, ak, y


def rakuten(app_id, access_key, kw, hits=15):
    q = urllib.parse.urlencode({
        "applicationId": app_id, "accessKey": access_key,
        "keyword": kw, "hits": hits, "genreId": 0,
        "format": "json", "sort": "-reviewCount",
    })
    url = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260701?" + q
    try:
        data = get_json(url, headers={
            "Referer": "https://amane-tools.github.io/select-team/",
            "Origin": "https://amane-tools.github.io",
        })
    except urllib.error.HTTPError as e:
        print("  楽天 HTTP %s: %s" % (e.code, e.read().decode()[:300]))
        return []
    except Exception as e:
        print("  楽天 失敗: %s: %s" % (type(e).__name__, e))
        return []
    out = []
    for w in data.get("Items", []):
        it = w.get("Item", w)
        out.append(dict(
            source="rakuten",
            name=it.get("itemName", ""),
            price=it.get("itemPrice"),
            review_count=it.get("reviewCount"),
            review_avg=it.get("reviewAverage"),
            shop=it.get("shopName"),
            brand="",                         # 楽天は brand を返さない
            jan=it.get("janCode") or it.get("jan"),
        ))
    return out


MAX_QUERIES = 12   # 1切り口あたりの検索クエリ上限（無料枠の保護）


def theme_variants(theme):
    """テーマ語の検索用バリエーション（検索に強い語へ言い換える）。
    ねらい：テーマ名がそのままでは商品タイトルに出ないケースを救う。
      ・複合名   「水筒・タンブラー」→ 水筒 / タンブラー
      ・「AのB」 「お盆・帰省の手土産」→ 手土産（＝実際に検索で当たる中心語）
                「夏休みの自由研究」→ 自由研究 / 夏休み
    Bを先に置く（日本語の「AのB」はBが中心語なので検索で当たりやすい）。
    戻り値: [元のテーマ, 変種…]（重複なし・順序維持）
    """
    out, seen = [], set()

    def add(t):
        t = (t or "").strip()
        if t and t not in seen:
            seen.add(t); out.append(t)

    segs = [p for p in re.split(r"[・/／]", theme or "") if p.strip()]

    add(theme)                                   # ① 元のテーマ
    for s in [theme] + segs:                     # ② 「AのB」の B（中心語）＝一番検索に強い
        parts = [p for p in (s or "").split("の") if p.strip()]
        if len(parts) >= 2:
            add(parts[-1])
    for s in segs:                               # ③ 中黒で分割した語
        add(s)
    for s in [theme] + segs:                     # ④ 「AのB」の A（保険）
        parts = [p for p in (s or "").split("の") if p.strip()]
        if len(parts) >= 2:
            add(parts[0])
    return out


def plan_queries(kw, extra_queries=None):
    """kw(=「テーマ語 修飾語…」)を、短い検索クエリの列へ分解する。
    肝：全語を1回のAND検索に押し込まない。悩み語(重い/氷が持たない)が混じると
    AND条件が厳しすぎて0件になるため、『テーマ＋修飾語1語』を個別クエリにして後でunionする。
    悩み語のクエリが0件でも無害（他クエリで埋まる）。最後にテーマ単独で件数保証。
      入力 "クーラーボックス 重い ファミリー"
      → ["クーラーボックス 重い", "クーラーボックス ファミリー", "クーラーボックス"]
      入力 "炭酸水・ミネラルウォーター 強炭酸"
      → ["炭酸水・ミネラルウォーター 強炭酸", "炭酸水 強炭酸", "ミネラルウォーター 強炭酸",
         "炭酸水・ミネラルウォーター", "炭酸水", "ミネラルウォーター"]
    extra_queries: 呼び出し側（種別付き combo 等）が用意した優先クエリ。先頭に足す。
    """
    toks = [t for t in (kw or "").split() if t]
    plan = list(extra_queries or [])
    if toks:
        themes = theme_variants(toks[0])           # テーマ名の言い換え（複合名・「AのB」）
        mods = [t for t in toks[1:] if t != toks[0]]
        # tier別に枠を分ける。※単純に順番で詰めると tier0 が上限を食い尽くし、
        #   一番効く「テーマ中心語の単独検索」（例：手土産）に到達しない事故が起きる。
        t0 = ["%s %s" % (th, m) for th in themes[:3] for m in mods[:2]]   # テーマ(変種)+修飾語
        t1 = list(themes[:4])                                            # テーマ(変種)単独
        # tier2（救済）: 修飾語だけ。「花火大会」「夏祭り」など“場面”テーマは
        #   商品名が修飾語のほう（浴衣/甚平/工作キット）なので、これが無いと当たらない。
        t2 = list(mods[:3])
        # 順序：テーマ単独(t1=本物の商品が並ぶ)を先頭に、テーマ+修飾(t0)と交互に投げる。
        #   ※以前は t0 を先に全部投げていたため、修飾語で汚染された候補（例「おむつ 漏れない」で
        #     ヒットするおむつゴミ箱/犬用）だけで候補下限に達し、肝心の「テーマ単独」検索
        #     （本物の商品）が実行される前に打ち切られていた。これが用途違い混入の主因。
        inter = []
        for i in range(max(len(t1), len(t0))):
            if i < len(t1):
                inter.append(t1[i])
            if i < len(t0):
                inter.append(t0[i])
        plan += inter + t2
    # 重複除去（順序維持）＋ クエリ数の上限（無料枠保護）
    seen, out = set(), []
    for q in plan:
        q = (q or "").strip()
        if q and q not in seen:
            seen.add(q); out.append(q)
        if len(out) >= MAX_QUERIES:
            break
    return out


def gather_candidates(app_id, access_key, cid, kw, min_need=None, extra_queries=None):
    """楽天+Yahooから候補を集める。plan_queries()で作った複数の短いクエリを順に投げ、
    結果をunion(名寄せ)する。min_need件たまったら早期停止＝無料枠を節約。
    関連度は後段のGeminiリランクが本来の切り口(kw全体)で並べ替えるので、広めに集めてよい。

    min_need は「除外・Amazon照合で落ちる分」を見込んで目標件数の3倍にする。
    ※以前は表示目標件数だけで打ち切っていたため、候補8件→除外/照合落ちで最終1件、
      という切り口（例：炭酸水）が出ていた。プールを厚くして取りこぼしを防ぐ。
    戻り値: (候補list, 実際に投げたクエリlog)
    """
    if min_need is None:
        min_need = POOL_MIN
    queries = plan_queries(kw, extra_queries)
    if not queries:
        return [], []
    seen, cands, tried = set(), [], []
    # 早期停止は「最低 MIN_QUERIES 本を投げた後」に限る。
    #   ※テーマ単独クエリだけで min_need に達して打ち切ると、場面テーマ(花火大会等)の
    #     本命である『テーマ+修飾語（花火大会 場所取り→レジャーシート）』が走らず、
    #     汎用品ばかりになる事故を防ぐ。最初の数本で t1(テーマ単独)と t0(テーマ+修飾)を必ず通す。
    MIN_QUERIES = 4
    for i, q in enumerate(queries):
        rows = rakuten(app_id, access_key, q) + yahoo(cid, q)
        added = 0
        for r in rows:
            nm = r.get("name") or ""
            k = (r.get("source"), norm(nm))
            if nm and k not in seen:
                r["_search_query"] = q
                seen.add(k); cands.append(r); added += 1
        tried.append("%s(+%d)" % (q, added))
        if len(cands) >= min_need and i >= MIN_QUERIES - 1:
            break
    return cands, tried


def select_resolve_candidates(candidates, limit=AMAZON_RESOLVE_MAX, max_per_known_brand=3):
    """Amazon解決枠を検索クエリ×取得元へラウンドロビン配分する。

    先頭クエリやSEOの強い1ブランドが全14枠を独占するのを防ぐ。ブランド不明が多い
    楽天候補はクエリ×取得元の分散で補い、既知ブランドは解決前にも緩い上限を設ける。
    """
    lanes = {}
    lane_order = []
    for cand in candidates or []:
        lane = (cand.get("_search_query") or "", cand.get("source") or "")
        if lane not in lanes:
            lanes[lane] = []
            lane_order.append(lane)
        lanes[lane].append(cand)

    picked, brand_counts = [], {}
    while len(picked) < limit:
        progressed = False
        for lane in lane_order:
            rows = lanes[lane]
            while rows:
                cand = rows.pop(0)
                brand = _norm_brand(cand.get("brand"))
                if brand and brand_counts.get(brand, 0) >= max_per_known_brand:
                    continue
                picked.append(cand)
                if brand:
                    brand_counts[brand] = brand_counts.get(brand, 0) + 1
                progressed = True
                break
            if len(picked) >= limit:
                break
        if not progressed:
            break
    return picked


def yahoo(cid, kw, hits=15):
    q = urllib.parse.urlencode({"appid": cid, "query": kw, "results": hits})
    url = "https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch?" + q
    try:
        data = get_json(url)
    except urllib.error.HTTPError as e:
        print("  Yahoo HTTP %s: %s" % (e.code, e.read().decode()[:300]))
        return []
    except Exception as e:
        print("  Yahoo 失敗: %s: %s" % (type(e).__name__, e))
        return []
    out = []
    for h in data.get("hits", []):
        out.append(dict(
            source="yahoo",
            name=h.get("name") or "",
            price=h.get("price"),
            review_count=dig(h, "review", "count"),
            review_avg=dig(h, "review", "rate"),
            shop=dig(h, "seller", "name"),
            brand=(dig(h, "brand", "name") or "").strip(),
            jan=h.get("janCode"),
        ))
    return out


# ============================================================
# 2. 除外ルール
# ============================================================
def match_any(res, text):
    for r in res:
        if r.search(text):
            return r.pattern
    return None


def classify_exclusion(cand):
    """除外理由を返す。除外しないなら None、弱フラグは ('flag', 理由)。"""
    name = cand.get("name") or ""
    # (a) セット/まとめ買い
    hit = match_any(SET_RE, name)
    if hit:
        return ("drop", "セット/まとめ買い (%s)" % hit)
    # (d-1) 効果効能カテゴリ語
    nlow = norm(name)
    for w in EFFICACY_CATEGORY_WORDS:
        if norm(w) in nlow:
            return ("drop", "効果効能カテゴリ (%s)" % w)
    # (d-2) 怪しい効能表現
    hit = match_any(CLAIM_RE, name)
    if hit:
        return ("drop", "怪しい効能表現 (%s)" % hit)
    # (e) 中華製ヒューリスティック → 弱フラグのみ（除外しない）
    hit = match_any(NONAME_RE, name)
    if hit:
        return ("flag", "無名/激安ヒント (%s)" % hit)
    return None


# ============================================================
# 3. Amazon解決（Creators API searchItems）— test_amazon_resolve.py 流用
# ============================================================
def amazon_conf():
    if not os.path.exists(AMZ):
        die("amazon_creators.json が無い")
    return json.load(open(AMZ, encoding="utf-8"))


def amazon_token(c):
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials", "scope": c["scope"]}).encode()
    basic = base64.b64encode(
        ("%s:%s" % (c["credential_id"], c["credential_secret"])).encode()).decode()
    tok = get_json(c["token_url"], data=body, headers={
        "Authorization": "Basic " + basic,
        "Content-Type": "application/x-www-form-urlencoded"})
    if not tok.get("access_token"):
        die("Amazonトークン取得失敗: " + json.dumps(tok)[:300])
    return tok["access_token"]


NOISE_RE = re.compile(
    r"\d+%|クーポン|送料無料|楽天\d*位|ポイント\d+倍|限定|お買い物マラソン|"
    r"半額|最安|正規品|即納|あす楽|新品|人気|話題|【[^】]*】", re.I)


def clean_query(name, brand):
    """ブランド＋商品名の要点だけ（記号・煽り文句を軽く除去）。"""
    s = re.sub(r"[【】\[\]（）()★☆／/|・]+", " ", name or "")
    s = NOISE_RE.sub(" ", s)
    words = s.split()
    head = " ".join(words[:6])
    if brand and brand not in head and brand != "ブランド登録なし":
        head = brand + " " + head
    return head.strip()


def amazon_search(c, token, keywords, count=3, sort_by=None, min_reviews_rating=None):
    url = c["api_url"].replace("getItems", "searchItems")
    payload = {
        "keywords": keywords,
        "itemCount": count,
        "resources": ["itemInfo.title", "itemInfo.byLineInfo", "itemInfo.features",
                      "offersV2.listings.price", "images.primary.large"],
        "partnerTag": c["partner_tag"],
        "partnerType": "Associates",
        "marketplace": c["marketplace"],
    }
    if sort_by:
        payload["sortBy"] = sort_by
    if min_reviews_rating is not None:
        payload["minReviewsRating"] = min_reviews_rating
    try:
        data = get_json(url, data=json.dumps(payload).encode(), headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "x-marketplace": c["marketplace"]})
    except urllib.error.HTTPError as e:
        return {"_err": "HTTP %s: %s" % (e.code, e.read().decode()[:300])}
    except Exception as e:
        return {"_err": "%s: %s" % (type(e).__name__, e)}
    return data


def build_amazon_query(intent, search_kw):
    """Amazon用に、検索kwと構造化された最重要軸を1本の短いクエリへまとめる。"""
    parts = [x for x in (search_kw or "").split() if x]
    grouped = {}
    for kind, value in normalize_components((intent or {}).get("components")):
        grouped.setdefault(kind, []).append(value)
    # 悩み・買い軸を先に、対象・シーンは後に足す。同じ語は重複させない。
    for kind in ("nayami", "buy", "attr", "scene"):
        for value in grouped.get(kind) or []:
            value = str(value).strip()
            if value and not any(value in p or p in value for p in parts):
                parts.append(value)
    return " ".join(parts[:6]).strip()


def concrete_fallback_queries(intent, search_kw):
    """不足時にだけ使う、無料の決め打ち商品カテゴリ検索。

    抽象的な切り口（子連れ、軽い、映える等）は、そのままAmazonに投げると
    商品カテゴリへ翻訳されないことがある。Gemini展開が使えない/空振りの時だけ、
    少数の安全なカテゴリ語を候補にする。通常経路のAPI呼び出し数は増えない。
    """
    spec = intent or {}
    components = normalize_components(spec.get("components"))
    values = [norm(v) for _, v in components]
    text = norm(" ".join([
        spec.get("theme") or "", spec.get("angle_title") or "", search_kw or "",
        " ".join(values),
    ]))
    out = []

    def add(q):
        q = (q or "").strip()
        if q and q not in out:
            out.append(q)

    # 具体商品名が切り口に含まれる場合は代用品を混ぜず、その商品種だけ救済する。
    if "うちわ" in text or "団扇" in text:
        add("竹 うちわ 日本製 1本")
        add("祭り うちわ 単品")
    if "子連れ" in text or "子供" in text or "こども" in text:
        add("子供 迷子防止 タグ")
        add("子供 冷感タオル 夏")
        add("子供 虫よけ 夏")
    if "パンク" in text:
        add("自転車 パンク修理キット")
        add("ロードバイク 携帯ポンプ")
        add("自転車 タイヤレバー")
    if "軽い" in text or "軽量" in text:
        if "電動ドライバー" in text or "電動ドリル" in text:
            add("軽量 電動ドライバー")
        elif "キャンプ" in text:
            add("ソロキャンプ 軽量 クッカー")
            add("ソロキャンプ 軽量 チェア")
    if "高さ" in text and "枕" in text:
        add("枕 高さ調整")
        add("枕 高め 低め")
    if "ひんやり" in text or "冷感" in text:
        if "ペット" in text or "犬" in text or "猫" in text:
            add("ペット 冷感マット")
            add("犬 ひんやり ベッド")
    if "すき間" in text or "すきま" in text or "隙間" in text:
        if "風呂" in text or "浴室" in text:
            add("風呂 隙間 掃除ブラシ")
            add("浴室 目地 掃除ブラシ")
    return out[:4]


def _amazon_lane(c, token, query, lane="relevance", count=AMAZON_LANE_COUNT):
    """Amazonの1検索系統を、解決済み候補rowとして返す。"""
    review_lane = lane == "reviews"
    res = amazon_search(
        c, token, query, count=count,
        sort_by="AvgCustomerReviews" if review_lane else None,
        min_reviews_rating=4 if review_lane else None)
    if not isinstance(res, dict):
        return [], [], "invalid Amazon response"
    if "_err" in res:
        return [], [], res["_err"]
    items = dig(res, "searchResult", "items") or dig(res, "items") or []
    rows, dropped = [], []
    for rank, item in enumerate(items, 1):
        amz = parse_amazon_item(item, c["partner_tag"])
        if not amz.get("asin") or not amz.get("title"):
            continue
        excl = classify_exclusion({"name": amz.get("title"), "source": "amazon"})
        if excl and excl[0] == "drop":
            dropped.append((amz.get("title") or "", excl[1]))
            continue
        candidate = {
            "source": "amazon",
            "name": amz.get("title") or "",
            "price": amz.get("price"),
            "review_count": None,  # Creators APIは正確な件数を返さない
            "review_avg": None,
            "brand": amz.get("brand") or "",
            "jan": None,
            "_search_query": query,
            "amazon_lanes": [lane],
            "%s_rank" % lane: rank,
        }
        rows.append({
            "candidate": candidate,
            "amazon": amz,
            "verdict": "採用",
            "reason": "Amazon Creators APIの直接検索",
            "weak_flag": None,
            "sources": ["amazon"],
            "consensus": 1,
        })
    return rows, dropped, None


def merge_amazon_rows(groups):
    """ASINを主キーに、関連性順・評価順・救済検索の候補を統合する。"""
    merged = {}
    for rows in groups or []:
        for row in rows or []:
            asin = row.get("amazon", {}).get("asin")
            if not asin:
                continue
            current = merged.get(asin)
            if current is None:
                current = row
                current["candidate"]["amazon_lanes"] = list(
                    current["candidate"].get("amazon_lanes") or [])
                merged[asin] = current
                continue
            cand = current["candidate"]
            incoming = row["candidate"]
            for lane in incoming.get("amazon_lanes") or []:
                if lane not in cand["amazon_lanes"]:
                    cand["amazon_lanes"].append(lane)
                key = "%s_rank" % lane
                if incoming.get(key) is not None:
                    cand[key] = min(cand.get(key, 999), incoming[key])

    for row in merged.values():
        lanes = set(row["candidate"].get("amazon_lanes") or [])
        row["consensus"] = len(lanes)
        row["sources"] = ["amazon"]
    return list(merged.values())


def amazon_direct_candidates(c, token, primary_query, fallback_queries=None):
    """90点版の候補収集。通常はAmazon 2呼び出し、薄い時だけ1回追加する。"""
    groups, tried, dropped = [], [], []
    for lane in ("relevance", "reviews"):
        rows, lane_dropped, err = _amazon_lane(c, token, primary_query, lane=lane)
        groups.append(rows); dropped += lane_dropped
        tried.append({"query": primary_query, "lane": lane, "count": len(rows), "error": err})
        time.sleep(SLEEP_BETWEEN_AMAZON)
    merged = merge_amazon_rows(groups)

    # 0件や業務用ロットだらけの時だけ、AI展開の具体商品クエリで救済。
    need_fallback = len(merged) < AMAZON_DIRECT_MIN or len(dropped) >= 4
    if need_fallback:
        base = _norm_key(primary_query)
        for query in fallback_queries or []:
            query = (query or "").strip()
            if not query or _norm_key(query) == base:
                continue
            rows, lane_dropped, err = _amazon_lane(c, token, query, lane="fallback")
            groups.append(rows); dropped += lane_dropped
            tried.append({"query": query, "lane": "fallback", "count": len(rows), "error": err})
            time.sleep(SLEEP_BETWEEN_AMAZON)
            break
        merged = merge_amazon_rows(groups)
    return merged, tried, dropped


def select_rerank_candidates(items, limit=AI_RERANK_MAX, max_per_brand=3):
    """AIに渡す候補を、関連性順と評価順から交互に選ぶ。

    Gemini呼び出し数は1回のまま、入力トークンとSEOブランド独占を抑える。
    """
    lanes = []
    for lane in ("relevance", "reviews", "fallback"):
        key = "%s_rank" % lane
        lane_rows = sorted(
            [r for r in items or [] if r.get("candidate", {}).get(key) is not None],
            key=lambda r: r["candidate"][key])
        lanes.append(lane_rows)
    picked, seen, brands = [], set(), {}
    while len(picked) < limit:
        progressed = False
        for rows in lanes:
            while rows:
                row = rows.pop(0)
                asin = row.get("amazon", {}).get("asin")
                if not asin or asin in seen:
                    continue
                brand = _norm_brand(row.get("amazon", {}).get("brand"))
                if brand and brands.get(brand, 0) >= max_per_brand:
                    continue
                picked.append(row); seen.add(asin)
                if brand:
                    brands[brand] = brands.get(brand, 0) + 1
                progressed = True
                break
            if len(picked) >= limit:
                break
        if not progressed:
            break
    return picked


def amazon_direct_asins(c, token, kw, count=8):
    """Amazonを独立ソースとして、切り口キーワードで直接検索したASIN集合。
       楽天/Yahoo経由の解決ASINとの一致＝多源コンセンサスの1票になる。"""
    res = amazon_search(c, token, kw, count=count)
    if not isinstance(res, dict):
        print("    Amazon直接検索エラー: invalid Amazon response")
        return set()
    if "_err" in res:
        print("    Amazon直接検索エラー:", res["_err"])
        return set()
    items = dig(res, "searchResult", "items") or dig(res, "items") or []
    return {it.get("asin") for it in items if it.get("asin")}


def parse_amazon_item(it, partner_tag):
    asin = it.get("asin")
    return dict(
        asin=asin,
        parent_asin=it.get("parentAsin") or it.get("parentASIN"),
        title=dig(it, "itemInfo", "title", "displayValue") or "",
        brand=(dig(it, "itemInfo", "byLineInfo", "brand", "displayValue")
               or dig(it, "itemInfo", "byLineInfo", "manufacturer", "displayValue")
               or ""),
        price=(dig(it, "offersV2", "listings", 0, "price", "money", "displayAmount")
               or dig(it, "offersV2", "listings", 0, "price", "money", "amount")
               or dig(it, "offersV2", "listings", 0, "price", "displayAmount")
               or dig(it, "offers", "listings", 0, "price", "displayAmount")),
        image=dig(it, "images", "primary", "large", "url"),
        features=(dig(it, "itemInfo", "features", "displayValues") or []),
        url=(it.get("detailPageURL")
             or ("https://www.amazon.co.jp/dp/%s?tag=%s" % (asin, partner_tag))),
    )


# ============================================================
# 4. ブランド照合ゲート（採用/要確認/保留）
# ============================================================
# 実質ブランド無しとみなす語（比較から除外）
GENERIC_BRANDS = {"", "-", "ブランド登録なし", "ノーブランド", "ノーブランド品",
                  "ノーブランド品", "不明", "generic", "no brand", "その他", "none"}


def brand_variants(brand, minlen=2):
    """ブランド文字列を比較用の断片集合に分解。
       例 'ESMERALDA（エスメラルダ）' → {'esmeralda', 'エスメラルダ'}"""
    s = norm(brand)
    s = re.sub(r"(株式会社|有限会社|\(株\)|\(有\)|co\.,?\s*ltd\.?|inc\.?|corp\.?|®|™)", "", s)
    out = set()
    for p in re.split(r"[（()）]", s) + [s]:
        p = re.sub(r"\s+", "", p).strip()
        if len(p) >= minlen:
            out.add(p)
    return out


def is_generic_brand(s):
    if norm(s) in GENERIC_BRANDS:
        return True
    return len(re.sub(r"\s+", "", norm(s))) < 2


def brand_equal(a, b):
    """2つのブランド欄が同一か（表記ゆらぎ・読み括弧を吸収）。"""
    if is_generic_brand(a) or is_generic_brand(b):
        return False
    va, vb = brand_variants(a, 2), brand_variants(b, 2)
    if va & vb:
        return True
    for x in va:
        for y in vb:
            if len(x) >= 3 and len(y) >= 3 and (x in y or y in x):
                return True
    return False


def brand_in_text(brand, text, minlen=3):
    """ブランド(の断片)が自由文中に含まれるか。誤検出防止で最低3文字。"""
    if is_generic_brand(brand):
        return False
    t = re.sub(r"\s+", "", norm(text))
    return any(v in t for v in brand_variants(brand, minlen))


MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-]{2,}", re.I)
# 寸法・仕様トークン（型番ではない）: 10CM / 150X200CM / 2WAY / 20MM 等
UNIT_TOKEN_RE = re.compile(
    r"^\d+(?:[.\-x×]\d+)*(?:cm|mm|kg|ml|l|g|m|w|way|p|pcs|inch|通り|段階|層|人用|畳|色|点|枚|本)?$",
    re.I)


def model_tokens(text):
    """型番っぽいトークンだけ抽出：英字＋数字が混在し、寸法/仕様語でないもの。"""
    toks = set()
    for m in MODEL_RE.findall(unicodedata.normalize("NFKC", text or "")):
        m = m.strip("-").upper()
        if len(m) < 3:
            continue
        if not (any(c.isdigit() for c in m) and any(c.isalpha() for c in m)):
            continue                      # "100" や "ABC" を除外（英数混在のみ型番扱い）
        if UNIT_TOKEN_RE.match(m):
            continue                      # 10CM / 150X200CM / 2WAY 等の寸法・仕様を除外
        toks.add(m)
    return toks


def match_gate(cand, amz):
    """候補 cand と Amazon商品 amz の一致度を判定。
       returns (verdict, reason) verdict ∈ 採用/要確認/保留"""
    cbrand = cand.get("brand") or ""
    abrand = amz.get("brand") or ""
    # ① ブランド強一致 → 採用
    if brand_equal(cbrand, abrand):
        return ("採用", "ブランド一致(%s≒%s)" % (cbrand, abrand))
    # ② JAN一致 → 採用（Amazon側にJANは通常無いが将来の別ソース用に残す）
    if cand.get("jan") and amz.get("jan") and norm(cand["jan"]) == norm(amz["jan"]):
        return ("採用", "JAN一致")
    # ③ Amazonのブランドが候補名に含まれる → 同一商品とみなす（楽天はbrand欄が空でも名前に入る）
    if brand_in_text(abrand, cand.get("name")):
        return ("採用", "Amazonブランドが候補名に一致(%s)" % abrand)
    # ④ 型番強一致 → 要確認
    ctok = model_tokens(cand.get("name"))
    atok = model_tokens(amz.get("title"))
    common = ctok & atok
    if common:
        return ("要確認", "型番一致(%s)" % ",".join(sorted(common)[:2]))
    # ⑤ 候補ブランドがAmazonタイトルに含まれる（弱） → 要確認
    if brand_in_text(cbrand, amz.get("title")):
        return ("要確認", "候補ブランドがタイトルに含む(%s)" % cbrand)
    # ⑥ それ以外 → 保留（無名候補が別ブランドに化けるケース）
    return ("保留", "ブランド/型番の一致なし（別商品の可能性）")


# ============================================================
# 5. 重複排除（ASIN／親ASIN／正規化タイトル）
# ============================================================
def dedup_key(row):
    amz = row["amazon"]
    if amz.get("parent_asin"):
        return ("parent", amz["parent_asin"])
    if amz.get("asin"):
        return ("asin", amz["asin"])
    # フォールバック：ブランド＋タイトル先頭を正規化（カラバリ集約の弱名寄せ）
    t = norm(amz.get("title"))[:24]
    return ("title", norm(amz.get("brand")) + "|" + t)


VERDICT_ORDER = {"採用": 0, "要確認": 1, "保留": 2}


# ---- 同ブランドの「色/サイズ/個数違い・重複出品」を畳む（カテゴリ非依存の堅牢版） ----
_VAR_SIZE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:m|cm|mm|㎝|l|kg|g|ml|畳|人用|段階|枚|個|点|本|脚|セット|set|冠|位|層|通り|%|x|×|✕)"
    r"(?:\s*(?:m|cm|mm|人用))?|\d+\s*[-〜~]\s*\d+")
_VAR_COLOR = re.compile(
    r"ブラック|ホワイト|グレー|ネイビー|ベージュ|カーキ|グリーン|ブルー|レッド|ピンク|ブラウン|"
    r"アイボリー|サンドベージュ|シルバー|ゴールド|パープル|オレンジ|イエロー|モカ|深緑|黒|白|赤|青|緑|紺")


def _title_tokens(title):
    """タイトルから色/サイズ/記号を除いた語トークン集合（2文字以上）。"""
    t = norm(title)
    t = _VAR_SIZE.sub(" ", t)
    t = _VAR_COLOR.sub(" ", t)
    t = re.sub(r"[【】\[\]()（）★☆/・|,、。!！?？·＼／「」『』…]", " ", t)
    return set(w for w in re.split(r"\s+", t) if len(w) >= 2)


def collapse_variants(rows):
    """並び済みリストから、同ブランドで“ほぼ同じ商品”（色/サイズ/個数違い・重複出品）を1つに畳む。
    上位（＝良い順）を残す。ブランド不明/汎用ブランドは誤集約を避けて畳まない。
    判定：同ブランド かつ タイトル語の Jaccard≥0.7（別モデルを誤って畳まないよう厳しめ）。"""
    kept, keys = [], []   # keys: [(norm_brand, token_set)]
    for r in rows:
        amz = r.get("amazon", {})
        nb = _norm_brand(amz.get("brand"))
        toks = _title_tokens(amz.get("title"))
        is_dup = False
        if nb and norm(amz.get("brand")) not in GENERIC_BRANDS and len(toks) >= 3:
            for kb, kt in keys:
                if kb != nb:
                    continue
                inter = len(toks & kt)
                union = len(toks | kt) or 1
                if inter / union >= 0.7:   # 語のほとんどが一致＝同一品の色/サイズ/個数違い・重複出品
                    is_dup = True
                    break
        if not is_dup:
            kept.append(r)
            keys.append((nb, toks))
    return kept


# ============================================================
# 6.（任意）AIリランク：切り口適合度で並べ替え（Gemini）
#   --rerank 指定かつ gemini_api.json がある時だけ実行。無ければ静かにスキップ。
#   鍵はMacのファイルにのみ。日次上限ガード付き（無料枠を超えない）。
# ============================================================
GEMINI_CONF = os.path.join(HERE, "gemini_api.json")
GEMINI_USAGE = os.path.join(HERE, "gemini_usage.json")

# --- Gemini呼び出しの“共有予算”（実行をまたいでSupabaseで管理／太平洋日でリセット） ---
# 目的：GitHub Actionsは実行ごとにローカルのカウンタが初期化され、複数ジョブが同じ無料枠(RPD)を
#       食い合って枯渇→429になる。そこでSupabaseの1カウンタに集約し、安全側で止める。
# 優先度：リランク（不適合ドロップ＋ai_score付与）を最優先。クエリ変換(expand)は枠の6割で
#         打ち切ってリランク用の枠を温存する。
SB_PUBLISHABLE = "sb_publishable_hbtP3WrNCJp0BUuBrDs4Ww_6x79K4uc"  # 公開キー（RLS＋batchトークンで保護）
GEMINI_EXPAND_FRACTION = 0.6   # expandはこの割合の使用量まで。残りはrerank用に温存。
_BUDGET = {"loaded": False, "used": 0, "db": False, "exhausted": False}


def _pac_today():
    """太平洋時間の日付（Geminiの無料枠RPDは太平洋深夜にリセットされる）。"""
    import datetime
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
    except Exception:
        return (datetime.datetime.utcnow() - datetime.timedelta(hours=8)).date().isoformat()


def _sb_url():
    return os.environ.get("SUPABASE_URL") or ""


def _batch_token():
    kp = os.path.join(os.path.dirname(HERE), "sources", "service_key.txt")
    try:
        for line in open(kp, encoding="utf-8"):
            s = line.strip()
            if s and not s.startswith("#") and s != "ここにキーを貼る":
                return s
    except Exception:
        pass
    return None


def _sb_rpc(name, payload, timeout=15):
    """Supabaseの SECURITY DEFINER RPC を叩く共通関数。batchトークンを自動付与。失敗時None。"""
    url, tok = _sb_url(), _batch_token()
    if not url or not tok:
        return None
    try:
        p = dict(payload); p["p_secret"] = tok
        body = json.dumps(p, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url + "/rest/v1/rpc/" + name, data=body, method="POST",
            headers={"apikey": SB_PUBLISHABLE, "Authorization": "Bearer " + SB_PUBLISHABLE,
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            t = r.read().decode("utf-8")
            return json.loads(t) if t.strip() else None
    except Exception:
        return None


def _budget_rpc(add):
    """共有カウンタを add 加算し、新しい used を返す（p_add=0で読み取りのみ）。失敗時None。"""
    r = _sb_rpc("v2_ai_budget_take", {"p_pac_date": _pac_today(), "p_add": int(add)})
    try:
        return int(r) if r is not None else None
    except Exception:
        return None


# ---- AI結果の資産化キャッシュ（切り口→クエリ変換／切り口×商品→適合スコア） ----
# 一度計算したら二度と計算しない。angle_key に「プロンプト版＋学習内容」を織り込むので、
# それらが変わった時だけ自動で無効化＝採り直しになる（＝学習は生きたまま効率化）。
def _norm_key(s):
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def _fewshot_sig(few_shot):
    """学習お手本のシグネチャ。学習が更新されればスコアキャッシュが自動で無効化される。"""
    if not few_shot:
        return "0"
    import hashlib
    return hashlib.md5("\n".join(sorted(str(x) for x in few_shot)).encode("utf-8")).hexdigest()[:8]


def _qkey(intent_key):
    # q4 = 具体商品名の代用品と業務用まとめ買いを検索段階から避ける版。
    return "q4|" + _norm_key(intent_key)


def _skey(intent_key, few_shot):
    # s3 = 構造化意図・featuresに加え、具体商品軸と軽量根拠を厳格化した採点版。
    return "s3|%s|%s" % (_fewshot_sig(few_shot), _norm_key(intent_key))


def _cache_query_get(intent_key):
    r = _sb_rpc("v2_ai_query_get", {"p_angle_key": _qkey(intent_key)})
    return [str(x) for x in r] if isinstance(r, list) else None


def _cache_query_put(intent_key, queries):
    _sb_rpc("v2_ai_query_put", {"p_angle_key": _qkey(intent_key),
                                "p_queries": list(queries or []), "p_model": "gemini"})


def _cache_score_get(intent_key, few_shot, asins):
    r = _sb_rpc("v2_ai_score_get", {"p_angle_key": _skey(intent_key, few_shot),
                                    "p_asins": list(asins or [])})
    out = {}
    if isinstance(r, list):
        for row in r:
            if isinstance(row, dict) and row.get("asin"):
                reason, product_type = _decode_cached_reason(row.get("reason") or "")
                out[row["asin"]] = (row.get("score"), reason, product_type)
    return out


def _cache_score_put(intent_key, few_shot, items):
    if items:
        _sb_rpc("v2_ai_score_put", {"p_angle_key": _skey(intent_key, few_shot),
                                    "p_items": items, "p_model": "gemini"})


_TYPE_CACHE_RE = re.compile(r"^\[\[type:(.{1,40}?)\]\]\s*")


def _encode_cached_reason(reason, product_type):
    """DBスキーマを増やさず、商品タイプを既存reasonキャッシュに保存する。"""
    product_type = re.sub(r"[\[\]\r\n]", "", str(product_type or "")).strip()[:40]
    return ("[[type:%s]] " % product_type if product_type else "") + (reason or "")


def _decode_cached_reason(value):
    value = str(value or "")
    m = _TYPE_CACHE_RE.match(value)
    if not m:
        return value, ""
    return value[m.end():], m.group(1).strip()


def _local_used():
    """DBが使えない時のローカル・フォールバック（Mac単発実行用）。"""
    if os.path.exists(GEMINI_USAGE):
        try:
            u = json.load(open(GEMINI_USAGE, encoding="utf-8"))
            if u.get("date") == _pac_today():
                return int(u.get("count", 0))
        except Exception:
            pass
    return 0


def _local_bump(n):
    try:
        json.dump({"date": _pac_today(), "count": n},
                  open(GEMINI_USAGE, "w", encoding="utf-8"))
    except Exception:
        pass

# ---- ⑤学習：手動シード(learning_seed.json)＋自動学習(learning.json)を合算して並び/お手本に反映 ----
LEARNING_FILE = os.path.join(HERE, "learning.json")        # learn.py が自動生成
LEARNING_SEED = os.path.join(HERE, "learning_seed.json")   # 手動の叩き台（編集可）
LEARN = None  # {"brand_weight":{正規化ブランド:score}, "few_shot":[...]} or None


def _norm_brand(b):
    """learn.py と同じ正規化（括弧内の読み除去・空白除去・小文字化）。"""
    if not b:
        return ""
    b = re.sub(r"[（(].*?[）)]", "", str(b))
    return re.sub(r"\s+", "", b).lower()


def load_learning():
    """手動シード＋自動学習を合算。どちらも無ければ LEARN=None（＝通常動作）。"""
    global LEARN
    bw, fs = {}, []
    # 手動シード（キーは表記のまま→ここで正規化）
    if os.path.exists(LEARNING_SEED):
        try:
            sd = json.load(open(LEARNING_SEED, encoding="utf-8"))
            for k, v in (sd.get("brand_weight") or {}).items():
                nb = _norm_brand(k)
                if nb:
                    bw[nb] = bw.get(nb, 0.0) + float(v)
            fs += list(sd.get("few_shot") or [])
        except Exception as e:
            print("    学習シードの読込失敗（無視）:", e)
    # 自動学習（キーは既に正規化済み）
    if os.path.exists(LEARNING_FILE):
        try:
            ld = json.load(open(LEARNING_FILE, encoding="utf-8"))
            for k, v in (ld.get("brand_weight") or {}).items():
                try:
                    bw[k] = bw.get(k, 0.0) + float(v)
                except Exception:
                    pass
            fs += list(ld.get("few_shot") or [])
        except Exception as e:
            print("    学習ファイルの読込失敗（無視）:", e)
    LEARN = {"brand_weight": bw, "few_shot": fs[:16]} if (bw or fs) else None
    return LEARN


def learn_score(brand):
    """ブランドの学習重み。効きすぎ防止で ±6 にクリップ。学習が無ければ 0。"""
    if not LEARN:
        return 0.0
    w = (LEARN.get("brand_weight") or {}).get(_norm_brand(brand), 0.0)
    try:
        return max(-6.0, min(6.0, float(w)))
    except Exception:
        return 0.0


_FEMALE_RE = re.compile(r"レディース|婦人|女性|ウィメンズ|ウーマン|女の子")
_MALE_RE   = re.compile(r"メンズ|紳士|男性|男の子")


def _gender_of(title):
    """商品タイトルから性別を推定。'f'/'m'/None(両用・不明)。両方の語があれば両用(None)。"""
    t = norm(title)
    f = bool(_FEMALE_RE.search(t)); m = bool(_MALE_RE.search(t))
    if f and not m:
        return "f"
    if m and not f:
        return "m"
    return None   # 両用・不明は性別に依らず残す


def gender_consistency(items):
    """性別のある商品が混在する場合、多数派の性別＋両用に揃える。
    ・明確に性別付きの商品が2種類とも一定数ある時だけ作動（家電/食品等は無影響）。
    ・表示件数を埋めるために少数派を戻さない。"""
    fem = [r for r in items if _gender_of(r.get("amazon", {}).get("title")) == "f"]
    mal = [r for r in items if _gender_of(r.get("amazon", {}).get("title")) == "m"]
    if len(fem) < 2 or len(mal) < 2:
        return items   # 片方しか無い/性別商品でない → そのまま
    keep_g = "f" if len(fem) >= len(mal) else "m"   # 多数派に寄せる
    return [r for r in items if _gender_of(r.get("amazon", {}).get("title")) in (keep_g, None)]


def diversify_brands(items, max_per=MAX_PER_BRAND):
    """並び済みリストから、同ブランドが max_per を超えないように選ぶ（多様性確保）。
    ・上位（＝良い順）から、各ブランド max_per 件までを採用。
    ・超過分は表示件数を満たすために戻さない。
    戻り値：多様性を効かせた並び済みリスト。"""
    picked, counts = [], {}
    for r in items:
        b = _norm_brand(r.get("amazon", {}).get("brand"))
        if b and counts.get(b, 0) >= max_per:
            continue
        picked.append(r)
        counts[b] = counts.get(b, 0) + 1
    return picked


def load_gemini_conf():
    if not os.path.exists(GEMINI_CONF):
        return None
    try:
        d = json.load(open(GEMINI_CONF, encoding="utf-8"))
    except Exception as e:
        print("    Gemini設定の読込失敗:", e); return None
    if not d.get("api_key") or "＜" in d.get("api_key", ""):
        print("    Gemini: api_key 未入力のためリランクをスキップ"); return None
    d.setdefault("model", "gemini-2.0-flash")
    d.setdefault("daily_limit", 500)
    return d


def gemini_usage_ok(daily_limit, kind="rerank"):
    """共有予算ガード。実行をまたいだ使用量(Supabase)を見て上限未満なら True。
       kind='expand' は枠の一部までに制限し、rerank(不適合ドロップ)用の枠を温存する。
       戻り値は従来互換の (ok, today, used)。"""
    if not _BUDGET["loaded"]:
        u = _budget_rpc(0)
        if u is not None:
            _BUDGET["used"], _BUDGET["db"] = u, True
        else:
            _BUDGET["used"], _BUDGET["db"] = _local_used(), False
        _BUDGET["loaded"] = True
    if _BUDGET.get("exhausted"):
        return False, _pac_today(), _BUDGET["used"]
    used = _BUDGET["used"]
    cap = daily_limit
    if kind == "expand":
        cap = min(daily_limit, int(daily_limit * GEMINI_EXPAND_FRACTION))
    return used < cap, _pac_today(), used


def gemini_usage_bump(today, used):
    """共有カウンタ(Supabase)を+1。DB不可時はローカルファイルに退避。"""
    if _BUDGET.get("db"):
        u = _budget_rpc(1)
        _BUDGET["used"] = u if u is not None else _BUDGET["used"] + 1
    else:
        _BUDGET["used"] += 1
        _local_bump(_BUDGET["used"])


def _budget_exhaust():
    """Geminiが429(枠切れ)を返した時に呼ぶ。以降このプロセスではGemini呼び出しを止める。
       共有カウンタにも上限相当を記録し、後続の実行も無駄打ちしないようにする。"""
    _BUDGET["exhausted"] = True
    if _BUDGET.get("db"):
        # 残枠を埋めて cap 超えにする（他ジョブも即スキップさせる）
        remaining = 950 - int(_BUDGET.get("used", 0))
        if remaining > 0:
            u = _budget_rpc(remaining)
            if u is not None:
                _BUDGET["used"] = u


def gemini_expand_queries(conf, intent_text, intent_key):
    """切り口(検索意図)を、楽天/Yahooで“本命の商品”に当たる具体的な検索クエリ2〜4個へ変換する。
    肝：切り口の概念語(盗難/場所取り/食いつき/高さ 等)は、そのままでは商品名にないため、
        それを満たす具体的な商品名・カテゴリに翻訳する（盗難→自転車 鍵/U字ロック 等）。
    これを gather_candidates の extra_queries（最優先クエリ）に渡すと、本命が候補プールに入る。
    失敗時は [] を返し、通常の plan_queries にフォールバックする。"""
    cached = _cache_query_get(intent_key)
    if cached is not None:
        return cached          # 資産キャッシュから即返す（Geminiを呼ばない）
    # expandは“あると良い”寄り。共有予算が細ったら省略し、rerank用の枠を温存する。
    ok, today, used = gemini_usage_ok(conf["daily_limit"], kind="expand")
    if not ok:
        print("    Gemini: 予算温存のためクエリ変換を省略（used=%d）→ 通常クエリで続行" % used)
        return []
    prompt = (
        "あなたはEC検索のプロです。以下の『切り口(検索意図)』に最も合う商品を"
        "楽天/Yahooショッピングで見つけるための検索クエリを2〜4個作ってください。\n"
        "重要ルール:\n"
        "0)【最優先】切り口の中心は“悩み・目的”（多くは「」で囲まれた語。例:「盗難」「場所取り」"
        "「外れる」「高さ」）。シーン語（週末ライド/通勤/花火大会 等）ではなく、この悩み・目的を"
        "『解決する商品』を狙う。例)『週末ライドでの「盗難」を抑える自転車用品』→シーンの"
        "サイクルウェアではなく「自転車 鍵 U字ロック」「自転車 GPS 盗難防止」。\n"
        "1) 切り口の“概念・悩み・目的”の語（例: 盗難/場所取り/食いつき/高さ/外れる/涼しい/日持ち）は、"
        "それを解決する『最も具体的な商品カテゴリ』に翻訳する。テーマの総称や“近いが別物”で妥協しない。\n"
        "   例) 自転車 盗難 → 「自転車 鍵 U字ロック」「自転車 GPS 盗難防止」\n"
        "       花火大会 場所取り → 「レジャーシート 大判」\n"
        "       枕 高さで選ぶ → 「枕 高さ 低め」「枕 高さ 10cm」（高さ調整は切り口が明示した時だけ）\n"
        "       シニア犬 食いつき ドッグフード → 「シニア犬 ドッグフード 嗜好性」\n"
        "       浴衣 着付け → 「着付け 帯 クリップ」「浴衣 帯板」（浴衣本体ではない）\n"
        "       ノート かさばる → 「薄型 ノート」「方眼 ノート 薄い」（普通のノートではない）\n"
        "       犬 抜ける ハーネス → 「犬 ハーネス 脱走防止」\n"
        "       夏祭り 子連れ → 「子供 迷子防止 ハーネス」（下駄など祭りの衣類ではない）\n"
        "2) 切り口が指定する対象（シニア/新生児/レディース/メンズ 等）は必ずクエリに残す。\n"
        "2.5) 買い軸が具体的な商品名（うちわ/コンロ/チューブ等）なら、その商品種そのものだけを狙い、"
        "近い用途の代用品を混ぜない。例: うちわに扇子・ハンディファン・携帯扇風機を含めない。\n"
        "2.6) 一般消費者が1つ買える単品を優先し、10本組・100枚・業務用ロット・ケース販売を狙わない。\n"
        "3) 各クエリは実際に商品タイトルに現れる2〜4語の日本語。抽象語だけのクエリは作らない。\n"
        "4) 商品カテゴリが曖昧な切り口（『こだわる〇〇グッズ』『カップル向けの〇〇』等）でも、"
        "その場面・目的で最も定番の具体商品を1〜2種類に絞って出す"
        "（例: 花火大会 カップル → 「浴衣 レディース」「手持ち花火 セット」）。\n"
        "5) 衣類・履物・下着など“性別のある商品”で切り口に性別指定が無い場合は、"
        "レディース向けで統一する（文脈でメンズが自然ならメンズで統一。性別を混在させない）。\n"
        "出力は必ずJSON配列（文字列の配列）のみ: [\"クエリ1\",\"クエリ2\",...]\n\n"
        "切り口の構造化情報:\n" + intent_text)
    url = ("https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent?key=%s"
           % (conf["model"], conf["api_key"]))
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json"},
    }).encode()
    data = None
    gemini_usage_bump(today, used)   # 予約：実行前に加算（リトライ/失敗も枠を消費するため）
    for attempt in range(1, 3):
        try:
            data = get_json(url, data=body,
                            headers={"Content-Type": "application/json"}, timeout=60)
            break
        except urllib.error.HTTPError as e:
            if e.code == 429:
                _budget_exhaust(); return []   # 無料枠切れ→以降スキップ
            if e.code in (503, 500) and attempt < 2:
                time.sleep(5); continue
            return []
        except Exception:
            if attempt < 2:
                time.sleep(5); continue
            return []
    if data is None:
        return []
    try:
        txt = dig(data, "candidates", 0, "content", "parts", 0, "text") or "[]"
        arr = json.loads(txt)
        out = []
        for q in arr:
            q = str(q).strip()
            if q and q not in out:
                out.append(q)
        out = out[:4]
        _cache_query_put(intent_key, out)   # 資産に保存（次回以降は0コスト）
        return out
    except Exception:
        return []


def _rerank_sort(pool):
    """並べ替え：AI関連性→Amazon評価順レーン→照合確度。"""
    def review_rank(row):
        value = row.get("candidate", {}).get("reviews_rank")
        return int(value) if value is not None else 999
    pool.sort(key=lambda r: (-(r.get("ai_score") if r.get("ai_score") is not None else -1),
                             review_rank(r) == 999,
                             review_rank(r),
                             VERDICT_ORDER[r["verdict"]],
                             -learn_score(r["amazon"].get("brand")),
                             -r["consensus"],
                             -(r["candidate"].get("review_count") or 0)))


_WEIGHT_EVIDENCE_RE = re.compile(
    r"軽量|超軽量|軽い|重量|重さ|[0-9０-９]+(?:[\.,．][0-9０-９]+)?\s*(?:kg|g|キログラム|グラム)", re.I)
_HEIGHT_EVIDENCE_RE = re.compile(r"高さ|高め|低め|低さ|高低|段階調整|[0-9０-９]+(?:\.[0-9０-９]+)?\s*(?:cm|mm|㎝)", re.I)
_GAP_EVIDENCE_RE = re.compile(r"すき間|すきま|隙間|目地|溝|コーナー|角|細部|細い|スリム", re.I)
_COOL_EVIDENCE_RE = re.compile(r"ひんやり|冷感|涼感|接触冷感|冷却|クール|pcm|ジェル", re.I)


def apply_axis_evidence(items, components=None):
    """AIのソフト採点に、明示できる買い軸だけ決定的な証拠ゲートを重ねる。

    全軸を単純な文字一致にすると誤除外が増えるため、現時点では証拠を安全に判定できる
    影テストで証拠を安全に判定できた軽量・高さ・すき間・冷感と、
    具体商品名である「うちわ」を対象にする。
    """
    axes = [norm(value) for kind, value in normalize_components(components) if kind == "buy"]
    need_weight = any(axis in ("軽い", "軽量") for axis in axes)
    need_uchiwa = "うちわ" in axes
    need_height = "高さ" in axes
    need_gap = any(axis in ("すき間", "すきま", "隙間") for axis in axes)
    need_cool = any(axis in ("ひんやり", "冷感", "涼しい") for axis in axes)
    for row in items or []:
        amz = row.get("amazon") or {}
        text = norm(" ".join([amz.get("title") or ""] + [str(x) for x in (amz.get("features") or [])]))
        failures = []
        if need_weight and not _WEIGHT_EVIDENCE_RE.search(text):
            failures.append("買い軸『軽い』の重量・軽量根拠なし")
        if need_uchiwa and not ("うちわ" in text or "団扇" in text):
            failures.append("商品種『うちわ』ではない")
        if need_height and not _HEIGHT_EVIDENCE_RE.search(text):
            failures.append("買い軸『高さ』の明示なし")
        if need_gap and not _GAP_EVIDENCE_RE.search(text):
            failures.append("買い軸『すき間』の形状・用途根拠なし")
        if need_cool and not _COOL_EVIDENCE_RE.search(text):
            failures.append("買い軸『ひんやり』の冷感根拠なし")
        if failures and row.get("ai_score") is not None:
            row["ai_score"] = min(row["ai_score"], 30)
            suffix = " / ".join(failures)
            row["ai_reason"] = (row.get("ai_reason") or "") + (" / " if row.get("ai_reason") else "") + suffix
    return items


_PRODUCT_TYPE_PATTERNS = [
    ("うちわ", r"うちわ|団扇"), ("浴衣", r"浴衣"), ("帯・着付け小物", r"帯|帯板|着付け"),
    ("枕", r"枕|ピロー"), ("冷感マット", r"マット|冷却シート|冷感シート"),
    ("ペットベッド", r"ペットベッド|ドーム|ハウス"), ("冷感ブランケット", r"ブランケット|毛布"),
    ("電動ドライバー", r"電動ドライバ|電動ドリル"), ("修理キット", r"パンク修理|リペアキット|パッチ"),
    ("携帯ポンプ", r"空気入れ|携帯ポンプ|ミニポンプ"), ("タイヤレバー", r"タイヤレバー"),
    ("チューブレスプラグ", r"チューブレス|プラグ"), ("予備チューブ", r"チューブ"),
    ("キャンプテーブル", r"テーブル|ロールテーブル"), ("キャンプチェア", r"チェア|イス"),
    ("調理器具", r"クッカー|鍋|フライパン|メスティン"), ("バーナー", r"バーナー|ストーブ|コンロ"),
    ("タープ", r"タープ"), ("ペグハンマー", r"ハンマー"),
    ("風鈴キット", r"風鈴"), ("ランプキット", r"ランプ|ライト"), ("時計キット", r"時計"),
    ("標本・レジン", r"標本|レジン"), ("科学実験キット", r"科学実験|実験キット"),
    ("掃除ブラシ", r"ブラシ|ブラシセット"), ("スクレーパー", r"スクレーパー|ヘラ"),
]


def infer_product_type(row):
    """AIが返した短い商品種を優先し、古いキャッシュはタイトルから補う。"""
    explicit = str(row.get("product_type") or "").strip()
    if explicit:
        return _norm_key(explicit)[:40]
    amz = row.get("amazon") or {}
    text = norm(" ".join([amz.get("title") or ""] + [str(x) for x in (amz.get("features") or [])[:2]]))
    for label, pattern in _PRODUCT_TYPE_PATTERNS:
        if re.search(pattern, text, re.I):
            return label
    return "その他"


def diversify_product_types(items, max_per=MAX_PER_PRODUCT_TYPE):
    """商品種を分散する。同じ既知商品種は最大 max_per 件までにする。

    商品種を判定できない「その他」は、誤判定による過剰な空洞化を避けるため上限を設けない。
    既知商品種の超過分を後から戻すことはしない（件数合わせで多様性を無効化しない）。
    """
    picked, counts = [], {}
    for row in items or []:
        key = infer_product_type(row)
        row["product_type"] = key
        if key != "その他" and counts.get(key, 0) >= max_per:
            continue
        picked.append(row)
        counts[key] = counts.get(key, 0) + 1
    return picked


def select_final_pool(items, require_ai=True, rel_min=REL_MIN,
                      target_max=TARGET_MAX, max_per_brand=MAX_PER_BRAND, components=None):
    """適格候補だけから最大 target_max 件を選ぶ。

    rejectされた候補を件数合わせで復活させないことが、この関数の最重要契約。
    """
    pool = list(items or [])
    if require_ai:
        apply_axis_evidence(pool, components)
        pool = [r for r in pool
                if r.get("ai_score") is not None and r.get("ai_score") >= rel_min]
    _rerank_sort(pool)
    pool = gender_consistency(pool)
    pool = diversify_brands(pool, max_per=max_per_brand)
    pool = diversify_product_types(pool)
    return pool[:target_max]


def gemini_rerank(conf, intent_text, intent_key, pool, few_shot=None):
    """poolを切り口適合度でリランク。各itemに ai_score(0-100)/ai_reason を付与。
       資産キャッシュ（切り口×ASIN→スコア）を優先し、未キャッシュ分だけGeminiで採点する。
       few_shot=過去の👍商品。失敗時はpoolをそのまま返す。"""
    # 1) 資産キャッシュを適用：既知の(切り口×ASIN)はDBから即スコア（Geminiを使わない）
    asins = [r["amazon"].get("asin") for r in pool if r["amazon"].get("asin")]
    cached = _cache_score_get(intent_key, few_shot, asins) if asins else {}
    todo = []
    for r in pool:
        a = r["amazon"].get("asin")
        if a and a in cached:
            r["ai_score"], r["ai_reason"], r["product_type"] = cached[a]
        else:
            r["ai_score"], r["ai_reason"] = None, ""
            r["product_type"] = ""
            todo.append(r)
    # 2) 全件キャッシュ命中ならGeminiを呼ばず確定
    if not todo:
        _rerank_sort(pool)
        print("    AIスコア: 全%d件キャッシュ命中（Gemini呼び出し0）" % len(pool))
        return pool, True

    # 3) 未キャッシュ分だけ採点（プロンプトも縮小＝トークン節約）
    ok, today, used = gemini_usage_ok(conf["daily_limit"])
    if not ok:
        print("    Gemini: 日次上限(%d)到達。未採点%d件は採点せず→既存保護" % (conf["daily_limit"], len(todo)))
        return pool, False

    lines = []
    for i, r in enumerate(todo, 1):
        amz = r["amazon"]
        features = " / ".join(str(x).strip() for x in (amz.get("features") or [])[:3])[:280]
        lines.append("%d) %s | ブランド:%s | 特徴:%s | 価格:%s | %d源一致 | レビュー%s件"
                     % (i, (amz.get("title") or "")[:70], amz.get("brand") or "-",
                        features or "-", amz.get("price") or "-", r["consensus"],
                        r["candidate"].get("review_count") or "?"))
    fewshot_txt = ""
    if few_shot:
        fewshot_txt = "\nチームの過去の判断（お手本／採用=良い・見送り=避けたい）:\n" + "\n".join("- " + s for s in few_shot)

    prompt = (
        "あなたはECの商品選定アシスタントです。以下の『切り口(検索意図)』に、各商品がどれだけ合致するかを"
        "0〜100で採点してください。\n"
        "【最優先】切り口の中心は“悩み・目的”（多くは「」で囲まれた語。例:「盗難」「場所取り」「外れる」）。"
        "シーン語（週末ライド/通勤/花火大会 等）だけ合っても、その悩み・目的を解決しない商品は大きく減点。"
        "例)『週末ライドでの「盗難」を抑える』では、盗難を防ぐ鍵/ロック/GPSが高評価、"
        "サイクルウェア等“ライドで使うが盗難と無関係”な商品は20点以下。\n"
        "【買い軸】『○○で選ぶ』の○○は比較に必要な条件です。タイトルまたは特徴にその属性の"
        "具体的な証拠が無ければ30点以下。ただし『高さで選ぶ』は固定高さの明記でもよく、"
        "『高さ調整で選ぶ』と明記された場合だけ調整式を必須にしてください。\n"
        "買い軸が具体的商品名なら別商品種を代用品として認めない。例:『うちわアイテム』の扇子・"
        "ハンディファン・携帯扇風機は30点以下。『軽い』では重量値または軽量の明記を根拠とし、"
        "小型・コンパクトというだけで軽いと推定しない。\n"
        "【最重要】切り口が求める“商品カテゴリ・用途・形状”に合わないものは大きく減点（20点以下）してください。\n"
        "  - 別カテゴリの無関係な商品（例：イヤホンの切り口にキーケース/ゴミ箱/ペット用品）＝ほぼ0点。\n"
        "  - “近いが別物”（同じテーマ内でも悩みが求めるサブカテゴリと違う）も減点。例：\n"
        "      「首が痛い枕」に抱き枕、「サンダル」にスリッパ/ルームシューズ、「高さ調整枕」に高さ非調整の枕、\n"
        "      「浴衣の着付け対策」に浴衣本体（着付け小物ではない）、「脱走防止ハーネス」に普通の首輪。\n"
        "  - カテゴリは同じでも切り口の用途に合わない形状/仕様（例：『運動/通勤で外れない』なら、"
        "コードが邪魔で外れやすい“有線”や、固定力の弱い開放型は低評価。『高音質でじっくり』なら逆に許容）。\n"
        "ブランドの信頼性・不自然な煽り/粗悪さも加味し、価格の高安だけで過度に上下させないこと。\n"
        "product_typeは多様性判定用の短い一般名にする（例: 風鈴キット/月ランプ/携帯ポンプ）。"
        "ブランド名・色・サイズは含めない。\n"
        "出力は必ずJSON配列のみ: "
        "[{\"i\":番号,\"score\":整数,\"reason\":\"20字程度の理由\",\"product_type\":\"短い一般名\"}]\n\n"
        "切り口の構造化情報:\n" + intent_text + fewshot_txt + "\n\n商品一覧:\n" + "\n".join(lines))

    url = ("https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent?key=%s"
           % (conf["model"], conf["api_key"]))
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }).encode()
    data = None
    gemini_usage_bump(today, used)   # 予約：実行前に加算（リトライ/失敗も枠を消費するため）
    for attempt in range(1, 4):                     # 503/タイムアウトは一時的なので最大3回
        try:
            data = get_json(url, data=body,
                            headers={"Content-Type": "application/json"}, timeout=40)
            break
        except urllib.error.HTTPError as e:
            body_txt = e.read().decode()[:200]
            if e.code == 429:
                print("    Gemini HTTP 429（無料枠切れ）→ 以降のリランクをスキップ")
                _budget_exhaust(); return pool, False
            if e.code in (503, 500) and attempt < 3:
                print("    Gemini HTTP %s（%d回目）…%d秒待って再試行" % (e.code, attempt, 6 * attempt))
                time.sleep(6 * attempt); continue
            print("    Gemini HTTP %s: %s" % (e.code, body_txt)); return pool, False
        except Exception as e:
            if attempt < 3:
                print("    Gemini %s（%d回目）…%d秒待って再試行" % (type(e).__name__, attempt, 6 * attempt))
                time.sleep(6 * attempt); continue
            print("    Gemini 失敗: %s: %s" % (type(e).__name__, e)); return pool, False
    if data is None:
        return pool, False

    try:
        txt = dig(data, "candidates", 0, "content", "parts", 0, "text") or "[]"
        scores = json.loads(txt)
        by_i = {int(s["i"]): s for s in scores if "i" in s}
    except Exception as e:
        print("    Geminiの応答解析失敗（リランクせず）:", e)
        return pool, False

    new_items = []
    for i, r in enumerate(todo, 1):
        s = by_i.get(i, {})
        r["ai_score"] = s.get("score")
        r["ai_reason"] = s.get("reason", "")
        r["product_type"] = s.get("product_type", "")
        a = r["amazon"].get("asin")
        if a and r["ai_score"] is not None:
            new_items.append({
                "asin": a,
                "score": r["ai_score"],
                "reason": _encode_cached_reason(r["ai_reason"], r.get("product_type")),
            })
    _cache_score_put(intent_key, few_shot, new_items)   # 新規採点を資産に追加（次回以降0コスト）
    _rerank_sort(pool)
    print("    AIスコア: %d件キャッシュ + %d件新規採点" % (len(pool) - len(todo), len(todo)))
    return pool, True


# ============================================================
# メイン
# ============================================================
def main():
    ap = argparse.ArgumentParser(description="切り口に合うAmazon商品候補を生成")
    ap.add_argument("query", nargs="*", help="外部EC検索用キーワード")
    ap.add_argument("--theme", default="", help="テーマ名")
    ap.add_argument("--intent", default="", help="切り口タイトル（AI選定意図）")
    ap.add_argument("--components-json", default="[]",
                    help='切り口要素のJSON（例: [["nayami","パンク"]]）')
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--rerank", action="store_true")
    ns = ap.parse_args()
    want_json = ns.json
    want_rerank = ns.rerank
    kw = " ".join(ns.query).strip() or "ソロ キャンプ 軽量"
    try:
        components = json.loads(ns.components_json)
    except Exception:
        print("    切り口要素JSONの解析失敗。要素なしで続行")
        components = []
    intent = build_intent_spec(ns.theme, ns.intent, kw, components)

    load_learning()   # ⑤ learning.json があればブランド重み＋お手本を適用（無ければ通常動作）
    print("=" * 62)
    print("検索キーワード:", kw)
    print("選定する切り口:", intent["angle_title"])
    print("=" * 62)

    # AIクエリ変換は既存キャッシュを優先。Amazonの通常2系統が薄い時だけ、
    # ここで得た具体商品クエリを1本だけ救済検索に使う。
    gconf = load_gemini_conf() if want_rerank else None
    if want_rerank and not gconf:
        emit_empty(want_json, "AI設定が無いため、未採点候補は公開しません")
    ai_queries = []
    if gconf:
        ai_queries = gemini_expand_queries(gconf, intent["intent_text"], intent["intent_key"])
        if ai_queries:
            print("[0] AIクエリ変換 → " + " / ".join(ai_queries))

    # --- 1. Amazonを正式候補として2系統検索 ---
    c = amazon_conf()
    token = amazon_token(c)
    primary_query = build_amazon_query(intent, kw) or kw
    fallback_queries = (
        list(ai_queries or [])
        + concrete_fallback_queries(intent, kw)
        + plan_queries(kw)
    )
    resolved, tried, dropped = amazon_direct_candidates(
        c, token, primary_query, fallback_queries=fallback_queries)
    print("\n[1] Amazon直接候補: %d件（クエリ=%s）" % (len(resolved), primary_query))
    for log in tried:
        suffix = " / ERROR=" + log["error"] if log.get("error") else ""
        print("    %s: %s → %d件%s" % (log["lane"], log["query"], log["count"], suffix))
    if dropped:
        print("    業務用ロット・法務バックストップで %d件を除外" % len(dropped))
        for title, why in dropped[:6]:
            print("      ✗ %s … %s" % (title[:44], why))
    if not resolved:
        emit_empty(want_json, "Amazonの関連性順・評価順・救済検索がすべて0件")

    # --- 2. AI投入前にレーン分散・ブランド偏り・重複を圧縮 ---
    resolved = select_rerank_candidates(resolved)
    best = {}
    for r in resolved:
        k = dedup_key(r)
        if k not in best:
            best[k] = r
    uniq = list(best.values())
    _before_var = len(uniq)
    uniq = collapse_variants(uniq)
    if _before_var != len(uniq):
        print("    同ブランドの重複/色サイズ違いを %d件 畳み込み" % (_before_var - len(uniq)))
    multi = sum(1 for r in uniq if r["consensus"] >= 2)
    print("\n[2] AI投入候補: %d件 → %d件（関連性順+評価順の両方に出現=%d件）"
          % (len(resolved), len(uniq), multi))
    if LEARN:
        _bw = LEARN.get("brand_weight") or {}
        _fs = LEARN.get("few_shot") or []
        print("    学習を適用：ブランド重み%d件・お手本%d件（承認/👍👎から）" % (len(_bw), len(_fs)))

    # --- 3. AIリランク＋関連性フィルタ：切り口に用途が合わない商品を落とす ---
    #     ※最終件数を決める前に全候補(uniq)を採点し、スコアが低い（＝
    #       切り口の用途に合わない。例: 「おむつ」でヒットしたおむつゴミ箱/犬用おむつ）
    #       を除外してから選ぶ。これにより先読みでも用途違いが並ばない。
    reranked = False
    if want_rerank:
        # gconf は main 冒頭でロード済み（AIクエリ変換と共用）。二重ロードしない。
        if gconf:
            print("\n[3] AIリランク（Gemini %s）で切り口適合度を採点し用途不一致を除外…" % gconf["model"])
            _few = (LEARN.get("few_shot") if LEARN else None) or None
            uniq, reranked = gemini_rerank(
                gconf, intent["intent_text"], intent["intent_key"], uniq, few_shot=_few)
            if not reranked:
                emit_empty(want_json, "AI採点が未完了。未採点候補は公開しません")

    # --- 4. 適格候補だけから最大6件を選ぶ。除外候補・同ブランド超過分は復活させない。 ---
    before_relevance = len(uniq)
    pool = select_final_pool(uniq, require_ai=want_rerank, components=intent["components"])
    if want_rerank:
        rejected = before_relevance - sum(
            1 for r in uniq if r.get("ai_score") is not None and r.get("ai_score") >= REL_MIN)
        print("    関連性フィルタ：スコア<%d または未採点 %d件を不可逆に除外"
              % (REL_MIN, rejected))
    if not pool:
        emit_empty(want_json, "品質基準を満たす商品が0件")
    print("    最終選抜：最大%d件／1ブランド最大%d件（不足時も水増しなし）"
          % (TARGET_MAX, MAX_PER_BRAND))

    print("\n" + "=" * 62)
    print("最終プール（%d件）: 採用%d / 要確認%d / 保留%d"
          % (len(pool),
             sum(1 for r in pool if r["verdict"] == "採用"),
             sum(1 for r in pool if r["verdict"] == "要確認"),
             sum(1 for r in pool if r["verdict"] == "保留")))
    print("=" * 62)
    for i, r in enumerate(pool, 1):
        amz = r["amazon"]
        print("%2d. [%s] %d源一致(%s) %s"
              % (i, r["verdict"], r["consensus"], "/".join(r["sources"]),
                 (amz.get("title") or "")[:40]))
        print("     ASIN=%s / brand=%s / %s / ★件数(候補)=%s"
              % (amz.get("asin"), amz.get("brand") or "-",
                 amz.get("price") or "-",
                 r["candidate"].get("review_count") or "-"))
        if reranked and r.get("ai_score") is not None:
            print("     AI適合=%s / %s" % (r["ai_score"], r.get("ai_reason", "")))
        print("     %s" % amz.get("url"))

    if want_json:
        out = [{
            "verdict": r["verdict"], "reason": r["reason"],
            "consensus": r["consensus"], "sources": r["sources"],
            "ai_score": r.get("ai_score"), "ai_reason": r.get("ai_reason"),
            "product_type": r.get("product_type"),
            "asin": r["amazon"].get("asin"),
            "parent_asin": r["amazon"].get("parent_asin"),
            "title": r["amazon"].get("title"),
            "brand": r["amazon"].get("brand"),
            "price": r["amazon"].get("price"),
            "url": r["amazon"].get("url"),
            "image_url": r["amazon"].get("image"),
            "features": (r["amazon"].get("features") or [])[:5],
            "source_candidate": r["candidate"].get("name"),
            "candidate_source": r["candidate"].get("source"),
            "review_count": r["candidate"].get("review_count"),
        } for r in pool]
        print("\n---- JSON ----")
        print(json.dumps(out, ensure_ascii=False, indent=2))

    print("\n==== 見方 ====")
    print("正式候補=Amazon Creators APIの直接検索。評価順レーンは星4以上指定。")
    print("並び順=AI適合→Amazon評価順レーン→学習重み→多様性。AI%d未満は表示しない。"
          % REL_MIN)


if __name__ == "__main__":
    main()
