"""Independent risk gate shared by Paper and Live execution.

Every OrderIntent — paper or live — must pass :meth:`RiskGate.check` before an
executor may act on it. The gate is transport-agnostic: it consumes the
normalized :class:`execution.market.BookView` produced by the orderbook adapter.

Checks (audit spec §9): price band, quantity, book presence/freshness, slippage,
duplicate order, max position, and available capital.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable

from execution.market import BookView
from execution.order_intent import OrderIntent, OrderStatus, Side, utc_now_iso


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    code: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "code": self.code, "message": self.message}


@dataclass
class RiskConfig:
    min_price: Decimal = Decimal("0.05")
    max_price: Decimal = Decimal("0.98")
    max_order_size: Decimal = Decimal("5")
    max_position_shares: Decimal = Decimal("25")
    max_book_age_seconds: float = 3.0
    max_slippage: Decimal = Decimal("0.10")   # allowed adverse move vs best bid/ask
    duplicate_window_seconds: float = 60.0


def _dup_key(intent: OrderIntent) -> tuple[str, ...]:
    return (
        intent.market or "",
        intent.token_id,
        intent.side.value,
        format(intent.price, "f"),
        intent.strategy,
        intent.order_type.value,
    )


class RiskGate:
    """Fail-closed pre-trade gate. No live state; pure function of its inputs."""

    def __init__(self, config: RiskConfig | None = None) -> None:
        self.config = config or RiskConfig()

    def check(
        self,
        intent: OrderIntent,
        book: BookView | None,
        *,
        open_orders: Iterable[OrderIntent] = (),
        current_position_shares: Decimal = Decimal("0"),
        available_capital_usdc: Decimal | None = None,
        min_order_size: Decimal | None = None,
    ) -> RiskDecision:
        cfg = self.config

        # Price band.
        if intent.price < cfg.min_price:
            return RiskDecision(False, "PRICE_BELOW_MIN", f"limit {intent.price} < min {cfg.min_price}")
        if intent.price > cfg.max_price:
            return RiskDecision(False, "PRICE_ABOVE_MAX", f"limit {intent.price} > max {cfg.max_price}")

        # Quantity.
        if intent.quantity <= 0:
            return RiskDecision(False, "QUANTITY_NONPOSITIVE", "quantity must be > 0")
        if intent.quantity > cfg.max_order_size:
            return RiskDecision(False, "QUANTITY_ABOVE_MAX", f"quantity {intent.quantity} > max {cfg.max_order_size}")

        # Book presence / freshness / completeness.
        if book is None or not book.ready:
            return RiskDecision(False, "BOOK_MISSING_OR_INCOMPLETE", "executable book is required")
        if book.token_id != intent.token_id:
            return RiskDecision(False, "BOOK_TOKEN_MISMATCH", "book token does not match intent token")
        if book.age_seconds > cfg.max_book_age_seconds:
            return RiskDecision(False, "STALE_BOOK", f"book age {book.age_seconds:.2f}s > {cfg.max_book_age_seconds}s")

        # Slippage: adverse move between intent limit and current best quote.
        ref = book.best_ask if intent.side is Side.BUY else book.best_bid
        if ref is None:
            return RiskDecision(False, "NO_QUOTE", "no best quote on the required side")
        slip = (ref - intent.price) / ref if intent.side is Side.BUY else (intent.price - ref) / ref
        if slip > cfg.max_slippage:
            return RiskDecision(False, "SLIPPAGE_EXCEEDED", f"adverse move {slip:.4f} > max {cfg.max_slippage}")

        # Min order size (quantity, not fill).
        if min_order_size is not None and intent.quantity < min_order_size:
            return RiskDecision(False, "BELOW_MIN_ORDER_SIZE", f"quantity {intent.quantity} < min {min_order_size}")

        # Duplicate order protection against currently open intents.
        new_key = _dup_key(intent)
        for existing in open_orders:
            if existing.order_id == intent.order_id:
                continue
            if existing.status is not None and existing.status.terminal:
                continue
            if _dup_key(existing) == new_key:
                return RiskDecision(False, "DUPLICATE_OPEN_ORDER", "an equivalent open order already exists")

        # Max position.
        if intent.side is Side.BUY:
            projected = current_position_shares + intent.quantity
        else:
            projected = current_position_shares - intent.quantity
        if projected > cfg.max_position_shares or projected < 0:
            return RiskDecision(False, "MAX_POSITION_EXCEEDED", f"projected position {projected} out of [0, {cfg.max_position_shares}]")

        # Available capital (paper cash; live uses exchange balance, so optional).
        if available_capital_usdc is not None and intent.side is Side.BUY:
            cost = intent.quantity * intent.price
            if cost > available_capital_usdc:
                return RiskDecision(False, "INSUFFICIENT_CAPITAL", f"cost {cost} > available {available_capital_usdc}")

        return RiskDecision(True, "RISK_PASS", "all pre-trade gates passed")
