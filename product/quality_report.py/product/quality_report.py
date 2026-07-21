#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""商品取得の品質イベントを、GitHub Actionsで読める短いレポートにする。

イベントには商品名やAPIキーを入れず、切り口・結果・AI点数・理由だけを記録する。
検索やAIの呼び出しは行わないため、無料枠を追加消費しない。
"""
import argparse
import collections
import datetime
import json
import os
from typing import Iterable


STATUS_LABELS = {
    "saved": "保存",
    "promoted": "品質昇格",
    "shadow": "影テスト",
    "skip": "既存保持/スキップ",
    "quality_hold": "品質保留",
    "empty": "候補なし",
    "error": "エラー",
    "limit": "上限停止",
    "nokw": "検索語不明",
}


def _now_jst():
    return datetime.datetime.now(datetime.timezone.utc).astimezone(
        datetime.timezone(datetime.timedelta(hours=9)))


def _safe_text(value, limit=160):
    """Markdownを壊さず、ログが巨大にならないようにする。"""
    text = str(value or "").replace("\n", " ").replace("\r", " ").replace("|", "／")
    return text[:limit]


def record_event(path, status, catalog_date, theme, angle, scores=None,
                 reason="", mode=""):
    """取得処理から1行JSONを追記する。レポート失敗で取得処理を止めない。"""
    if not path:
        return
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        clean_scores = []
        for score in (scores or []):
            if isinstance(score, (int, float)) and not isinstance(score, bool):
                clean_scores.append(score)
        event = {
            "catalog_date": catalog_date or "",
            "theme": theme or "",
            "angle": angle or "",
            "status": status,
            "scores": clean_scores,
            "reason": _safe_text(reason, 240),
            "mode": mode or "",
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        # 品質レポートは補助機能。API取得・DB保存を巻き戻したり止めたりしない。
        return


def load_events(path) -> list[dict]:
    events = []
    if not path or not os.path.exists(path):
        return events
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                if isinstance(item, dict) and item.get("status"):
                    item["scores"] = [s for s in (item.get("scores") or [])
                                       if isinstance(s, (int, float)) and not isinstance(s, bool)]
                    events.append(item)
    except Exception:
        return []
    return events


def _score_bucket(score):
    if score >= 90:
        return "90点以上"
    if score >= 80:
        return "80〜89点"
    if score >= 70:
        return "70〜79点"
    return "70点未満"


def render(events: Iterable[dict], generated_at=None) -> str:
    events = list(events)
    counts = collections.Counter(e.get("status") for e in events)
    scores = [score for e in events for score in (e.get("scores") or [])]
    buckets = collections.Counter(_score_bucket(float(score)) for score in scores)
    reasons = collections.Counter(
        _safe_text(e.get("reason")) for e in events
        if e.get("status") in {"quality_hold", "empty", "error", "nokw", "limit", "shadow"}
        and e.get("reason")
        and e.get("reason") not in {"保存完了", "品質昇格", "正式反映条件を通過"}
    )
    when = generated_at or _now_jst().strftime("%Y-%m-%d %H:%M JST")

    lines = [
        "# 商品取得 品質レポート",
        "",
        "生成時刻: %s" % when,
        "",
        "## 全体",
        "",
        "- 処理した切り口: **%d件**" % len(events),
        "- 保存: **%d件**" % counts["saved"],
        "- 品質昇格: **%d件**" % counts["promoted"],
        "- 影テスト: **%d件**" % counts["shadow"],
        "- 既存保持/スキップ: **%d件**" % counts["skip"],
        "- 品質保留: **%d件**" % counts["quality_hold"],
        "- 候補なし: **%d件**" % counts["empty"],
        "- エラー: **%d件**" % counts["error"],
        "- 上限停止: **%d件**" % counts["limit"],
        "",
        "## AIスコア分布",
        "",
        "- 90点以上: **%d件**" % buckets["90点以上"],
        "- 80〜89点: **%d件**" % buckets["80〜89点"],
        "- 70〜79点: **%d件**" % buckets["70〜79点"],
        "- 70点未満: **%d件**" % buckets["70点未満"],
    ]
    if scores:
        lines.append("- AIスコア平均: **%.1f点**（%d商品）" %
                     (sum(scores) / len(scores), len(scores)))
    else:
        lines.append("- AIスコア: 今回は記録された商品がありません")

    if reasons:
        lines += ["", "## 保留・失敗の主な理由", ""]
        for reason, count in reasons.most_common(12):
            lines.append("- %d件: %s" % (count, reason))

    lines += ["", "## 切り口別", "", "| テーマ | 切り口 | 結果 | 商品数 | AI点数 | 理由 |",
              "|---|---|---:|---:|---|---|"]
    if events:
        for event in events:
            score_text = ", ".join(str(s) for s in (event.get("scores") or [])) or "—"
            lines.append("| %s | %s | %s | %d | %s | %s |" % (
                _safe_text(event.get("theme")),
                _safe_text(event.get("angle")),
                STATUS_LABELS.get(event.get("status"), event.get("status")),
                len(event.get("scores") or []),
                _safe_text(score_text, 90),
                _safe_text(event.get("reason"), 140),
            ))
    else:
        lines.append("| — | — | イベントなし | 0 | — | 取得処理が開始されなかった可能性があります |")
    lines += ["", "※このレポートは取得処理の記録だけを集計しています。APIキーやトークンは含みません。"]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description="商品取得イベントから品質レポートを作る")
    ap.add_argument("--events", default=os.environ.get("QUALITY_REPORT_PATH", "product/quality_events.jsonl"))
    ap.add_argument("--out", default="product/quality_report.md")
    args = ap.parse_args()
    report = render(load_events(args.events))
    parent = os.path.dirname(os.path.abspath(args.out))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(report)
    print(report, end="")


if __name__ == "__main__":
    main()
