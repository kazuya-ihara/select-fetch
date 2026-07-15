#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
係② ⑤バックテスト（Mac実行・標準ライブラリのみ）

既知の切り口（backtest_set.json）を実際に build_candidates.py で流し、
結果を「採点シート」HTMLに書き出す。人が各商品を ◯適合/△/✗不適合 で採点すると、
その場で 精度@5・精度@8・並びの良さ を自動計算する（サーバ不要・ブラウザで開くだけ）。

使い方:
  python3 product/backtest.py            # セットを流して採点シートを作成
  python3 product/backtest.py --rerank   # AIリランクも掛けた状態で採点

出力: product/採点シート.html （ダブルクリックで開いて採点）
狙い: 学習/シード/並びの重みが妥当かを人の目で校正する。
"""
import os, sys, json, html

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import product_fetch as pf   # build_pool を再利用（build_candidates を --json で呼ぶ）

SET_FILE = os.path.join(HERE, "backtest_set.json")
OUT = os.path.join(HERE, "採点シート.html")


def esc(s):
    return html.escape("" if s is None else str(s))


def yen(p):
    if not p:
        return "—"
    d = "".join(ch for ch in str(p) if ch.isdigit())
    return "￥{:,}".format(int(d)) if d else "—"


def collect(rerank):
    try:
        st = json.load(open(SET_FILE, encoding="utf-8"))
    except Exception as e:
        print("‼ backtest_set.json 読込失敗:", e); return None
    data = []
    for entry in st:
        kw = entry.get("kw")
        label = entry.get("label") or kw
        if not kw:
            continue
        print("▶ バックテスト:", label, "（kw=%s, rerank=%s）" % (kw, rerank))
        pool = pf.build_pool(kw, rerank) or []
        items = []
        for i, it in enumerate(pool, 1):
            items.append({
                "rank": i, "asin": it.get("asin"), "title": it.get("title"),
                "brand": it.get("brand"), "price": yen(it.get("price")),
                "verdict": it.get("verdict"), "consensus": it.get("consensus"),
                "sources": "/".join(it.get("sources") or []),
                "ai_score": it.get("ai_score"), "ai_reason": it.get("ai_reason"),
                "review_count": it.get("review_count"), "url": it.get("url"),
                "image_url": it.get("image_url"),
            })
        data.append({"label": label, "kw": kw, "items": items})
    return data


PAGE = """<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Select. 採点シート（バックテスト）</title>
<style>
 :root{--bg:#f5f7fb;--panel:#fff;--ink:#1a2236;--soft:#5b6577;--faint:#98a1b3;--line:#e4e9f2;
   --blue:#3b6fe0;--green:#1f9d6b;--green-s:#e6f6ee;--amber:#c9871f;--amber-s:#fbf2dd;--red:#d24a52;--red-s:#fbe9ea;--purple:#7b5bd6;--purple-s:#efeafb;--teal:#0f9c8f;--teal-s:#e2f5f3;--gray-s:#f0f2f6;
   --shadow:0 1px 2px rgba(26,34,54,.04),0 8px 24px rgba(26,34,54,.06)}
 *{box-sizing:border-box;margin:0;padding:0}
 body{font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans","Segoe UI",sans-serif;background:var(--bg);color:var(--ink);line-height:1.5;padding:24px 18px 60px}
 .wrap{max-width:1000px;margin:0 auto}
 h1{font-size:21px;font-weight:700}
 .lead{color:var(--soft);font-size:13px;margin:6px 0 14px;max-width:760px}
 .sec{margin:22px 0}
 .sh{display:flex;align-items:center;gap:10px;font-size:16px;font-weight:700;margin-bottom:4px}
 .sh .kw{font-size:11px;color:var(--faint);font-weight:600}
 .metrics{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0 12px}
 .m{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:8px 13px;font-size:12px;color:var(--soft);box-shadow:var(--shadow)}
 .m b{font-size:17px;color:var(--ink);margin-left:4px}
 .m.good b{color:var(--green)} .m.warn b{color:var(--amber)} .m.bad b{color:var(--red)}
 .row{display:flex;gap:11px;align-items:center;border:1px solid var(--line);border-radius:11px;padding:10px 12px;background:var(--panel);box-shadow:var(--shadow);margin-bottom:8px}
 .rk{font-size:12px;font-weight:700;color:var(--faint);width:26px;text-align:center;flex-shrink:0}
 .thumb{width:52px;height:52px;border-radius:8px;border:1px solid var(--line);background:#fbfcfe center/contain no-repeat;flex-shrink:0}
 .thumb img{width:100%;height:100%;object-fit:contain}
 .info{flex:1;min-width:0}
 .tt{font-size:13px;font-weight:700;line-height:1.3}
 .tt a{color:var(--ink);text-decoration:none}.tt a:hover{color:var(--blue);text-decoration:underline}
 .meta{font-size:11px;color:var(--soft);margin-top:3px;display:flex;gap:6px;flex-wrap:wrap;align-items:center}
 .b{font-size:9px;font-weight:700;padding:1px 6px;border-radius:100px}
 .b.ad{background:var(--green-s);color:var(--green)}.b.ck{background:var(--amber-s);color:var(--amber)}.b.ho{background:var(--gray-s);color:var(--soft)}
 .b.sr{background:var(--teal-s);color:var(--teal)}.b.ai{background:var(--purple-s);color:var(--purple)}
 .score{display:flex;gap:5px;flex-shrink:0}
 .score label{font-size:12px;font-weight:700;padding:6px 10px;border-radius:9px;border:1px solid var(--line);cursor:pointer;color:var(--soft);user-select:none}
 .score input{display:none}
 .score label.o.on{background:var(--green-s);color:var(--green);border-color:#c7e8d6}
 .score label.t.on{background:var(--amber-s);color:var(--amber);border-color:#f0dcae}
 .score label.x.on{background:var(--red-s);color:var(--red);border-color:#f2c9cc}
 .bar{position:sticky;top:0;z-index:5;background:var(--bg);padding:10px 0;display:flex;gap:10px;align-items:center;flex-wrap:wrap;border-bottom:1px solid var(--line);margin-bottom:8px}
 .bar .big{font-size:13px;font-weight:700}
 .bar button{background:var(--blue);color:#fff;border:none;border-radius:9px;padding:8px 16px;font-size:13px;font-weight:700;cursor:pointer}
 .hint{font-size:11px;color:var(--faint)}
</style></head><body><div class="wrap">
<div class="bar"><span class="big">採点シート（バックテスト）</span><button id="copyBtn">採点結果をコピー</button><span class="hint">◯=この切り口に合う / △=どちらとも / ✗=合わない。上位ほど◯が多いほど良い並び。</span></div>
<div class="lead">各切り口の並び順（上=システムの上位）に対し、人の目で◯△✗を付けてください。精度@5/@8と「並びの良さ」がその場で計算されます。数値が低い切り口は、learning_seed.json の重みや除外辞書を見直す材料になります。</div>
<div id="app"></div>
</div>
<script>
const DATA = __DATA__;
const V = {}; // key -> score(1/0.5/0)
function concordance(items){ // 上位ほど良いか（0-100%）。scoreの付いたペアのみ
  const s = items.map(it=>V[it.key]); let good=0,tot=0;
  for(let i=0;i<items.length;i++)for(let j=i+1;j<items.length;j++){
    if(s[i]==null||s[j]==null) continue; if(s[i]===s[j]) continue; tot++;
    if(s[i] > s[j]) good++; // 上位(i)の方が高評価なら正しい並び
  }
  return tot? Math.round(good/tot*100):null;
}
function prec(items,n){ const t=items.slice(0,n); const sc=t.map(it=>V[it.key]).filter(x=>x!=null);
  if(!sc.length) return null; return Math.round(sc.reduce((a,b)=>a+b,0)/t.length*100); }
function render(){
  const app=document.getElementById('app'); app.innerHTML='';
  DATA.forEach((sec,si)=>{
    const box=document.createElement('div'); box.className='sec';
    const p5=prec(sec.items,5), p8=prec(sec.items,8), co=concordance(sec.items);
    const cls=v=> v==null?'':(v>=70?'good':(v>=40?'warn':'bad'));
    box.innerHTML=`<div class="sh">${si+1}. ${sec.label} <span class="kw">kw=${sec.kw} ・ ${sec.items.length}件</span></div>
      <div class="metrics">
        <span class="m ${cls(p5)}">精度@5<b>${p5==null?'—':p5+'%'}</b></span>
        <span class="m ${cls(p8)}">精度@8<b>${p8==null?'—':p8+'%'}</b></span>
        <span class="m ${cls(co)}">並びの良さ<b>${co==null?'—':co+'%'}</b></span>
      </div>`;
    sec.items.forEach(it=>{
      const vb = it.verdict==='採用'?'<span class="b ad">採用</span>':it.verdict==='要確認'?'<span class="b ck">要確認</span>':'<span class="b ho">保留</span>';
      const sr = it.consensus?`<span class="b sr">${it.consensus}源</span>`:'';
      const ai = (it.ai_score!=null)?`<span class="b ai">AI ${it.ai_score}</span>`:'';
      const cur = V[it.key];
      const row=document.createElement('div'); row.className='row';
      row.innerHTML=`<div class="rk">#${it.rank}</div>
        <div class="thumb">${it.image_url?`<img src="${it.image_url}">`:''}</div>
        <div class="info">
          <div class="tt"><a href="${it.url}" target="_blank" rel="noopener">${(it.title||'').replace(/</g,'&lt;')}</a></div>
          <div class="meta"><b>${it.price}</b> ${it.brand?('・'+it.brand):''} ${vb} ${sr} ${ai} ${it.review_count!=null?('・レビュー'+it.review_count):''}</div>
          ${it.ai_reason?`<div class="meta" style="color:var(--faint)">AI: ${(it.ai_reason||'').replace(/</g,'&lt;')}</div>`:''}
        </div>
        <div class="score">
          <label class="o ${cur===1?'on':''}"><input type="radio" name="${it.key}" data-v="1">◯</label>
          <label class="t ${cur===0.5?'on':''}"><input type="radio" name="${it.key}" data-v="0.5">△</label>
          <label class="x ${cur===0?'on':''}"><input type="radio" name="${it.key}" data-v="0">✗</label>
        </div>`;
      row.querySelectorAll('input').forEach(inp=> inp.onchange=()=>{ V[it.key]=parseFloat(inp.dataset.v); render(); });
      box.appendChild(row);
    });
    app.appendChild(box);
  });
}
document.getElementById('copyBtn').onclick=()=>{
  const out=DATA.map(s=>({kw:s.kw, scores:s.items.map(it=>({rank:it.rank,asin:it.asin,brand:it.brand,score:V[it.key]==null?null:V[it.key]}))}));
  navigator.clipboard.writeText(JSON.stringify(out,null,2)).then(()=>alert('採点結果をコピーしました。チャットに貼れば校正します。'));
};
DATA.forEach((s,si)=> s.items.forEach(it=> it.key='s'+si+'_'+it.rank));
render();
</script></body></html>"""


def main():
    rerank = "--rerank" in sys.argv[1:]
    data = collect(rerank)
    if data is None:
        return
    payload = json.dumps(data, ensure_ascii=False)
    open(OUT, "w", encoding="utf-8").write(PAGE.replace("__DATA__", payload))
    n_items = sum(len(s["items"]) for s in data)
    print("→ 保存: product/採点シート.html（%d切り口 / 計%d件）" % (len(data), n_items))
    print("  ダブルクリックで開いて ◯△✗ を採点 → 精度@5/@8・並びの良さが自動計算されます。")
    print("  『採点結果をコピー』でJSONをコピーし、チャットに貼れば重みを校正します。")


if __name__ == "__main__":
    main()
