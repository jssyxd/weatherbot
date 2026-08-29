"""Independent pre-trade risk gate shared by Paper and Live.

Every order, paper or live, must pass the same checks before reaching an
executor. The gate is deliberately free of strategy logic: it only asks "is this
order safe and executable right now?".
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from execution.order_intent import OrderIntent, OrderStatus


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    code: str
    reason: str

    def as_dict(self) -> dict:
        return {"allowed": self.allowed, "code": self.code, "reason": self.reason}


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        d = Decimal(str(value))
    except Exception:
        return None
    return d if d.is_finite() else None


def _get(book: Any, name: str) -> Any:
    if isinstance(book, dict):
        return book.get(name)
    return getattr(book, name, None)


def risk_check(
    *,
    intent: OrderIntent,
    book: Any,
    min_price: Decimal,
    max_price: Decimal,
    max_slippage: Decimal,
    max_book_age_seconds: float,
    now_epoch: float | None = None,
) -> RiskDecision:
    """Run all pre-trade checks. Returns the first failure, else allowed."""
    import time

    if intent.quantity <= 0:
        return RiskDecision(False, "INVALID_QUANTITY", "quantity must be > 0")
    if not min_price <= intent.price <= max_price:
        return RiskDecision(
            False, "PRICE_OUTSIDE_GATE",
            f"price {intent.price} outside [{min_price}, {max_price}]",
        )

    if book is None:
        return RiskDecision(False, "MISSING_BOOK", "order book is required")

    # Book age / timestamp.
    fetched = _get(book, "fetched_at_epoch")
    if fetched is not None and (now_epoch or time.time()) - float(fetched) > max_book_age_seconds:
        return RiskDecision(False, "STALE_BOOK", "order book is stale")

    best_ask = _dec(_get(book, "best_ask"))
    best_bid = _dec(_get(book, "best_bid"))
    reference = best_ask if intent.side == "BUY" else best_bid
    if reference is None:
        return RiskDecision(False, "NO_EXECUTABLE_SIDE", "no executable reference price")

    # Slippage: effective reference must not exceed limit by more than max_slippage.
    slip = reference - intent.price if intent.side == "BUY" else intent.price - reference
    if slip > max_slippage:
        return RiskDecision(
            False, "SLIPPAGE_EXCEEDED",
            f"reference {reference} exceeds limit {intent.price} by {slip}",
        )

    return RiskDecision(True, "RISK_OK", "pre-trade checks passed")


def rejected_status(decision: RiskDecision) -> OrderStatus:
    """Map a failed gate decision to the canonical order status."""
    return OrderStatus.RISK_REJECTED
