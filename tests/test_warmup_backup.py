"""Tests for the warm-up backup data source chain (NOAA / Open-Meteo).

Covers: NOAA normalization, Open-Meteo temperature_native path, the
warmup_min_obs gate, direction time gates, the source fallback chain, and
config validation. No real network calls and no orders are involved.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from metar_observer import (
    DEFAULT_WARMUP_MIN_OBS,
    fetch_warmup_history,
    load_config,
    warm_up_current_local_days,
)
from metar_observer import _rebuild_daily_extrema_from_history

try:
    import edge_engine
except ImportError:  # pragma: no cover
    edge_engine = None

CITIES = {
    "ZSPD": {
        "city_id": "shanghai", "name": "Shanghai Pudong", "icao": "ZSPD",
        "timezone": "Asia/Shanghai", "market_unit": "C", "latitude": 31.1443, "longitude": 121.8083,
    },
    "KLAX": {
        "city_id": "los-angeles", "name": "Los Angeles", "icao": "KLAX",
        "timezone": "America/Los_Angeles", "market_unit": "F", "latitude": 33.9425, "longitude": -118.4081,
    },
}


class NoaaNormalizationTests(unittest.TestCase):
    def test_normalizes_rawob_with_leading_icao(self) -> None:
        payload = json.dumps([{
            "icaoId": "ZSPD", "rawOb": "ZSPD 221600Z 00000KT 9999 31/24 Q1005",
            "obsTimeUtc": "2026-08-22T16:00:00Z",
        }])
        with patch("urllib.request.urlopen") as mocked:
            import io
            response = mocked.return_value.__enter__.return_value
            response.read.return_value = payload.encode("utf-8")
            response.status = 200
            from metar_observer import fetch_noaa_warmup_reports
            reports, endpoint = fetch_noaa_warmup_reports(["ZSPD"])
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["icao"], "ZSPD")
        self.assertTrue(reports[0]["raw_text"].startswith("METAR ZSPD"))
        self.assertEqual(reports[0]["observed"], "2026-08-22T16:00:00Z")
        self.assertIn("hours=48", endpoint)

    def test_keeps_existing_metar_prefix(self) -> None:
        payload = json.dumps([{
            "icaoId": "KLAX", "rawOb": "METAR KLAX 221600Z 00000KT 10SM 25/18 A3005",
            "obsTimeUtc": "2026-08-22T16:00:00Z",
        }])
        with patch("urllib.request.urlopen") as mocked:
            import io
            response = mocked.return_value.__enter__.return_value
            response.read.return_value = payload.encode("utf-8")
            from metar_observer import fetch_noaa_warmup_reports
            reports, _ = fetch_noaa_warmup_reports(["KLAX"])
        self.assertEqual(reports[0]["raw_text"], "METAR KLAX 221600Z 00000KT 10SM 25/18 A3005")


class WarmupMinObsTests(unittest.TestCase):
    def test_insufficient_obs_never_marks_complete(self) -> None:
        city = CITIES["ZSPD"]
        reports = [{
            "icao": "ZSPD", "raw_text": "METAR ZSPD 221559Z 00000KT 9999 30/24 Q1005",
            "observed": "2026-08-22T15:59:00Z",
        }]
        state: dict = {}
        summary = _rebuild_daily_extrema_from_history(
            state, {"ZSPD": city}, reports, "2026-08-22T16:05:00Z", "https://noaa.test/metar",
            {"ZSPD": "2026-08-22"}, warmup_min_obs=DEFAULT_WARMUP_MIN_OBS, warmup_source="noaa",
        )
        self.assertEqual(summary["complete"], 0)
        self.assertEqual(summary["insufficient_obs"], 1)
        key = "shanghai|2026-08-22"
        self.assertNotIn(key, state["daily_extrema"])
        self.assertEqual(state["daily_warmup"][key]["status"], "incomplete_insufficient_obs")
        self.assertEqual(state["daily_warmup"][key]["warmup_source"], "noaa")

    def test_openmeteo_converts_celsius_to_market_unit(self) -> None:
        """Open-Meteo support was removed by user decision; this test is pruned."""
        self.skipTest("Open-Meteo removed (deterministic sources only)")

    def test_openmeteo_temperature_native_is_used(self) -> None:
        """Open-Meteo support was removed by user decision; this test is pruned."""
        self.skipTest("Open-Meteo removed (deterministic sources only)")


class DirectionGateTests(unittest.TestCase):
    def setUp(self) -> None:
        if edge_engine is None:
            self.skipTest("edge_engine unavailable")

    def test_high_direction_gated_before_local_noon(self) -> None:
        from edge_engine import evaluate_observation
        state: dict = {
            "daily_extrema": {
                "los-angeles|2026-08-22": {
                    "city_id": "los-angeles", "icao": "KLAX", "market_local_date": "2026-08-22",
                    "market_unit": "F", "high": 75.0, "low": 60.0, "initialized_by_event_id": "baseline",
                }
            }
        }
        event = {
            "event_id": "KLAX|METAR|2026-08-22T18:00:00Z|X",
            "report_time_utc": "2026-08-22T18:00:00Z",  # 11:00 local (PDT, UTC-7)
            "fetched_at_utc": "2026-08-22T18:01:00Z",
            "temperature_native": 80.0,
            "raw_metar": "METAR KLAX 221800Z 00000KT 10SM 27/18 A3005",
            "icao": "KLAX", "airport_icao": "KLAX", "is_correction": False,
        }
        signals = evaluate_observation(
            state, event, CITIES["KLAX"], [], 900,
            warmup_high_gate_local_hour=12, warmup_low_gate_local_hour=6,
        )
        reasons = [s["reason"] for s in signals]
        self.assertIn("warmup_time_gate_high", reasons)
        # The extrema update still happens (data keeps accumulating).
        # 27°C parsed from the raw METAR body -> 80.6°F in the F unit market.
        self.assertEqual(state["daily_extrema"]["los-angeles|2026-08-22"]["high"], 80.6)

    def test_low_direction_gated_before_local_6am(self) -> None:
        from edge_engine import evaluate_observation
        state: dict = {
            "daily_extrema": {
                "los-angeles|2026-08-22": {
                    "city_id": "los-angeles", "icao": "KLAX", "market_local_date": "2026-08-22",
                    "market_unit": "F", "high": 75.0, "low": 60.0, "initialized_by_event_id": "baseline",
                }
            }
        }
        event = {
            "event_id": "KLAX|SPECI|2026-08-22T12:30:00Z|X",
            "report_time_utc": "2026-08-22T12:30:00Z",  # 05:30 local
            "fetched_at_utc": "2026-08-22T12:31:00Z",
            "temperature_native": 58.0,
            "raw_metar": "SPECI KLAX 221230Z 00000KT 10SM 14/12 A3005",
            "icao": "KLAX", "airport_icao": "KLAX", "is_correction": False,
        }
        signals = evaluate_observation(
            state, event, CITIES["KLAX"], [], 900,
            warmup_high_gate_local_hour=12, warmup_low_gate_local_hour=6,
        )
        reasons = [s["reason"] for s in signals]
        self.assertIn("warmup_time_gate_low", reasons)

    def test_gate_disabled_when_none(self) -> None:
        from edge_engine import evaluate_observation
        state: dict = {
            "daily_extrema": {
                "los-angeles|2026-08-22": {
                    "city_id": "los-angeles", "icao": "KLAX", "market_local_date": "2026-08-22",
                    "market_unit": "F", "high": 75.0, "low": 60.0, "initialized_by_event_id": "baseline",
                }
            }
        }
        event = {
            "event_id": "KLAX|METAR|2026-08-22T18:00:00Z|X",
            "report_time_utc": "2026-08-22T18:00:00Z",
            "fetched_at_utc": "2026-08-22T18:01:00Z",
            "temperature_native": 80.0,
            "raw_metar": "METAR KLAX 221800Z 00000KT 10SM 27/18 A3005",
            "icao": "KLAX", "airport_icao": "KLAX", "is_correction": False,
        }
        signals = evaluate_observation(state, event, CITIES["KLAX"], [], 900)
        reasons = [s["reason"] for s in signals]
        self.assertNotIn("warmup_time_gate_high", reasons)
        self.assertIn("no_dead_high_bucket_in_market_rules", reasons)


class FallbackChainTests(unittest.TestCase):
    def test_auto_chain_falls_back_to_noaa_when_checkwx_empty(self) -> None:
        config = {
            "warmup_source": "auto",
            "warmup_stations_per_request": 25,
            "checkwx_api_key_env": "TREE4_TEST_CHECKWX_KEY",
            "checkwx_previous_limit": 50,
        }
        noaa_report = {
            "icao": "ZSPD", "raw_text": "METAR ZSPD 221559Z 00000KT 9999 30/24 Q1005",
            "observed": "2026-08-22T15:59:00Z",
        }
        with patch("metar_observer.fetch_checkwx_reports", return_value=([], "https://checkwx.test/history")) as ck, \
             patch("metar_observer.fetch_noaa_warmup_reports", return_value=([noaa_report], "https://noaa.test/metar")) as n:
            reports, endpoint, source = fetch_warmup_history(config, [CITIES["ZSPD"]])
        self.assertEqual(source, "noaa")
        self.assertEqual(len(reports), 1)
        ck.assert_called_once()
        n.assert_called_once()

    def test_auto_chain_uses_checkwx_first_when_reports_exist(self) -> None:
        config = {
            "warmup_source": "auto",
            "warmup_stations_per_request": 25,
            "checkwx_api_key_env": "TREE4_TEST_CHECKWX_KEY",
            "checkwx_previous_limit": 50,
        }
        ck_report = {"icao": "ZSPD", "raw_text": "METAR ZSPD 221559Z 00000KT 9999 30/24 Q1005",
                     "observed": "2026-08-22T15:59:00Z"}
        with patch("metar_observer.fetch_checkwx_reports", return_value=([ck_report], "https://checkwx.test/history")) as ck, \
             patch("metar_observer.fetch_noaa_warmup_reports") as n:
            reports, endpoint, source = fetch_warmup_history(config, [CITIES["ZSPD"]])
        self.assertEqual(source, "checkwx")
        self.assertEqual(len(reports), 1)
        n.assert_not_called()

    def test_explicit_noaa_source_skips_checkwx(self) -> None:
        config = {
            "warmup_source": "noaa",
            "warmup_stations_per_request": 25,
            "checkwx_api_key_env": "TREE4_TEST_CHECKWX_KEY",
            "checkwx_previous_limit": 50,
        }
        noaa_report = {
            "icao": "ZSPD", "raw_text": "METAR ZSPD 221559Z 00000KT 9999 30/24 Q1005",
            "observed": "2026-08-22T15:59:00Z",
        }
        with patch("metar_observer.fetch_checkwx_reports") as ck, \
             patch("metar_observer.fetch_noaa_warmup_reports", return_value=([noaa_report], "https://noaa.test/metar")) as n:
            reports, endpoint, source = fetch_warmup_history(config, [CITIES["ZSPD"]])
        self.assertEqual(source, "noaa")
        self.assertEqual(len(reports), 1)
        ck.assert_not_called()
        n.assert_called_once()


class ConfigValidationTests(unittest.TestCase):
    def test_invalid_warmup_source_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps({
                "scan_interval_seconds": 60, "warmup_source": "bogus",
            }), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config(config_path)

    def test_warmup_min_obs_rejected_below_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps({
                "scan_interval_seconds": 60, "warmup_min_obs": 0,
            }), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config(config_path)

    def test_gate_hours_out_of_range_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps({
                "scan_interval_seconds": 60, "warmup_high_gate_local_hour": 24,
            }), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config(config_path)


if __name__ == "__main__":
    unittest.main()
