from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import edge_engine as edge  # noqa: E402
import metar_observer as observer  # noqa: E402


class MetarObserverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cities = edge.load_contract_cities(Path(__file__).resolve().parents[1] / "config/contract_cities.json")

    def test_normalize_metar_with_valid_delay(self) -> None:
        record = observer.normalize_report(
            {
                "icaoId": "ZSPD", "metarType": "METAR", "reportTime": "2026-08-22T12:00:00.000Z",
                "receiptTime": "2026-08-22T12:05:16.281Z", "temp": 28,
                "rawOb": "METAR ZSPD 221200Z 12007MPS 9999 BKN016 28/26 Q1005 NOSIG",
            },
            {"ZSPD": "Shanghai Pudong"}, "https://example.test/metar", "2026-08-22T12:06:00Z",
        )
        assert record is not None
        self.assertEqual(record["airport_icao"], "ZSPD")
        self.assertEqual(record["report_type"], "METAR")
        self.assertEqual(record["temperature_c"], 28)
        self.assertEqual(record["awc_receipt_delay_status"], "available")
        self.assertEqual(record["awc_receipt_delay_seconds"], 316.281)

    def test_inconsistent_source_time_is_not_a_negative_delay(self) -> None:
        record = observer.normalize_report(
            {
                "icaoId": "OMDB", "metarType": "METAR", "reportTime": "2026-08-22T12:30:00.000Z",
                "receiptTime": "2026-08-22T12:28:39.000Z", "rawOb": "METAR OMDB 221230Z 33010KT 300V360 CAVOK 42/24 Q0998 NOSIG",
            },
            {"OMDB": "Dubai"}, "https://example.test/metar", "2026-08-22T12:31:00Z",
        )
        assert record is not None
        self.assertIsNone(record["awc_receipt_delay_seconds"])
        self.assertEqual(record["awc_receipt_delay_status"], "source_time_inconsistent")

    def test_only_metar_and_speci_are_retained(self) -> None:
        self.assertIsNone(
            observer.normalize_report(
                {"icaoId": "ZSPD", "metarType": "TAF", "rawOb": "TAF ZSPD 221100Z"},
                {}, "https://example.test/metar", "2026-08-22T12:06:00Z",
            )
        )

    def test_station_configuration_deduplicates_icao(self) -> None:
        stations = observer.normalize_stations([{"icao": "zspd", "name": "Shanghai Pudong"}, "ZSPD", "RCTP"])
        self.assertEqual(stations, [{"icao": "ZSPD", "name": "Shanghai Pudong"}, {"icao": "RCTP", "name": "RCTP"}])

    def test_interval_below_one_minute_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps({"scan_interval_seconds": 59, "stations": ["ZSPD"]}), encoding="utf-8")
            with self.assertRaises(ValueError):
                observer.load_config(config_path)

    def test_warmup_rebuild_uses_report_time_iana_day_not_receipt_or_fetch_time(self) -> None:
        city = self.cities["ZSPD"]
        reports = [
            {
                "icaoId": "ZSPD", "metarType": "METAR", "reportTime": "2026-08-22T15:59:00Z",
                "receiptTime": "2026-08-22T16:04:00Z", "temp": 30,
                "rawOb": "METAR ZSPD 221559Z 00000KT 9999 30/24 Q1005",
            },
            {
                "icaoId": "ZSPD", "metarType": "SPECI", "reportTime": "2026-08-22T16:00:00Z",
                "receiptTime": "2026-08-22T16:04:00Z", "temp": 31,
                "rawOb": "SPECI ZSPD 221600Z 00000KT 9999 31/24 Q1005",
            },
        ]
        state: dict = {}
        summary = observer._rebuild_daily_extrema_from_history(
            state, {"ZSPD": city}, reports, "2026-08-22T16:05:00Z", "https://awc.test/metar", {"ZSPD": "2026-08-22"},
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
            state, {"ZSPD": city}, [], "2026-08-22T16:05:00Z", "https://awc.test/metar", {"ZSPD": "2026-08-23"},
        )
        self.assertEqual(summary, {"complete": 0, "missing_current_local_day_reports": 1})
        self.assertNotIn("shanghai|2026-08-23", state["daily_extrema"])
        self.assertFalse(observer._warmup_is_complete(state, city, "2026-08-23"))
        self.assertEqual(state["daily_warmup"]["shanghai|2026-08-23"]["status"], "failed_no_current_local_day_reports")

    def test_health_snapshot_is_degraded_when_iana_warmup_is_missing(self) -> None:
        city = self.cities["ZSPD"]
        config = {
            "scan_interval_seconds": 60, "edge_config_max_age_seconds": 1800,
            "market_rules_max_age_seconds": 1800,
        }
        snapshot = observer.build_health_snapshot(config, {}, {"ZSPD": city})
        self.assertEqual(snapshot["status"], "degraded")
        self.assertFalse(snapshot["llm_in_minute_path"])
        self.assertEqual(snapshot["critical_path"], "deterministic_iana_state_machine_only")
        self.assertEqual(snapshot["untrusted_warmup_count"], 1)

    def test_warmup_history_window_must_cover_ianna_fallback_day(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps({"warmup_history_hours": 24}), encoding="utf-8")
            with self.assertRaises(ValueError):
                observer.load_config(config_path)


if __name__ == "__main__":
    unittest.main()
