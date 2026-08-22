from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import market_adapter as market  # noqa: E402


class MarketAdapterTests(unittest.TestCase):
    def test_event_slug_uses_explicit_contract_aliases(self) -> None:
        self.assertEqual(
            market.event_slug("nyc", "2026-08-23", "high"),
            "highest-temperature-in-nyc-on-august-23-2026",
        )
        self.assertEqual(
            market.event_slug("seoul", "2026-08-24", "low"),
            "lowest-temperature-in-seoul-on-august-24-2026",
        )

    def test_bucket_parser_handles_exact_range_and_open_ends(self) -> None:
        self.assertEqual(market.parse_bucket("30°C"), (30.0, 31.0, "C"))
        self.assertEqual(market.parse_bucket("between 88-89°F"), (88.0, 90.0, "F"))
        self.assertEqual(market.parse_bucket("20°C or below"), (None, 21.0, "C"))
        self.assertEqual(market.parse_bucket("32°C or higher"), (32.0, None, "C"))

    def test_event_parser_groups_all_buckets_and_retains_each_no_token(self) -> None:
        city = {"city_id": "shanghai", "icao": "ZSPD", "market_unit": "C"}
        event = {
            "id": "event-1", "slug": "highest-temperature-in-shanghai-on-august-22-2026",
            "markets": [
                {
                    "id": "market-31", "active": True, "closed": False, "acceptingOrders": True, "enableOrderBook": True,
                    "question": "Will the highest temperature in Shanghai be 31°C on August 22?",
                    "outcomes": json.dumps(["Yes", "No"]), "clobTokenIds": json.dumps(["yes-31", "no-31"]),
                },
                {
                    "id": "market-30", "active": True, "closed": False, "acceptingOrders": True, "enableOrderBook": True,
                    "question": "Will the highest temperature in Shanghai be 30°C on August 22?",
                    "outcomes": json.dumps(["Yes", "No"]), "clobTokenIds": json.dumps(["yes-30", "no-30"]),
                },
            ],
        }
        rules = market.parse_event_rules(event, city, "2026-08-22", "high")
        self.assertEqual(len(rules), 1)
        self.assertEqual([item["bucket_id"] for item in rules[0]["buckets"]], ["market-30", "market-31"])
        self.assertEqual(rules[0]["buckets"][1]["no_token_id"], "no-31")


if __name__ == "__main__":
    unittest.main()
