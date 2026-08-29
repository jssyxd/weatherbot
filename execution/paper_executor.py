"""PaperExecutor: simulate a FAK BUY against a fresh L2 book (audit §4/§5/§13).

Correct FAK semantics — "fill as much as possible, cancel the remainder":
walk ask levels from best to worst, fill up to the requested quantity, then
cancel whatever remains. Full fill / partial fill / zero fill are all modeled,
and the result is an OrderRecord with an explicit state (not a bare dict).
"""
from __future__ import annotations

from decimal import Decimal, ROUND_DOWN

from .order_intent import OrderIntent
from .order_state import OrderRecord, OrderState

_ONE = Decimal("1")


def _floor_size(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_DOWN)


class PaperExecutor:
    """Pure FAK simulator; no network. Takes a book snapshot + fee rate."""

    def __init__(self, *, min_price: Decimal = Decimal("0.40")) -> None:
        self.min_price = min_price

    def execute(self, intent: OrderIntent, book, fee_rate: Decimal) -> OrderRecord:
        asks = sorted(
            ({"price": Decimal(level["price"]), "size": Decimal(level["size"])} for level in (book.asks or ())),
            key=lambda level: level["price"],
        )

        filled = Decimal("0")
        principal = Decimal("0")
        fees = Decimal("0")

        for level in asks:
            price, size = level["price"], level["size"]
            if price > intent.price:
                break
            if price < self.min_price or size <= 0:
                continue
            quantity = min(size, intent.quantity - filled)
            quantity = _floor_size(quantity)
            if quantity <= 0:
                break
            # Polymarket NO-share fee model (reused from legacy paper execution)
            per_share_fee = fee_rate * price * (_ONE - price)
            principal += quantity * price
            fees += quantity * per_share_fee
            filled += quantity
            if filled >= intent.quantity:
                break

        if filled <= 0:
            # zero fill: FAK with nothing matched -> cancelled, no position change
            return OrderRecord(
                intent=intent,
                state=OrderState.CANCELLED,
                filled_shares=Decimal("0"),
                average_fill_price=None,
                remaining_shares=intent.quantity,
                cancel_reason="NO_EXECUTABLE_DEPTH",
                book_timestamp=getattr(book, "timestamp", None),
                book_hash=getattr(book, "book_hash", None),
            )

        avg_price = (principal + fees) / filled  # all-in cost per share

        if filled >= intent.quantity:
            return OrderRecord(
                intent=intent,
                state=OrderState.FILLED,
                filled_shares=filled,
                average_fill_price=avg_price,
                remaining_shares=Decimal("0"),
                book_timestamp=getattr(book, "timestamp", None),
                book_hash=getattr(book, "book_hash", None),
            )

        # partial fill: FAK cancels the unmatched remainder
        return OrderRecord(
            intent=intent,
            state=OrderState.PARTIALLY_FILLED,
            filled_shares=filled,
            average_fill_price=avg_price,
            remaining_shares=intent.quantity - filled,
            cancel_reason="CANCEL_REMAINDER",
            book_timestamp=getattr(book, "timestamp", None),
            book_hash=getattr(book, "book_hash", None),
        )
