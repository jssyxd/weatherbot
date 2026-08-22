from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import metar_observer as observer  # noqa: E402


class MetarObserverTests(unittest.TestCase):
    def test_normalize_metar_with_valid_delay(self) -> None:
        record = observer.normalize_report(
            {
                "icaoId": "ZSPD",
                "metarType": "METAR",
                "reportTime": "2026-08-22T12:00:00.000Z",
                "receiptTime": "2026-08-22T12:05:16.281Z",
                "temp": 28,
                "rawOb": "METAR ZSPD 221200Z 12007MPS 9999 BKN016 28/26 Q1005 NOSIG",
            },
            {"ZSPD": "Shanghai Pudong"},
            "https://example.test/metar",
            "2026-08-22T12:06:00Z",
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
                "icaoId": "OMDB",
                "metarType": "METAR",
                "reportTime": "2026-08-22T12:30:00.000Z",
                "receiptTime": "2026-08-22T12:28:39.000Z",
                "rawOb": "METAR OMDB 221230Z 33010KT 300V360 CAVOK 42/24 Q0998 NOSIG",
            },
            {"OMDB": "Dubai"},
            "https://example.test/metar",
            "2026-08-22T12:31:00Z",
        )
        assert record is not None
        self.assertIsNone(record["awc_receipt_delay_seconds"])
        self.assertEqual(record["awc_receipt_delay_status"], "source_time_inconsistent")

    def test_only_metar_and_speci_are_retained(self) -> None:
        self.assertIsNone(
            observer.normalize_report(
                {"icaoId": "ZSPD", "metarType": "TAF", "rawOb": "TAF ZSPD 221100Z"},
                {},
                "https://example.test/metar",
                "2026-08-22T12:06:00Z",
            )
        )

    def test_station_configuration_deduplicates_icao(self) -> None:
        stations = observer.normalize_stations([
            {"icao": "zspd", "name": "Shanghai Pudong"},
            "ZSPD",
            "RCTP",
        ])
        self.assertEqual(stations, [
            {"icao": "ZSPD", "name": "Shanghai Pudong"},
            {"icao": "RCTP", "name": "RCTP"},
        ])

    def test_interval_below_one_minute_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps({
                "scan_interval_seconds": 59,
                "stations": ["ZSPD"],
            }), encoding="utf-8")
            with self.assertRaises(ValueError):
                observer.load_config(config_path)


if __name__ == "__main__":
    unittest.main()
