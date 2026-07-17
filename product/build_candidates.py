#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
係② 商品あつめ — タスク①「除外/重複ルール＋ブランド照合ゲート」検証版（一括）

パイプライン（1本で通す・検証重視の見やすい出力）:
  1. 候補取得   : 楽天市場API ＋ Yahoo!ショッピングAPI
  2. 除外       : (a) セット/まとめ買いタイトル除外（単品優先）
                  (b) 効果効能カテゴリ＋怪しい効能表現の backstop 除外（薬機法/景表法/PSE）
  3. Amazon解決 : Creators API searchItems で正規ASIN/URL/ブランド/価格
  4. 照合ゲート : Amazon結果と候補のブランド一致 → 採用/要確認/保留 の段階化（空にしない）
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
import os, sys, re, json, time, base64, unicodedata
import urllib.parse, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
SHOP = os.path.join(HERE, "shopping_api.json")
AMZ = os.path.join(HERE, "amazon_creators.json")

# 目標件数（プールの下限。足りなければ 要確認→保留 を繰り上げて埋める）
TARGET_MIN = 9
# 検索で集める生候補の下限。除外・Amazon照合で落ちる分を見込んで目標の3倍を確保する。
POOL_MIN = TARGET_MIN * 3
# 1ブランドの最大表示数（同ブランドばかりにならないよう多様性を確保。足りなければ緩める）
MAX_PER_BRAND = 2
# Amazon照合する候補の上限。照合は1件ずつAPIを叩くので多いと激遅（先読み全体がタイムアウト）。
#   ※gather はAI変換クエリ→テーマ→修飾 の順に集めるので、先頭ほど「切り口の本命」。
#     レビュー数で並べ替えず gather 順のまま上位を残す＝本命(鍵/シート等)を維持しつつ高速化。
AMAZON_RESOLVE_MAX = 14

# ---- レート配慮（無料枠を超えないための最小限のウェイト）----
SLEEP_BETWEEN_AMAZON = 0.6   # Amazon searchItems 連打の間隔（秒）


