#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外部APIを使わず、商品選抜の品質契約を固定する回帰テスト。"""
import json
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_candidates as bc
import fetch_claimed
import product_fetch


def make_row(score, brand, verdict="採用", title=None, n=0):
    return {
        "ai_score": score,
        "verdict": verdict,
        "consensus": 1,
        "amazon": {
            "asin": "ASIN%03d" % n,
            "brand": brand,
            "title": title or "商品%d" % n,
        },
        "candidate": {"review_count": 100 - n},
    }


class SelectionPolicyTest(unittest.TestCase):
    def setUp(self):
        bc.LEARN = None

    def test_low_scores_are_never_used_as_fillers(self):
        rows = [make_row(95, "A", n=1), make_row(44, "B", n=2), make_row(0, "C", n=3)]
        out = bc.select_final_pool(rows)
        self.assertEqual([95], [r["ai_score"] for r in out])

    def test_all_rejected_returns_empty(self):
        rows = [make_row(20, "A", n=1), make_row(5, "B", n=2)]
        self.assertEqual([], bc.select_final_pool(rows))

    def test_ai_score_ranks_before_verdict(self):
        rows = [
            make_row(60, "A", verdict="採用", n=1),
            make_row(90, "B", verdict="保留", n=2),
        ]
        out = bc.select_final_pool(rows)
        self.assertEqual([90, 60], [r["ai_score"] for r in out])

    def test_brand_limit_is_strict_even_when_list_is_short(self):
        rows = [make_row(95 - i, "独占ブランド", n=i) for i in range(5)]
        rows.append(make_row(70, "別ブランド", n=9))
        out = bc.select_final_pool(rows)
        brands = [r["amazon"]["brand"] for r in out]
        self.assertEqual(2, brands.count("独占ブランド"))
        self.assertEqual(3, len(out))

    def test_target_is_maximum_not_minimum(self):
        rows = [make_row(90 - i, "B%d" % i, n=i) for i in range(12)]
        self.assertEqual(9, len(bc.select_final_pool(rows)))

    def test_same_search_kw_different_angles_have_different_cache_keys(self):
        a = bc.build_intent_spec(
            "BBQ用品", "初心者の「煙」に応えるBBQ用品", "BBQ用品 初心者",
            [["nayami", "煙"], ["attr", "初心者"]])
        b = bc.build_intent_spec(
            "BBQ用品", "初心者向けのこだわりBBQ用品", "BBQ用品 初心者",
            [["attr", "初心者"]])
        self.assertNotEqual(a["intent_key"], b["intent_key"])
        self.assertIn("最優先の悩み・目的: 煙", a["intent_text"])

    def test_resolve_slots_are_spread_across_query_and_source(self):
        rows = []
        for i in range(8):
            rows.append({
                "source": "rakuten", "name": "候補A%d" % i, "brand": "ブランドA",
                "_search_query": "検索A",
            })
        for i in range(8, 16):
            rows.append({
                "source": "yahoo", "name": "候補B%d" % i, "brand": "ブランドB",
                "_search_query": "検索B",
            })
        out = bc.select_resolve_candidates(rows, limit=6)
        self.assertEqual({"検索A", "検索B"}, {r["_search_query"] for r in out})
        self.assertEqual({"検索A": 3, "検索B": 3}, {
            q: sum(1 for r in out if r["_search_query"] == q) for q in ("検索A", "検索B")
        })


class IntentTransportTest(unittest.TestCase):
    def test_claimed_catalog_map_keeps_components(self):
        payload = {"ANGLE_DATA": {"自転車": {"angles": [{
            "t": "初心者の「パンク」に応える自転車",
            "kw": "自転車 初心者",
            "c": [["nayami", "パンク"], ["attr", "初心者"]],
        }]}}}
        spec = fetch_claimed.angle_map(payload)[("自転車", "初心者の「パンク」に応える自転車")]
        self.assertEqual("パンク", spec["components"][0][1])

    def test_product_fetch_passes_full_intent_to_child_process(self):
        completed = types.SimpleNamespace(
            returncode=0,
            stdout="---- JSON ----\n[]\n==== 見方 ====\n",
            stderr="",
        )
        components = [["nayami", "パンク"]]
        with mock.patch.object(product_fetch.subprocess, "run", return_value=completed) as run:
            result = product_fetch.build_pool(
                "自転車 初心者", True, theme="自転車",
                angle="初心者の「パンク」に応える自転車", components=components)
        self.assertEqual([], result)
        cmd = run.call_args.args[0]
        self.assertIn("--intent", cmd)
        self.assertIn("初心者の「パンク」に応える自転車", cmd)
        encoded = cmd[cmd.index("--components-json") + 1]
        self.assertEqual(components, json.loads(encoded))


if __name__ == "__main__":
    unittest.main()
