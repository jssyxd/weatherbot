from __future__ import annotations

import unittest
from decimal import Decimal

from execution.position import Position, realized_pnl_for_exit


class PositionTests(unittest.TestCase):
    def test_apply_fill_weighted_average(self) -> None:
        pos = Position(key="k1", token_id="no-1", side="BUY", outcome="NO")
        pos = pos.apply_fill(Decimal("0.90"), Decimal("2"))
        pos = pos.apply_fill(Decimal("0.95"), Decimal("3"))
        self.assertEqual(pos.shares, Decimal("5"))
        # (2*0.90 + 3*0.95)/5 = 0.93
        self.assertEqual(pos.avg_price, Decimal("0.9300"))
        self.assertEqual(pos.cost_basis_usdc, Decimal("4.65"))

    def test_apply_fill_ignores_non_positive_size(self) -> None:
        pos = Position(key="k1", token_id="no-1", side="BUY", outcome="NO")
        pos = pos.apply_fill(Decimal("0.90"), Decimal("0"))
        self.assertEqual(pos.shares, Decimal("0"))

    def test_apply_exit_realizes_pnl(self) -> None:
        pos = Position(key="k1", token_id="no-1", side="BUY", outcome="NO")
        pos = pos.apply_fill(Decimal("0.90"), Decimal("5"))
        pos = pos.apply_exit(Decimal("0.80"), Decimal("3"))
        # realized = (0.80 - 0.90) * 3 = -0.30
        self.assertEqual(pos.shares, Decimal("2"))
        self.assertEqual(pos.realized_pnl_usdc, Decimal("-0.30"))
        self.assertEqual(pos.avg_price, Decimal("0.9000"))  # remaining basis / remaining shares

    def test_apply_exit_cannot_oversell(self) -> None:
        pos = Position(key="k1", token_id="no-1", side="BUY", outcome="NO")
        pos = pos.apply_fill(Decimal("0.90"), Decimal("5"))
        pos = pos.apply_exit(Decimal("0.99"), Decimal("99"))
        self.assertEqual(pos.shares, Decimal("0"))
        self.assertEqual(pos.realized_pnl_usdc, Decimal("0.45"))  # (0.99-0.90)*5

    def test_unrealized_pnl_mark(self) -> None:
        pos = Position(key="k1", token_id="no-1", side="BUY", outcome="NO")
        pos = pos.apply_fill(Decimal("0.90"), Decimal("5"))
        self.assertEqual(pos.unrealized_pnl(Decimal("0.85")), Decimal("-0.25"))
        self.assertEqual(pos.unrealized_pnl(Decimal("1.00")), Decimal("0.50"))

    def test_realized_pnl_pure_function(self) -> None:
        self.assertEqual(realized_pnl_for_exit(Decimal("0.90"), Decimal("0.80"), Decimal("3")), Decimal("-0.30"))
        self.assertEqual(realized_pnl_for_exit(Decimal("0.50"), Decimal("0.60"), Decimal("2")), Decimal("0.20"))

    def test_as_dict_stringifies_decimals(self) -> None:
        pos = Position(key="k1", token_id="no-1", side="BUY", outcome="NO")
        pos = pos.apply_fill(Decimal("0.90"), Decimal("2"))
        data = pos.as_dict()
        self.assertEqual(data["shares"], "2")
        self.assertEqual(data["avg_price"], "0.9000")
        self.assertEqual(data["realized_pnl_usdc"], "0")


if __name__ == "__main__":
    unittest.main()