# ============================================================
# 0. 除外辞書（叩き台。KAZUYAさんが後で修正する前提）
# ============================================================
# (a) セット/まとめ買い/複数個 → 単品優先で除外
SET_PATTERNS = [
    r"セット", r"まとめ買い", r"まとめ売り", r"詰め合わせ", r"詰合せ",
    r"[0-9０-９]+\s*個(?:入|セット|組|パック|袋|本|枚|箱)",
    r"[0-9０-９]+\s*(?:個|本|枚|袋|箱|パック|セット|組)入",
    # 「×N」はサイズ表記(35×50cm等)と衝突するため、数量単位が続く時だけ複数個とみなす
    r"×\s*[0-9０-９]+\s*(?:個|本|枚|袋|箱|パック|セット|組|set|pcs)",
    r"x\s*[0-9]+\s*(?:個|本|set|pack)",
    r"[0-9０-９]+pcs", r"[0-9０-９]+点セット",
    r"福袋", r"アソート",
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
    ※以前は TARGET_MIN(8) で打ち切っていたため、候補8件→除外/照合落ちで最終1件、
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
                seen.add(k); cands.append(r); added += 1
        tried.append("%s(+%d)" % (q, added))
        if len(cands) >= min_need and i >= MIN_QUERIES - 1:
            break
    return cands, tried


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


def amazon_search(c, token, keywords, count=3):
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


def amazon_direct_asins(c, token, kw, count=8):
    """Amazonを独立ソースとして、切り口キーワードで直接検索したASIN集合。
       楽天/Yahoo経由の解決ASINとの一致＝多源コンセンサスの1票になる。"""
    res = amazon_search(c, token, kw, count=count)
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


# ============================================================
# 6.（任意）AIリランク：切り口適合度で並べ替え（Gemini）
#   --rerank 指定かつ gemini_api.json がある時だけ実行。無ければ静かにスキップ。
#   鍵はMacのファイルにのみ。日次上限ガード付き（無料枠を超えない）。
# ============================================================
GEMINI_CONF = os.path.join(HERE, "gemini_api.json")
GEMINI_USAGE = os.path.join(HERE, "gemini_usage.json")

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


def gender_consistency(items, target=TARGET_MIN):
    """性別のある商品が混在する場合、多数派の性別＋両用に揃える（少数派を後方へ）。
    ・明確に性別付きの商品が2種類とも一定数ある時だけ作動（家電/食品等は無影響）。
    ・揃えると target 未満になる場合は少数派を戻して空にしない。"""
    fem = [r for r in items if _gender_of(r.get("amazon", {}).get("title")) == "f"]
    mal = [r for r in items if _gender_of(r.get("amazon", {}).get("title")) == "m"]
    if len(fem) < 2 or len(mal) < 2:
        return items   # 片方しか無い/性別商品でない → そのまま
    keep_g = "f" if len(fem) >= len(mal) else "m"   # 多数派に寄せる
    primary = [r for r in items if _gender_of(r.get("amazon", {}).get("title")) in (keep_g, None)]
    minority = [r for r in items if _gender_of(r.get("amazon", {}).get("title")) not in (keep_g, None)]
    if len(primary) < target and minority:
        primary += minority[:target - len(primary)]
    return primary


def diversify_brands(items, max_per=MAX_PER_BRAND, target=TARGET_MIN):
    """並び済みリストから、同ブランドが max_per を超えないように選ぶ（多様性確保）。
    ・上位（＝良い順）から、各ブランド max_per 件までを採用。
    ・超過分は overflow へ退避し、目標件数に満たない時だけ良い順で補充（＝空にしない）。
    戻り値：多様性を効かせた並び済みリスト。"""
    picked, overflow, counts = [], [], {}
    for r in items:
        b = _norm_brand(r.get("amazon", {}).get("brand"))
        if b and counts.get(b, 0) >= max_per:
            overflow.append(r)
            continue
        picked.append(r)
        counts[b] = counts.get(b, 0) + 1
    if len(picked) < target and overflow:
        picked += overflow[:target - len(picked)]
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


def gemini_usage_ok(daily_limit):
    """日次上限ガード。今日の呼び出し回数が上限未満なら True。"""
    import datetime
    today = datetime.date.today().isoformat()
    used = 0
    if os.path.exists(GEMINI_USAGE):
        try:
            u = json.load(open(GEMINI_USAGE, encoding="utf-8"))
            if u.get("date") == today:
                used = int(u.get("count", 0))
        except Exception:
            used = 0
    return used < daily_limit, today, used


def gemini_usage_bump(today, used):
    try:
        json.dump({"date": today, "count": used + 1},
                  open(GEMINI_USAGE, "w", encoding="utf-8"))
    except Exception as e:
        print("    Gemini使用量の記録失敗:", e)


def gemini_expand_queries(conf, angle_kw):
    """切り口(検索意図)を、楽天/Yahooで“本命の商品”に当たる具体的な検索クエリ2〜4個へ変換する。
    肝：切り口の概念語(盗難/場所取り/食いつき/高さ 等)は、そのままでは商品名にないため、
        それを満たす具体的な商品名・カテゴリに翻訳する（盗難→自転車 鍵/U字ロック 等）。
    これを gather_candidates の extra_queries（最優先クエリ）に渡すと、本命が候補プールに入る。
    失敗時は [] を返し、通常の plan_queries にフォールバックする。"""
    ok, today, used = gemini_usage_ok(conf["daily_limit"])
    if not ok:
        return []
    prompt = (
        "あなたはEC検索のプロです。以下の『切り口(検索意図)』に最も合う商品を"
        "楽天/Yahooショッピングで見つけるための検索クエリを2〜4個作ってください。\n"
        "重要ルール:\n"
        "1) 切り口の“概念・悩み・目的”の語（例: 盗難/場所取り/食いつき/高さ/外れる/涼しい/日持ち）は、"
        "それを解決する具体的な商品名・カテゴリに翻訳する。\n"
        "   例) 自転車 盗難 → 「自転車 鍵 U字ロック」「自転車 GPS 盗難防止」\n"
        "       花火大会 場所取り → 「レジャーシート 大判」\n"
        "       枕 高さ → 「高さ調整枕」\n"
        "       シニア犬 食いつき ドッグフード → 「シニア犬 ドッグフード 嗜好性」\n"
        "2) 切り口が指定する対象（シニア/新生児/レディース/メンズ 等）は必ずクエリに残す。\n"
        "3) 各クエリは実際に商品タイトルに現れる2〜4語の日本語。抽象語だけのクエリは作らない。\n"
        "4) 商品カテゴリが曖昧な切り口（『こだわる〇〇グッズ』『カップル向けの〇〇』等）でも、"
        "その場面・目的で最も定番の具体商品を1〜2種類に絞って出す"
        "（例: 花火大会 カップル → 「浴衣 レディース」「手持ち花火 セット」）。\n"
        "5) 衣類・履物・下着など“性別のある商品”で切り口に性別指定が無い場合は、"
        "レディース向けで統一する（文脈でメンズが自然ならメンズで統一。性別を混在させない）。\n"
        "出力は必ずJSON配列（文字列の配列）のみ: [\"クエリ1\",\"クエリ2\",...]\n\n"
        "切り口: " + angle_kw)
    url = ("https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent?key=%s"
           % (conf["model"], conf["api_key"]))
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3, "responseMimeType": "application/json"},
    }).encode()
    data = None
    for attempt in range(1, 3):
        try:
            data = get_json(url, data=body,
                            headers={"Content-Type": "application/json"}, timeout=60)
            break
        except urllib.error.HTTPError as e:
            if e.code in (503, 500, 429) and attempt < 2:
                time.sleep(5); continue
            return []
        except Exception:
            if attempt < 2:
                time.sleep(5); continue
            return []
    if data is None:
        return []
    gemini_usage_bump(today, used)
    try:
        txt = dig(data, "candidates", 0, "content", "parts", 0, "text") or "[]"
        arr = json.loads(txt)
        out = []
        for q in arr:
            q = str(q).strip()
            if q and q not in out:
                out.append(q)
        return out[:4]
    except Exception:
        return []


