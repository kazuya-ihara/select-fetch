#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""品質レポートと候補なし理由の回帰テスト（外部API・DBを呼ばない）。"""
import json
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import product_fetch
import quality_report


class QualityReportTest(unittest.TestCase):
    def test_render_separates_empty_reason_breakdown(self):
        events = [
            {"status": "empty", "theme": "A", "angle": "a",
             "reason": "検索結果が0件（Amazon関連性・評価・救済）", "scores": []},
            {"status": "empty", "theme": "B", "angle": "b",
             "reason": "表示前の安全フィルターで除外", "scores": []},
            {"status": "quality_hold", "theme": "C", "angle": "c",
             "reason": "適格品2件（0〜2件は再試行対象）", "scores": [90, 85]},
        ]
        report = quality_report.render(events, generated_at="test")
        self.assertIn("## 候補なしの内訳", report)
        self.assertIn("1件: 検索結果が0件（Amazon関連性・評価・救済）", report)
        self.assertIn("1件: 表示前の安全フィルターで除外", report)

    def test_build_pool_classifies_search_empty_without_api(self):
        completed = types.SimpleNamespace(
            returncode=0,
            stdout=("QUALITY_REASON:search_empty\n"
                    "---- JSON ----\n[]\n==== 見方 ====\n"),
            stderr="",
        )
        with mock.patch.object(product_fetch.subprocess, "run", return_value=completed):
            pool, reason = product_fetch.build_pool(
                "枕 高さ", True, theme="枕", angle="高さで選ぶ",
                return_reason=True)
        self.assertEqual([], pool)
        self.assertEqual("search_empty", reason)

    def test_build_pool_classifies_safety_filter_after_candidates(self):
        completed = types.SimpleNamespace(
            returncode=0,
            stdout=("---- JSON ----\n" + json.dumps([
                {"asin": "A", "title": "人用 冷感マット"}
            ], ensure_ascii=False) + "\n==== 見方 ====\n"),
            stderr="",
        )
        with mock.patch.object(product_fetch.subprocess, "run", return_value=completed), \
             mock.patch.object(product_fetch, "apply_intent_category_evidence", return_value=[]):
            pool, reason = product_fetch.build_pool(
                "ペット 冷感", True, theme="ペット冷感グッズ",
                angle="ひんやりで選ぶ", return_reason=True)
        self.assertEqual([], pool)
        self.assertEqual("safety_filtered", reason)


if __name__ == "__main__":
    unittest.main()
