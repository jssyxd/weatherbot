"""Position and minimal realized-PnL accounting.

Only a :class:`execution.order_intent.Fill` mutates a Position (audit §15:
Fill -> Position -> PnL). No PnL is ever derived from a planned or cancelled
order.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from execution.order_intent import Fill, Side, utc_now_iso


@dataclass
class Position:
    token_id: str
    side: Side          # side the position is long (BUY NO / BUY YES)
    shares: Decimal = Decimal("0")
    avg_price: Decimal = Decimal("0")
    realized_pnl_usdc: Decimal = Decimal("0")
    updated_at: str = utc_now_iso()

    def as_dict(self) -> dict[str, Any]:
        return {
            "token_id": self.token_id,
            "side": self.side.value,
            "shares": str(self.shares),
            "avg_price": str(self.avg_price),
            "realized_pnl_usdc": str(self.realized_pnl_usdc),
            "updated_at": self.updated_at,
        }


def apply_fill(position: Position, fill: Fill) -> Position:
    """Apply one Fill to a Position, returning the updated Position.

    BUY increases size with a weighted average cost. SELL reduces size and
    realizes PnL = (sale price - avg cost) * shares sold.
    """
    if fill.token_id != position.token_id:
        raise ValueError("fill token does not match position token")

    if fill.side is Side.BUY:
        total = position.shares + fill.shares
        if total <= 0:
            return replace(position, updated_at=utc_now_iso())
        weighted = (position.avg_price * position.shares + fill.price * fill.shares) / total
        return replace(
            position,
            shares=total,
            avg_price=weighted,
            updated_at=fill.filled_at or utc_now_iso(),
        )

    # SELL: reduce size, realize PnL on the sold slice.
    sold = min(position.shares, fill.shares)
    if sold <= 0:
        return replace(position, updated_at=utc_now_iso())
    realized = (fill.price - position.avg_price) * sold
    return replace(
        position,
        shares=position.shares - sold,
        realized_pnl_usdc=position.realized_pnl_usdc + realized,
        updated_at=fill.filled_at or utc_now_iso(),
    )


def unrealized_pnl_usdc(position: Position, mark_price: Decimal | None) -> Decimal | None:
    """Unrealized PnL at a mark price (None when no mark is available)."""
    if mark_price is None:
        return None
    if position.side is Side.BUY:
        return (mark_price - position.avg_price) * position.shares
    return (position.avg_price - mark_price) * position.shares


def realized_pnl_for_exit(avg_price: Any, exit_price: Any, shares: Any) -> Decimal:
    """Estimated realized PnL of selling ``shares`` at ``exit_price``.

    Long-only convention (BUY NO/YES): (exit_price - avg_price) * shares.
    """
    avg = Decimal(str(avg_price))
    exit_px = Decimal(str(exit_price))
    size = Decimal(str(shares))
    return (exit_px - avg) * size
