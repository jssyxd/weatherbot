"""Paper execution: deterministic L2-depth matching with explicit FAK remainder.

This replaces the scattered, divergent paper-fill logic with one matcher used by
Paper (and as the reference model for Live). It walks the real ask/bid depth,
produces partial fills, computes a size-weighted average price, and — for FAK —
explicitly cancels the unfilled remainder.

Fee model: ``fee_rate * price * (1 - price)`` per share (Polymarket's
binary-outcome fee schedule), with ``fee_rate`` supplied by the caller.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN
from typing import Any, Iterable

from execution.market import BookView
from execution.order_intent import Fill, OrderStatus, OrderType, Side, utc_now_iso

_SIZE_QUANTUM = Decimal("0.01")


@dataclass
class MatchResult:
    order_id: str
    status: OrderStatus
    filled_shares: Decimal
    remaining_shares: Decimal          # > 0 only for non-FAK resting orders
    average_price: Decimal | None
    principal_usdc: Decimal
    fee_usdc: Decimal
    fills: tuple[Fill, ...] = field(default_factory=tuple)
    cancel_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "status": self.status.value,
            "filled_shares": str(self.filled_shares),
            "remaining_shares": str(self.remaining_shares),
            "average_price": str(self.average_price) if self.average_price is not None else None,
            "principal_usdc": str(self.principal_usdc),
            "fee_usdc": str(self.fee_usdc),
            "fills": [f.as_dict() for f in self.fills],
            "cancel_reason": self.cancel_reason,
        }


def _walk_levels(
    levels: Iterable[tuple[Decimal, Decimal]],
    target_shares: Decimal,
    *,
    max_price: Decimal,
    fee_rate: Decimal,
) -> list[tuple[Decimal, Decimal, Decimal]]:
    """Walk sorted levels; return [(price, shares, level_fee), ...]."""
    filled = Decimal("0")
    fills: list[tuple[Decimal, Decimal, Decimal]] = []
    for price, size in levels:
        if price > max_price or size <= 0:
            continue
        quantity = min(size, target_shares - filled).quantize(_SIZE_QUANTUM, rounding=ROUND_DOWN)
        if quantity <= 0:
            continue
        level_fee = quantity * fee_rate * price * (Decimal("1") - price)
        filled += quantity
        fills.append((price, quantity, level_fee))
        if filled >= target_shares:
            break
    return fills


def _build_result(
    intent: Any,
    levels: Iterable[tuple[Decimal, Decimal]],
    *,
    side: Side,
    fee_rate: Decimal,
    max_price: Decimal,
    order_type: OrderType,
) -> MatchResult:
    raw = _walk_levels(levels, intent.quantity, max_price=max_price, fee_rate=fee_rate)
    fills = tuple(
        Fill(order_id=intent.order_id, token_id=intent.token_id, side=side,
             price=price, shares=shares, fee_usdc=fee, filled_at=utc_now_iso())
        for price, shares, fee in raw
    )
    filled = sum((f.shares for f in fills), Decimal("0"))
    principal = sum((f.price * f.shares for f in fills), Decimal("0"))
    fees = sum((f.fee_usdc for f in fills), Decimal("0"))
    remaining = intent.quantity - filled

    if remaining > 0:
        if order_type is OrderType.FAK:
            # FAK: anything unfilled is cancelled immediately.
            return MatchResult(
                order_id=intent.order_id,
                status=OrderStatus.CANCELLED,
                filled_shares=filled,
                remaining_shares=Decimal("0"),
                average_price=(principal / filled) if filled > 0 else None,
                principal_usdc=principal,
                fee_usdc=fees,
                fills=fills,
                cancel_reason="FAK_REMAINDER_CANCELLED",
            )
        # GTC: unfilled size rests.
        return MatchResult(
            order_id=intent.order_id,
            status=OrderStatus.PARTIALLY_FILLED,
            filled_shares=filled,
            remaining_shares=remaining,
            average_price=(principal / filled) if filled > 0 else None,
            principal_usdc=principal,
            fee_usdc=fees,
            fills=fills,
        )

    return MatchResult(
        order_id=intent.order_id,
        status=OrderStatus.FILLED,
        filled_shares=filled,
        remaining_shares=Decimal("0"),
        average_price=(principal / filled) if filled > 0 else None,
        principal_usdc=principal,
        fee_usdc=fees,
        fills=fills,
    )


def match_fak(
    intent: Any,
    book: BookView,
    *,
    fee_rate: Decimal = Decimal("0"),
    max_price: Decimal | None = None,
) -> MatchResult:
    """Match a FAK intent against the full depth on its side.

    ``max_price`` caps the walk (defaults to the intent's own limit). BUY walks
    asks ascending; SELL walks bids. Returns FILLED, or CANCELLED with
    ``cancel_reason == "FAK_REMAINDER_CANCELLED"`` on partial, or CANCELLED with
    zero fill. Never reports a fill above available depth.
    """
    cap = max_price if max_price is not None else intent.price
    if intent.side == Side.BUY:
        levels = book.asks
        side = Side.BUY
    else:
        levels = book.bids
        side = Side.SELL
    return _build_result(intent, levels, side=side, fee_rate=fee_rate, max_price=cap, order_type=OrderType.FAK)


def match_gtc(
    intent: Any,
    book: BookView,
    *,
    fee_rate: Decimal = Decimal("0"),
) -> MatchResult:
    """Match a GTC limit order against depth up to its limit price.

    A GTC only fills levels at or better than its limit; any unfilled size
    rests (``remaining_shares > 0``, ``status == PARTIALLY_FILLED``).
    """
    if intent.side == Side.BUY:
        levels = book.asks
        side = Side.BUY
    else:
        levels = book.bids
        side = Side.SELL
    return _build_result(intent, levels, side=side, fee_rate=fee_rate, max_price=intent.price, order_type=OrderType.GTC)
