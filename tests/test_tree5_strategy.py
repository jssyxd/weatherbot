import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import edge_engine as edge  # noqa: E402
import tree5_strategy as tree5  # noqa: E402


class Tree5StrategyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.shanghai = edge.load_contract_cities(Path(__file__).resolve().parents[1] / "config/contract_cities.json")["ZSPD"]
        cls.date = "2026-08-22"
        cls.now = datetime(2026, 8, 22, 6, 30, tzinfo=timezone.utc)  # 14:30 Asia/Shanghai

    def rules(self):
        return [
            {
                "market_rule_id": "event|2026-08-22|high", "city_id": "shanghai", "market_local_date": self.date,
                "direction": "high", "market_unit": "C", "enabled": True,
                "buckets": [
                    {"bucket_id": "hi29", "label": "29°C", "lo": 29, "hi": 30, "yes_token_id": "yes-hi29", "no_token_id": "no-hi29"},
                    {"bucket_id": "hi30", "label": "30°C", "lo": 30, "hi": 31, "yes_token_id": "yes-hi30", "no_token_id": "no-hi30"},
                ],
            },
            {
                "market_rule_id": "event|2026-08-22|low", "city_id": "shanghai", "market_local_date": self.date,
                "direction": "low", "market_unit": "C", "enabled": True,
                "buckets": [
                    {"bucket_id": "lo21", "label": "21°C", "lo": 21, "hi": 22, "yes_token_id": "yes-lo21", "no_token_id": "no-lo21"},
                    {"bucket_id": "lo22", "label": "22°C", "lo": 22, "hi": 23, "yes_token_id": "yes-lo22", "no_token_id": "no-lo22"},
                ],
            },
        ]

    @staticmethod
    def book(ask=None, bid=None):
        return {"best_ask": ask, "best_bid": bid, "tick_size": "0.01", "min_order_size": "5", "source": "test"}

    @staticmethod
    def config():
        return {
            "target_order_shares": "5", "min_execution_price": "0.05", "max_execution_price": "0.98",
            "tree5_entry_price_discount": "0.05", "tree5_exit_retry_seconds": (0, 5, 20, 60, 120),
            "tree5_exit_slippage": ("0.10", "0.20", "0.35", "0.60", "0.90"), "tree5_exit_min_price": "0.01",
            "tree5_high_closure_start_hour": 13, "tree5_high_closure_end_hour": 17,
            "tree5_low_closure_start_hour": 1, "tree5_low_closure_end_hour": 5,
            "tree5_closure_shortfall_native": 1.0, "tree5_closure_trend_move_native": 0.5,
            "tree5_closure_price_decline": "0.20", "tree5_closure_check_seconds": 60,
        }

    def test_parse_taf_extremes_uses_iana_local_day_and_native_unit(self) -> None:
        raw = "TAF ZSPD 220000Z 2200/2306 00000KT CAVOK TX30/2210Z TN22/2214Z"
        parsed = tree5.parse_taf_extremes_for_local_day(raw, "2026-08-22T00:00:00Z", self.shanghai, self.date)
        self.assertEqual(parsed["high"]["value_native"], 30.0)
        self.assertEqual(parsed["low"]["value_native"], 22.0)
        self.assertEqual(parsed["high"]["forecast_time_utc"], "2026-08-22T10:00:00Z")

    def test_taf_entry_is_unique_bucket_five_share_gtc_at_five_percent_discount(self) -> None:
        state = {"tree5": {"taf_forecasts": {
            "shanghai|2026-08-22|high": {
                "city_id": "shanghai", "icao": "ZSPD", "market_local_date": self.date, "direction": "high",
                "value_native": 30.0, "market_unit": "C", "issued_utc": "2026-08-22T00:00:00Z",
            }
        }}}
        actions = tree5.plan_taf_entries(
            state, {"ZSPD": self.shanghai}, self.rules(), {"yes-hi30": self.book(ask="0.60")}, self.now, self.config(),
        )
        self.assertEqual(len(actions), 1)
        action = actions[0]
        self.assertEqual(action["action_type"], "tree5_submit_entry")
        self.assertEqual(action["order_type"], "GTC")
        self.assertEqual(action["requested_shares"], "5")
        self.assertEqual(action["limit_price"], "0.57")
        self.assertEqual(action["bucket"]["bucket_id"], "hi30")

    def test_exit_waits_for_interval_boundary_then_reprices_fak_from_best_bid(self) -> None:
        state = {"tree5": {"taf_forecasts": {
            "shanghai|2026-08-22|high": {
                "city_id": "shanghai", "icao": "ZSPD", "market_local_date": self.date, "direction": "high",
                "value_native": 30.0, "market_unit": "C", "issued_utc": "2026-08-22T00:00:00Z",
            }
        }}}
        tree5.plan_taf_entries(
            state, {"ZSPD": self.shanghai}, self.rules(), {"yes-hi30": self.book(ask="0.60")}, self.now, self.config(),
        )
        entry = next(iter(state["tree5"]["entries"].values()))
        tree5.attach_confirmed_position_for_replay(state, "yes-hi30", "5", self.now)
        # Exceeding the forecast point alone does not prove an interval bucket wrong.
        state["daily_extrema"] = {"shanghai|2026-08-22": {"high": 30.5, "low": 20}}
        no_exit = tree5.invalidate_entries_from_observation(state, self.shanghai, self.date, self.now, observed_temperature=30.5)
        self.assertEqual(no_exit, [])
        actions = tree5.invalidate_entries_from_observation(state, self.shanghai, self.date, self.now, observed_temperature=31.0)
        self.assertEqual(entry["status"], "invalidated_by_metar")
        self.assertEqual(actions[-1]["action_type"], "tree5_exit_chase_started")
        exit_actions = tree5.plan_due_exit_faks(state, {"yes-hi30": self.book(bid="0.40")}, self.now, self.config())
        self.assertEqual(len(exit_actions), 1)
        self.assertEqual(exit_actions[0]["order_type"], "FAK")
        self.assertEqual(exit_actions[0]["limit_price"], "0.36")
        self.assertEqual(state["tree5"]["exit_chases"][entry["entry_key"]]["next_attempt_utc"], "2026-08-22T06:30:05Z")

    def test_time_closure_requires_weather_reversal_and_market_decline(self) -> None:
        entry = {
            "entry_key": "event|2026-08-22|high|hi30", "city_id": "shanghai", "market_local_date": self.date,
            "direction": "high", "token_id": "yes-hi30", "entry_reference_best_ask": "0.60",
            "status": "planned_gtc_entry", "bucket": {"lo": 30, "hi": 31, "bucket_id": "hi30"},
            "external_order_id": None,
        }
        state = {
            "daily_extrema": {"shanghai|2026-08-22": {"high": 29, "low": 20}},
            "tree5": {"entries": {entry["entry_key"]: entry}, "temperature_history": {
                "shanghai|2026-08-22": [
                    {"temperature_native": 28.0}, {"temperature_native": 29.0}, {"temperature_native": 28.0},
                ]
            }},
        }
        actions = tree5.evaluate_time_closure(
            state, {"ZSPD": self.shanghai}, {"yes-hi30": self.book(bid="0.48")}, self.now, self.config(),
        )
        self.assertEqual(actions[0]["action_type"], "tree5_time_closure")
        self.assertEqual(actions[0]["status"], "triggered")
        self.assertEqual(entry["status"], "closure_exit_requested")

    def test_taf_due_only_after_local_0100_and_only_once_per_city_day(self) -> None:
        state = {}
        before = datetime(2026, 8, 21, 16, 59, tzinfo=timezone.utc)  # 00:59 Asia/Shanghai
        self.assertEqual(tree5.due_taf_cities(state, {"ZSPD": self.shanghai}, before, self.config()), [])
        after = datetime(2026, 8, 21, 17, 0, tzinfo=timezone.utc)  # 01:00 Asia/Shanghai
        self.assertEqual([city["icao"] for city in tree5.due_taf_cities(state, {"ZSPD": self.shanghai}, after, self.config())], ["ZSPD"])
        tree5.ensure_tree5_state(state)["taf_fetches"]["shanghai|2026-08-22"] = {"status": "complete", "market_local_date": "2026-08-22"}
        self.assertEqual(tree5.due_taf_cities(state, {"ZSPD": self.shanghai}, after, self.config()), [])


if __name__ == "__main__":
    unittest.main()
