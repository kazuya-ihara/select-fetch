#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
係② ⑤学習バッチ（Mac実行・標準ライブラリのみ）

チームのフィードバックを集計して product/learning.json を作る。
build_candidates.py がそれを読み、①並びのブランド重み付け と ②Geminiリランクのお手本(few-shot) に反映する。

集計元（Supabaseの v2_learn_pull RPC 経由・run.py と同じ 'batch' トークンで読取）:
  - v2_product_pick : 承認 / 申請 / 差し戻し（＝チームが実際に選んだ/見送った）
  - v2_product_vote : 👍 / 👎（軽い印象メモ）

使い方:
  python3 product/learn.py            # 直近120日を集計して learning.json を更新
学習を効かせて候補生成:
  python3 product/build_candidates.py "枕 高さ調整" --rerank   # learning.json があれば自動適用

重み（初期値・このファイル上部で調整可）。承認が一番強い正例、差し戻し/👎は負例。
"""
import os, re, json, datetime
import urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SOURCES = os.path.join(ROOT, "sources")
OUT = os.path.join(HERE, "learning.json")

SB_URL = os.environ.get("SUPABASE_URL") or ""
SB_PUBLISHABLE = "sb_publishable_hbtP3WrNCJp0BUuBrDs4Ww_6x79K4uc"

# ---- 重み（調整可） ----
W_APPROVED = 3.0    # 承認された（最も強い正例）
W_APPLIED  = 1.0    # 申請された（承認待ち）
W_REJECTED = -2.0   # 差し戻し（負例）
W_UP       = 1.0    # 👍
W_DOWN     = -1.5   # 👎
DAYS = 120
MAX_FEWSHOT_EACH = 8   # お手本の良い例／避けたい例 それぞれの最大件数


def norm_brand(b):
    """build_candidates.py と同じ正規化。括弧内の読みを除去し、空白除去・小文字化。"""
    if not b:
        return ""
    b = re.sub(r"[（(].*?[）)]", "", str(b))
    return re.sub(r"\s+", "", b).lower()


def read_token():
    kp = os.path.join(SOURCES, "service_key.txt")
    if os.path.exists(kp):
        for line in open(kp, encoding="utf-8"):
            s = line.strip()
            if s and not s.startswith("#") and s != "ここにキーを貼る":
                return s
    return None


def rpc(name, payload, timeout=30):
    req = urllib.request.Request(SB_URL + "/rest/v1/rpc/" + name,
        data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"apikey": SB_PUBLISHABLE, "Authorization": "Bearer " + SB_PUBLISHABLE,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        t = r.read().decode("utf-8")
        return json.loads(t) if t.strip() else None


def main():
    token = read_token()
    if not token:
        print("‼ トークン未設定（sources/service_key.txt）。run.pyと同じ'batch'トークンを置く。")
        return
    try:
        rows = rpc("v2_learn_pull", {"p_secret": token, "p_days": DAYS}) or []
    except urllib.error.HTTPError as e:
        print("取得失敗 HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:200])); return
    except Exception as e:
        print("取得失敗: %s: %s" % (type(e).__name__, e)); return

    print("フィードバック %d件を取得（直近%d日）" % (len(rows), DAYS))

    brand = {}
    stats = {"records": len(rows), "approved": 0, "applied": 0, "rejected": 0, "up": 0, "down": 0}
    pos, neg = [], []
    for r in rows:
        b = norm_brand(r.get("brand"))
        st = r.get("status")
        up = int(r.get("up") or 0)
        dn = int(r.get("down") or 0)
        stats["up"] += up; stats["down"] += dn
        s = 0.0
        if st == "承認":   s += W_APPROVED; stats["approved"] += 1
        elif st == "申請": s += W_APPLIED;  stats["applied"]  += 1
        elif st == "差し戻し": s += W_REJECTED; stats["rejected"] += 1
        s += up * W_UP + dn * W_DOWN
        if b:
            brand[b] = round(brand.get(b, 0.0) + s, 2)
        title = (r.get("title") or "")[:44]
        ang = r.get("angle_title") or r.get("theme") or ""
        good = (st == "承認") or (up - dn) >= 2
        bad  = (st == "差し戻し") or (dn - up) >= 2
        if good and title:
            pos.append((r.get("catalog_date", ""), "採用: 『%s』（切り口: %s）" % (title, ang)))
        elif bad and title:
            neg.append((r.get("catalog_date", ""), "見送り: 『%s』" % title))

    brand = {k: v for k, v in brand.items() if abs(v) >= 0.5}   # 意味のある重みだけ残す
    pos.sort(reverse=True); neg.sort(reverse=True)
    few = [t for _, t in pos[:MAX_FEWSHOT_EACH]] + [t for _, t in neg[:MAX_FEWSHOT_EACH]]
    stats["brands"] = len(brand)

    out = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "days": DAYS,
        "weights": {"approved": W_APPROVED, "applied": W_APPLIED, "rejected": W_REJECTED,
                    "up": W_UP, "down": W_DOWN},
        "stats": stats,
        "brand_weight": brand,   # 正規化ブランド名 → スコア（+で優遇 / −で抑制）
        "few_shot": few,         # Geminiに渡すお手本（採用=良い / 見送り=避けたい）
    }
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("→ 保存: product/learning.json")
    print("  承認%d / 申請%d / 差戻%d / 👍%d / 👎%d ｜ ブランド重み%d件 ｜ お手本%d件"
          % (stats["approved"], stats["applied"], stats["rejected"], stats["up"], stats["down"],
             len(brand), len(few)))
    if brand:
        top = sorted(brand.items(), key=lambda x: -x[1])[:5]
        print("  ↑優遇:", ", ".join("%s(%+.1f)" % (k, v) for k, v in top))
        bot = [(k, v) for k, v in sorted(brand.items(), key=lambda x: x[1])[:3] if v < 0]
        if bot:
            print("  ↓抑制:", ", ".join("%s(%+.1f)" % (k, v) for k, v in bot))
    if not rows:
        print("  ※まだフィードバックが少ないです。承認や👍👎が貯まるほど効きます。")


if __name__ == "__main__":
    main()
