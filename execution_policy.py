"""Fail-closed execution policy shared by paper and future live adapters."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class ExecutionDecision:
    allowed: bool
    code: str
    message: str
    token_id: str
    mode: str
    best_ask: str | None = None
    book_timestamp: str | None = None
    book_hash: str | None = None
    book_age_seconds: float | None = None
    executable_depth_shares: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def decide_buy_no(
    *,
    mode: str,
    token_id: str,
    book_summary: dict[str, Any],
    target_shares: Decimal,
    min_order_size: Decimal,
    max_price: Decimal = Decimal("0.95"),
    max_book_age_seconds: float = 3.0,
) -> ExecutionDecision:
    """Decide whether a BUY_NO is eligible; never creates/submits an order."""
    token = str(token_id)
    if mode not in {"observe", "paper", "live"}:
        return ExecutionDecision(False, "INVALID_MODE", "unsupported execution mode", token, mode)
    if mode == "live":
        return ExecutionDecision(False, "LIVE_EXECUTOR_DISABLED", "tree2 has no enabled live executor", token, mode)
    if book_summary.get("status") == "STALE_OR_MISSING_BOOK":
        return ExecutionDecision(False, "STALE_OR_MISSING_BOOK", "fresh executable book is required", token, mode)
    if book_summary.get("status") == "EMPTY_ASK":
        return ExecutionDecision(
            False,
            "EMPTY_ASK",
            "NO has no resting ask; displayed price is not executable liquidity",
            token,
            mode,
            book_timestamp=book_summary.get("book_timestamp"),
            book_hash=book_summary.get("book_hash"),
        )
    age = float(book_summary.get("book_age_seconds", 10**9))
    best_ask = Decimal(str(book_summary.get("best_ask"))) if book_summary.get("best_ask") is not None else None
    depth = Decimal(str(book_summary.get("ask_depth_shares", "0")))
    if age > max_book_age_seconds:
        code = "STALE_BOOK"
    elif best_ask is None:
        code = "NO_EXECUTABLE_ASK"
    elif best_ask >= max_price:
        code = "ASK_OUTSIDE_LIMIT"
    elif depth < target_shares:
        code = "DEPTH_LT_TARGET"
    elif depth < min_order_size:
        code = "DEPTH_LT_MIN_ORDER"
    else:
        return ExecutionDecision(
            True,
            "PAPER_ELIGIBLE",
            "fresh ask depth satisfies paper pre-trade gates",
            token,
            mode,
            best_ask=str(best_ask),
            book_timestamp=book_summary.get("book_timestamp"),
            book_hash=book_summary.get("book_hash"),
            book_age_seconds=age,
            executable_depth_shares=str(depth),
        )
    return ExecutionDecision(
        False,
        code,
        "paper pre-trade gate rejected the snapshot",
        token,
        mode,
        best_ask=str(best_ask) if best_ask is not None else None,
        book_timestamp=book_summary.get("book_timestamp"),
        book_hash=book_summary.get("book_hash"),
        book_age_seconds=age,
        executable_depth_shares=str(depth),
    )
