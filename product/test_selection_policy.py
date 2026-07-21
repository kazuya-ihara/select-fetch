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

    def test_borderline_score_is_not_displayed(self):
        rows = [make_row(60, "A", n=1), make_row(70, "B", n=2)]
        self.assertEqual([70], [r["ai_score"] for r in bc.select_final_pool(rows)])

    def test_ai_score_ranks_before_verdict(self):
        rows = [
            make_row(70, "A", verdict="採用", n=1),
            make_row(90, "B", verdict="保留", n=2),
        ]
        out = bc.select_final_pool(rows)
        self.assertEqual([90, 70], [r["ai_score"] for r in out])

    def test_brand_limit_is_strict_even_when_list_is_short(self):
        rows = [make_row(95 - i, "独占ブランド", n=i) for i in range(5)]
        rows.append(make_row(70, "別ブランド", n=9))
        out = bc.select_final_pool(rows)
        brands = [r["amazon"]["brand"] for r in out]
        self.assertEqual(2, brands.count("独占ブランド"))
        self.assertEqual(3, len(out))

    def test_target_is_maximum_not_minimum(self):
        rows = [make_row(90 - i, "B%d" % i, n=i) for i in range(12)]
        self.assertEqual(6, len(bc.select_final_pool(rows)))

    def test_bulk_quantity_without_set_word_is_excluded(self):
        for title in ("丸うちわ 白 無地 100枚 うちわ", "竹製うちわ 10本組"):
            result = bc.classify_exclusion({"name": title, "source": "amazon"})
            self.assertIsNotNone(result)
            self.assertEqual("drop", result[0])

    def test_functional_retail_set_is_allowed(self):
        for title in ("科学実験セット", "浴衣3点セット", "パンク修理キット"):
            self.assertIsNone(bc.classify_exclusion({"name": title, "source": "amazon"}))

    def test_lightweight_axis_requires_explicit_evidence(self):
        unsupported = make_row(90, "A", title="21V 電動ドライバー コンパクト", n=1)
        supported = make_row(80, "B", title="軽量 電動ドライバー", n=2)
        out = bc.select_final_pool(
            [unsupported, supported], components=[["buy", "軽い"]])
        self.assertEqual([supported], out)

    def test_uchiwa_axis_rejects_handheld_fan(self):
        fan = make_row(90, "A", title="携帯扇風機 ハンディファン", n=1)
        uchiwa = make_row(80, "B", title="竹製うちわ", n=2)
        out = bc.select_final_pool([fan, uchiwa], components=[["buy", "うちわ"]])
        self.assertEqual([uchiwa], out)

    def test_amazon_review_lane_breaks_ai_score_tie(self):
        relevance = make_row(90, "A", n=1)
        reviews = make_row(90, "B", n=2)
        relevance["candidate"]["relevance_rank"] = 1
        reviews["candidate"]["reviews_rank"] = 5
        out = bc.select_final_pool([relevance, reviews])
        self.assertEqual([reviews, relevance], out)

    def test_product_type_diversity_is_used_before_overflow(self):
        rows = [make_row(95 - i, "B%d" % i, n=i) for i in range(5)]
        for row, product_type in zip(rows, ["テーブル", "テーブル", "テーブル", "チェア", "タープ"]):
            row["product_type"] = product_type
        out = bc.select_final_pool(rows, target_max=5)
        self.assertEqual(["テーブル", "テーブル", "チェア", "タープ"],
                         [r["product_type"] for r in out])

    def test_unknown_product_type_does_not_block_good_candidates(self):
        rows = [make_row(95 - i, "B%d" % i, n=i) for i in range(5)]
        for row in rows:
            row["product_type"] = "その他"
        out = bc.select_final_pool(rows, target_max=5)
        self.assertEqual(5, len(out))

    def test_build_amazon_query_adds_problem_before_scene(self):
        intent = bc.build_intent_spec(
            "自転車・サイクル用品", "ロード初心者の「パンク」に応える", "自転車 ロード初心者",
            [["nayami", "パンク"], ["attr", "ロード初心者"]])
        self.assertEqual("自転車 ロード初心者 パンク", bc.build_amazon_query(intent, "自転車 ロード初心者"))

    def test_concrete_fallback_is_only_specific_product_categories(self):
        intent = bc.build_intent_spec(
            "夏祭り", "夏祭りの「子連れ」対策グッズ", "夏祭り 子連れ",
            [["nayami", "子連れ"]])
        qs = bc.concrete_fallback_queries(intent, "夏祭り 子連れ")
        self.assertIn("子供 迷子防止 タグ", qs)
        self.assertNotIn("夏祭り", qs)

    def test_direct_amazon_lanes_merge_without_resolving_each_item(self):
        item_a = {"asin": "A", "itemInfo": {"title": {"displayValue": "軽量 電動ドライバー"}}}
        item_b = {"asin": "B", "itemInfo": {"title": {"displayValue": "電動ドライバー 300g"}}}
        conf = {"api_url": "https://example/searchItems", "partner_tag": "tag", "marketplace": "www.amazon.co.jp"}
        with mock.patch.object(bc, "amazon_search", side_effect=[
                {"searchResult": {"items": [item_a]}},
                {"searchResult": {"items": [item_a, item_b]}},
            ]), mock.patch.object(bc.time, "sleep"):
            rows, tried, _ = bc.amazon_direct_candidates(conf, "token", "電動ドライバー 軽量")
        self.assertEqual(2, len(tried))
        self.assertEqual(2, len(rows))
        merged = next(r for r in rows if r["amazon"]["asin"] == "A")
        self.assertEqual({"relevance", "reviews"}, set(merged["candidate"]["amazon_lanes"]))

    def test_cached_reason_keeps_type_out_of_visible_reason(self):
        packed = bc._encode_cached_reason("切り口に合致", "携帯ポンプ")
        self.assertEqual(("切り口に合致", "携帯ポンプ"), bc._decode_cached_reason(packed))

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


