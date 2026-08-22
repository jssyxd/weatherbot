from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
