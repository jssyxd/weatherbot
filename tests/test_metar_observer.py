from __future__ import annotations

import json
import sys
import tempfile
import unittest
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

    def test_interval_below_checkwx_cache_window_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps({"scan_interval_seconds": 899, "stations": ["ZSPD"]}), encoding="utf-8")
            with self.assertRaises(ValueError):
                observer.load_config(config_path)

    def test_checkwx_batch_size_above_25_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps({"stations_per_request": 26, "scan_interval_seconds": 900}), encoding="utf-8")
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
        config = {"scan_interval_seconds": 900, "market_rules_max_age_seconds": 1800}
        snapshot = observer.build_health_snapshot(config, {}, {"ZSPD": city})
        self.assertEqual(snapshot["status"], "degraded")
        self.assertFalse(snapshot["llm_in_minute_path"])
        self.assertEqual(snapshot["critical_path"], "deterministic_iana_state_machine_only")
        self.assertEqual(snapshot["untrusted_warmup_count"], 1)

    def test_checkwx_15_minute_scan_interval_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps({"scan_interval_seconds": 900, "stations": ["ZSPD"]}), encoding="utf-8")
            loaded = observer.load_config(config_path)
            self.assertEqual(loaded["scan_interval_seconds"], 900)
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
            config_path.write_text(json.dumps({"scan_interval_seconds": 900, "checkwx_previous_limit": 51}), encoding="utf-8")
            with self.assertRaises(ValueError):
                observer.load_config(config_path)


if __name__ == "__main__":
    unittest.main()