class SaveQualityTest(unittest.TestCase):
    def test_one_or_two_items_do_not_replace_existing_result(self):
        rows = [{"asin": "A", "ai_score": 95}, {"asin": "B", "ai_score": 90}]
        ok, reason = product_fetch.validate_new_result(rows, rerank=True)
        self.assertFalse(ok)
        self.assertIn("2件", reason)

    def test_three_high_confidence_items_can_be_saved(self):
        rows = [{"asin": "A", "ai_score": 95}, {"asin": "B", "ai_score": 90},
                {"asin": "C", "ai_score": 80}]
        self.assertTrue(product_fetch.validate_new_result(rows, rerank=True)[0])

    def test_score_below_public_threshold_is_blocked(self):
        rows = [{"asin": "A", "ai_score": 95}, {"asin": "B", "ai_score": 90},
                {"asin": "C", "ai_score": 69}]
        self.assertFalse(product_fetch.validate_new_result(rows, rerank=True)[0])

    def test_quality_promotion_allows_four_strong_items(self):
        rows = [{"asin": "A", "ai_score": 95}, {"asin": "B", "ai_score": 90},
                {"asin": "C", "ai_score": 85}, {"asin": "D", "ai_score": 80}]
        ok, reason = product_fetch.validate_quality_promotion(rows, 9, rerank=True)
        self.assertTrue(ok, reason)

    def test_quality_promotion_allows_new_high_quality_result(self):
        rows = [{"asin": "A", "ai_score": 95}, {"asin": "B", "ai_score": 90},
                {"asin": "C", "ai_score": 85}, {"asin": "D", "ai_score": 80}]
        ok, reason = product_fetch.validate_quality_promotion(rows, 0, rerank=True)
        self.assertTrue(ok, reason)
        self.assertIn("新規登録", reason)

    def test_quality_promotion_rejects_three_items_even_if_scores_are_high(self):
        rows = [{"asin": "A", "ai_score": 100}, {"asin": "B", "ai_score": 95},
                {"asin": "C", "ai_score": 90}]
        ok, reason = product_fetch.validate_quality_promotion(rows, 9, rerank=True)
        self.assertFalse(ok)
        self.assertIn("4件以上", reason)

    def test_quality_promotion_rejects_low_average(self):
        rows = [{"asin": "A", "ai_score": 90}, {"asin": "B", "ai_score": 85},
                {"asin": "C", "ai_score": 80}, {"asin": "D", "ai_score": 80}]
        ok, reason = product_fetch.validate_quality_promotion(rows, 9, rerank=True)
        self.assertFalse(ok)
        self.assertIn("平均AI85点未満", reason)

    def test_promote_can_replace_larger_existing_pool_when_gate_passes(self):
        pool = [{"asin": "A", "ai_score": 95, "title": "A"},
                {"asin": "B", "ai_score": 90, "title": "B"},
                {"asin": "C", "ai_score": 85, "title": "C"},
                {"asin": "D", "ai_score": 80, "title": "D"}]
        rows = [{"asin": x["asin"], "ai_score": x["ai_score"]} for x in pool]
        with mock.patch.object(product_fetch, "rpc", side_effect=[9, 4]) as rpc, \
             mock.patch.object(product_fetch, "build_pool", return_value=pool), \
             mock.patch.object(product_fetch, "to_rows", return_value=rows), \
             mock.patch.object(product_fetch, "usage_load", return_value=0), \
             mock.patch.object(product_fetch, "usage_bump"), \
             mock.patch.object(product_fetch, "read_batch_token", return_value="token"):
            result = product_fetch.fetch_and_save(
                "token", "2026-07-21", "テーマ", "切り口", "検索語",
                rerank=True, force=False, components=[], promote=True)
        self.assertEqual("promoted", result)
        self.assertEqual(2, rpc.call_count)

    def test_force_does_not_replace_a_larger_existing_pool(self):
        pool = [{"asin": "A", "ai_score": 95, "title": "A"},
                {"asin": "B", "ai_score": 90, "title": "B"},
                {"asin": "C", "ai_score": 80, "title": "C"}]
        rows = [{"asin": x["asin"], "ai_score": x["ai_score"]} for x in pool]
        with mock.patch.object(product_fetch, "rpc", return_value=6) as rpc, \
             mock.patch.object(product_fetch, "build_pool", return_value=pool), \
             mock.patch.object(product_fetch, "to_rows", return_value=rows), \
             mock.patch.object(product_fetch, "usage_load", return_value=0), \
             mock.patch.object(product_fetch, "usage_bump"), \
             mock.patch.object(product_fetch, "read_batch_token", return_value="token"):
            result = product_fetch.fetch_and_save(
                "token", "2026-07-21", "テーマ", "切り口", "検索語",
                rerank=True, force=True, components=[])
        self.assertEqual("empty", result)
        self.assertEqual(1, rpc.call_count)


if __name__ == "__main__":
    unittest.main()
