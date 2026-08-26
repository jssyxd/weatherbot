from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import metar_observer as observer  # noqa: E402


class ExecutionBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.signal = {
            "signal_type": "candidate_no_signal",
            "city_id": "shanghai",
            "market_local_date": "2026-08-23",
            "bucket": {"bucket_id": "bucket-31", "no_token_id": "no-token"},
        }
        self.book = {
            "timestamp": "123456", "hash": "book-hash", "tick_size": "0.01", "min_order_size": "5",
            "asks": [{"price": "0.50", "size": "10.00"}],
        }
        self.fee = {"base_fee": 500}

    def test_paper_fill_uses_public_depth_and_fee_in_total_debit_cap(self) -> None:
        state: dict = {}
        with patch("paper_execution.fetch_order_book", return_value=(self.book, "https://clob.test/book")), \
             patch("paper_execution.fetch_fee_rate", return_value=(self.fee, "https://clob.test/fee")):
            output = observer.enrich_execution(self.signal, "paper", state)
        execution = output["execution"]
        self.assertEqual(execution["status"], "paper_fill_estimate")
        self.assertEqual(execution["order_type"], "FAK")
        self.assertEqual(execution["side"], "BUY_NO")
        self.assertEqual(execution["no_token_id"], "no-token")
        self.assertGreater(execution["estimated_fee_usdc"], 0)
        # Fixed 5-share intent (exchange minimum order size)
        self.assertEqual(execution["target_shares"], 5.0)
        self.assertEqual(execution["estimated_shares"], 5.0)
        self.assertEqual(execution["base_fee_bps"], 500.0)
        self.assertIn("shanghai|2026-08-23", state["paper_city_day_total_debit"])

    def test_fixed_five_share_intent_walks_levels(self) -> None:
        # 2 shares at 0.50 + 3 shares at 0.51 = 5 shares total, split across two levels
        multi_level_book = dict(self.book, asks=[{"price": "0.50", "size": "2.00"}, {"price": "0.51", "size": "3.00"}])
        state: dict = {}
        with patch("paper_execution.fetch_order_book", return_value=(multi_level_book, "https://clob.test/book")), \
             patch("paper_execution.fetch_fee_rate", return_value=(self.fee, "https://clob.test/fee")):
            output = observer.enrich_execution(self.signal, "paper", state)
        execution = output["execution"]
        self.assertEqual(execution["status"], "paper_fill_estimate")
        self.assertEqual(execution["estimated_shares"], 5.0)
        self.assertEqual(len(execution["fills"]), 2)
        self.assertEqual(execution["fills"][0]["shares"], 2.0)
        self.assertEqual(execution["fills"][1]["shares"], 3.0)

    def test_intent_is_fixed_five_shares_across_price_bands(self) -> None:
        fee_rate = Decimal(self.fee["base_fee"]) / Decimal("10000")
        for ask in ("0.10", "0.30", "0.31", "0.60", "0.61", "0.80"):
            state: dict = {}
            tier_book = dict(self.book, asks=[{"price": ask, "size": "200"}])
            with patch("paper_execution.fetch_order_book", return_value=(tier_book, "https://clob.test/book")), \
                 patch("paper_execution.fetch_fee_rate", return_value=(self.fee, "https://clob.test/fee")):
                output = observer.enrich_execution(self.signal, "paper", state)
            execution = output["execution"]
            self.assertEqual(execution["status"], "paper_fill_estimate", msg=f"ask={ask}")
            self.assertEqual(execution["target_shares"], 5.0, msg=f"ask={ask}")
            self.assertEqual(execution["estimated_shares"], 5.0, msg=f"ask={ask}")
            expected = Decimal("5") * Decimal(ask) * (Decimal("1") + fee_rate * (Decimal("1") - Decimal(ask)))
            self.assertEqual(execution["total_debit_usdc"], float(expected), msg=f"ask={ask}")

    def test_paper_fill_rejects_ask_outside_strict_price_gate(self) -> None:
        state: dict = {}
        for bad_price in ("0.04", "0.99"):
            invalid_book = dict(self.book, asks=[{"price": bad_price, "size": "20"}])
            with patch("paper_execution.fetch_order_book", return_value=(invalid_book, "https://clob.test/book")), \
                 patch("paper_execution.fetch_fee_rate", return_value=(self.fee, "https://clob.test/fee")):
                output = observer.enrich_execution(self.signal, "paper", state)
            self.assertEqual(output["execution"]["status"], "paper_fill_rejected_best_ask_outside_gate")
            self.assertEqual(state["paper_city_day_total_debit"], {})

    def test_paper_fill_rejects_when_intent_no_longer_fits_city_day_total_debit(self) -> None:
        # 19.5 already spent: 5 shares at ask 0.50 (~2.56 USDC total) no longer fit the 20 USDC cap.
        deep_book = dict(self.book, asks=[{"price": "0.50", "size": "200"}])
        state: dict = {"paper_city_day_total_debit": {"shanghai|2026-08-23": 19.5}}
        with patch("paper_execution.fetch_order_book", return_value=(deep_book, "https://clob.test/book")), \
             patch("paper_execution.fetch_fee_rate", return_value=(self.fee, "https://clob.test/fee")):
            output = observer.enrich_execution(self.signal, "paper", state)
        self.assertEqual(output["execution"]["status"], "paper_fill_rejected_city_day_cap")
        self.assertEqual(output["execution"]["total_debit_budget_usdc"], 20.0)

    def test_live_mode_never_submits_an_order_or_reads_a_book(self) -> None:
        with patch("metar_observer.simulate_paper_fak", side_effect=AssertionError("must not be called")):
            output = observer.enrich_execution(self.signal, "live", {})
        self.assertEqual(output["execution"]["status"], "blocked_no_live_executor")
        self.assertNotIn("no_token_id", output["execution"])


if __name__ == "__main__":
    unittest.main()
