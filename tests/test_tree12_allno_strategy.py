from __future__ import annotations
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from tree12_allno_strategy import (
    TREE12_MIN_NO_ASK, allow_new_entries, consensus_top2_token_ids,
    hybrid_limit_price, paper_fill_working_order, plan_tree12_entries,
    plan_tree12_exits_from_metar, position_key, record_ws_ask_sample,
    plan_tree12_due_exit_faks,
    parse_taf_extremes_for_local_day, record_tree12_taf_reports, ensure_tree12_state,
    settle_tree12_expired_positions,
    tree12_paper_fill,
)

def city_shanghai():
    return {"city_id": "shanghai", "icao": "ZSPD", "timezone": "Asia/Shanghai", "market_unit": "C"}

class Tree12AllNoTests(unittest.TestCase):
    def test_lead_window(self):
        city = city_shanghai()
        self.assertTrue(allow_new_entries(city, "2026-09-01", datetime(2026, 8, 30, 15, 0, tzinfo=timezone.utc)))
        self.assertFalse(allow_new_entries(city, "2026-09-01", datetime(2026, 8, 31, 17, 0, tzinfo=timezone.utc)))

    def test_top3(self):
        buckets = [{"_no_token_id": "a"}, {"_no_token_id": "b"}, {"_no_token_id": "c"}, {"_no_token_id": "d"}]
        books = {"a": {"best_ask": "0.90"}, "b": {"best_ask": "0.86"}, "c": {"best_ask": "0.95"}, "d": {"best_ask": "0.99"}}
        # 53d7a92: consensus 从 top2 升级为 top3(排除 ask 最低的 3 个)
        self.assertEqual(consensus_top2_token_ids(buckets, books), {"a", "b", "c"})

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
        now = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)  # lead=28h ∈ (18,30]
        rules = [{"enabled": True, "city_id": "shanghai", "market_local_date": local_date, "direction": "high",
                  "buckets": [
                      {"bucket_id": "b30", "lo": 30, "hi": 31, "no_token_id": "no-30"},
                      {"bucket_id": "b31", "lo": 31, "hi": 32, "no_token_id": "no-31"},
                      {"bucket_id": "b32", "lo": 32, "hi": 33, "no_token_id": "no-32"},
                      {"bucket_id": "b33", "lo": 33, "hi": 34, "no_token_id": "no-33"},
                  ]}]
        books = {"no-30": {"best_ask": "0.86", "tick_size": "0.01"},
                 "no-31": {"best_ask": "0.87", "tick_size": "0.01"},
                 "no-32": {"best_ask": "0.95", "tick_size": "0.01"},
                 "no-33": {"best_ask": "0.90", "tick_size": "0.01"}}
        actions = plan_tree12_entries({}, {"shanghai": city}, rules, books, now, {"target_order_shares": "5"})
        submits = [a for a in actions if a.get("action_type") == "tree12_submit_entry"]
        self.assertTrue(submits)
        self.assertEqual(submits[0]["token_id"], "no-32")

    def test_ask_range_inclusive_085_to_095(self):
        city = city_shanghai()
        now = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)  # lead=28h ∈ (18,30]
        rules = [{"enabled": True, "city_id": "shanghai", "market_local_date": "2026-09-10", "direction": "high",
                  "buckets": [
                      {"bucket_id": "b0", "lo": 0, "hi": 1, "no_token_id": "n0"},
                      {"bucket_id": "b1", "lo": 1, "hi": 2, "no_token_id": "n1"},
                      {"bucket_id": "b2", "lo": 2, "hi": 3, "no_token_id": "n2"},
                      {"bucket_id": "b3", "lo": 3, "hi": 4, "no_token_id": "n3"},
                  ]}]
        books = {"n0": {"best_ask": "0.83", "tick_size": "0.01"},
                 "n1": {"best_ask": "0.84", "tick_size": "0.01"},
                 "n2": {"best_ask": "0.85", "tick_size": "0.01"},
                 "n3": {"best_ask": "0.95", "tick_size": "0.01"}}
        actions = plan_tree12_entries({}, {"shanghai": city}, rules, books, now, {"target_order_shares": "5"})
        submits = [a for a in actions if a.get("action_type") == "tree12_submit_entry"]
        # top3 排除 n0/n1/n2(最低 3 个 ask), 剩 n3(0.95 边界内)提交
        self.assertEqual({s["token_id"] for s in submits}, {"n3"})

    def test_ask_above_095_blocked(self):
        city = city_shanghai()
        now = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)  # lead=28h ∈ (18,30]
        rules = [{"enabled": True, "city_id": "shanghai", "market_local_date": "2026-09-10", "direction": "high",
                  "buckets": [
                      {"bucket_id": "b0", "lo": 0, "hi": 1, "no_token_id": "n0"},
                      {"bucket_id": "b1", "lo": 1, "hi": 2, "no_token_id": "n1"},
                      {"bucket_id": "b2", "lo": 2, "hi": 3, "no_token_id": "n2"},
                      {"bucket_id": "b3", "lo": 3, "hi": 4, "no_token_id": "n3"},
                      {"bucket_id": "b4", "lo": 4, "hi": 5, "no_token_id": "n4"},
                  ]}]
        books = {"n0": {"best_ask": "0.83", "tick_size": "0.01"},
                 "n1": {"best_ask": "0.84", "tick_size": "0.01"},
                 "n2": {"best_ask": "0.85", "tick_size": "0.01"},
                 "n3": {"best_ask": "0.96", "tick_size": "0.01"},
                 "n4": {"best_ask": "0.80", "tick_size": "0.01"}}
        actions = plan_tree12_entries({}, {"shanghai": city}, rules, books, now, {"target_order_shares": "5"})
        submits = [a for a in actions if a.get("action_type") == "tree12_submit_entry"]
        # top3 排除 n4/n0/n1, n2(0.85)提交, n3(0.96>0.95)被区间上限阻断
        self.assertEqual({s["token_id"] for s in submits}, {"n2"})

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
                 "shares": "5", "bucket": {"bucket_id": "b32", "lo": 32, "hi": 33}}}, "exit_chases": {}, "ws_ask_samples": {}},
                 "daily_extrema": {"shanghai|2026-09-10": {"high": 32.5, "low": 20.0}}}
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
                    "token_id": "no-32", "lo": 32, "hi": 33, "limit_price": "0.90"}}, "positions": {}, "exit_chases": {}, "ws_ask_samples": {}}}
        now = datetime(2026, 9, 8, 0, 0, tzinfo=timezone.utc)
        book = {"best_ask": "0.90", "asks": [{"price": "0.90", "size": "3"}, {"price": "0.91", "size": "2"}]}
        result = tree12_paper_fill(state, "k1", Decimal("5"), book, now)
        self.assertEqual(result["status"], "paper_filled")
        # limit=0.90 只吃 0.90 档 3 股（0.91 越限），剩余 2 股留在 GTC 挂单。
        self.assertEqual(Decimal(result["filled"]), Decimal("3"))
        self.assertEqual(Decimal(state["tree12"]["working_orders"]["k1"]["remaining_shares"]), Decimal("2"))
        self.assertGreater(float(state["paper_total_debit_usdc"]), 0)
        # Exhaust capital: a second fill should be blocked.
        state["paper_total_debit_usdc"] = 999.0
        state["tree12"]["working_orders"]["k2"] = {"key": "k2", "status": "working_gtc_buy_no", "remaining_shares": "5",
            "city_id": "shanghai", "market_local_date": "2026-09-10", "direction": "high", "bucket_id": "b33", "token_id": "no-33", "lo": 33, "hi": 34, "limit_price": "0.90"}
        blocked = tree12_paper_fill(state, "k2", Decimal("5"), book, now)
        self.assertEqual(blocked["status"], "blocked_insufficient_capital")
        self.assertEqual(state["tree12"]["working_orders"]["k2"]["status"], "blocked_insufficient_capital")

    def _expired_position_state(self, high: float):
        city = city_shanghai()
        key = position_key("shanghai", "2026-09-10", "high", "b32")
        state = {
            "paper_initial_capital_usdc": 1000.0,
            "paper_total_debit_usdc": 10.0,
            "daily_extrema": {"shanghai|2026-09-10": {
                "city_id": "shanghai", "market_local_date": "2026-09-10",
                "market_unit": "C", "high": high, "low": 20.0,
            }},
            "tree12": {
                "working_orders": {key: {"key": key, "status": "working_gtc_buy_no",
                    "city_id": "shanghai", "market_local_date": "2026-09-10",
                    "direction": "high", "bucket_id": "b32", "token_id": "no-32"}},
                "positions": {key: {"key": key, "order_id": "t12-abc",
                    "city_id": "shanghai", "market_local_date": "2026-09-10",
                    "direction": "high", "bucket_id": "b32", "token_id": "no-32",
                    "shares": "5", "avg_price": "0.90",
                    "bucket": {"bucket_id": "b32", "lo": 28.0, "hi": 29.0}}},
                "exit_chases": {}, "ws_ask_samples": {},
            },
        }
        return city, key, state

    def test_expired_win_settles_and_releases_capital(self):
        city, key, state = self._expired_position_state(high=30.0)  # 30 outside [28, 29) → NO wins
        now = datetime(2026, 9, 11, 0, 0, tzinfo=timezone.utc)
        actions = settle_tree12_expired_positions(state, {"shanghai": city}, [], now, {})
        self.assertEqual([a["action_type"] for a in actions], ["tree12_settled_win"])
        a = actions[0]
        self.assertEqual(a["payout_price"], "1")
        self.assertEqual(Decimal(a["proceeds_usdc"]), Decimal("5.0"))
        self.assertEqual(Decimal(a["realized_pnl_usdc"]), Decimal("0.50"))
        self.assertNotIn(key, state["tree12"]["positions"])
        self.assertNotIn(key, state["tree12"]["working_orders"])
        self.assertAlmostEqual(float(state["paper_total_debit_usdc"]), 5.0, places=5)
        self.assertEqual(state["tree12"]["settled_positions"][key]["outcome"], "win")

    def test_expired_loss_settles_zero_recovery(self):
        city, key, state = self._expired_position_state(high=28.5)  # 28.5 inside [28, 29) → NO loses
        now = datetime(2026, 9, 11, 0, 0, tzinfo=timezone.utc)
        actions = settle_tree12_expired_positions(state, {"shanghai": city}, [], now, {})
        self.assertEqual([a["action_type"] for a in actions], ["tree12_settled_loss"])
        a = actions[0]
        self.assertEqual(a["payout_price"], "0")
        self.assertEqual(Decimal(a["proceeds_usdc"]), Decimal(0))
        self.assertEqual(Decimal(a["realized_pnl_usdc"]), Decimal("-4.50"))
        self.assertNotIn(key, state["tree12"]["positions"])
        self.assertNotIn(key, state["tree12"]["working_orders"])
        # release(0): debit unchanged
        self.assertAlmostEqual(float(state["paper_total_debit_usdc"]), 10.0, places=5)
        self.assertEqual(state["tree12"]["settled_positions"][key]["outcome"], "loss")

    def test_not_expired_position_not_settled(self):
        city, key, state = self._expired_position_state(high=30.0)
        # Within the 6h grace window after local day end (16:00Z) but before 22:00Z expiry.
        now = datetime(2026, 9, 10, 18, 0, tzinfo=timezone.utc)
        actions = settle_tree12_expired_positions(state, {"shanghai": city}, [], now, {})
        self.assertEqual(actions, [])
        self.assertIn(key, state["tree12"]["positions"])

    def test_expired_settlement_removes_position_and_working_order(self):
        city, key, state = self._expired_position_state(high=28.5)
        now = datetime(2026, 9, 11, 0, 0, tzinfo=timezone.utc)
        settle_tree12_expired_positions(state, {"shanghai": city}, [], now, {})
        self.assertNotIn(key, state["tree12"]["positions"])
        self.assertNotIn(key, state["tree12"]["working_orders"])
        self.assertIn(key, state["tree12"]["settled_positions"])

if __name__ == "__main__":
    unittest.main()
