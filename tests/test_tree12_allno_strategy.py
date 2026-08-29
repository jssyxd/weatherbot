from __future__ import annotations
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from tree12_allno_strategy import (
    TREE12_MIN_NO_ASK, allow_new_entries, consensus_top2_token_ids,
    hybrid_limit_price, paper_fill_working_order, plan_tree12_entries,
    plan_tree12_exits_from_metar, position_key, record_ws_ask_sample,
    start_tree12_exit_chase, plan_tree12_due_exit_faks,
    parse_taf_extremes_for_local_day, record_tree12_taf_reports, ensure_tree12_state,
    tree12_paper_fill,
)
from paper_capital import remaining_capital_usdc, reserve

def city_shanghai():
    return {"city_id": "shanghai", "icao": "ZSPD", "timezone": "Asia/Shanghai", "market_unit": "C"}

class Tree12AllNoTests(unittest.TestCase):
    def test_lead_window(self):
        city = city_shanghai()
        self.assertTrue(allow_new_entries(city, "2026-09-01", datetime(2026, 8, 30, 15, 0, tzinfo=timezone.utc)))
        self.assertFalse(allow_new_entries(city, "2026-09-01", datetime(2026, 8, 31, 17, 0, tzinfo=timezone.utc)))

    def test_top2(self):
        buckets = [{"_no_token_id": "a"}, {"_no_token_id": "b"}, {"_no_token_id": "c"}]
        books = {"a": {"best_ask": "0.90"}, "b": {"best_ask": "0.86"}, "c": {"best_ask": "0.95"}}
        self.assertEqual(consensus_top2_token_ids(buckets, books), {"a", "b"})

    def test_hybrid(self):
        state = {}
        now = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
        for i in range(10):
            record_ws_ask_sample(state, "tok", Decimal("0.92"), Decimal("5"), now - timedelta(minutes=i * 10))
        limit = hybrid_limit_price(state, "tok", Decimal("0.96"), Decimal("0.01"), now)
        self.assertGreater(limit, TREE12_MIN_NO_ASK)
        self.assertLessEqual(limit, Decimal("0.96"))

    def test_entry_plans(self):
        city = city_shanghai()
        local_date = "2026-09-10"
        now = datetime(2026, 9, 8, 0, 0, tzinfo=timezone.utc)
        rules = [{"enabled": True, "city_id": "shanghai", "market_local_date": local_date, "direction": "high",
                  "buckets": [
                      {"bucket_id": "b30", "lo": 30, "hi": 31, "no_token_id": "no-30"},
                      {"bucket_id": "b31", "lo": 31, "hi": 32, "no_token_id": "no-31"},
                      {"bucket_id": "b32", "lo": 32, "hi": 33, "no_token_id": "no-32"},
                  ]}]
        books = {"no-30": {"best_ask": "0.86", "tick_size": "0.01"},
                 "no-31": {"best_ask": "0.87", "tick_size": "0.01"},
                 "no-32": {"best_ask": "0.91", "tick_size": "0.01"}}
        actions = plan_tree12_entries({}, {"shanghai": city}, rules, books, now, {"target_order_shares": "5"})
        submits = [a for a in actions if a.get("action_type") == "tree12_submit_entry"]
        self.assertTrue(submits)
        self.assertEqual(submits[0]["token_id"], "no-32")

    def test_paper_fill(self):
        state = {"tree12": {"working_orders": {"k1": {"key": "k1", "status": "working_gtc_buy_no", "remaining_shares": "5",
                 "city_id": "shanghai", "market_local_date": "2026-09-10", "direction": "high", "bucket_id": "b32",
                 "token_id": "no-32", "lo": 32, "hi": 33}}, "positions": {}, "exit_chases": {}, "ws_ask_samples": {}}}
        now = datetime(2026, 8, 29, tzinfo=timezone.utc)
        paper_fill_working_order(state, "k1", Decimal("2"), Decimal("0.90"), now)
        self.assertEqual(state["tree12"]["positions"]["k1"]["shares"], "2")

    def test_metar_exit_chase_and_fak(self):
        city = city_shanghai()
        key = position_key("shanghai", "2026-09-10", "high", "b32")
        state = {"tree12": {"working_orders": {}, "positions": {key: {"key": key, "city_id": "shanghai",
                 "market_local_date": "2026-09-10", "direction": "high", "bucket_id": "b32", "token_id": "no-32",
                 "shares": "5", "bucket": {"bucket_id": "b32", "lo": 32, "hi": 33}}}, "exit_chases": {}, "ws_ask_samples": {}}}
        now = datetime(2026, 9, 10, 6, 0, tzinfo=timezone.utc)
        actions = plan_tree12_exits_from_metar(state, city, "2026-09-10", 32.5, now)
        self.assertTrue(any(a.get("action_type") == "tree12_exit" for a in actions))
        faks = plan_tree12_due_exit_faks(state, {"no-32": {"best_bid": "0.20", "tick_size": "0.01"}}, now, {})
        self.assertTrue(any(a.get("action_type") == "tree12_exit_fak" for a in faks))

    def test_tree12_taf_is_self_contained(self):
        city = city_shanghai()
        state = {}
        reports = [{"icao": "ZSPD", "raw_text": "TAF ZSPD 091100Z 0912/1018 TX32/1006Z TN26/0918Z", "issued": "2026-09-09T11:00:00Z"}]
        actions = record_tree12_taf_reports(state, reports, {"ZSPD": city}, datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc), "test")
        tree = ensure_tree12_state(state)
        self.assertIn("shanghai|2026-09-10|high", tree["taf_forecasts"])
        self.assertEqual(tree["taf_forecasts"]["shanghai|2026-09-10|high"]["value_native"], 32.0)
        self.assertTrue(any(a.get("action_type") == "tree12_taf_forecast_recorded" for a in actions))

    def test_taf_parse_maps_local_day(self):
        city = city_shanghai()
        parsed = parse_taf_extremes_for_local_day(
            "TAF ZSPD 091100Z 0912/1018 TX32/1006Z TN26/0918Z", "2026-09-09T11:00:00Z", city, "2026-09-10")
        self.assertIn("high", parsed)
        self.assertEqual(parsed["high"]["value_native"], 32.0)
        self.assertEqual(parsed["high"]["direction"], "high")

    def test_tree12_paper_fill_reserves_capital_and_blocks_when_exhausted(self):
        state = {"paper_initial_capital_usdc": 1000.0, "paper_total_debit_usdc": 0.0,
                 "tree12": {"working_orders": {"k1": {"key": "k1", "status": "working_gtc_buy_no", "remaining_shares": "5",
                    "city_id": "shanghai", "market_local_date": "2026-09-10", "direction": "high", "bucket_id": "b32",
                    "token_id": "no-32", "lo": 32, "hi": 33}}, "positions": {}, "exit_chases": {}, "ws_ask_samples": {}}}
        now = datetime(2026, 9, 8, 0, 0, tzinfo=timezone.utc)
        result = tree12_paper_fill(state, "k1", Decimal("5"), Decimal("0.90"), now)
        self.assertEqual(result["status"], "paper_filled")
        self.assertGreater(float(state["paper_total_debit_usdc"]), 0)
        # Exhaust capital: a second 5-share fill at 0.90 should be blocked.
        state["paper_total_debit_usdc"] = 999.0
        state["tree12"]["working_orders"]["k2"] = {"key": "k2", "status": "working_gtc_buy_no", "remaining_shares": "5",
            "city_id": "shanghai", "market_local_date": "2026-09-10", "direction": "high", "bucket_id": "b33", "token_id": "no-33", "lo": 33, "hi": 34}
        blocked = tree12_paper_fill(state, "k2", Decimal("5"), Decimal("0.90"), now)
        self.assertEqual(blocked["status"], "blocked_insufficient_capital")
        self.assertEqual(state["tree12"]["working_orders"]["k2"]["status"], "blocked_insufficient_capital")

if __name__ == "__main__":
    unittest.main()
