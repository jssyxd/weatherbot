from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import edge_engine as edge  # noqa: E402


class EdgeEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cities = edge.load_contract_cities(Path(__file__).resolve().parents[1] / "config/contract_cities.json")

    def test_strict_contract_scope_has_49_verified_metar_cities(self) -> None:
        self.assertEqual(len(self.cities), 49)
        self.assertNotIn("VHHH", self.cities)
        self.assertNotIn("ZSJN", self.cities)
        self.assertEqual(self.cities["ZSPD"]["timezone"], "Asia/Shanghai")
        self.assertEqual(self.cities["KLGA"]["market_unit"], "F")

    def test_local_market_date_uses_airport_timezone(self) -> None:
        moment = datetime(2026, 8, 22, 16, 0, tzinfo=timezone.utc)
        self.assertEqual(edge.local_market_date(moment, self.cities["ZSPD"]), "2026-08-23")
        self.assertEqual(edge.local_market_date(moment, self.cities["KLGA"]), "2026-08-22")

    def test_taf_groups_are_assigned_to_the_correct_local_day(self) -> None:
        city = self.cities["ZSPD"]
        parsed = edge.parse_taf_extremes(
            "TAF ZSPD 220907Z 2212/2318 12007MPS TX28/2212Z TX31/2306Z TN26/2221Z",
            datetime(2026, 8, 22, 9, 7, tzinfo=timezone.utc),
            city,
        )
        self.assertEqual([item["value_c"] for item in parsed["2026-08-22"]], [28.0])
        self.assertEqual(sorted(item["kind"] for item in parsed["2026-08-23"]), ["high", "low"])

    def test_selects_only_adjacent_newly_invalidated_high_bucket(self) -> None:
        buckets = [
            {"bucket_id": "29", "lo": 29, "hi": 30},
            {"bucket_id": "30", "lo": 30, "hi": 31},
            {"bucket_id": "31", "lo": 31, "hi": 32},
        ]
        selected = edge.select_adjacent_invalidated_bucket(buckets, "high", 29.2, 31.1)
        self.assertIsNotNone(selected)
        self.assertEqual(selected["bucket_id"], "30")

    def test_edge_signal_requires_new_extreme_and_is_idempotent(self) -> None:
        city = self.cities["ZSPD"]
        event_base = {
            "airport_icao": "ZSPD",
            "report_time_utc": "2026-08-22T12:00:00Z",
            "fetched_at_utc": "2026-08-22T12:03:00Z",
            "temperature_c": 29,
            "raw_metar": "METAR ZSPD 221200Z 12007MPS 9999 29/24 Q1005 NOSIG",
            "event_id": "baseline",
        }
        state: dict = {"edge_configs": {}, "daily_extrema": {}, "handled_candidate_buckets": {}}
        first = edge.evaluate_observation(state, event_base, city, [], 600)
        self.assertEqual(first[0]["reason"], "daily_baseline_initialized")
        local_date = "2026-08-22"
        state["edge_configs"][edge.edge_key(city["city_id"], local_date, "high")] = {
            "activation_edge_native": 30.0, "source_type": "taf_tx",
        }
        rules = [{
            "market_rule_id": "market-high", "market_id": "m1", "no_token_id": "no", "city_id": city["city_id"],
            "market_local_date": local_date, "direction": "high", "market_unit": "C", "enabled": True,
            "buckets": [{"bucket_id": "30", "lo": 30, "hi": 31}],
        }]
        event_break = dict(event_base, event_id="break", temperature_c=31, raw_metar="METAR ZSPD 221230Z 12007MPS 9999 31/24 Q1005 NOSIG", report_time_utc="2026-08-22T12:30:00Z", fetched_at_utc="2026-08-22T12:33:00Z")
        signals = edge.evaluate_observation(state, event_break, city, rules, 600)
        candidates = [item for item in signals if item["signal_type"] == "candidate_no_signal"]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["bucket"]["bucket_id"], "30")
        repeated = edge.evaluate_observation(state, dict(event_break, event_id="repeat", temperature_c=32, raw_metar="METAR ZSPD 221300Z 12007MPS 9999 32/24 Q1005 NOSIG", report_time_utc="2026-08-22T13:00:00Z", fetched_at_utc="2026-08-22T13:03:00Z"), city, rules, 600)
        self.assertNotIn("candidate_no_signal", [item["signal_type"] for item in repeated])


if __name__ == "__main__":
    unittest.main()
