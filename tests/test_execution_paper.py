from __future__ import annotations

import unittest
from decimal import Decimal

from execution.paper_executor import match_fak, match_gtc
from execution.order_intent import OrderIntent, OrderStatus, OrderType, Side
from execution.market import BookView
from adapters.polymarket.orderbook import from_any
from execution.risk_gate import RiskGate, RiskConfig


def _book(asks, bids=None):
    return BookView(
        token_id="tok",
        asks=tuple((Decimal(str(p)), Decimal(str(s))) for p, s in asks),
        bids=tuple((Decimal(str(p)), Decimal(str(s))) for p, s in (bids or [])),
        best_ask=Decimal(str(asks[0][0])) if asks else None,
        best_bid=Decimal(str(bids[0][0])) if bids else None,
        age_seconds=0.0,
    )


def _intent(price="0.94", quantity="5", order_type="FAK", order_id="o1"):
    return OrderIntent.new(
        token_id="tok", side=Side.BUY, price=price, quantity=quantity,
        order_type=order_type, strategy="test", signal_reason="test", order_id=order_id,
    )


class PaperMatchTests(unittest.TestCase):
    def test_full_fill_walks_depth(self):
        # ask 0.92×1, 0.93×2, 0.94×10 → BUY 5 @ 0.94 fills 5.
        b = _book([("0.92", "1"), ("0.93", "2"), ("0.94", "10")])
        r = match_fak(_intent("0.94"), b)
        self.assertEqual(r.status, OrderStatus.FILLED)
        self.assertEqual(r.filled_shares, Decimal("5"))
        self.assertEqual(r.remaining_shares, Decimal("0"))
        self.assertEqual(len(r.fills), 3)

    def test_partial_fill_fak_cancels_remainder(self):
        # ask 0.92×1, 0.93×2 → BUY 5 @ 0.93 fills 3, cancels 2.
        b = _book([("0.92", "1"), ("0.93", "2")])
        r = match_fak(_intent("0.93"), b)
        self.assertEqual(r.status, OrderStatus.CANCELLED)
        self.assertEqual(r.filled_shares, Decimal("3"))
        self.assertEqual(r.cancel_reason, "FAK_REMAINDER_CANCELLED")

    def test_zero_fill_cancelled(self):
        b = _book([("0.99", "100")])
        r = match_fak(_intent("0.93"), b)
        self.assertEqual(r.status, OrderStatus.CANCELLED)
        self.assertEqual(r.filled_shares, Decimal("0"))

    def test_gtc_rests_remainder(self):
        b = _book([("0.92", "1")])
        r = match_gtc(_intent("0.93", order_type="GTC"), b)
        self.assertEqual(r.status, OrderStatus.PARTIALLY_FILLED)
        self.assertEqual(r.filled_shares, Decimal("1"))
        self.assertEqual(r.remaining_shares, Decimal("4"))

    def test_average_price_is_depth_weighted(self):
        b = _book([("0.90", "2"), ("0.95", "3")])
        r = match_fak(_intent("0.95"), b)
        self.assertEqual(r.filled_shares, Decimal("5"))
        self.assertEqual(r.average_price, Decimal("0.93"))  # (2*0.90+3*0.95)/5


class RiskGateTests(unittest.TestCase):
    def test_price_outside_gate_rejected(self):
        b = _book([("0.90", "10")])
        gate = RiskGate(RiskConfig(min_price=Decimal("0.40"), max_price=Decimal("0.95")))
        d = gate.check(_intent("0.99"), b)
        self.assertFalse(d.allowed)
        self.assertEqual(d.code, "PRICE_ABOVE_MAX")

    def test_missing_book_rejected(self):
        gate = RiskGate(RiskConfig(min_price=Decimal("0.40"), max_price=Decimal("0.95")))
        d = gate.check(_intent(), None)
        self.assertFalse(d.allowed)
        self.assertEqual(d.code, "BOOK_MISSING_OR_INCOMPLETE")

    def test_ok(self):
        b = _book([("0.90", "10")])
        gate = RiskGate(RiskConfig(min_price=Decimal("0.40"), max_price=Decimal("0.95")))
        d = gate.check(_intent("0.90"), b)
        self.assertTrue(d.allowed)
        self.assertEqual(d.code, "RISK_PASS")


if __name__ == "__main__":
    unittest.main()
