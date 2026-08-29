"""Unified order model shared by Paper and future Live executors.

Before this module, each strategy (tree1/tree2/tree3/tree12) built its own ad-hoc
order dict, and tree12's paper fill mutated a ``working_orders`` dict directly.
``OrderIntent`` is the single "what I want to trade" value object; the execution
layer turns it into ``Fill``s and eventually a ``Position``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum


class OrderStatus(str, Enum):
    """Deterministic order lifecycle. Paper records every transition."""

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


@dataclass(frozen=True)
class OrderIntent:
    """A strategy's trading desire, without any execution decision baked in.

    ``strategy`` and ``signal_reason`` keep the intent auditable back to the
    weather signal that produced it.
    """

    order_id: str
    token_id: str
    side: str  # BUY | SELL
    outcome: str  # YES | NO
    price: Decimal
    quantity: Decimal
    order_type: str  # FAK | FOK | GTC
    strategy: str
    signal_reason: str
    created_at_utc: str
    metadata: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "token_id": self.token_id,
            "side": self.side,
            "outcome": self.outcome,
            "price": str(self.price),
            "quantity": str(self.quantity),
            "order_type": self.order_type,
            "strategy": self.strategy,
            "signal_reason": self.signal_reason,
            "created_at_utc": self.created_at_utc,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class Fill:
    """One matched level of an execution result."""

    price: Decimal
    size: Decimal

    def as_dict(self) -> dict:
        return {"price": str(self.price), "size": str(self.size)}
