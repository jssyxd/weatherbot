"""Paper exit settlement: positions removed, capital released, PnL realized."""
from __future__ import annotations

import unittest
from decimal import Decimal

from paper_capital import remaining_capital_usdc, reserve, release, total_debit_usdc
from tree13_allno_strategy import (
    classify_metar_for_position,
    ensure_allno_state,
    paper_fill_entry,
    plan_entries,
    plan_exit,
    process_metar_paper_exits,
    settle_paper_exit,
)


class PaperCapitalTests(unittest.TestCase):
    def test_reserve_and_release_roundtrip(self):
        state = {"paper_initial_capital_usdc": 1000.0, "paper_total_debit_usdc": 0.0}
        self.assertIsNotNone(reserve(state, "4.50"))
        self.assertEqual(total_debit_usdc(state), Decimal("4.50"))
        release(state, "4.50")
        self.assertEqual(total_debit_usdc(state), Decimal("0.00"))
        self.assertEqual(remaining_capital_usdc(state), Decimal("1000.00"))

    def test_release_never_negative(self):
        state = {"paper_initial_capital_usdc": 100.0, "paper_total_debit_usdc": 1.0}
        release(state, "50")
        self.assertEqual(total_debit_usdc(state), Decimal("0"))


class PaperExitSettlementTests(unittest.TestCase):
    def setUp(self):
        self.state = {
            "paper_initial_capital_usdc": 1000.0,
            "paper_total_debit_usdc": 0.0,
        }
        tree = ensure_allno_state(self.state)
        tree["orders"]["ord-1"] = {
            "order_key": "ord-1",
            "status": "PENDING_GTC",
            "city_id": "shanghai",
            "market_local_date": "2026-08-30",
            "direction": "high",
            "bucket_id": "22",
            "bucket": {"lo": 22, "hi": 23, "bucket_id": "22"},
            "token_id": "tok-no-22",
            "requested_shares": "5",
            "limit_price": "0.90",
            "reserved_usdc": "4.50",
        }

    def test_fill_then_metar_exit_removes_position_and_releases_cash(self):
        filled = paper_fill_entry(self.state, "ord-1", fill_price="0.90")
        self.assertEqual(filled["status"], "filled")
        self.assertIn("ord-1", self.state["tree13_allno"]["positions"])
        self.assertEqual(total_debit_usdc(self.state), Decimal("4.50000"))

        pos = self.state["tree13_allno"]["positions"]["ord-1"]
        self.assertEqual(classify_metar_for_position(pos, 22.5), "FACT_INVALIDATED_EXIT")

        actions = process_metar_paper_exits(
            self.state,
            running_extremes_by_position_key={"ord-1": 22.5},
            books_by_token={"tok-no-22": {"best_bid": "0.80"}},
        )
        statuses = [a.get("status") for a in actions]
        self.assertIn("settled", statuses)
        self.assertNotIn("ord-1", self.state["tree13_allno"]["positions"])
        self.assertIn("ord-1", self.state["tree13_allno"]["closed_positions"])

        closed = self.state["tree13_allno"]["closed_positions"]["ord-1"]
        # attempt 0 slip 0.03 -> 0.80 * 0.97 = 0.776
        sale = Decimal(closed["sale_price"])
        self.assertGreater(sale, Decimal("0.75"))
        self.assertLess(sale, Decimal("0.80"))
        proceeds = Decimal(closed["proceeds_usdc"])
        # debit was 4.50; release proceeds -> debit = 4.50 - proceeds
        self.assertEqual(
            total_debit_usdc(self.state),
            (Decimal("4.50000") - proceeds).quantize(Decimal("0.00001")),
        )
        pnl = Decimal(closed["realized_pnl_usdc"])
        self.assertEqual(pnl, (sale - Decimal("0.90")) * Decimal("5"))

    def test_settle_idempotent(self):
        paper_fill_entry(self.state, "ord-1", fill_price="0.90")
        a = settle_paper_exit(self.state, "ord-1", "0.50", reason="FACT_INVALIDATED_EXIT")
        self.assertEqual(a["status"], "settled")
        b = settle_paper_exit(self.state, "ord-1", "0.50", reason="FACT_INVALIDATED_EXIT")
        self.assertEqual(b["status"], "skipped_already_closed")
        # cash only released once
        self.assertEqual(total_debit_usdc(self.state), Decimal("4.50000") - Decimal("2.50000"))

    def test_dead_book_settles_at_hard_floor(self):
        paper_fill_entry(self.state, "ord-1", fill_price="0.90")
        actions = process_metar_paper_exits(
            self.state,
            running_extremes_by_position_key={"ord-1": 22.5},
            books_by_token={},  # no bid
        )
        closed = self.state["tree13_allno"]["closed_positions"]["ord-1"]
        self.assertEqual(Decimal(closed["sale_price"]), Decimal("0.05"))
        self.assertEqual(closed["close_reason"], "FACT_INVALIDATED_EXIT")

    def test_proven_impossible_hold_does_not_exit(self):
        paper_fill_entry(self.state, "ord-1", fill_price="0.90")
        # high bucket [22,23), extreme 23 means proven impossible for this NO -> hold
        actions = process_metar_paper_exits(
            self.state,
            running_extremes_by_position_key={"ord-1": 23.0},
            books_by_token={"tok-no-22": {"best_bid": "0.50"}},
        )
        self.assertIn("ord-1", self.state["tree13_allno"]["positions"])
        self.assertTrue(any(a.get("status") == "hold_proven_impossible" for a in actions))


class PlanExitLadderTests(unittest.TestCase):
    def test_mild_slippage_and_floor(self):
        pos = {"token_id": "x"}
        a0 = plan_exit(position=pos, reason="x", best_bid="0.80", remaining_shares="5", attempt=0)
        self.assertEqual(a0["minimum_price"], str(Decimal("0.80") * Decimal("0.97")))
        dead = plan_exit(position=pos, reason="x", best_bid="0.02", remaining_shares="5", attempt=0)
        self.assertEqual(Decimal(dead["minimum_price"]), Decimal("0.05"))


if __name__ == "__main__":
    unittest.main()
