"""Depth-aware paper order matching against a real L2 order book.

The only correct way to simulate a fill is to walk the visible price levels in
price-time priority and consume size level by level. This is the single source
of truth for FAK/FOK/GTC matching; it replaces the four ad-hoc simulators that
previously existed (``paper_execution``, ``tree2_execution``,
``tree3_execution``, ``tree12_paper_fill``).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any

from execution.order_intent import Fill


@dataclass
class MatchResult:
    filled_size: Decimal
    filled_notional: Decimal
    fills: list[Fill]
    avg_price: Decimal | None
    # For FAK: the portion that could not be matched and is cancelled.
    cancelled_size: Decimal
    status: str  # FILLED | PARTIALLY_FILLED | CANCELLED

    def as_dict(self) -> dict:
        return {
            "filled_size": str(self.filled_size),
            "filled_notional": str(self.filled_notional),
            "avg_price": str(self.avg_price) if self.avg_price is not None else None,
            "fills": [f.as_dict() for f in self.fills],
            "cancelled_size": str(self.cancelled_size),
            "status": self.status,
        }


def _levels(book: Any, side: str) -> list[tuple[Decimal, Decimal]]:
    """Return sorted (price, size) levels for the given taker side.

    BUY walks asks ascending; SELL walks bids descending. Accepts either a
    dict-shaped book (``{"asks": [...], "bids": [...]}``) or an object with
    ``.asks`` / ``.bids`` attributes (e.g. ``BookSnapshot``).
    """
    if book is None:
        return []
    if isinstance(book, dict):
        raw = book.get("asks") if side == "BUY" else book.get("bids")
    elif hasattr(book, "asks") and hasattr(book, "bids"):
        raw = list(book.asks) if side == "BUY" else list(book.bids)
    else:
        raw = None
    if not isinstance(raw, (list, tuple)):
        return []
    parsed: list[tuple[Decimal, Decimal]] = []
    for level in raw:
        if not isinstance(level, dict):
            continue
        price = Decimal(str(level.get("price")))
        size = Decimal(str(level.get("size") or 0))
        if size > 0 and price > 0:
            parsed.append((price, size))
    reverse = side == "SELL"
    parsed.sort(key=lambda row: row[0], reverse=reverse)
    return parsed


def match_l2(
    *,
    book: Any,
    side: str,
    quantity: Decimal,
    limit_price: Decimal,
    order_type: str = "FAK",
) -> MatchResult:
    """Match ``quantity`` against real book depth at/inside ``limit_price``.

    - BUY: walk asks ascending, fill while ask <= limit_price.
    - SELL: walk bids descending, fill while bid >= limit_price.
    - FAK: unmatched remainder is reported as ``cancelled_size``.
    - FOK: if the full quantity cannot fill, nothing fills.
    """
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    levels = _levels(book, side)
    target = Decimal(str(quantity))
    filled = Decimal("0")
    notional = Decimal("0")
    fills: list[Fill] = []

    for price, size in levels:
        if filled >= target:
            break
        if side == "BUY" and price > limit_price:
            break
        if side == "SELL" and price < limit_price:
            break
        take = min(size, target - filled)
        if take <= 0:
            continue
        filled += take
        notional += take * price
        fills.append(Fill(price=price, size=take))

    if order_type == "FOK" and filled < target:
        return MatchResult(
            filled_size=Decimal("0"), filled_notional=Decimal("0"), fills=[],
            avg_price=None, cancelled_size=target, status="CANCELLED",
        )

    remaining = target - filled
    status = "FILLED" if remaining == 0 else ("PARTIALLY_FILLED" if filled > 0 else "CANCELLED")
    return MatchResult(
        filled_size=filled,
        filled_notional=notional,
        fills=fills,
        avg_price=(notional / filled).quantize(Decimal("0.0001")) if filled > 0 else None,
        cancelled_size=remaining,
        status=status,
    )
