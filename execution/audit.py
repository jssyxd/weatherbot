"""Audit writer: full order traceability + fill->position->PnL (audit §14/§15).

Every order's lifecycle (created -> risk -> submit -> fill/cancel) is recorded
under one order_id. Only actual fills change position and debit the risk_ledger,
so Paper PnL stays consistent with the order lifecycle. This closes the gap
where risk_ledger existed but was never written by production code.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from audit_store import AuditStore

from .order_state import OrderRecord


class AuditWriter:
    """Thin wrapper over the SQLite AuditStore; owns the ledger writes."""

    def __init__(self, db_path: Path) -> None:
        self.store = AuditStore(db_path)

    def record_order(self, record: OrderRecord, *, mode: str, now_utc: str) -> int:
        """Append the order's current state as one audit event."""
        return self.store.append(
            created_at_utc=now_utc,
            event_type=f"order_{record.state.value.lower()}",
            correlation_id=record.intent.order_id,
            mode=mode,
            token_id=record.intent.token_id,
            payload=record.as_dict(),
        )

    def record_fill(self, record: OrderRecord, *, mode: str, now_utc: str, debit_usdc: Decimal, ledger_key: str) -> int:
        """Only a fill (filled_shares > 0) changes position and debit."""
        if record.filled_shares <= 0:
            return -1
        # position = cumulative filled shares for this token
        position_key = f"position|{record.intent.token_id}|{record.intent.side}"
        prior = Decimal(self.store.get_ledger(position_key))
        self.store.set_ledger(position_key, str(prior + record.filled_shares), now_utc)
        # spend = cumulative debit for this city-day bucket
        prior_debit = Decimal(self.store.get_ledger(ledger_key))
        self.store.set_ledger(ledger_key, str(prior_debit + debit_usdc), now_utc)
        return self.store.append(
            created_at_utc=now_utc,
            event_type="order_fill",
            correlation_id=record.intent.order_id,
            mode=mode,
            token_id=record.intent.token_id,
            payload={
                **record.as_dict(),
                "debit_usdc": str(debit_usdc),
                "position_shares": str(prior + record.filled_shares),
            },
        )

    def close(self) -> None:
        self.store.close()
