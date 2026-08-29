"""Deterministic order state machine shared by Paper and Live.

The committed ``execution.order_intent.OrderStatus`` enum defines the states;
this module adds the *transition* rules and the FAK "fill what you can, cancel
the rest" semantics that the enum alone cannot express.

Composes with ``execution.paper_executor.match_fak`` / ``match_gtc``: the matcher answers "how
much fills against this book", and the state machine answers "what is the
order's lifecycle status now, and why".

FAK partial fill is recorded as ``PARTIALLY_FILLED`` (filled_size > 0) and then
settled to terminal ``CANCELLED`` with ``cancel_reason == "FAK_REMAINDER_CANCELLED"``,
so an audit trail can always distinguish "filled 3 then cancelled 2" from a
zero-fill cancel (filled_size == 0) and from a full fill.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from execution.order_intent import OrderStatus

# Allowed outgoing transitions per source state (audit spec §8).
_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.CREATED: frozenset({OrderStatus.SUBMITTING, OrderStatus.RISK_REJECTED}),
    OrderStatus.SUBMITTING: frozenset({OrderStatus.SUBMITTED, OrderStatus.REJECTED, OrderStatus.ERROR}),
    OrderStatus.SUBMITTED: frozenset({OrderStatus.ACKED, OrderStatus.REJECTED, OrderStatus.ERROR}),
    OrderStatus.ACKED: frozenset({
        OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED,
    }),
    OrderStatus.PARTIALLY_FILLED: frozenset({
        OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED,
    }),
    OrderStatus.RISK_REJECTED: frozenset(),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
    OrderStatus.ERROR: frozenset(),
}

TERMINAL = frozenset({
    OrderStatus.RISK_REJECTED, OrderStatus.FILLED,
    OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.ERROR,
})

FAK_REMAINDER_CANCELLED = "FAK_REMAINDER_CANCELLED"


class InvalidTransitionError(ValueError):
    pass


@dataclass
class OrderState:
    order_id: str
    status: OrderStatus
    filled_size: Decimal
    remaining_size: Decimal
    cancel_reason: str | None = None
    reject_reason: str | None = None
    error_message: str | None = None

    @classmethod
    def created(cls, order_id: str, quantity: Any) -> "OrderState":
        return cls(
            order_id=order_id,
            status=OrderStatus.CREATED,
            filled_size=Decimal("0"),
            remaining_size=Decimal(str(quantity)),
        )

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL

    def as_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "status": self.status.value,
            "filled_size": str(self.filled_size),
            "remaining_size": str(self.remaining_size),
            "cancel_reason": self.cancel_reason,
            "reject_reason": self.reject_reason,
            "error_message": self.error_message,
        }


class OrderStateMachine:
    """Validates and applies status transitions for one order."""

    def __init__(self, state: OrderState) -> None:
        self.state = state

    def can_transition(self, to: OrderStatus) -> bool:
        return to in _TRANSITIONS.get(self.state.status, frozenset())

    def transition(self, to: OrderStatus, *, reason: str | None = None) -> OrderState:
        if not self.can_transition(to):
            raise InvalidTransitionError(
                f"illegal transition {self.state.status.value} -> {to.value}"
            )
        self.state = replace(
            self.state,
            status=to,
            cancel_reason=reason if to is OrderStatus.CANCELLED else self.state.cancel_reason,
            reject_reason=reason if to in (OrderStatus.REJECTED, OrderStatus.RISK_REJECTED) else self.state.reject_reason,
            error_message=reason if to is OrderStatus.ERROR else self.state.error_message,
        )
        return self.state

    def apply_match(self, filled_size: Any, *, order_type: str = "FAK") -> OrderState:
        """Apply a match of ``filled_size`` and settle the resulting status.

        FAK: unfilled remainder is cancelled immediately (terminal CANCELLED,
        cancel_reason FAK_REMAINDER_CANCELLED, filled_size preserved).
        GTC: unfilled remainder rests (terminal-lite PARTIALLY_FILLED).
        """
        filled = Decimal(str(filled_size))
        if filled < 0 or filled > self.state.remaining_size:
            raise ValueError("filled_size out of range for remaining_size")
        remaining = self.state.remaining_size - filled
        filled_total = self.state.filled_size + filled

        if remaining > 0:
            if order_type == "FAK":
                self.state = replace(
                    self.state,
                    status=OrderStatus.CANCELLED,
                    filled_size=filled_total,
                    remaining_size=Decimal("0"),
                    cancel_reason=FAK_REMAINDER_CANCELLED,
                )
                return self.state
            self.state = replace(
                self.state,
                status=OrderStatus.PARTIALLY_FILLED,
                filled_size=filled_total,
                remaining_size=remaining,
            )
            return self.state
        self.state = replace(
            self.state,
            status=OrderStatus.FILLED,
            filled_size=filled_total,
            remaining_size=Decimal("0"),
        )
        return self.state

    def cancel(self, *, reason: str) -> OrderState:
        """Cancel an open (non-terminal) order."""
        if self.state.terminal:
            raise InvalidTransitionError("cannot cancel a terminal order")
        self.state = replace(
            self.state,
            status=OrderStatus.CANCELLED,
            remaining_size=Decimal("0"),
            cancel_reason=reason,
        )
        return self.state

