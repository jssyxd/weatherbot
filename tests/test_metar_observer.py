from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import edge_engine as edge  # noqa: E402
import metar_observer as observer  # noqa: E402


class FakeHTTPResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "FakeHTTPResponse":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        return False


class MetarObserverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cities = edge.load_contract_cities(Path(__file__).resolve().parents[1] / "config/contract_cities.json")

    def test_normalize_checkwx_short_metar(self) -> None:
        record = observer.normalize_report(
            {
                "icao": "ZSPD",
                "raw_text": "METAR ZSPD 221200Z 12007MPS 9999 BKN016 28/26 Q1005 NOSIG",
                "observed": "2026-08-22T12:00:00Z",
            },
            {"ZSPD": "Shanghai Pudong"}, "https://api.checkwx.com/v2/metar/ZSPD/short", "2026-08-22T12:06:00Z",
        )
        assert record is not None
        self.assertEqual(record["airport_icao"], "ZSPD")
        self.assertEqual(record["report_type"], "METAR")
        self.assertEqual(record["temperature_c"], None)
        self.assertEqual(record["report_time_utc"], "2026-08-22T12:00:00Z")
        self.assertEqual(record["checkwx_report_age_seconds"], 360.0)
        self.assertEqual(record["source"], "CheckWX Aviation Weather API v2")
        self.assertNotIn("x-api-key", record["source_endpoint"].lower())

    def test_normalize_checkwx_speci_and_correction(self) -> None:
        record = observer.normalize_report(
            {
                "icao": "OMDB",
                "raw_text": "SPECI OMDB 221230Z COR 33010KT 300V360 CAVOK 42/24 Q0998 NOSIG",
                "observed": "2026-08-22T12:30:00Z",
            },
            {"OMDB": "Dubai"}, "https://api.checkwx.com/v2/metar/OMDB/short", "2026-08-22T12:31:00Z",
        )
        assert record is not None
        self.assertEqual(record["report_type"], "SPECI")
        self.assertTrue(record["is_correction"])

    def test_only_metar_and_speci_are_retained(self) -> None:
        self.assertIsNone(
            observer.normalize_report(
                {"icao": "ZSPD", "raw_text": "TAF ZSPD 221100Z", "observed": "2026-08-22T12:00:00Z"},
                {}, "https://example.test/metar", "2026-08-22T12:06:00Z",
            )
        )

    def test_fetch_checkwx_uses_header_and_short_endpoint(self) -> None:
        response = FakeHTTPResponse(
            {"results": 1, "data": [{"icao": "ZSPD", "raw_text": "METAR ZSPD 221200Z 00000KT 28/24 Q1005", "observed": "2026-08-22T12:00:00Z"}]}
        )
        with patch.dict("os.environ", {"TREE4_TEST_CHECKWX_KEY": "test-only-key"}, clear=False):
            with patch("urllib.request.urlopen", return_value=response) as mocked:
                records, endpoint = observer.fetch_checkwx_reports(["zspd"], "TREE4_TEST_CHECKWX_KEY")
        self.assertEqual(records[0]["icao"], "ZSPD")
        self.assertEqual(endpoint, "https://api.checkwx.com/v2/metar/ZSPD/short")
        request = mocked.call_args.args[0]
        self.assertEqual(request.get_header("X-api-key"), "test-only-key")
        self.assertNotIn("test-only-key", request.full_url)

    def test_fetch_checkwx_rejects_invalid_response_shape(self) -> None:
        response = FakeHTTPResponse({"results": 1, "data": []})
        with patch.dict("os.environ", {"TREE4_TEST_CHECKWX_KEY": "test-only-key"}, clear=False):
            with patch("urllib.request.urlopen", return_value=response):
                with self.assertRaisesRegex(RuntimeError, "results 与 data 长度不一致"):
                    observer.fetch_checkwx_reports(["ZSPD"], "TREE4_TEST_CHECKWX_KEY")

    def test_checkwx_429_uses_retry_after_header(self) -> None:
        error = urllib.error.HTTPError(
            "https://api.checkwx.com/v2/metar/ZSPD/short", 429, "Too Many Requests", {"Retry-After": "120"}, None,
        )
        with patch.dict("os.environ", {"TREE4_TEST_CHECKWX_KEY": "test-only-key"}, clear=False):
            with patch("urllib.request.urlopen", side_effect=error):
                with self.assertRaises(observer.CheckWXRateLimitError) as raised:
                    observer.fetch_checkwx_reports(["ZSPD"], "TREE4_TEST_CHECKWX_KEY")
        self.assertEqual(raised.exception.retry_after_seconds, 120)

    def test_aviationweather_history_normalizes_raw_observation(self) -> None:
        response = FakeHTTPResponse([
            {"icaoId": "ZSPD", "rawOb": "METAR ZSPD 221200Z 00000KT 9999 28/24 Q1005", "obsTime": "2026-08-22T12:00:00Z"}
        ])
        with patch("aviationweather_warmup.urllib.request.urlopen", return_value=response) as mocked:
            from aviationweather_warmup import fetch_aviationweather_history
            records, endpoint = fetch_aviationweather_history(["ZSPD"], hours=48)
        self.assertEqual(records[0]["icao"], "ZSPD")
        self.assertTrue(records[0]["raw_text"].startswith("METAR ZSPD"))
        self.assertEqual(records[0]["observed"], "2026-08-22T12:00:00Z")
        self.assertIn("hours=48", endpoint)
        self.assertEqual(mocked.call_args.args[0].get_header("User-agent"), "weatherbot-tree5/5.1 (warmup; contact=repository-issues)")

    def test_warmup_auto_falls_back_only_for_checkwx_missing_city(self) -> None:
        shanghai = self.cities["ZSPD"]
        los_angeles = self.cities["KLAX"]
        fixed_now = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
        config = {
            "warmup_retry_seconds": 60, "warmup_stations_per_request": 25, "checkwx_api_key_env": "TREE4_TEST_CHECKWX_KEY",
            "checkwx_previous_limit": 50, "warmup_source": "auto", "aviationweather_warmup_hours": 48,
            "aviationweather_warmup_stations_per_request": 8,
        }
        primary = [{"icao": "ZSPD", "raw_text": "METAR ZSPD 220400Z 00000KT 9999 28/24 Q1005", "observed": "2026-08-22T04:00:00Z"}]
        fallback = [{"icao": "KLAX", "raw_text": "METAR KLAX 221100Z 00000KT 10SM 20/12 A2992", "observed": "2026-08-22T11:00:00Z"}]
        state: dict = {}
        with patch("metar_observer.utc_now", return_value=fixed_now):
            with patch("metar_observer.fetch_checkwx_reports", return_value=(primary, "https://checkwx.test/previous")) as primary_mock:
                with patch("metar_observer.fetch_aviationweather_history", return_value=(fallback, "https://aviationweather.test/history")) as fallback_mock:
                    summary = observer.warm_up_current_local_days(config, state, {"ZSPD": shanghai, "KLAX": los_angeles})
        self.assertEqual(summary["complete"], 2)
        self.assertEqual(summary["fallback_city_count"], 1)
        self.assertEqual(primary_mock.call_count, 1)
        self.assertEqual(fallback_mock.call_args.args[0], ["KLAX"])
        self.assertEqual(state["daily_warmup"]["shanghai|2026-08-22"]["warmup_source"], "checkwx_previous")
        self.assertEqual(state["daily_warmup"]["los-angeles|2026-08-22"]["warmup_source"], "aviationweather")

    def test_realtime_fallback_only_requests_checkwx_missing_city(self) -> None:
        config = {
            "stations_per_request": 25, "checkwx_api_key_env": "TREE4_TEST_CHECKWX_KEY",
            "aviationweather_realtime_fallback_enabled": True, "aviationweather_realtime_fallback_hours": 2,
            "aviationweather_realtime_fallback_stations_per_request": 8,
        }
        primary = [{"icao": "ZSPD", "raw_text": "METAR ZSPD 221200Z 00000KT 9999 28/24 Q1005", "observed": "2026-08-22T12:00:00Z"}]
        fallback = [{"icao": "KLAX", "raw_text": "METAR KLAX 221200Z 00000KT 10SM 20/12 A2992", "observed": "2026-08-22T12:00:00Z"}]
        with patch("metar_observer.fetch_checkwx_reports", return_value=(primary, "https://checkwx.test/short")):
            with patch("metar_observer.fetch_aviationweather_history", return_value=(fallback, "https://aviationweather.test/realtime")) as fallback_mock:
                result = observer.fetch_realtime_weather_reports(config, ["ZSPD", "KLAX"])
        self.assertEqual(result["fallback_icaos"], ["KLAX"])
        self.assertEqual(result["fallback_success_icaos"], ["KLAX"])
        self.assertEqual(fallback_mock.call_args.args[0], ["KLAX"])
        self.assertEqual({item["source"] for item in result["reports"]}, {"CheckWX Aviation Weather API v2", "AviationWeather.gov Data API (CheckWX fallback)"})

    def test_checkwx_key_must_exist_in_environment(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "未设置 CheckWX API 密钥环境变量"):
            observer.fetch_checkwx_reports(["ZSPD"], "TREE4_MISSING_CHECKWX_API_KEY")

    def test_checkwx_icao_request_limit_is_enforced(self) -> None:
        station_ids = [f"A{i:03d}" for i in range(observer.CHECKWX_MAX_ICAOS_PER_REQUEST + 1)]
        with self.assertRaises(ValueError):
            observer.fetch_checkwx_reports(station_ids, "TREE4_MISSING_CHECKWX_API_KEY")

    def test_station_configuration_deduplicates_icao(self) -> None:
        stations = observer.normalize_stations([{"icao": "zspd", "name": "Shanghai Pudong"}, "ZSPD", "RCTP"])
        self.assertEqual(stations, [{"icao": "ZSPD", "name": "Shanghai Pudong"}, {"icao": "RCTP", "name": "RCTP"}])

    def test_interval_below_one_minute_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps({"scan_interval_seconds": 59, "stations": ["ZSPD"]}), encoding="utf-8")
            with self.assertRaises(ValueError):
                observer.load_config(config_path)

    def test_checkwx_batch_size_above_25_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps({"stations_per_request": 26, "scan_interval_seconds": 60}), encoding="utf-8")
            with self.assertRaises(ValueError):
                observer.load_config(config_path)

    def test_warmup_rebuild_uses_observed_time_iana_day(self) -> None:
        city = self.cities["ZSPD"]
        reports = [
            {
                "icao": "ZSPD", "raw_text": "METAR ZSPD 221559Z 00000KT 9999 30/24 Q1005",
                "observed": "2026-08-22T15:59:00Z",
            },
            {
                "icao": "ZSPD", "raw_text": "SPECI ZSPD 221600Z 00000KT 9999 31/24 Q1005",
                "observed": "2026-08-22T16:00:00Z",
            },
        ]
        state: dict = {}
        summary = observer._rebuild_daily_extrema_from_history(
            state, {"ZSPD": city}, reports, "2026-08-22T16:05:00Z", "https://checkwx.test/metar", {"ZSPD": "2026-08-22"},
        )
        key = "shanghai|2026-08-22"
        self.assertEqual(summary, {"complete": 1, "missing_current_local_day_reports": 0})
        self.assertEqual(state["daily_extrema"][key]["high"], 30.0)
        self.assertEqual(state["daily_warmup"][key]["status"], "complete")
        self.assertEqual(state["daily_extrema"][key]["warmup_latest_report_time_utc"], "2026-08-22T15:59:00Z")

    def test_warmup_fetches_only_icao_codes_still_due(self) -> None:
        shanghai = self.cities["ZSPD"]
        los_angeles = self.cities["KLAX"]
        fixed_now = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
        state = {
            "daily_warmup": {
                "shanghai|2026-08-22": {
                    "status": "complete", "market_local_date": "2026-08-22",
                }
            }
        }
        config = {
            "warmup_retry_seconds": 60,
            "warmup_stations_per_request": 25,
            "checkwx_api_key_env": "TREE4_TEST_CHECKWX_KEY",
            "checkwx_previous_limit": 50,
            "warmup_source": "checkwx",
        }
        with patch("metar_observer.utc_now", return_value=fixed_now):
            with patch("metar_observer.fetch_checkwx_reports", return_value=([], "https://checkwx.test/history")) as mocked:
                observer.warm_up_current_local_days(state=state, config=config, cities={"ZSPD": shanghai, "KLAX": los_angeles})
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(mocked.call_args.args[0], ["KLAX"])
        self.assertEqual(mocked.call_args.kwargs["previous_limit"], 50)

    def test_warmup_fails_closed_when_current_iana_day_has_no_report(self) -> None:
        city = self.cities["ZSPD"]
        state: dict = {"daily_extrema": {"shanghai|2026-08-23": {"high": 99}}}
        summary = observer._rebuild_daily_extrema_from_history(
            state, {"ZSPD": city}, [], "2026-08-22T16:05:00Z", "https://checkwx.test/metar", {"ZSPD": "2026-08-23"},
        )
        self.assertEqual(summary, {"complete": 0, "missing_current_local_day_reports": 1})
        self.assertNotIn("shanghai|2026-08-23", state["daily_extrema"])
        self.assertFalse(observer._warmup_is_complete(state, city, "2026-08-23"))
        self.assertEqual(state["daily_warmup"]["shanghai|2026-08-23"]["status"], "failed_no_current_local_day_reports")

    def test_health_snapshot_is_degraded_when_iana_warmup_is_missing(self) -> None:
        city = self.cities["ZSPD"]
        config = {"scan_interval_seconds": 60, "market_rules_max_age_seconds": 1800}
        snapshot = observer.build_health_snapshot(config, {}, {"ZSPD": city})
        self.assertEqual(snapshot["status"], "degraded")
        self.assertFalse(snapshot["llm_in_minute_path"])
        self.assertEqual(snapshot["critical_path"], "deterministic_iana_state_machine_only")
        self.assertEqual(snapshot["untrusted_warmup_count"], 1)

    def test_two_minute_scan_interval_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps({"scan_interval_seconds": 120, "stations": ["ZSPD"]}), encoding="utf-8")
            loaded = observer.load_config(config_path)
            self.assertEqual(loaded["scan_interval_seconds"], 120)
            self.assertEqual(loaded["rate_limit_backoff_seconds"], 120)
            self.assertEqual(loaded["stations_per_request"], 25)

    def test_single_instance_lock_is_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            first = observer.acquire_single_instance_lock(state_path)
            try:
                with self.assertRaises(RuntimeError):
                    observer.acquire_single_instance_lock(state_path)
            finally:
                first.close()

    def test_checkwx_previous_limit_must_be_supported_range(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps({"scan_interval_seconds": 60, "checkwx_previous_limit": 51}), encoding="utf-8")
            with self.assertRaises(ValueError):
                observer.load_config(config_path)

    def test_tree11_records_taf_versions_on_two_minute_schedule_without_orders(self) -> None:
        city = {"city_id": "shanghai", "icao": "ZSSS", "timezone": "Asia/Shanghai", "market_unit": "C", "market_city_slug": "shanghai"}
        config = {"tree11_enabled": True, "tree11_taf_poll_seconds": 120, "stations_per_request": 25, "checkwx_api_key_env": "TEST_KEY"}
        state: dict = {}
        now = datetime(2026, 8, 27, 2, 0, tzinfo=timezone.utc)
        report = {"icao": "ZSSS", "issued": "2026-08-27T00:00:00Z", "raw_text": "TAF ZSSS 270000Z 2700/2806 TX30/2710Z TN27/2704Z"}
        with patch.object(observer, "fetch_checkwx_taf_reports", return_value=([report], "https://example.test/taf")) as mocked:
            actions = observer.process_tree11_taf_versions(config, state, {"ZSSS": city}, now, 100)
            repeated = observer.process_tree11_taf_versions(config, state, {"ZSSS": city}, now, 101)
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(sum(item["status"] == "recorded" for item in actions), 2)
        self.assertEqual(repeated, [])
        versions = state["tree11"]["taf_versions"]
        self.assertEqual(len(versions["shanghai|2026-08-27|high"]), 1)
        self.assertEqual(len(versions["shanghai|2026-08-27|low"]), 1)
        self.assertEqual(actions[0]["safety"]["orders_submitted"], 0)

    def test_tree11_taf_poll_below_two_minutes_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps({"scan_interval_seconds": 120, "tree11_taf_poll_seconds": 119}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "tree11_taf_poll_seconds"):
                observer.load_config(config_path)


if __name__ == "__main__":
    unittest.main()
