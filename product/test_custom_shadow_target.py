import unittest

from fetch_claimed import parse_custom_targets


class CustomShadowTargetTests(unittest.TestCase):
    def test_custom_targets_are_capped_and_deduplicated(self):
        text = """\
        A|a|a kw
        A|a|a kw
        B|b|b kw
        C|c|c kw
        D|d|d kw
        """
        rows = parse_custom_targets(text)
        self.assertEqual([r["theme"] for r in rows], ["A", "B", "C"])
        self.assertEqual(rows[0]["kw"], "a kw")

    def test_normal_target_is_not_custom(self):
        self.assertIsNone(parse_custom_targets("枕|高さで選ぶ"))

    def test_invalid_custom_target_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_custom_targets("A|a|a kw\nB|b")


if __name__ == "__main__":
    unittest.main()