def gemini_rerank(conf, angle_kw, pool, few_shot=None):
    """poolを切り口適合度でリランク。各itemに ai_score(0-100)/ai_reason を付与。
       few_shot=過去の👍商品（将来用。今は空でOK）。失敗時はpoolをそのまま返す。"""
    ok, today, used = gemini_usage_ok(conf["daily_limit"])
    if not ok:
        print("    Gemini: 日次上限(%d)に到達。リランクをスキップ" % conf["daily_limit"])
        return pool, False

    lines = []
    for i, r in enumerate(pool, 1):
        amz = r["amazon"]
        lines.append("%d) %s | ブランド:%s | 価格:%s | %d源一致 | レビュー%s件"
                     % (i, (amz.get("title") or "")[:70], amz.get("brand") or "-",
                        amz.get("price") or "-", r["consensus"],
                        r["candidate"].get("review_count") or "?"))
    fewshot_txt = ""
    if few_shot:
        fewshot_txt = "\nチームの過去の判断（お手本／採用=良い・見送り=避けたい）:\n" + "\n".join("- " + s for s in few_shot)

    prompt = (
        "あなたはECの商品選定アシスタントです。以下の『切り口(検索意図)』に、各商品がどれだけ合致するかを"
        "0〜100で採点してください。\n"
        "【最重要】切り口が求める“商品カテゴリ・用途・形状”に合わないものは大きく減点（20点以下）してください。\n"
        "  - 別カテゴリの無関係な商品（例：イヤホンの切り口にキーケース/ゴミ箱/ペット用品）＝ほぼ0点。\n"
        "  - カテゴリは同じでも切り口の用途に合わない形状/仕様（例：『運動/通勤で外れない』なら、"
        "コードが邪魔で外れやすい“有線”や、固定力の弱い開放型は低評価。『高音質でじっくり』なら逆に許容）。\n"
        "ブランドの信頼性・不自然な煽り/粗悪さも加味し、価格の高安だけで過度に上下させないこと。\n"
        "出力は必ずJSON配列のみ: [{\"i\":番号,\"score\":整数,\"reason\":\"20字程度の理由\"}]\n\n"
        "切り口: " + angle_kw + fewshot_txt + "\n\n商品一覧:\n" + "\n".join(lines))

    url = ("https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent?key=%s"
           % (conf["model"], conf["api_key"]))
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }).encode()
    data = None
    for attempt in range(1, 4):                     # 503/タイムアウトは一時的なので最大3回
        try:
            data = get_json(url, data=body,
                            headers={"Content-Type": "application/json"}, timeout=40)
            break
        except urllib.error.HTTPError as e:
            body_txt = e.read().decode()[:200]
            if e.code in (503, 500, 429) and attempt < 3:
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
    gemini_usage_bump(today, used)

    try:
        txt = dig(data, "candidates", 0, "content", "parts", 0, "text") or "[]"
        scores = json.loads(txt)
        by_i = {int(s["i"]): s for s in scores if "i" in s}
    except Exception as e:
        print("    Geminiの応答解析失敗（リランクせず）:", e)
        return pool, False

    for i, r in enumerate(pool, 1):
        s = by_i.get(i, {})
        r["ai_score"] = s.get("score")
        r["ai_reason"] = s.get("reason", "")
    # 並べ替え：採用優先 → AIスコア高い → コンセンサス多い → レビュー多い
    pool.sort(key=lambda r: (VERDICT_ORDER[r["verdict"]],
                             -(r.get("ai_score") if r.get("ai_score") is not None else -1),
                             -learn_score(r["amazon"].get("brand")),
                             -r["consensus"],
                             -(r["candidate"].get("review_count") or 0)))
    return pool, True


