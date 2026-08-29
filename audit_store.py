"""Transactional local audit store for tree2.

SQLite is used as an append-only operational ledger. JSONL remains supported as
an export format, but decisions must be reconstructable from this store.

The store also records a unified ``order_id`` lifecycle
(SIGNAL→INTENT→RISK→SUBMIT→ACK→FILL→CANCEL→POSITION→PNL) so every stage of an
order can be reconstructed from one identifier (PRD Step 7).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at_utc TEXT NOT NULL,
    event_type TEXT NOT NULL,
    correlation_id TEXT,
    mode TEXT,
    token_id TEXT,
    order_id TEXT,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_events(created_at_utc);
CREATE INDEX IF NOT EXISTS idx_audit_token ON audit_events(token_id);
CREATE TABLE IF NOT EXISTS risk_ledger (
    ledger_key TEXT PRIMARY KEY,
    debit_usdc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);
"""

# Lifecycle stages, in order (PRD Step 7 / audit-c 4.2).
ORDER_STAGES = (
    "SIGNAL", "INTENT", "RISK", "SUBMIT", "ACK",
    "FILL", "CANCEL", "POSITION", "PNL",
)


class AuditStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, timeout=10, isolation_level="DEFERRED")
        self._connection.executescript(SCHEMA)
        self._migrate_order_id_column()
        # The order_id index is created after the migration so a legacy DB
        # (without order_id) can be upgraded without the SCHEMA index failing.
        self._connection.execute("CREATE INDEX IF NOT EXISTS idx_audit_order ON audit_events(order_id)")
        self._connection.commit()

    def _migrate_order_id_column(self) -> None:
        """Add the ``order_id`` column to databases created before Step 7."""
        columns = {
            row[1]
            for row in self._connection.execute("PRAGMA table_info(audit_events)").fetchall()
        }
        if "order_id" not in columns:
            self._connection.execute("ALTER TABLE audit_events ADD COLUMN order_id TEXT")
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_order ON audit_events(order_id)"
            )

    def append(self, *, created_at_utc: str, event_type: str, payload: dict[str, Any], correlation_id: str | None = None, mode: str | None = None, token_id: str | None = None, order_id: str | None = None) -> int:
        with self._connection:
            cursor = self._connection.execute(
                "INSERT INTO audit_events(created_at_utc,event_type,correlation_id,mode,token_id,order_id,payload_json) VALUES(?,?,?,?,?,?,?)",
                (created_at_utc, event_type, correlation_id, mode, token_id, order_id, json.dumps(payload, ensure_ascii=False, sort_keys=True)),
            )
        return int(cursor.lastrowid)

    def append_order_event(self, *, created_at_utc: str, order_id: str, stage: str, payload: dict[str, Any] | None = None, mode: str | None = None, token_id: str | None = None) -> int:
        """Record one lifecycle stage for an order under its unified order_id."""
        if stage not in ORDER_STAGES:
            raise ValueError(f"unknown_order_stage:{stage}")
        return self.append(
            created_at_utc=created_at_utc,
            event_type="order_lifecycle",
            correlation_id=order_id,
            mode=mode,
            token_id=token_id,
            order_id=order_id,
            payload={"stage": stage, **(payload or {})},
        )

    def events_for_order(self, order_id: str) -> list[dict[str, Any]]:
        """All audit rows carrying this order_id, oldest first."""
        rows = self._connection.execute(
            "SELECT created_at_utc,event_type,correlation_id,mode,token_id,order_id,payload_json "
            "FROM audit_events WHERE order_id=? ORDER BY id ASC",
            (str(order_id),),
        ).fetchall()
        out = []
        for created_at_utc, event_type, correlation_id, mode, token_id, order_id, payload_json in rows:
            try:
                payload = json.loads(payload_json)
            except json.JSONDecodeError:
                payload = {}
            out.append({
                "created_at_utc": created_at_utc, "event_type": event_type,
                "correlation_id": correlation_id, "mode": mode, "token_id": token_id,
                "order_id": order_id, "payload": payload,
            })
        return out

    def get_ledger(self, key: str) -> str:
        row = self._connection.execute("SELECT debit_usdc FROM risk_ledger WHERE ledger_key=?", (key,)).fetchone()
        return str(row[0]) if row else "0"

    def set_ledger(self, key: str, debit_usdc: str, updated_at_utc: str) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT INTO risk_ledger(ledger_key,debit_usdc,updated_at_utc) VALUES(?,?,?) ON CONFLICT(ledger_key) DO UPDATE SET debit_usdc=excluded.debit_usdc, updated_at_utc=excluded.updated_at_utc",
                (key, debit_usdc, updated_at_utc),
            )

    def close(self) -> None:
        self._connection.close()
