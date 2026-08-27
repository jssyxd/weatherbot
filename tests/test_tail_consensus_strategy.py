from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from local_order_book import LocalOrderBook
from tail_consensus_strategy import (
    TailConsensusConfig,
    evaluate_tail_entries,
    mark_temperature_breaks,
    monitor_tail_positions,
)


class TailConsensusStrategyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 8, 27, 15, 30, tzinfo=timezone.utc)
        self.clock = [self.now.timestamp()]
        self.city = {
            "city_id": "london", "icao": "EGLL", "timezone": "Europe/London", "market_unit": "C",
        }
        self.cities = {"EGLL": self.city}
        self.config = TailConsensusConfig.from_mapping({
            "tail_consensus_enabled": True,
            "tail_consensus_high_start_local": "16:00",
            "tail_consensus_high_end_local": "17:00",
            "tail_consensus_low_start_local": "04:00",
            "tail_consensus_low_end_local": "05:00",
            "tail_consensus_stability_seconds": 1800,
            "tail_consensus_stable_min_price": "0.90",
            "tail_consensus_entry_min_price": "0.92",
            "tail_consensus_entry_max_price": "0.98",
            "tail_consensus_target_shares": "5",
            "tail_consensus_max_open_positions": 10,
            "tail_consensus_market_alert_bid": "0.85",
            "tail_consensus_rotation_multiplier": "3",
            "tail_consensus_max_rotations": 1,
            "tail_consensus_price_mode": "best_ask_plus_one_tick",
            "local_book_max_age_seconds": 3,
        })
        self.rule = {
            "city_id": "london", "icao": "EGLL", "market_local_date": "2026-08-27",
            "direction": "high", "market_unit": "C", "buckets": [
                {"bucket_id": "24", "market_id": "m24", "label": "24°C", "lo": 24, "hi": 25, "yes_token_id": "yes-24"},
                {"bucket_id": "25", "market_id": "m25", "label": "25°C", "lo": 25, "hi": 26, "yes_token_id": "yes-25"},
            ],
        }

    def book(self, token: str, *, ask: str = "0.95", bid: str = "0.94", ask_size: str = "20", bid_size: str = "20"):
        book = LocalOrderBook(token, clock=lambda: self.clock[0])
        book.apply_book({
            "tokenId": token, "market": f"market-{token}", "timestamp": "1", "hash": f"h-{token}",
            "minOrderSize": "5", "tickSize": "0.01", "negRisk": False,
            "asks": [{"price": ask, "size": ask_size}], "bids": [{"price": bid, "size": bid_size}],
        })
        return book.snapshot()

    def test_enters_single_stable_yes_consensus_at_tail_time(self) -> None:
        state: dict = {"daily_extrema": {"london|2026-08-27": {"high": 23, "low": 15}}}
        books = {"yes-24": self.book("yes-24"), "yes-25": self.book("yes-25", ask="0.89")}
        first = self.now - timedelta(minutes=30)
        evaluate_tail_entries(state, self.config, self.cities, [self.rule], books, first)
        signals = evaluate_tail_entries(state, self.config, self.cities, [self.rule], books, self.now)
        entries = [item for item in signals if item["signal_type"] == "tail_yes_entry"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["execution"]["side"], "BUY_YES")
        self.assertEqual(entries[0]["execution"]["limit_price"], "0.96")
        self.assertEqual(Decimal(state["tail_positions"]["london|2026-08-27|high"]["shares"]), Decimal("5"))

    def test_multiple_eligible_buckets_fail_closed(self) -> None:
        state: dict = {"daily_extrema": {"london|2026-08-27": {"high": 23, "low": 15}}}
        books = {"yes-24": self.book("yes-24"), "yes-25": self.book("yes-25")}
        evaluate_tail_entries(state, self.config, self.cities, [self.rule], books, self.now - timedelta(minutes=30))
        signals = evaluate_tail_entries(state, self.config, self.cities, [self.rule], books, self.now)
        self.assertTrue(any(item.get("reason") == "multiple_executable_consensus_buckets" for item in signals))
        self.assertEqual(state.get("tail_positions"), {})

    def test_limit_price_plus_one_tick_never_exceeds_98_cents(self) -> None:
        state: dict = {"daily_extrema": {"london|2026-08-27": {"high": 23, "low": 15}}}
        books = {"yes-24": self.book("yes-24", ask="0.98"), "yes-25": self.book("yes-25", ask="0.89")}
        evaluate_tail_entries(state, self.config, self.cities, [self.rule], books, self.now - timedelta(minutes=30))
        signals = evaluate_tail_entries(state, self.config, self.cities, [self.rule], books, self.now)
        entry = next(item for item in signals if item["signal_type"] == "tail_yes_entry")
        self.assertEqual(entry["execution"]["limit_price"], "0.98")

    def test_sub_85_cent_bid_only_alerts_without_rotation(self) -> None:
        state = {
            "tail_positions": {
                "london|2026-08-27|high": {
                    "position_key": "london|2026-08-27|high", "city_id": "london", "icao": "EGLL",
                    "market_local_date": "2026-08-27", "direction": "high", "token_id": "yes-24",
                    "bucket": self.rule["buckets"][0], "shares": "5", "rotation_count": 0,
                }
            }
        }
        books = {"yes-24": self.book("yes-24", bid="0.84"), "yes-25": self.book("yes-25")}
        signals = monitor_tail_positions(state, self.config, self.cities, [self.rule], books, self.now)
        alerts = [item for item in signals if item["signal_type"] == "market_reversal_alert"]
        self.assertEqual(len(alerts), 1)
        self.assertIn("london|2026-08-27|high", state["tail_positions"])
        self.assertFalse(any(item["signal_type"] == "tail_yes_rotation" for item in signals))

    def test_temperature_breach_exits_then_rotates_once_into_three_times_new_yes(self) -> None:
        state = {
            "daily_extrema": {"london|2026-08-27": {"high": 24, "low": 15}},
            "tail_positions": {
                "london|2026-08-27|high": {
                    "position_key": "london|2026-08-27|high", "city_id": "london", "icao": "EGLL",
                    "market_local_date": "2026-08-27", "direction": "high", "token_id": "yes-24",
                    "bucket": self.rule["buckets"][0], "shares": "5", "rotation_count": 0,
                }
            },
            "tail_consensus": {
                "london|2026-08-27|high|yes-25": {
                    "above_threshold_since_utc": (self.now - timedelta(minutes=31)).isoformat().replace("+00:00", "Z"),
                    "last_seen_utc": self.now.isoformat().replace("+00:00", "Z"),
                }
            },
        }
        event = {"report_time_utc": self.now.isoformat().replace("+00:00", "Z"), "fetched_at_utc": self.now.isoformat().replace("+00:00", "Z"), "raw_metar": "METAR EGLL 271630Z 00000KT 9999 25/10 Q1012="}
        breaches = mark_temperature_breaks(state, self.config, event, self.city, self.now)
        self.assertEqual(breaches[0]["signal_type"], "temperature_break_pending_exit")
        books = {"yes-24": self.book("yes-24", bid="0.80"), "yes-25": self.book("yes-25", ask="0.95", bid="0.94")}
        signals = monitor_tail_positions(state, self.config, self.cities, [self.rule], books, self.now)
        rotations = [item for item in signals if item["signal_type"] == "tail_yes_rotation"]
        self.assertEqual(len(rotations), 1)
        self.assertEqual(rotations[0]["exit_execution"]["side"], "SELL_YES")
        self.assertEqual(rotations[0]["entry_execution"]["side"], "BUY_YES")
        self.assertEqual(Decimal(rotations[0]["entry_execution"]["filled_shares"]), Decimal("15"))
        position = state["tail_positions"]["london|2026-08-27|high"]
        self.assertEqual(position["token_id"], "yes-25")
        self.assertEqual(position["rotation_count"], 1)

    def test_fahrenheit_integer_c_observation_cannot_trigger_temperature_exit(self) -> None:
        city = {"city_id": "austin", "icao": "KAUS", "timezone": "America/Chicago", "market_unit": "F"}
        state = {
            "daily_extrema": {"austin|2026-08-27": {"high": 77, "low": 60}},
            "tail_positions": {
                "austin|2026-08-27|high": {
                    "position_key": "austin|2026-08-27|high", "city_id": "austin", "icao": "KAUS",
                    "market_local_date": "2026-08-27", "direction": "high", "token_id": "yes-78",
                    "bucket": {"bucket_id": "78", "lo": 78, "hi": 79, "yes_token_id": "yes-78"}, "shares": "5", "rotation_count": 0,
                }
            },
        }
        event = {"report_time_utc": "2026-08-27T20:30:00Z", "fetched_at_utc": "2026-08-27T20:30:00Z", "raw_metar": "METAR KAUS 272030Z 00000KT 9999 26/10 Q1012="}
        self.assertEqual(mark_temperature_breaks(state, self.config, event, city, self.now), [])
        self.assertIsNone(state["tail_positions"]["austin|2026-08-27|high"].get("pending_temperature_break"))

    def test_second_breach_after_one_rotation_only_attempts_exit(self) -> None:
        state = {
            "daily_extrema": {"london|2026-08-27": {"high": 25, "low": 15}},
            "tail_positions": {
                "london|2026-08-27|high": {
                    "position_key": "london|2026-08-27|high", "city_id": "london", "icao": "EGLL",
                    "market_local_date": "2026-08-27", "direction": "high", "token_id": "yes-25",
                    "bucket": self.rule["buckets"][1], "shares": "15", "rotation_count": 1,
                }
            },
        }
        event = {"report_time_utc": self.now.isoformat().replace("+00:00", "Z"), "fetched_at_utc": self.now.isoformat().replace("+00:00", "Z"), "raw_metar": "METAR EGLL 271630Z 00000KT 9999 26/10 Q1012="}
        mark_temperature_breaks(state, self.config, event, self.city, self.now)
        books = {"yes-25": self.book("yes-25", bid="0.80"), "yes-24": self.book("yes-24")}
        signals = monitor_tail_positions(state, self.config, self.cities, [self.rule], books, self.now)
        self.assertTrue(any(item.get("reason") == "rotation_limit_reached" for item in signals))
        self.assertNotIn("london|2026-08-27|high", state.get("tail_positions", {}))


if __name__ == "__main__":
    unittest.main()
