"""Unified order-intent model shared by Paper and Live execution.

This is the single source of truth for "what a strategy wants to trade".
Strategy code must produce an :class:`OrderIntent` and must never talk to
Polymarket directly. Field set follows the audit spec §7:

    order_id / token_id / side / price / quantity /
    order_type / strategy / signal_reason / created_at

Only fields Paper and Live reconciliation actually need are added (market,
outcome). No speculative fields.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_order_id() -> str:
    return uuid.uuid4().hex


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    GTC = "GTC"  # Good-Til-Cancelled: rest until filled or cancelled
    FAK = "FAK"  # Fill-And-Kill: fill what is available, cancel the remainder
    FOK = "FOK"  # Fill-Or-Kill: full fill or nothing
    GTD = "GTD"  # Good-Til-Date: expire at a set timestamp


class OrderStatus(str, Enum):
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

    @property
    def terminal(self) -> bool:
        return self in TERMINAL_STATUSES


TERMINAL_STATUSES = frozenset({
    OrderStatus.RISK_REJECTED,
    OrderStatus.FILLED,
    OrderStatus.CANCELLED,
    OrderStatus.REJECTED,
    OrderStatus.ERROR,
})


@dataclass
class OrderIntent:
    """A strategy's request to trade. Free of execution and risk details."""

    order_id: str
    token_id: str
    side: Side
    price: Decimal          # limit price (BUY: max price willing to pay)
    quantity: Decimal       # requested size in shares
    order_type: OrderType
    strategy: str
    signal_reason: str
    created_at: str         # ISO-8601 UTC
    market: str | None = None
    outcome: str | None = None   # "YES" | "NO" | None

    @classmethod
    def new(
        cls,
        *,
        token_id: Any,
        side: Side | str,
        price: Any,
        quantity: Any,
        order_type: OrderType | str,
        strategy: str,
        signal_reason: str,
        market: str | None = None,
        outcome: str | None = None,
        created_at: str | None = None,
        order_id: str | None = None,
    ) -> "OrderIntent":
        return cls(
            order_id=order_id or new_order_id(),
            token_id=str(token_id),
            side=Side(side),
            price=Decimal(str(price)),
            quantity=Decimal(str(quantity)),
            order_type=OrderType(order_type),
            strategy=strategy,
            signal_reason=signal_reason,
            created_at=created_at or utc_now_iso(),
            market=market,
            outcome=outcome,
        )

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["side"] = self.side.value
        value["order_type"] = self.order_type.value
        value["price"] = str(self.price)
        value["quantity"] = str(self.quantity)
        return value


@dataclass(frozen=True)
class Fill:
    """One matched price level of an execution.

    Only a Fill mutates Position. Never fabricate a Fill for unmatched size.
    """

    order_id: str
    token_id: str
    side: Side
    price: Decimal
    shares: Decimal
    fee_usdc: Decimal = Decimal("0")
    filled_at: str = field(default_factory=utc_now_iso)

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["side"] = self.side.value
        value["price"] = str(self.price)
        value["shares"] = str(self.shares)
        value["fee_usdc"] = str(self.fee_usdc)
        return value
