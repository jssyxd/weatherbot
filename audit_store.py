"""Transactional local audit store for tree2.

SQLite is used as an append-only operational ledger. JSONL remains supported as
an export format, but decisions must be reconstructable from this store.
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


class AuditStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, timeout=10, isolation_level="DEFERRED")
        self._connection.executescript(SCHEMA)
        self._connection.commit()

    def append(self, *, created_at_utc: str, event_type: str, payload: dict[str, Any], correlation_id: str | None = None, mode: str | None = None, token_id: str | None = None) -> int:
        with self._connection:
            cursor = self._connection.execute(
                "INSERT INTO audit_events(created_at_utc,event_type,correlation_id,mode,token_id,payload_json) VALUES(?,?,?,?,?,?)",
                (created_at_utc, event_type, correlation_id, mode, token_id, json.dumps(payload, ensure_ascii=False, sort_keys=True)),
            )
        return int(cursor.lastrowid)

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
