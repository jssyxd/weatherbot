from __future__ import annotations

import unittest
from datetime import datetime, timezone

from tree11_yes_strategy import evaluate_fact_reversal, record_taf_versions

UTC = timezone.utc


class Tree11YesStrategyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.city = {"city_id": "shanghai", "icao": "ZSSS", "timezone": "Asia/Shanghai", "market_unit": "C", "market_city_slug": "shanghai"}
        self.cities = {"ZSSS": self.city}
        self.now = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)
        self.taf = {"icao": "ZSSS", "issued": "2026-08-27T00:00:00Z", "raw_text": "TAF ZSSS 270000Z 2700/2806 TX30/2710Z TN27/2704Z"}
        self.rules = [
            {"market_rule_id": "high-rule", "city_id": "shanghai", "market_local_date": "2026-08-27", "direction": "high", "market_unit": "C", "enabled": True, "buckets": [
                {"bucket_id": "h29", "lo": 29, "hi": 30, "yes_token_id": "yes-h29"},
                {"bucket_id": "h30", "lo": 30, "hi": 31, "yes_token_id": "yes-h30"},
                {"bucket_id": "h31", "lo": 31, "hi": 32, "yes_token_id": "yes-h31"},
            ]},
            {"market_rule_id": "low-rule", "city_id": "shanghai", "market_local_date": "2026-08-27", "direction": "low", "market_unit": "C", "enabled": True, "buckets": [
                {"bucket_id": "l26", "lo": 26, "hi": 27, "yes_token_id": "yes-l26"},
                {"bucket_id": "l27", "lo": 27, "hi": 28, "yes_token_id": "yes-l27"},
                {"bucket_id": "l28", "lo": 28, "hi": 29, "yes_token_id": "yes-l28"},
            ]},
        ]

    def state_with_taf(self) -> dict:
        state: dict = {}
        actions = record_taf_versions(state, [self.taf], self.cities, self.now, 100, "checkwx-taf")
        self.assertEqual(sum(item["status"] == "recorded" for item in actions), 2)
        return state

    def event(self, event_id: str, temperature: float, observed: str = "2026-08-27T03:00:00Z", fetched: str = "2026-08-27T03:02:00Z") -> dict:
        return {"event_id": event_id, "report_time_utc": observed, "fetched_at_utc": fetched, "temperature_native": temperature}

    def test_high_and_low_are_independent_and_target_current_bucket(self) -> None:
        state = self.state_with_taf()
        high_actions = evaluate_fact_reversal(state, self.event("high-1", 31.0), self.city, self.rules, self.now, 200, warmup_complete=True)
        high = [item for item in high_actions if item.get("status") == "PENDING_CONSENSUS"]
        self.assertEqual(len(high), 1)
        self.assertEqual(high[0]["direction"], "high")
        self.assertEqual(high[0]["old_bucket"]["bucket_id"], "h30")
        self.assertEqual(high[0]["new_bucket"]["bucket_id"], "h31")
        self.assertEqual(high[0]["token_id"], "yes-h31")
        self.assertTrue(high[0]["safety"]["paper_only"])
        self.assertEqual(high[0]["safety"]["orders_submitted"], 0)

        low_actions = evaluate_fact_reversal(state, self.event("low-1", 26.0, "2026-08-27T04:00:00Z", "2026-08-27T04:01:00Z"), self.city, self.rules, self.now, 300, warmup_complete=True)
        low = [item for item in low_actions if item.get("status") == "PENDING_CONSENSUS"]
        self.assertEqual(len(low), 1)
        self.assertEqual(low[0]["direction"], "low")
        self.assertEqual(low[0]["old_bucket"]["bucket_id"], "l27")
        self.assertEqual(low[0]["new_bucket"]["bucket_id"], "l26")

    def test_same_bucket_and_stale_report_do_not_create_yes_candidate(self) -> None:
        state = self.state_with_taf()
        same = evaluate_fact_reversal(state, self.event("same", 30.5), self.city, self.rules, self.now, 200, warmup_complete=True)
        self.assertIn("no_crossed_contract_bucket", {item["status"] for item in same})
        stale = evaluate_fact_reversal(state, self.event("stale", 31, "2026-08-27T03:00:00Z", "2026-08-27T03:05:00Z"), self.city, self.rules, self.now, 201, warmup_complete=True)
        self.assertEqual(stale[0]["status"], "blocked_report_age")
        self.assertEqual(len(state["tree11"]["signals"]), 0)

    def test_latest_t0_visible_taf_prevents_using_superseded_forecast(self) -> None:
        state = self.state_with_taf()
        newer = {"icao": "ZSSS", "issued": "2026-08-27T01:00:00Z", "raw_text": "TAF ZSSS 270100Z 2701/2806 TX31/2710Z TN27/2704Z"}
        record_taf_versions(state, [newer], self.cities, datetime(2026, 8, 27, 1, 1, tzinfo=UTC), 150, "checkwx-taf")
        actions = evaluate_fact_reversal(state, self.event("new-taf", 31.0), self.city, self.rules, self.now, 200, warmup_complete=True)
        self.assertNotIn("PENDING_CONSENSUS", {item["status"] for item in actions})
        self.assertIn("no_crossed_contract_bucket", {item["status"] for item in actions})

    def test_incomplete_warmup_blocks_even_when_boundary_crossed(self) -> None:
        state = self.state_with_taf()
        actions = evaluate_fact_reversal(state, self.event("warmup", 31.0), self.city, self.rules, self.now, 200, warmup_complete=False)
        self.assertEqual(actions[0]["status"], "blocked_warmup_incomplete")
        self.assertEqual(state["tree11"]["signals"], {})

    def test_fahrenheit_two_degree_bucket_requires_actual_boundary_cross(self) -> None:
        city = {"city_id": "nyc", "icao": "KJFK", "timezone": "America/New_York", "market_unit": "F", "market_city_slug": "new-york-city"}
        cities = {"KJFK": city}
        rules = [{"market_rule_id": "fh", "city_id": "nyc", "market_local_date": "2026-08-27", "direction": "high", "market_unit": "F", "enabled": True, "buckets": [
            {"bucket_id": "f80-81", "lo": 80, "hi": 82, "yes_token_id": "yes-80"},
            {"bucket_id": "f82-83", "lo": 82, "hi": 84, "yes_token_id": "yes-82"},
        ]}, {"market_rule_id": "fl", "city_id": "nyc", "market_local_date": "2026-08-27", "direction": "low", "market_unit": "F", "enabled": True, "buckets": []}]
        state: dict = {}
        taf = {"icao": "KJFK", "issued": "2026-08-27T04:00:00Z", "raw_text": "TAF KJFK 270400Z 2704/2806 TX27/2710Z TN20/2704Z"}
        record_taf_versions(state, [taf], cities, datetime(2026, 8, 27, 4, 1, tzinfo=UTC), 100, "checkwx-taf")
        # 81.9°F remains inside [80,82) even though it is warmer than the 27°C TAF point conversion.
        same = evaluate_fact_reversal(state, {"event_id": "f-same", "report_time_utc": "2026-08-27T05:00:00Z", "fetched_at_utc": "2026-08-27T05:01:00Z", "temperature_native": 81.9}, city, rules, self.now, 200, warmup_complete=True)
        self.assertNotIn("PENDING_CONSENSUS", {item["status"] for item in same})
        crossed = evaluate_fact_reversal(state, {"event_id": "f-cross", "report_time_utc": "2026-08-27T05:30:00Z", "fetched_at_utc": "2026-08-27T05:31:00Z", "temperature_native": 82.0}, city, rules, self.now, 300, warmup_complete=True)
        candidate = next(item for item in crossed if item.get("status") == "PENDING_CONSENSUS")
        self.assertEqual(candidate["new_bucket"]["bucket_id"], "f82-83")


if __name__ == "__main__":
    unittest.main()
