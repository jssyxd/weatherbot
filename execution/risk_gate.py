"""RiskGate: the single, independent pre-trade risk check (audit §9).

Every order — paper or live — must pass RiskGate before execution. The weather
strategy never bypasses it. This module absorbs the old execution_policy.decide_buy_no
and adds the checks it was missing: slippage, max order size, duplicate order,
and available balance.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from decimal import Decimal
from typing import Any, Iterable

from .order_intent import OrderIntent


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    code: str
    message: str
    token_id: str
    mode: str
    best_ask: str | None = None
    book_timestamp: str | None = None
    book_age_seconds: float | None = None
    book_hash: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class RiskGate:
    """Configurable, stateless risk evaluator (no side effects)."""

    def __init__(
        self,
        *,
        min_price: Decimal = Decimal("0.40"),
        max_price: Decimal = Decimal("0.98"),
        max_slippage: Decimal = Decimal("0.10"),
        max_book_age_seconds: float = 3.0,
        min_order_size: Decimal = Decimal("5"),
        max_order_size: Decimal = Decimal("100"),
    ) -> None:
        self.min_price = min_price
        self.max_price = max_price
        self.max_slippage = max_slippage
        self.max_book_age_seconds = max_book_age_seconds
        self.min_order_size = min_order_size
        self.max_order_size = max_order_size

    def evaluate(
        self,
        intent: OrderIntent,
        book,
        *,
        mode: str = "paper",
        available_balance_usdc: Decimal | None = None,
        open_orders: Iterable[Any] = (),
        now_epoch: float | None = None,
    ) -> RiskDecision:
        import time as _time

        token = str(intent.token_id)
        now = _time.time() if now_epoch is None else now_epoch

        if mode not in {"observe", "paper", "live"}:
            return RiskDecision(False, "INVALID_MODE", "unsupported execution mode", token, mode)
        if mode == "live":
            return RiskDecision(False, "LIVE_EXECUTOR_DISABLED", "live trading is disabled in this phase", token, mode)

        # --- book presence / freshness ---
        if book is None:
            return RiskDecision(False, "STALE_OR_MISSING_BOOK", "fresh executable book is required", token, mode)

        best_ask = getattr(book, "best_ask", None)
        asks = getattr(book, "asks", None) or ()
        fetched_at = getattr(book, "fetched_at_epoch", 0.0)
        age = float(now - fetched_at) if fetched_at else float("inf")

        if age > self.max_book_age_seconds:
            return RiskDecision(
                False, "STALE_BOOK", "book snapshot is older than max_book_age_seconds",
                token, mode,
                best_ask=str(best_ask) if best_ask is not None else None,
                book_timestamp=getattr(book, "timestamp", None),
                book_age_seconds=age,
                book_hash=getattr(book, "book_hash", None),
            )
        if best_ask is None:
            return RiskDecision(False, "NO_EXECUTABLE_ASK", "book has no resting ask", token, mode,
                                book_timestamp=getattr(book, "timestamp", None), book_age_seconds=age)

        # --- price / size gates ---
        if intent.price > self.max_price:
            return RiskDecision(False, "PRICE_ABOVE_MAX", "limit price exceeds max_execution_price", token, mode,
                                best_ask=str(best_ask), book_age_seconds=age)
        if intent.price < self.min_price:
            return RiskDecision(False, "PRICE_BELOW_MIN", "limit price below min_execution_price", token, mode,
                                best_ask=str(best_ask), book_age_seconds=age)
        if intent.quantity > self.max_order_size:
            return RiskDecision(False, "SIZE_ABOVE_MAX", "quantity exceeds max_order_size", token, mode,
                                best_ask=str(best_ask), book_age_seconds=age)

        # --- slippage: worst fill must not exceed best_ask + max_slippage ---
        if intent.price - best_ask > self.max_slippage:
            return RiskDecision(False, "SLIPPAGE_EXCEEDED", "limit price exceeds best ask + max_slippage", token, mode,
                                best_ask=str(best_ask), book_age_seconds=age)
        # --- executable depth ---
        # FAK may fill partially (fill-what-you-can, cancel remainder); only GTC
        # requires full depth. Both require at least one min order size.
        executable_depth = sum(
            (Decimal(level["size"]) for level in asks if Decimal(level["price"]) <= intent.price),
            Decimal("0"),
        )
        if intent.order_type == "GTC" and executable_depth < intent.quantity:
            return RiskDecision(False, "DEPTH_LT_TARGET", "executable depth below requested quantity", token, mode,
                                best_ask=str(best_ask), book_age_seconds=age)
        if executable_depth < self.min_order_size:
            return RiskDecision(False, "DEPTH_LT_MIN_ORDER", "executable depth below min order size", token, mode,
                                best_ask=str(best_ask), book_age_seconds=age)

        # --- duplicate / in-flight order ---
        for other in open_orders:
            oi = getattr(other, "intent", other)
            if (
                getattr(oi, "token_id", None) == intent.token_id
                and getattr(oi, "side", None) == intent.side
                and getattr(oi, "price", None) == intent.price
            ):
                return RiskDecision(False, "DUPLICATE_ORDER", "an identical open order already exists", token, mode,
                                    best_ask=str(best_ask), book_age_seconds=age)

        # --- balance ---
        if available_balance_usdc is not None:
            worst_debit = intent.quantity * intent.price
            if worst_debit > available_balance_usdc:
                return RiskDecision(False, "INSUFFICIENT_BALANCE", "order debit exceeds available balance", token, mode,
                                    best_ask=str(best_ask), book_age_seconds=age)

        return RiskDecision(
            True, "PAPER_ELIGIBLE", "fresh ask depth satisfies pre-trade gates",
            token, mode,
            best_ask=str(best_ask),
            book_timestamp=getattr(book, "timestamp", None),
            book_age_seconds=age,
            book_hash=getattr(book, "book_hash", None),
        )
