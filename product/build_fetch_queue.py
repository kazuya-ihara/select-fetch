#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
係② 先読み取得キュー生成（Mac実行・標準ライブラリのみ）

今日のカタログ（data/catalog/latest.json、run.py が生成）から、
「上位テーマ × backed(裏付けあり)の切り口」だけを取り出して
product/fetch_queue.json を作る。これを product_fetch.py --queue が読む。

毎朝バッチの並び：run.py(カタログ) → build_fetch_queue.py → learn.py → product_fetch.py

上限（上部で調整可）：
  TOP_THEMES … 取得対象にする上位テーマ数（ピン留め優先→スコア順）
  MAX_ANGLES … キュー全体の最大切り口数（product_fetch の日次上限より少なめに）
  PER_THEME  … 1テーマあたりの最大切り口数
"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import product_fetch as pf   # rpc / read_batch_token を再利用（Supabaseから今日のカタログを読む）

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CATALOG = os.path.join(ROOT, "data", "catalog", "latest.json")
OUT = os.path.join(HERE, "fetch_queue.json")

TOP_THEMES = int(os.environ.get("TOP_THEMES", "80"))    # 実質すべてのテーマ
MAX_ANGLES = int(os.environ.get("MAX_ANGLES", "300"))   # キュー全体の上限
PER_THEME  = int(os.environ.get("PER_THEME", "4"))


def theme_sort_key(t):
    # ピン留め優先 → スコア高い順
    return (0 if t.get("pin") else 1, -(t.get("score") or 0))


def load_catalog():
    """(date, payload) を返す。まず Supabase の最新カタログ、ダメならローカル。
    ※クラウド(GitHub Actions)ではリポジトリ内の latest.json が古いので、必ずSupabaseを優先する。"""
    try:
        token = pf.read_batch_token()
        if token:
            c = pf.rpc("v2_catalog_pull", {"p_secret": token})
            if c and c.get("payload"):
                print("  カタログ: Supabase の最新（%s）を使用" % c.get("date"))
                return c.get("date"), c["payload"]
    except Exception as e:
        print("  カタログ(Supabase)取得に失敗、ローカルを試す:", type(e).__name__, e)
    if os.path.exists(CATALOG):
        d = json.load(open(CATALOG, encoding="utf-8"))
        print("  カタログ: ローカル latest.json（%s）を使用" % d.get("date"))
        return d.get("date"), d
    return None, None


def main():
    date, d = load_catalog()
    if not d:
        print("‼ カタログが取得できません（先に run.py / catalog.yml を実行）"); return

    themes = sorted(d.get("THEMES") or [], key=theme_sort_key)
    ANGLE = d.get("ANGLE_DATA") or {}
    queue, seen = [], set()
    for t in themes[:TOP_THEMES]:
        name = t.get("t")
        angles = ((ANGLE.get(name) or {}).get("angles")) or []
        picked = 0
        for a in angles:
            st = a.get("st")
            verdict = a.get("verdict")
            # backed（裏付けあり）と、gray でも救済(save)されたものだけ。out は除外。
            if st == "out":
                continue
            if not (st == "backed" or (st == "gray" and verdict == "save")):
                continue
            kw = a.get("kw"); title = a.get("t")
            if not kw or not title:
                continue
            key = (name, title)
            if key in seen:
                continue
            seen.add(key)
            queue.append({"theme": name, "angle_title": title, "kw": kw})
            picked += 1
            if picked >= PER_THEME or len(queue) >= MAX_ANGLES:
                break
        if len(queue) >= MAX_ANGLES:
            break

    json.dump(queue, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("→ 保存: product/fetch_queue.json（%d切り口 / 上位%dテーマ / カタログ日=%s）"
          % (len(queue), min(TOP_THEMES, len(themes)), date))
    for q in queue[:12]:
        print("  ・%s / %s（kw=%s）" % (q["theme"], q["angle_title"], q["kw"]))
    if len(queue) > 12:
        print("  …ほか%d件" % (len(queue) - 12))


if __name__ == "__main__":
    main()
