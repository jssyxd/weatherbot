from __future__ import annotations

import unittest
from decimal import Decimal

from execution.position import (
    Position,
    apply_fill,
    realized_pnl_for_exit,
    unrealized_pnl_usdc,
)
from execution.order_intent import Fill, Side


def _fill(price, shares, token_id="no-1", side=Side.BUY):
    return Fill(order_id="o1", token_id=token_id, side=side, price=Decimal(price), shares=Decimal(shares))


class PositionTests(unittest.TestCase):
    def test_buy_fills_weighted_average(self) -> None:
        pos = Position(token_id="no-1", side=Side.BUY)
        pos = apply_fill(pos, _fill("0.90", "2"))
        pos = apply_fill(pos, _fill("0.95", "3"))
        self.assertEqual(pos.shares, Decimal("5"))
        # (2*0.90 + 3*0.95)/5 = 0.93
        self.assertEqual(pos.avg_price, Decimal("0.93"))
        self.assertEqual(pos.realized_pnl_usdc, Decimal("0"))

    def test_apply_fill_ignores_non_positive_size(self) -> None:
        pos = Position(token_id="no-1", side=Side.BUY)
        pos = apply_fill(pos, _fill("0.90", "0"))
        self.assertEqual(pos.shares, Decimal("0"))

    def test_sell_fill_realizes_pnl(self) -> None:
        pos = Position(token_id="no-1", side=Side.BUY)
        pos = apply_fill(pos, _fill("0.90", "5"))
        pos = apply_fill(pos, _fill("0.80", "3", side=Side.SELL))
        # realized = (0.80 - 0.90) * 3 = -0.30
        self.assertEqual(pos.shares, Decimal("2"))
        self.assertEqual(pos.realized_pnl_usdc, Decimal("-0.30"))
        self.assertEqual(pos.avg_price, Decimal("0.90"))

    def test_sell_cannot_oversell(self) -> None:
        pos = Position(token_id="no-1", side=Side.BUY)
        pos = apply_fill(pos, _fill("0.90", "5"))
        pos = apply_fill(pos, _fill("0.99", "99", side=Side.SELL))
        self.assertEqual(pos.shares, Decimal("0"))
        self.assertEqual(pos.realized_pnl_usdc, Decimal("0.45"))  # (0.99-0.90)*5

    def test_unrealized_pnl_mark(self) -> None:
        pos = Position(token_id="no-1", side=Side.BUY)
        pos = apply_fill(pos, _fill("0.90", "5"))
        self.assertEqual(unrealized_pnl_usdc(pos, Decimal("0.85")), Decimal("-0.25"))
        self.assertEqual(unrealized_pnl_usdc(pos, Decimal("1.00")), Decimal("0.50"))

    def test_realized_pnl_pure_function(self) -> None:
        self.assertEqual(realized_pnl_for_exit("0.90", "0.80", "3"), Decimal("-0.30"))
        self.assertEqual(realized_pnl_for_exit("0.50", "0.60", "2"), Decimal("0.20"))

    def test_as_dict_stringifies_decimals(self) -> None:
        pos = Position(token_id="no-1", side=Side.BUY)
        pos = apply_fill(pos, _fill("0.90", "2"))
        data = pos.as_dict()
        self.assertEqual(data["shares"], "2")
        self.assertEqual(data["avg_price"], "0.90")
        self.assertEqual(data["realized_pnl_usdc"], "0")


if __name__ == "__main__":
    unittest.main()
