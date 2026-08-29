"""Test matrix for the unified execution layer (audit §18/§19).

Verifies actual quantities and states, not just "function returned True".
Run: python3 -m pytest tests/test_execution_layer.py -v
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal

import pytest

from execution import (
    LiveExecutor,
    OrderIntent,
    OrderState,
    PaperExecutor,
    RiskGate,
)
from execution.audit import AuditWriter


@dataclass(frozen=True)
class Book:
    asks: tuple
    timestamp: str = "2026-08-30T00:00:00Z"
    book_hash: str = "hash-1"
    fetched_at_epoch: float = 0.0

    @property
    def best_ask(self):
        return min((x["price"] for x in self.asks), default=None)


def intent(qty="5", price="0.93", token_id="123", order_id="o1"):
    return OrderIntent(
        order_id=order_id, token_id=token_id, side="BUY_NO",
        price=Decimal(price), quantity=Decimal(qty), order_type="FAK",
        strategy="test", signal_reason="matrix", created_at="2026-08-30T00:00:00Z",
    )


def fresh_book(asks):
    return Book(asks=asks, fetched_at_epoch=time.time())


def executor():
    return PaperExecutor()


def gate(**kw):
    defaults = dict(max_book_age_seconds=3.0, min_order_size=Decimal("1"), max_order_size=Decimal("100"))
    defaults.update(kw)
    return RiskGate(**defaults)


def run_fill(qty, asks):
    b = fresh_book(asks)
    g = gate()
    d = g.evaluate(intent(qty=qty), b)
    assert d.allowed, f"risk rejected: {d.code}"
    return executor().execute(intent(qty=qty), b, Decimal("0.01"))


def test_1_full_fill():
    r = run_fill("5", ({"price": Decimal("0.92"), "size": Decimal("5")},))
    assert r.state == OrderState.FILLED
    assert r.filled_shares == Decimal("5")
    assert r.remaining_shares == Decimal("0")


def test_2_partial_fill():
    r = run_fill("5", ({"price": Decimal("0.92"), "size": Decimal("2")},))
    assert r.state == OrderState.PARTIALLY_FILLED
    assert r.filled_shares == Decimal("2")
    assert r.remaining_shares == Decimal("3")


def test_3_fak_cancel_remainder():
    r = run_fill("5", ({"price": Decimal("0.92"), "size": Decimal("2")},))
    assert r.cancel_reason == "CANCEL_REMAINDER"
    assert r.filled_shares + r.remaining_shares == Decimal("5")


def test_4_zero_fill_cancelled():
    b = fresh_book(({"price": Decimal("0.99"), "size": Decimal("5")},))
    r = executor().execute(intent(price="0.93"), b, Decimal("0.01"))
    assert r.state == OrderState.CANCELLED
    assert r.filled_shares == Decimal("0")


def test_5_price_above_max_rejected():
    g = gate(max_price=Decimal("0.98"))
    d = g.evaluate(intent(price="0.99"), fresh_book(({"price": Decimal("0.92"), "size": Decimal("5")},)))
    assert not d.allowed
    assert d.code == "PRICE_ABOVE_MAX"


def test_6_stale_book_rejected():
    b = Book(asks=({"price": Decimal("0.92"), "size": Decimal("5")},), fetched_at_epoch=time.time() - 100)
    d = gate().evaluate(intent(), b)
    assert not d.allowed
    assert d.code == "STALE_BOOK"


def test_7_duplicate_order_rejected():
    b = fresh_book(({"price": Decimal("0.92"), "size": Decimal("5")},))
    open_order = executor().execute(intent(), b, Decimal("0.01"))
    d = gate().evaluate(intent(), b, open_orders=[open_order])
    assert not d.allowed
    assert d.code == "DUPLICATE_ORDER"


def test_8_disconnect_then_resnapshot():
    # disconnected -> no book -> fail-closed
    d = gate().evaluate(intent(), None)
    assert not d.allowed
    assert d.code == "STALE_OR_MISSING_BOOK"
    # re-snapshot -> fresh book -> eligible again
    d2 = gate().evaluate(intent(), fresh_book(({"price": Decimal("0.92"), "size": Decimal("5")},)))
    assert d2.allowed


def test_9_position_matches_fill(tmp_path):
    w = AuditWriter(tmp_path / "audit.sqlite3")
    b = fresh_book(({"price": Decimal("0.92"), "size": Decimal("5")},))
    now = "2026-08-30T00:00:00Z"
    filled = executor().execute(intent(), b, Decimal("0.01"))
    w.record_fill(filled, mode="paper", now_utc=now, debit_usdc=Decimal("4.60"), ledger_key="debit|city|day")
    # position advanced by filled shares
    assert Decimal(w.store.get_ledger("position|123|BUY_NO")) == Decimal("5")
    # a cancelled order must NOT change position
    cancelled = executor().execute(intent(order_id="o2", price="0.93"), fresh_book(({"price": Decimal("0.99"), "size": Decimal("5")},)), Decimal("0.01"))
    assert cancelled.state == OrderState.CANCELLED
    assert w.record_fill(cancelled, mode="paper", now_utc=now, debit_usdc=Decimal("0"), ledger_key="debit|city|day") == -1
    assert Decimal(w.store.get_ledger("position|123|BUY_NO")) == Decimal("5")
    w.close()


def test_10_paper_live_share_model():
    # live and paper use the same OrderIntent + RiskGate; only the executor differs
    live = LiveExecutor()
    i = intent()
    r = live.execute(i)
    assert r.state == OrderState.REJECTED
    assert r.reject_reason == "LIVE_EXECUTOR_DISABLED"
    # RiskGate rejects live mode at the gate too (fail-closed double layer)
    d = gate().evaluate(i, fresh_book(({"price": Decimal("0.92"), "size": Decimal("5")},)), mode="live")
    assert d.code == "LIVE_EXECUTOR_DISABLED"
