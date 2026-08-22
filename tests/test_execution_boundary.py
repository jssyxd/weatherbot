from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import metar_observer as observer  # noqa: E402


class ExecutionBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.signal = {
            "signal_type": "candidate_no_signal",
            "city_id": "shanghai",
            "market_local_date": "2026-08-23",
        }

    def test_paper_intents_are_capped_per_city_day(self) -> None:
        state: dict = {}
        one = observer.enrich_execution(self.signal, "paper", state)
        two = observer.enrich_execution(self.signal, "paper", state)
        three = observer.enrich_execution(self.signal, "paper", state)
        self.assertEqual(one["execution"]["status"], "paper_order_intent_pending_price_gate")
        self.assertEqual(two["execution"]["status"], "paper_order_intent_pending_price_gate")
        self.assertEqual(three["execution"]["status"], "paper_intent_skipped_city_day_cap")
        self.assertEqual(state["paper_city_day_notional"]["shanghai|2026-08-23"], 2.0)

    def test_live_mode_never_submits_an_order(self) -> None:
        output = observer.enrich_execution(self.signal, "live", {})
        self.assertEqual(output["execution"]["status"], "blocked_no_live_executor")
        self.assertNotIn("no_token_id", output["execution"])


if __name__ == "__main__":
    unittest.main()
