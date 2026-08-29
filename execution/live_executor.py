"""LiveExecutor: thin fail-closed stub (audit §20).

Live trading is OFF for the current phase. This executor never signs, never
submits, and never loads credentials. It exists so the Paper/Live boundary is
explicit and a future authenticated adapter can slot in without touching the
strategy or RiskGate.

When live is eventually enabled, this becomes a thin adapter over the official
Polymarket py-sdk (do NOT reimplement signing / auth / order serialization).
"""
from __future__ import annotations

from typing import Any

from .order_intent import OrderIntent
from .order_state import OrderRecord, OrderState


class LiveExecutor:
    """Rejects every order with LIVE_EXECUTOR_DISABLED. No network, no keys."""

    def __init__(self) -> None:
        self._enabled = False  # hard fail-closed; never auto-enable

    def execute(self, intent: OrderIntent) -> OrderRecord:
        return OrderRecord(
            intent=intent,
            state=OrderState.REJECTED,
            reject_reason="LIVE_EXECUTOR_DISABLED",
        )

    def cancel(self, order_record: OrderRecord) -> OrderRecord:
        return order_record.mark(OrderState.CANCELLED, cancel_reason="LIVE_EXECUTOR_DISABLED")