# ============================================================
# メイン
# ============================================================
def main():
    args = [a for a in sys.argv[1:] if a.strip()]
    want_json = "--json" in args
    want_rerank = "--rerank" in args
    args = [a for a in args if a not in ("--json", "--rerank")]
    kw = " ".join(args) or "ソロ キャンプ 軽量"

    load_learning()   # ⑤ learning.json があればブランド重み＋お手本を適用（無ければ通常動作）
    app_id, access_key, cid = load_shop_conf()
    print("=" * 62)
    print("検索キーワード:", kw)
    print("=" * 62)

    # AIクエリ変換（--rerank時のみ・Gemini設定があれば）。切り口の概念語を具体的な商品語へ
    #   翻訳し、gather の最優先クエリにする（例：自転車 盗難→自転車 鍵/U字ロック）。
    gconf = load_gemini_conf() if want_rerank else None
    ai_queries = []
    if gconf:
        ai_queries = gemini_expand_queries(gconf, kw)
        if ai_queries:
            print("[0] AIクエリ変換 → " + " / ".join(ai_queries))

    # --- 1. 候補取得（AI変換クエリを最優先に、足りなければテーマ/修飾語で補完）---
    cands, tried = gather_candidates(app_id, access_key, cid, kw, extra_queries=(ai_queries or None))
    print("\n[1] 候補取得: 計 %d件（試したクエリ: %s）"
          % (len(cands), " / ".join(tried)))
    if not cands:
        emit_empty(want_json, "全クエリで候補0件（テーマ単独でも0）。ネットワーク/キーの可能性")

    # --- 2. 除外 ---
    kept, dropped, flags = [], [], {}
    for cand in cands:
        res = classify_exclusion(cand)
        if res and res[0] == "drop":
            dropped.append((cand, res[1]))
        else:
            if res and res[0] == "flag":
                flags[id(cand)] = res[1]
            kept.append(cand)
    print("\n[2] 除外: %d件を除外 / %d件が通過" % (len(dropped), len(kept)))
    for cand, why in dropped:
        print("    ✗ [%s] %s … %s" % (cand["source"], cand["name"][:36], why))
    if flags:
        print("    （弱フラグ %d件：中華製ヒント。除外はしない）" % len(flags))

    # Amazon照合は1件ずつAPIを叩くので、多いと激遅（先読みがタイムアウト）。上位のみに制限。
    #   gather順（AI変換クエリ→テーマ→修飾）のまま先頭を残すので、切り口の本命が優先される。
    if len(kept) > AMAZON_RESOLVE_MAX:
        print("    照合を上位 %d件 に制限（全 %d件中／速度確保）" % (AMAZON_RESOLVE_MAX, len(kept)))
        kept = kept[:AMAZON_RESOLVE_MAX]

    # --- 3. Amazon解決 ---
    c = amazon_conf()
    token = amazon_token(c)
    amz_direct = amazon_direct_asins(c, token, kw)
    time.sleep(SLEEP_BETWEEN_AMAZON)
    print("\n[3] Amazon解決: トークンOK。Amazon直接検索=%d ASIN（独立ソース）。%d件を searchItems で照会…"
          % (len(amz_direct), len(kept)))
    resolved = []
    for cand in kept:
        q = clean_query(cand["name"], cand.get("brand"))
        res = amazon_search(c, token, q, count=2)
        time.sleep(SLEEP_BETWEEN_AMAZON)
        if "_err" in res:
            print("    × [%s] %s → %s" % (cand["source"], cand["name"][:28], res["_err"]))
            continue
        items = dig(res, "searchResult", "items") or dig(res, "items") or []
        if not items:
            print("    ・ヒットなし: %s" % cand["name"][:34])
            continue
        amz = parse_amazon_item(items[0], c["partner_tag"])
        # backstop: Amazonタイトル側にもセット/効果効能除外を適用（候補名が単品でもAmazonが束売りのことがある）
        amz_excl = classify_exclusion({"name": amz.get("title"), "source": "amazon"})
        if amz_excl and amz_excl[0] == "drop":
            print("    ✗Amazon側除外: %s … %s"
                  % ((amz.get("title") or "")[:30], amz_excl[1]))
            continue
        verdict, reason = match_gate(cand, amz)
        resolved.append({
            "candidate": cand,
            "amazon": amz,
            "verdict": verdict,
            "reason": reason,
            "weak_flag": flags.get(id(cand)),
        })

    if not resolved:
        emit_empty(want_json, "Amazon解決が0件（候補はあったがASIN化できず）")

    # --- 4. 照合ゲート結果 ---
    print("\n[4] 照合ゲート:")
    for r in resolved:
        amz = r["amazon"]
        mark = {"採用": "◎", "要確認": "△", "保留": "▽"}[r["verdict"]]
        print("    %s %s | ASIN=%s | %s"
              % (mark, r["verdict"], amz.get("asin"), (amz.get("title") or "")[:34]))
        print("        候補[%s]=%s" % (r["candidate"]["source"], r["candidate"]["name"][:40]))
        print("        理由=%s%s"
              % (r["reason"], "  ⚠%s" % r["weak_flag"] if r["weak_flag"] else ""))

    # --- 5. 重複排除＋多源コンセンサス（同一ASINに何ソースが一致したか）---
    best, src_by_key = {}, {}
    for r in resolved:
        k = dedup_key(r)
        src_by_key.setdefault(k, set()).add(r["candidate"]["source"])  # rakuten / yahoo
        cur = best.get(k)
        if cur is None or VERDICT_ORDER[r["verdict"]] < VERDICT_ORDER[cur["verdict"]]:
            best[k] = r
    for k, r in best.items():
        src = set(src_by_key[k])
        if r["amazon"].get("asin") in amz_direct:                      # Amazon直接検索の一致
            src.add("amazon")
        r["sources"] = sorted(src)
        r["consensus"] = len(src)
    uniq = list(best.values())
    # 並び：採用優先 → コンセンサス多い → レビュー件数多い
    uniq.sort(key=lambda r: (VERDICT_ORDER[r["verdict"]],
                             -learn_score(r["amazon"].get("brand")),
                             -r["consensus"],
                             -(r["candidate"].get("review_count") or 0)))
    multi = sum(1 for r in uniq if r["consensus"] >= 2)
    print("\n[5] 重複排除＋多源コンセンサス: %d件 → %d件（うち複数ソース一致=%d件）"
          % (len(resolved), len(uniq), multi))
    if LEARN:
        _bw = LEARN.get("brand_weight") or {}
        _fs = LEARN.get("few_shot") or []
        print("    学習を適用：ブランド重み%d件・お手本%d件（承認/👍👎から）" % (len(_bw), len(_fs)))

    # --- 6.（先に）AIリランク＋関連性フィルタ：切り口に用途が合わない商品を落とす ---
    #     ※プールを TARGET_MIN 件に絞る前に、全候補(uniq)を採点し、スコアが低い（＝
    #       切り口の用途に合わない。例: 「おむつ」でヒットしたおむつゴミ箱/犬用おむつ）
    #       を除外してから選ぶ。これにより先読みでも用途違いが並ばない。
    reranked = False
    if want_rerank:
        # gconf は main 冒頭でロード済み（AIクエリ変換と共用）。二重ロードしない。
        if gconf:
            print("\n[6] AIリランク（Gemini %s）で切り口適合度を採点し用途不一致を除外…" % gconf["model"])
            _few = (LEARN.get("few_shot") if LEARN else None) or None
            uniq, reranked = gemini_rerank(gconf, kw, uniq, few_shot=_few)
            if reranked:
                REL_MIN = 45   # これ未満は「切り口に用途が合わない」とみなす除外閾値
                relevant = [r for r in uniq if (r.get("ai_score") or 0) >= REL_MIN]
                off = [r for r in uniq if (r.get("ai_score") or 0) < REL_MIN]
                if len(relevant) >= TARGET_MIN:
                    if off:
                        print("    関連性フィルタ：用途不一致 %d件を除外（スコア<%d）" % (len(off), REL_MIN))
                    uniq = relevant
                elif relevant:
                    off.sort(key=lambda r: -(r.get("ai_score") or 0))
                    need = TARGET_MIN - len(relevant)
                    print("    関連性フィルタ：関連%d件＋高スコア補充%d件（関連品が目標未満のため空にしない）"
                          % (len(relevant), need))
                    uniq = relevant + off[:need]
                # relevant が0件（全滅）なら uniq は変えず従来動作
        else:
            print("\n[6] AIリランク: gemini_api.json が無い/未設定のためスキップ")

    # --- 7a. 性別の統一：レディース/メンズが混在する切り口は多数派＋両用に揃える ---
    _before_g = len(uniq)
    uniq = gender_consistency(uniq)
    if _before_g != len(uniq):
        print("    性別統一：混在する少数派 %d件を後方へ" % (_before_g - len(uniq)))

    # --- 7b. ブランド多様性：同ブランドばかりにならないよう間引く（上位優先・空にしない） ---
    _before_div = len(uniq)
    uniq = diversify_brands(uniq)
    if _before_div != len(uniq):
        print("    ブランド多様性：同ブランド超過分を %d件 後方へ（1ブランド最大%d件）"
              % (_before_div - len(uniq), MAX_PER_BRAND))

    # --- 空にしない：目標件数に満たなければ 要確認→保留 を繰り上げ ---
    adopted = [r for r in uniq if r["verdict"] == "採用"]
    pool = adopted[:]
    if len(pool) < TARGET_MIN:
        for v in ("要確認", "保留"):
            for r in uniq:
                if r["verdict"] == v and r not in pool:
                    pool.append(r)
                    if len(pool) >= TARGET_MIN:
                        break
            if len(pool) >= TARGET_MIN:
                break

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
    print("◎採用=ブランド/JAN一致で確度高い / △要確認=型番やブランド語の弱一致 / ▽保留=一致なし。")
    print("N源一致=同一ASINに何ソース(楽天/Yahoo/Amazon)が一致したか。多いほど信頼できる。")
    print("並び順=採用優先→コンセンサス多い→レビュー件数多い。★足切りは⑤(Edge)で追加予定。")


if __name__ == "__main__":
    main()
