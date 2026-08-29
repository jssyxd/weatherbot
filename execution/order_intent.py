"""OrderIntent: the single business-layer order model shared by Paper and Live.

The weather strategy never touches the exchange. It only expresses "what to
buy, at what limit, how much, why" as an OrderIntent. Whether the order can
run, how it fills, and its final state are the executor's concern.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from decimal import Decimal
from typing import Any

SIDE_BUY_NO = "BUY_NO"
SIDE_BUY_YES = "BUY_YES"

ORDER_TYPE_FAK = "FAK"
ORDER_TYPE_GTC = "GTC"

_VALID_SIDES = {SIDE_BUY_NO, SIDE_BUY_YES}
_VALID_ORDER_TYPES = {ORDER_TYPE_FAK, ORDER_TYPE_GTC}


@dataclass(frozen=True)
class OrderIntent:
    """Immutable expression of trading intent (audit §7)."""

    order_id: str
    token_id: str
    side: str              # BUY_NO | BUY_YES
    price: Decimal         # worst acceptable fill price (FAK limit)
    quantity: Decimal      # shares
    order_type: str        # FAK | GTC
    strategy: str          # which strategy produced this intent
    signal_reason: str     # human-readable "why"
    created_at: str        # ISO-8601 UTC

    def __post_init__(self) -> None:
        if self.side not in _VALID_SIDES:
            raise ValueError(f"invalid side: {self.side}")
        if self.order_type not in _VALID_ORDER_TYPES:
            raise ValueError(f"invalid order_type: {self.order_type}")
        if self.quantity <= 0:
            raise ValueError("quantity must be > 0")
        if self.price <= 0:
            raise ValueError("price must be > 0")
        if not str(self.token_id).strip():
            raise ValueError("token_id is required")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
