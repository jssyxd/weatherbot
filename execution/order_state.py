"""Order state machine: a small, explicit lifecycle for every order (audit §8).

Avoids the vague "下单成功" state. Every order starts CREATED, passes through
RiskGate, and ends in exactly one terminal state.

FAK lifecycle:
    CREATED -> (risk) -> SUBMITTED -> match -> FULL=FILLED
                                             PARTIAL=PARTIALLY_FILLED -> CANCEL_REMAINDER
                                             ZERO=CANCELLED
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any


class OrderState(str, Enum):
    CREATED = "CREATED"
    RISK_REJECTED = "RISK_REJECTED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    ACKED = "ACKED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    ERROR = "ERROR"


TERMINAL_STATES = frozenset({
    OrderState.RISK_REJECTED,
    OrderState.FILLED,
    OrderState.CANCELLED,
    OrderState.REJECTED,
    OrderState.ERROR,
})


@dataclass
class OrderRecord:
    """One order tracked end-to-end (audit §14: full traceability)."""

    intent: Any
    state: OrderState = OrderState.CREATED
    filled_shares: Decimal = Decimal("0")
    average_fill_price: Decimal | None = None
    remaining_shares: Decimal | None = None
    reject_reason: str | None = None
    cancel_reason: str | None = None
    book_timestamp: str | None = None
    book_age_seconds: float | None = None
    book_hash: str | None = None

    def __post_init__(self) -> None:
        if self.remaining_shares is None:
            self.remaining_shares = self.intent.quantity - self.filled_shares

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def mark(self, state: OrderState, **kwargs: Any) -> "OrderRecord":
        """Return a copy advanced to `state` (state machine is a pure function)."""
        values = {
            "intent": self.intent,
            "state": state,
            "filled_shares": self.filled_shares,
            "average_fill_price": self.average_fill_price,
            "remaining_shares": self.remaining_shares,
            "reject_reason": self.reject_reason,
            "cancel_reason": self.cancel_reason,
            "book_timestamp": self.book_timestamp,
            "book_age_seconds": self.book_age_seconds,
            "book_hash": self.book_hash,
        }
        values.update(kwargs)
        return OrderRecord(**values)

    def as_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.intent.order_id,
            "token_id": self.intent.token_id,
            "side": self.intent.side,
            "order_type": self.intent.order_type,
            "requested_price": str(self.intent.price),
            "requested_size": str(self.intent.quantity),
            "state": self.state.value,
            "filled_size": str(self.filled_shares),
            "remaining_size": str(self.remaining_shares),
            "average_fill_price": str(self.average_fill_price) if self.average_fill_price is not None else None,
            "reject_reason": self.reject_reason,
            "cancel_reason": self.cancel_reason,
            "book_timestamp": self.book_timestamp,
            "book_age_seconds": self.book_age_seconds,
            "book_hash": self.book_hash,
            "strategy": self.intent.strategy,
            "signal_reason": self.intent.signal_reason,
        }
