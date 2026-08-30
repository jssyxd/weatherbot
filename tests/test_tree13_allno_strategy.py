import unittest
from datetime import datetime, timezone
from decimal import Decimal

from tree13_allno_strategy import (
    classify_metar_for_position,
    conservative_limit,
    ensure_allno_state,
    plan_entries,
    plan_exit,
)


class Tree13AllNoStrategyTests(unittest.TestCase):
    def setUp(self):
        self.city = {"city_id": "x", "icao": "KXXX", "market_unit": "C", "timezone": "UTC"}
        self.rule = {"market_rule_id": "m", "city_id": "x", "market_local_date": "2026-08-30", "direction": "high", "buckets": [
            {"bucket_id": "a", "lo": 20, "hi": 21, "no_token_id": "na"},
            {"bucket_id": "b", "lo": 21, "hi": 22, "no_token_id": "nb"},
            {"bucket_id": "c", "lo": 22, "hi": 23, "no_token_id": "nc"},
            {"bucket_id": "d", "lo": 23, "hi": 24, "no_token_id": "nd"},
        ]}
        self.books = {t: {"best_ask": p, "tick_size": "0.01"} for t, p in (("na", "0.86"), ("nb", "0.90"), ("nc", "0.92"), ("nd", "0.96"))}
        self.history = {t: [{"monotonic_ns": 1_000_000_000 * i, "best_ask": p} for i, p in enumerate(("0.90", "0.91", "0.92"), 1)] for t, p in (("na", "0.90"), ("nb", "0.91"), ("nc", "0.92"), ("nd", "0.93"))}

    def test_conservative_limit_uses_lower_of_discount_and_midpoint(self):
        self.assertEqual(conservative_limit("0.96", "0.90", "0.01"), Decimal("0.91"))

    def test_entries_exclude_three_cheapest_and_require_history(self):
        state = {}
        result = plan_entries(state=state, city=self.city, local_date="2026-08-30", direction="high", rules=[self.rule], books=self.books, ask_history=self.history, now_monotonic_ns=3_000_000_000)
        self.assertEqual([x["status"] for x in result if x.get("status") == "PENDING_GTC"], ["PENDING_GTC"])
        self.assertEqual(result[-1]["token_id"], "nd")
        self.assertEqual(result[-1]["outcome"], "NO")

    def test_taf_bucket_is_blocked(self):
        state = {"tree13_allno": {"taf_versions": {"x|2026-08-30|high": [{"visible_at_monotonic_ns": 1, "value_native": 22, "taf_version_id": "t"}]}}}
        result = plan_entries(state=state, city=self.city, local_date="2026-08-30", direction="high", rules=[self.rule], books=self.books, ask_history=self.history, now_monotonic_ns=3_000_000_000)
        blocked = [x for x in result if x.get("token_id") == "nc"]
        self.assertIn("taf_bucket", blocked[0]["reasons"])

    def test_metar_priority_and_bucket_boundary(self):
        pos = {"direction": "high", "bucket": {"lo": 22, "hi": 23}, "token_id": "nc"}
        self.assertEqual(classify_metar_for_position(pos, 22.5), "FACT_INVALIDATED_EXIT")
        self.assertEqual(classify_metar_for_position(pos, 23), "PROVEN_IMPOSSIBLE_HOLD")
        low = {"direction": "low", "bucket": {"lo": 18, "hi": 19}, "token_id": "nl"}
        self.assertEqual(classify_metar_for_position(low, 17.9), "PROVEN_IMPOSSIBLE_HOLD")

    def test_exit_is_no_fak_and_requires_reconciliation(self):
        pos = {"token_id": "nc"}
        action = plan_exit(position=pos, reason="FACT_INVALIDATED", best_bid="0.10", remaining_shares="5", attempt=0)
        self.assertEqual(action["outcome"], "NO")
        self.assertEqual(action["order_type"], "FAK")
        self.assertTrue(action["requires_reconciled_order_id"])
        self.assertEqual(plan_exit(position=pos, reason="x", best_bid="0.1", remaining_shares="0")["status"], "blocked_no_reconciled_shares")


if __name__ == "__main__":
    unittest.main()
