"""Minimal Position + PnL model driven by fills.

Before this module the repo had no PnL anywhere (audit-c M1: "全程无 PnL
计算"): positions were a bare ``shares``/``avg_price`` dict and exits never
settled anything.  ``Position`` keeps the weighted-average cost basis and
tracks realized PnL as fills/exits happen, so a paper run can answer "did this
bucket win or lose".

This is deliberately small and pure: no I/O, no strategy logic.  Paper and
future Live share the same math.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

ZERO = Decimal("0")


@dataclass(frozen=True)
class Position:
    """A long position in one outcome token."""

    key: str
    token_id: str
    side: str  # BUY
    outcome: str  # YES | NO
    shares: Decimal = ZERO
    avg_price: Decimal = ZERO
    realized_pnl_usdc: Decimal = ZERO
    # Cost basis in USDC = shares * avg_price (fees are tracked separately by
    # the paper ledger, not folded into PnL here).
    cost_basis_usdc: Decimal = ZERO
    fills: list[dict] = field(default_factory=list)

    def apply_fill(self, price: Decimal, size: Decimal) -> "Position":
        """Add a buy fill, updating the weighted-average cost basis."""
        size = Decimal(str(size))
        if size <= 0:
            return self
        price = Decimal(str(price))
        prev_shares = self.shares
        prev_basis = self.cost_basis_usdc
        total_shares = prev_shares + size
        new_basis = prev_basis + price * size
        avg = (new_basis / total_shares).quantize(Decimal("0.0001")) if total_shares > 0 else ZERO
        return Position(
            key=self.key,
            token_id=self.token_id,
            side=self.side,
            outcome=self.outcome,
            shares=total_shares,
            avg_price=avg,
            realized_pnl_usdc=self.realized_pnl_usdc,
            cost_basis_usdc=new_basis,
            fills=self.fills + [{"price": str(price), "size": str(size)}],
        )

    def apply_exit(self, price: Decimal, size: Decimal) -> "Position":
        """Reduce the position at ``price`` and realize PnL on the closed part."""
        size = Decimal(str(size))
        if size <= 0:
            return self
        price = Decimal(str(price))
        close = min(size, self.shares)
        if close <= 0:
            return self
        realized = (price - self.avg_price) * close
        remaining_shares = self.shares - close
        remaining_basis = self.cost_basis_usdc - self.avg_price * close
        avg = (remaining_basis / remaining_shares).quantize(Decimal("0.0001")) if remaining_shares > 0 else ZERO
        return Position(
            key=self.key,
            token_id=self.token_id,
            side=self.side,
            outcome=self.outcome,
            shares=remaining_shares,
            avg_price=avg,
            realized_pnl_usdc=self.realized_pnl_usdc + realized,
            cost_basis_usdc=remaining_basis,
            fills=self.fills + [{"exit_price": str(price), "size": str(close)}],
        )

    def unrealized_pnl(self, mark_price: Decimal) -> Decimal:
        """Mark-to-market on the remaining shares (long: (mark - avg) * shares)."""
        return (Decimal(str(mark_price)) - self.avg_price) * self.shares

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "token_id": self.token_id,
            "side": self.side,
            "outcome": self.outcome,
            "shares": str(self.shares),
            "avg_price": str(self.avg_price),
            "cost_basis_usdc": str(self.cost_basis_usdc),
            "realized_pnl_usdc": str(self.realized_pnl_usdc),
            "fills": self.fills,
        }


def realized_pnl_for_exit(avg_price: Decimal, exit_price: Decimal, shares: Decimal) -> Decimal:
    """Pure realized-PnL formula for a SELL of a long position.

    (exit_price - avg_price) * shares; positive means the exit made money.
    """
    return (Decimal(str(exit_price)) - Decimal(str(avg_price))) * Decimal(str(shares))
