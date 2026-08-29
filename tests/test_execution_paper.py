from __future__ import annotations

import unittest
from decimal import Decimal

from execution.paper_executor import match_l2
from execution.order_intent import OrderIntent, OrderStatus
from execution.risk_gate import risk_check


def book(asks, bids=None):
    return {"asks": asks, "bids": bids or []}


class PaperMatchTests(unittest.TestCase):
    def test_full_fill_walks_depth(self):
        # ask 0.92×1, 0.93×2, 0.94×10 → BUY 5 @ 0.94 fills 5.
        b = book([{"price": "0.92", "size": "1"}, {"price": "0.93", "size": "2"}, {"price": "0.94", "size": "10"}])
        r = match_l2(book=b, side="BUY", quantity=Decimal("5"), limit_price=Decimal("0.94"), order_type="FAK")
        self.assertEqual(r.status, "FILLED")
        self.assertEqual(r.filled_size, Decimal("5"))
        self.assertEqual(r.cancelled_size, Decimal("0"))
        self.assertEqual(len(r.fills), 3)

    def test_partial_fill_fak_cancels_remainder(self):
        # ask 0.92×1, 0.93×2 → BUY 5 @ 0.93 fills 3, cancels 2.
        b = book([{"price": "0.92", "size": "1"}, {"price": "0.93", "size": "2"}])
        r = match_l2(book=b, side="BUY", quantity=Decimal("5"), limit_price=Decimal("0.93"), order_type="FAK")
        self.assertEqual(r.status, "PARTIALLY_FILLED")
        self.assertEqual(r.filled_size, Decimal("3"))
        self.assertEqual(r.cancelled_size, Decimal("2"))

    def test_zero_fill_cancelled(self):
        b = book([{"price": "0.99", "size": "100"}])
        r = match_l2(book=b, side="BUY", quantity=Decimal("5"), limit_price=Decimal("0.93"), order_type="FAK")
        self.assertEqual(r.status, "CANCELLED")
        self.assertEqual(r.filled_size, Decimal("0"))
        self.assertEqual(r.cancelled_size, Decimal("5"))

    def test_fok_does_not_partially_fill(self):
        b = book([{"price": "0.92", "size": "1"}])
        r = match_l2(book=b, side="BUY", quantity=Decimal("5"), limit_price=Decimal("0.93"), order_type="FOK")
        self.assertEqual(r.status, "CANCELLED")
        self.assertEqual(r.filled_size, Decimal("0"))

    def test_average_price_is_depth_weighted(self):
        b = book([{"price": "0.90", "size": "2"}, {"price": "0.95", "size": "3"}])
        r = match_l2(book=b, side="BUY", quantity=Decimal("5"), limit_price=Decimal("0.95"), order_type="FAK")
        self.assertEqual(r.filled_size, Decimal("5"))
        self.assertEqual(r.avg_price, Decimal("0.9300"))  # (2*0.90+3*0.95)/5


class RiskGateTests(unittest.TestCase):
    def _intent(self, price="0.90", quantity="5"):
        return OrderIntent(
            order_id="o1", token_id="tok", side="BUY", outcome="NO",
            price=Decimal(price), quantity=Decimal(quantity), order_type="FAK",
            strategy="test", signal_reason="test", created_at_utc="2026-01-01T00:00:00Z",
        )

    def test_price_outside_gate_rejected(self):
        b = {"asks": [{"price": "0.90", "size": "10"}], "best_ask": "0.90"}
        d = risk_check(intent=self._intent("0.99"), book=b, min_price=Decimal("0.40"),
                       max_price=Decimal("0.95"), max_slippage=Decimal("0.10"), max_book_age_seconds=3)
        self.assertFalse(d.allowed)
        self.assertEqual(d.code, "PRICE_OUTSIDE_GATE")

    def test_missing_book_rejected(self):
        d = risk_check(intent=self._intent(), book=None, min_price=Decimal("0.40"),
                       max_price=Decimal("0.95"), max_slippage=Decimal("0.10"), max_book_age_seconds=3)
        self.assertFalse(d.allowed)
        self.assertEqual(d.code, "MISSING_BOOK")

    def test_ok(self):
        b = {"asks": [{"price": "0.90", "size": "10"}], "best_ask": "0.90"}
        d = risk_check(intent=self._intent(), book=b, min_price=Decimal("0.40"),
                       max_price=Decimal("0.95"), max_slippage=Decimal("0.10"), max_book_age_seconds=3)
        self.assertTrue(d.allowed)


if __name__ == "__main__":
    unittest.main()
