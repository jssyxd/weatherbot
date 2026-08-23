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
        self.assertEqual(self.cities["ZSPD"]["latitude"], 31.146)
        self.assertEqual(self.cities["ZSPD"]["longitude"], 121.8)
        self.assertEqual(self.cities["ZSPD"]["coordinate_source"], "AviationWeather.gov Data API METAR JSON station fields")

    def test_local_market_date_uses_airport_timezone(self) -> None:
        moment = datetime(2026, 8, 22, 16, 0, tzinfo=timezone.utc)
        self.assertEqual(edge.local_market_date(moment, self.cities["ZSPD"]), "2026-08-23")
        self.assertEqual(edge.local_market_date(moment, self.cities["KLGA"]), "2026-08-22")

    def test_local_midnight_boundary_16z_is_next_day_for_utc_plus_8(self) -> None:
        # Shanghai local 00:00 == 16:00Z; a report stamped exactly 16:00Z must
        # belong to the NEW local day, and 15:59Z to the previous one.
        city = self.cities["ZSPD"]
        self.assertEqual(edge.local_market_date(datetime(2026, 8, 22, 16, 0, tzinfo=timezone.utc), city), "2026-08-23")
        self.assertEqual(edge.local_market_date(datetime(2026, 8, 22, 15, 59, tzinfo=timezone.utc), city), "2026-08-22")
        # Wellington UTC+12: local 00:00 == 12:00Z previous day.
        wlg = self.cities["NZWN"]
        self.assertEqual(edge.local_market_date(datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc), wlg), "2026-08-23")
        self.assertEqual(edge.local_market_date(datetime(2026, 8, 22, 11, 59, tzinfo=timezone.utc), wlg), "2026-08-22")

    def test_first_report_of_local_day_only_initializes_baseline(self) -> None:
        city = self.cities["ZSPD"]
        event = {
            "event_id": "first", "report_time_utc": "2026-08-22T12:00:00Z",
            "fetched_at_utc": "2026-08-22T12:03:00Z", "temperature_c": 29,
            "raw_metar": "METAR ZSPD 221200Z 12007MPS 9999 29/24 Q1005 NOSIG",
        }
        state: dict = {"handled_candidate_buckets": {}}
        signals = edge.evaluate_observation(state, event, city, [], 900)
        self.assertEqual(signals[0]["reason"], "daily_baseline_initialized")
        self.assertNotIn("candidate_no_signal", [item["signal_type"] for item in signals])
        self.assertEqual(state["daily_extrema"]["shanghai|2026-08-22"]["high"], 29.0)

    def test_new_high_kills_previous_extreme_high_bucket(self) -> None:
        city = self.cities["ZSPD"]
        state: dict = {
            "daily_extrema": {"shanghai|2026-08-22": {
                "city_id": "shanghai", "icao": "ZSPD", "market_local_date": "2026-08-22",
                "market_unit": "C", "high": 24.0, "low": 24.0,
            }},
            "handled_candidate_buckets": {},
        }
        rules = [{
            "market_rule_id": "event-high", "city_id": "shanghai", "market_local_date": "2026-08-22",
            "direction": "high", "market_unit": "C", "enabled": True,
            "buckets": [
                {"bucket_id": "24", "lo": 24, "hi": 25, "no_token_id": "no-24"},
                {"bucket_id": "25", "lo": 25, "hi": 26, "no_token_id": "no-25"},
            ],
        }]
        # 24 -> 25: "highest 24°C" bucket [24,25) is impossible -> buy NO on 24.
        # 14:00Z == Shanghai local 22:00 on 8/22 (16:00Z is the midnight edge).
        event = {
            "event_id": "break", "report_time_utc": "2026-08-22T14:00:00Z",
            "fetched_at_utc": "2026-08-22T14:10:00Z", "temperature_c": 25,
            "raw_metar": "METAR ZSPD 221400Z 12007MPS 9999 25/24 Q1005 NOSIG",
            "receipt_time_utc": "2026-08-22T14:08:08Z",
        }
        signals = edge.evaluate_observation(state, event, city, rules, 900)
        candidates = [item for item in signals if item["signal_type"] == "candidate_no_signal"]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["direction"], "high")
        self.assertEqual(candidates[0]["bucket"]["bucket_id"], "24")
        self.assertEqual(candidates[0]["previous_candidate_extreme"], 24.0)
        self.assertEqual(state["daily_extrema"]["shanghai|2026-08-22"]["high"], 25.0)

    def test_new_low_kills_previous_extreme_low_bucket(self) -> None:
        city = self.cities["ZSPD"]
        state: dict = {
            "daily_extrema": {"shanghai|2026-08-22": {
                "city_id": "shanghai", "icao": "ZSPD", "market_local_date": "2026-08-22",
                "market_unit": "C", "high": 13.0, "low": 13.0,
            }},
            "handled_candidate_buckets": {},
        }
        rules = [{
            "market_rule_id": "event-low", "city_id": "shanghai", "market_local_date": "2026-08-22",
            "direction": "low", "market_unit": "C", "enabled": True,
            "buckets": [
                {"bucket_id": "12", "lo": 12, "hi": 13, "no_token_id": "no-12"},
                {"bucket_id": "13", "lo": 13, "hi": 14, "no_token_id": "no-13"},
            ],
        }]
        # 13 -> 11: BOTH [13,14) and [12,13) are newly dead (11 < lo for both
        # while previous 13 was still >= lo) -> buy NO on both buckets.
        event = {
            "event_id": "cold", "report_time_utc": "2026-08-22T14:00:00Z",
            "fetched_at_utc": "2026-08-22T14:10:00Z", "temperature_c": 11,
            "raw_metar": "METAR ZSPD 221400Z 12007MPS 9999 11/09 Q1005 NOSIG",
            "receipt_time_utc": "2026-08-22T14:08:08Z",
        }
        signals = edge.evaluate_observation(state, event, city, rules, 900)
        candidates = [item for item in signals if item["signal_type"] == "candidate_no_signal"]
        self.assertEqual([item["bucket"]["bucket_id"] for item in candidates], ["12", "13"])
        self.assertEqual([item["direction"] for item in candidates], ["low", "low"])
        self.assertEqual(state["daily_extrema"]["shanghai|2026-08-22"]["low"], 11.0)

    def test_multidegree_jump_kills_all_newly_invalidated_buckets(self) -> None:
        buckets = [
            {"bucket_id": "30", "lo": 30, "hi": 31},
            {"bucket_id": "31", "lo": 31, "hi": 32},
            {"bucket_id": "32", "lo": 32, "hi": 33},
        ]
        # 30.4 -> 33: all three buckets are newly dead; every one is tradeable.
        dead = edge.select_dead_buckets(buckets, "high", 30.4, 33.0)
        self.assertEqual([item["bucket_id"] for item in dead], ["30", "31", "32"])
        # user example: 12 -> 11 low kills "lowest 12°C" bucket [12,13)
        low_buckets = [{"bucket_id": "12", "lo": 12, "hi": 13}]
        self.assertEqual([item["bucket_id"] for item in edge.select_dead_buckets(low_buckets, "low", 12.0, 11.0)], ["12"])
        # an in-bucket move (no boundary crossed) kills nothing
        self.assertEqual(edge.select_dead_buckets(low_buckets, "low", 12.0, 12.5), [])

    def test_open_ended_buckets_are_tolerated_and_never_dead(self) -> None:
        buckets = [
            {"bucket_id": "or-below-20", "lo": None, "hi": 20},
            {"bucket_id": "12", "lo": 12, "hi": 13},
            {"bucket_id": "or-above-30", "lo": 30, "hi": None},
        ]
        # Low direction 12 -> 11: picks the real bucket, open tails do not crash.
        dead = edge.select_dead_buckets(buckets, "low", 12.0, 11.0)
        self.assertEqual([item["bucket_id"] for item in dead], ["12"])
        # High direction 33 with only open-top bucket: nothing invalidated.
        self.assertEqual(edge.select_dead_buckets([{"bucket_id": "or-above-30", "lo": 30, "hi": None}], "high", 29.0, 33.0), [])
        # Low direction with only open-bottom bucket: nothing invalidated.
        self.assertEqual(edge.select_dead_buckets([{"bucket_id": "or-below-20", "lo": None, "hi": 20}], "low", 12.0, 11.0), [])

    def test_fast_multi_degree_break_emits_one_candidate_per_dead_bucket(self) -> None:
        city = self.cities["ZSPD"]
        state: dict = {
            "daily_extrema": {"shanghai|2026-08-22": {
                "city_id": "shanghai", "icao": "ZSPD", "market_local_date": "2026-08-22",
                "market_unit": "C", "high": 24.0, "low": 24.0,
            }},
            "handled_candidate_buckets": {},
        }
        rules = [{
            "market_rule_id": "event-high", "city_id": "shanghai", "market_local_date": "2026-08-22",
            "direction": "high", "market_unit": "C", "enabled": True,
            "buckets": [
                {"bucket_id": "24", "lo": 24, "hi": 25, "no_token_id": "no-24"},
                {"bucket_id": "25", "lo": 25, "hi": 26, "no_token_id": "no-25"},
                {"bucket_id": "26", "lo": 26, "hi": 27, "no_token_id": "no-26"},
                {"bucket_id": "27", "lo": 27, "hi": 28, "no_token_id": "no-27"},
            ],
        }]
        # 24 -> 27 in one report: buckets 24, 25, 26 are all impossible -> 3 NO buys.
        event = {
            "event_id": "jump", "report_time_utc": "2026-08-22T14:00:00Z",
            "fetched_at_utc": "2026-08-22T14:10:00Z", "temperature_c": 27,
            "raw_metar": "METAR ZSPD 221400Z 12007MPS 9999 27/24 Q1005 NOSIG",
            "receipt_time_utc": "2026-08-22T14:08:08Z",
        }
        signals = edge.evaluate_observation(state, event, city, rules, 900)
        candidates = [item for item in signals if item["signal_type"] == "candidate_no_signal"]
        self.assertEqual([item["bucket"]["bucket_id"] for item in candidates], ["24", "25", "26"])
        # idempotency: the same report replayed must not re-emit any candidate
        replayed = edge.evaluate_observation(state, dict(event, event_id="jump-again"), city, rules, 900)
        self.assertNotIn("candidate_no_signal", [item["signal_type"] for item in replayed])

    def test_half_open_tail_bucket_killed_by_opposite_direction_does_not_crash(self) -> None:
        city = self.cities["ZSPD"]
        state: dict = {
            "daily_extrema": {"shanghai|2026-08-22": {
                "city_id": "shanghai", "icao": "ZSPD", "market_local_date": "2026-08-22",
                "market_unit": "C", "high": 24.0, "low": 24.0,
            }},
            "handled_candidate_buckets": {},
        }
        # "25°C or below" tail bucket (lo=None) IS killed by a new high of 26
        # (high >= 25 makes "highest <= 25" impossible). Sorting must not
        # crash on the None lo bound (v2 review catch).
        rules = [{
            "market_rule_id": "event-high", "city_id": "shanghai", "market_local_date": "2026-08-22",
            "direction": "high", "market_unit": "C", "enabled": True,
            "buckets": [
                {"bucket_id": "or-below-25", "lo": None, "hi": 25, "no_token_id": "no-tail"},
                {"bucket_id": "25", "lo": 25, "hi": 26, "no_token_id": "no-25"},
            ],
        }]
        event = {
            "event_id": "jump", "report_time_utc": "2026-08-22T14:00:00Z",
            "fetched_at_utc": "2026-08-22T14:10:00Z", "temperature_c": 26,
            "raw_metar": "METAR ZSPD 221400Z 12007MPS 9999 26/24 Q1005 NOSIG",
            "receipt_time_utc": "2026-08-22T14:08:08Z",
        }
        signals = edge.evaluate_observation(state, event, city, rules, 900)
        candidates = [item for item in signals if item["signal_type"] == "candidate_no_signal"]
        # both the tail bucket and the [25,26) bucket are newly dead
        self.assertEqual(sorted(item["bucket"]["bucket_id"] for item in candidates), ["25", "or-below-25"])

    def test_midrange_fluctuation_within_seen_range_does_not_trigger(self) -> None:
        city = self.cities["ZSPD"]
        state: dict = {
            "daily_extrema": {"shanghai|2026-08-22": {
                "city_id": "shanghai", "icao": "ZSPD", "market_local_date": "2026-08-22",
                "market_unit": "C", "high": 31.0, "low": 27.0,
            }},
            "handled_candidate_buckets": {},
        }
        rules = [{
            "market_rule_id": "event-high", "city_id": "shanghai", "market_local_date": "2026-08-22",
            "direction": "high", "market_unit": "C", "enabled": True, "buckets": [],
        }]
        # 29-30 first appearance inside the already-seen 27..31 range: no trade.
        event = {
            "event_id": "mid", "report_time_utc": "2026-08-22T13:00:00Z",
            "fetched_at_utc": "2026-08-22T13:03:00Z", "temperature_c": 29,
            "raw_metar": "METAR ZSPD 221300Z 12007MPS 9999 29/24 Q1005 NOSIG",
        }
        signals = edge.evaluate_observation(state, event, city, rules, 900)
        self.assertNotIn("candidate_no_signal", [item["signal_type"] for item in signals])
        self.assertEqual(signals[0]["reason"], "not_new_daily_high")
        self.assertEqual(signals[1]["reason"], "not_new_daily_low")

    def test_receipt_time_latency_baseline_absorbs_awc_publish_delay(self) -> None:
        # AWC received the on-hour report ~490s late; we fetched ~120s after
        # receipt. True reaction lag is 120s, so a 300s gate must pass even
        # though report_time -> fetch is ~610s.
        city = self.cities["ZSPD"]
        event = {
            "event_id": "late", "report_time_utc": "2026-08-22T18:00:00Z",
            "receipt_time_utc": "2026-08-22T18:08:08Z",
            "fetched_at_utc": "2026-08-22T18:10:00Z", "temperature_c": 25,
            "raw_metar": "METAR ZSPD 221800Z 12007MPS 9999 25/24 Q1005 NOSIG",
        }
        signals = edge.evaluate_observation({}, event, city, [], 300)
        self.assertNotEqual(signals[0]["reason"], "report_too_old")
        self.assertEqual(signals[0]["reason"], "daily_baseline_initialized")

    def test_absolute_backstop_rejects_genuinely_stale_report(self) -> None:
        city = self.cities["ZSPD"]
        event = {
            "event_id": "stale", "report_time_utc": "2026-08-22T12:00:00Z",
            "receipt_time_utc": "2026-08-22T12:01:00Z",
            "fetched_at_utc": "2026-08-22T12:46:00Z", "temperature_c": 25,
            "raw_metar": "METAR ZSPD 221200Z 12007MPS 9999 25/24 Q1005 NOSIG",
        }
        signals = edge.evaluate_observation({}, event, city, [], 900)
        self.assertEqual(signals[0]["reason"], "report_too_old")
        self.assertGreater(signals[0]["age_seconds"], 2700)

    def test_fahrenheit_bucket_requires_tenths_c_remark_for_candidate_signal(self) -> None:
        city = self.cities["KLGA"]
        state: dict = {
            "daily_extrema": {"new-york-city|2026-08-22": {
                "city_id": "new-york-city", "icao": "KLGA", "market_local_date": "2026-08-22",
                "market_unit": "F", "high": 80.0, "low": 80.0,
            }},
            "handled_candidate_buckets": {},
        }
        rule = {
            "market_rule_id": "event-high", "city_id": "new-york-city", "market_local_date": "2026-08-22",
            "direction": "high", "market_unit": "F", "enabled": True,
            "buckets": [{"bucket_id": "80", "lo": 80, "hi": 82, "market_id": "market-80", "no_token_id": "no-80"}],
        }
        # 80F=26.7C; an integer-C 28C body maps to 82.4F >= 82 -> would kill the
        # 80F bucket, but without RMK T precision we must fail closed.
        event = {"event_id": "f-body", "report_time_utc": "2026-08-22T17:00:00Z", "fetched_at_utc": "2026-08-22T17:03:00Z", "temperature_c": 28, "raw_metar": "METAR KLGA 221700Z 00000KT 10SM 28/20 A3000"}
        signals = edge.evaluate_observation(state, event, city, [rule], 900)
        self.assertIn("f_unit_precision_ambiguous", [item.get("reason") for item in signals])

    def test_awc_nine_char_remark_temperature_group_is_parsed(self) -> None:
        city = self.cities["ZSPD"]  # C market
        # Real AWC format T[sign][TTT][DDHH]: T02220178 = +22.2°C
        event = {"raw_metar": "METAR KLAX 230253Z 26008KT 10SM FEW280 22/17 A2984 RMK AO2 SLP098 T02220178 53006"}
        parsed = edge.observed_temperature_native(event, city)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertAlmostEqual(parsed[0], 22.2, places=4)
        self.assertEqual(parsed[1], "metar_remark_tenths_c")
        # negative: T1[TTT] -> -3.8°C
        event_neg = {"raw_metar": "METAR KXXX 230100Z 00000KT 10SM SKC M04/M08 A3010 RMK AO2 T10380000"}
        parsed_neg = edge.observed_temperature_native(event_neg, city)
        self.assertIsNotNone(parsed_neg)
        assert parsed_neg is not None
        self.assertAlmostEqual(parsed_neg[0], -3.8, places=4)

    def test_fahrenheit_city_emits_candidate_when_remark_gives_tenths_precision(self) -> None:
        city = self.cities["KLGA"]
        state: dict = {
            "daily_extrema": {"new-york-city|2026-08-22": {
                "city_id": "new-york-city", "icao": "KLGA", "market_local_date": "2026-08-22",
                "market_unit": "F", "high": 71.0, "low": 71.0,
            }},
            "handled_candidate_buckets": {},
        }
        rule = {
            "market_rule_id": "event-high", "city_id": "new-york-city", "market_local_date": "2026-08-22",
            "direction": "high", "market_unit": "F", "enabled": True,
            "buckets": [{"bucket_id": "72", "lo": 72, "hi": 74, "market_id": "market-72", "no_token_id": "no-72"}],
        }
        # 23.5°C (RMK T-group T02350199) = 74.3°F >= 74 -> [72,74) bucket dead;
        # with tenths precision the F precision guard must NOT block the candidate.
        event = {"event_id": "f-remark", "report_time_utc": "2026-08-22T14:00:00Z", "fetched_at_utc": "2026-08-22T14:03:00Z", "temperature_c": 24, "raw_metar": "METAR KLGA 221400Z 00000KT 10SM 24/20 A3000 RMK AO2 T02350199"}
        signals = edge.evaluate_observation(state, event, city, [rule], 900)
        candidates = [item for item in signals if item["signal_type"] == "candidate_no_signal"]
        self.assertEqual(len(candidates), 1, msg=[item.get("reason") for item in signals])
        self.assertEqual(candidates[0]["bucket"]["bucket_id"], "72")

    def test_correction_is_audit_only_until_full_day_replay_exists(self) -> None:
        city = self.cities["ZSPD"]
        event = {"event_id": "cor", "report_time_utc": "2026-08-22T12:00:00Z", "fetched_at_utc": "2026-08-22T12:01:00Z", "temperature_c": 31, "raw_metar": "METAR ZSPD 221200Z COR 31/24 Q1005", "is_correction": True}
        signals = edge.evaluate_observation({}, event, city, [], 900)
        self.assertEqual(signals[0]["reason"], "correction_requires_full_day_rebuild")

    def test_candidate_is_idempotent_per_market_rule_bucket(self) -> None:
        city = self.cities["ZSPD"]
        state: dict = {
            "daily_extrema": {"shanghai|2026-08-22": {
                "city_id": "shanghai", "icao": "ZSPD", "market_local_date": "2026-08-22",
                "market_unit": "C", "high": 29.0, "low": 29.0,
            }},
            "handled_candidate_buckets": {},
        }
        rules = [{
            "market_rule_id": "market-high", "market_id": "m1", "no_token_id": "no", "city_id": "shanghai",
            "market_local_date": "2026-08-22", "direction": "high", "market_unit": "C", "enabled": True,
            "buckets": [{"bucket_id": "30", "lo": 30, "hi": 31}],
        }]
        event_break = {"event_id": "break", "report_time_utc": "2026-08-22T12:30:00Z", "fetched_at_utc": "2026-08-22T12:33:00Z", "temperature_c": 31, "raw_metar": "METAR ZSPD 221230Z 12007MPS 9999 31/24 Q1005 NOSIG"}
        first = edge.evaluate_observation(state, event_break, city, rules, 900)
        candidates = [item for item in first if item["signal_type"] == "candidate_no_signal"]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["bucket"]["bucket_id"], "30")
        # A later event can no longer kill the same bucket: once the extreme
        # moved past it, it is either not newly invalidated or already handled.
        event_repeat = {"event_id": "repeat", "report_time_utc": "2026-08-22T13:00:00Z", "fetched_at_utc": "2026-08-22T13:03:00Z", "temperature_c": 32, "raw_metar": "METAR ZSPD 221300Z 12007MPS 9999 32/24 Q1005 NOSIG"}
        second = edge.evaluate_observation(state, event_repeat, city, rules, 900)
        self.assertNotIn("candidate_no_signal", [item["signal_type"] for item in second])

    def test_no_signal_records_carry_city_id(self) -> None:
        city = self.cities["ZSPD"]
        event = {"event_id": "x", "fetched_at_utc": "2026-08-22T12:03:00Z", "temperature_c": 29, "raw_metar": "METAR ZSPD 221200Z 12007MPS 9999 29/24 Q1005 NOSIG"}
        signals = edge.evaluate_observation({}, event, city, [], 900)
        self.assertEqual(signals[0]["reason"], "missing_report_time")
        self.assertEqual(signals[0]["city_id"], "shanghai")


if __name__ == "__main__":
    unittest.main()
