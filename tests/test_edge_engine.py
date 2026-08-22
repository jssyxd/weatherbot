from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

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
        self.assertEqual(self.cities["ZSPD"]["latitude"], 31.146)
        self.assertEqual(self.cities["ZSPD"]["longitude"], 121.8)
        self.assertEqual(self.cities["ZSPD"]["coordinate_source"], "AviationWeather.gov Data API METAR JSON station fields")

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

    def test_openmeteo_ecmwf_ifs025_is_explicit_and_keeps_audit_fields(self) -> None:
        city = self.cities["KLGA"]
        payload = json.dumps({
            "latitude": 40.7794, "longitude": -73.8803, "elevation": 9.0,
            "timezone": "America/New_York", "utc_offset_seconds": -14400,
            "daily": {"time": ["2026-08-22"], "temperature_2m_max": [83.2], "temperature_2m_min": [68.4]},
        })
        with patch.object(edge, "_read_url", return_value=(payload, "https://api.open-meteo.test/request?models=ecmwf_ifs025")):
            snapshot = edge.fetch_openmeteo_ecmwf_forecast(city)
        edges = edge.openmeteo_ecmwf_edges_for_city_date(snapshot, city, "2026-08-22")
        self.assertEqual(snapshot["requested_model"], "ecmwf_ifs025")
        self.assertEqual(snapshot["requested_coordinates"], {"latitude": 40.7794, "longitude": -73.8803})
        self.assertEqual(edges["high"]["source_type"], "openmeteo_ecmwf_ifs025_high")
        self.assertEqual(edges["high"]["forecast_value_native"], 83.2)
        self.assertEqual(edges["low"]["activation_edge_native"], 69.4)
        self.assertEqual(edges["high"]["source_detail"]["returned_timezone"], "America/New_York")

    def test_refresh_uses_ecmwf_before_wunderground_when_taf_has_no_extrema(self) -> None:
        city = self.cities["ZSPD"]
        snapshot = {
            "endpoint": "https://api.open-meteo.test/?models=ecmwf_ifs025", "raw_hash": "hash",
            "retrieved_at_utc": "2026-08-22T00:00:00Z", "requested_model": "ecmwf_ifs025",
            "requested_coordinates": {"latitude": 31.146, "longitude": 121.8},
            "returned_coordinates": {"latitude": 31.146, "longitude": 121.8, "elevation": 4},
            "returned_timezone": "Asia/Shanghai", "utc_offset_seconds": 28800,
            "response": {"daily": {"time": ["2026-08-22"], "temperature_2m_max": [31], "temperature_2m_min": [25]}},
        }
        state: dict = {}
        with patch.object(edge, "fetch_awc_tafs", return_value=({}, "https://awc.test/taf")), \
             patch.object(edge, "fetch_openmeteo_ecmwf_forecast", return_value=snapshot), \
             patch.object(edge, "fetch_wunderground_forecast", side_effect=AssertionError("WU must not be called")):
            summary = edge.refresh_edge_configs(state, {"ZSPD": city}, {"ZSPD": "2026-08-22"})
        self.assertEqual(summary["ecmwf_edges"], 2)
        self.assertEqual(summary["wu_edges"], 0)
        self.assertEqual(state["edge_configs"][edge.edge_key("shanghai", "2026-08-22", "high")]["source_type"], "openmeteo_ecmwf_ifs025_high")

    def test_final_fallback_failure_removes_stale_edge(self) -> None:
        city = self.cities["ZSPD"]
        key = edge.edge_key("shanghai", "2026-08-22", "high")
        state: dict = {"edge_configs": {key: {"source_type": "old"}}}
        with patch.object(edge, "fetch_awc_tafs", return_value=({}, "https://awc.test/taf")), \
             patch.object(edge, "fetch_openmeteo_ecmwf_forecast", side_effect=RuntimeError("down")), \
             patch.object(edge, "fetch_wunderground_forecast", side_effect=RuntimeError("down")):
            edge.refresh_edge_configs(state, {"ZSPD": city}, {"ZSPD": "2026-08-22"})
        self.assertNotIn(key, state["edge_configs"])
        self.assertTrue(state["edge_failures"][key].startswith("wu_forecast_unavailable"))

    def test_fahrenheit_bucket_requires_tenths_c_remark_for_candidate_signal(self) -> None:
        city = self.cities["KLGA"]
        state: dict = {
            "edge_configs": {edge.edge_key("new-york-city", "2026-08-22", "high"): {"activation_edge_native": 80.0, "source_type": "taf_tx"}},
            "daily_extrema": {"new-york-city|2026-08-22": {"city_id": "new-york-city", "icao": "KLGA", "market_local_date": "2026-08-22", "market_unit": "F", "high": 80.0, "low": 80.0}},
            "handled_candidate_buckets": {},
        }
        rule = {
            "market_rule_id": "event-high", "city_id": "new-york-city", "market_local_date": "2026-08-22", "direction": "high", "market_unit": "F", "enabled": True,
            "buckets": [{"bucket_id": "80", "lo": 80, "hi": 82, "market_id": "market-80", "no_token_id": "no-80"}],
        }
        event = {"event_id": "f-body", "report_time_utc": "2026-08-22T17:00:00Z", "fetched_at_utc": "2026-08-22T17:03:00Z", "temperature_c": 28, "raw_metar": "METAR KLGA 221700Z 00000KT 10SM 28/20 A3000"}
        signals = edge.evaluate_observation(state, event, city, [rule], 600)
        self.assertIn("f_unit_precision_ambiguous", [item.get("reason") for item in signals])

    def test_correction_is_audit_only_until_full_day_replay_exists(self) -> None:
        city = self.cities["ZSPD"]
        event = {"event_id": "cor", "report_time_utc": "2026-08-22T12:00:00Z", "fetched_at_utc": "2026-08-22T12:01:00Z", "temperature_c": 31, "raw_metar": "METAR ZSPD 221200Z COR 31/24 Q1005", "is_correction": True}
        signals = edge.evaluate_observation({}, event, city, [], 600)
        self.assertEqual(signals[0]["reason"], "correction_requires_full_day_rebuild")

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
            "airport_icao": "ZSPD", "report_time_utc": "2026-08-22T12:00:00Z",
            "fetched_at_utc": "2026-08-22T12:03:00Z", "temperature_c": 29,
            "raw_metar": "METAR ZSPD 221200Z 12007MPS 9999 29/24 Q1005 NOSIG", "event_id": "baseline",
        }
        state: dict = {"edge_configs": {}, "daily_extrema": {}, "handled_candidate_buckets": {}}
        first = edge.evaluate_observation(state, event_base, city, [], 600)
        self.assertEqual(first[0]["reason"], "daily_baseline_initialized")
        local_date = "2026-08-22"
        state["edge_configs"][edge.edge_key(city["city_id"], local_date, "high")] = {"activation_edge_native": 30.0, "source_type": "taf_tx"}
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
