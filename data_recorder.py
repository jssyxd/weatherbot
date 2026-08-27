"""Append-only raw event recording for paper-only research.

This module has no network, account, signing, order, or strategy dependency.
Collectors hand it raw payloads; it records their receipt order and creates a
hashable daily evidence trail suitable for replay and later data freezing.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

SCHEMA_VERSION = "1.0"


class RecorderError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_payload(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def payload_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_payload(value).encode("utf-8")).hexdigest()


def _safe_component(value: str, field: str) -> str:
    if not value or any(part in value for part in ("/", "\\", "..", "\x00")):
        raise RecorderError(f"invalid_{field}")
    if not all(char.isalnum() or char in "._-=" for char in value):
        raise RecorderError(f"invalid_{field}")
    return value


@dataclass
class RecorderHealth:
    session_id: str
    source: str
    stream: str
    started_at_utc: str
    records_written: int = 0
    duplicate_payloads: int = 0
    errors: list[str] = field(default_factory=list)
    last_received_utc: str | None = None
    last_received_monotonic_ns: int | None = None
    last_payload_sha256: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "session_id": self.session_id,
            "source": self.source,
            "stream": self.stream,
            "started_at_utc": self.started_at_utc,
            "last_received_utc": self.last_received_utc,
            "last_received_monotonic_ns": self.last_received_monotonic_ns,
            "last_payload_sha256": self.last_payload_sha256,
            "records_written": self.records_written,
            "duplicate_payloads": self.duplicate_payloads,
            "errors": list(self.errors),
            "status": "PASS" if not self.errors else "DEGRADED",
            "safety": {"paper_only": True, "orders_submitted": 0, "credentials_recorded": False},
        }


class AppendOnlyRecorder:
    """Write raw collector evidence to ``data_lake/dt=.../<stream>/``.

    A new session creates a new part file. The class never rewrites raw event
    files; health is materialized as a separate atomically-replaced snapshot.
    """

    def __init__(
        self,
        data_lake_root: Path,
        *,
        date_utc: str,
        stream: str,
        source: str,
        session_id: str,
        wall_clock: Callable[[], str] = utc_now,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        try:
            datetime.strptime(date_utc, "%Y-%m-%d")
        except ValueError as exc:
            raise RecorderError("invalid_date_utc") from exc
        self.date_utc = date_utc
        self.stream = _safe_component(stream, "stream")
        self.source = _safe_component(source, "source")
        self.session_id = _safe_component(session_id, "session_id")
        self.wall_clock = wall_clock
        self.monotonic_ns = monotonic_ns
        self.directory = Path(data_lake_root) / f"dt={date_utc}" / self.stream
        self.directory.mkdir(parents=True, exist_ok=True)
        self.part_path = self.directory / f"part-{self.session_id}.jsonl"
        if self.part_path.exists():
            raise RecorderError("session_part_already_exists")
        self._handle = self.part_path.open("x", encoding="utf-8", buffering=1)
        self.health = RecorderHealth(session_id, self.source, self.stream, self.wall_clock())
        self._previous_hash: str | None = None
        self._health_extra: dict[str, Any] = {}

    def append(self, payload: Any, *, event_type: str | None = None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        """Append one raw payload and return the persisted evidence envelope."""
        if self._handle.closed:
            raise RecorderError("recorder_closed")
        received_utc = self.wall_clock()
        received_monotonic_ns = int(self.monotonic_ns())
        digest = payload_sha256(payload)
        envelope = {
            "schema_version": SCHEMA_VERSION,
            "source": self.source,
            "stream": self.stream,
            "session_id": self.session_id,
            "sequence": self.health.records_written + 1,
            "received_utc": received_utc,
            "received_monotonic_ns": received_monotonic_ns,
            "event_type": event_type,
            "payload_sha256": digest,
            "previous_payload_sha256": self._previous_hash,
            "payload": payload,
            "metadata": metadata or {},
            "safety": {"paper_only": True, "orders_submitted": 0, "credentials_recorded": False},
        }
        self._handle.write(canonical_payload(envelope) + "\n")
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self.health.records_written += 1
        if digest == self._previous_hash:
            self.health.duplicate_payloads += 1
        self.health.last_received_utc = received_utc
        self.health.last_received_monotonic_ns = received_monotonic_ns
        self.health.last_payload_sha256 = digest
        self._previous_hash = digest
        return envelope

    def note_error(self, code: str) -> None:
        self.health.errors.append(_safe_component(code, "error_code"))

    def write_health(self, extra: dict[str, Any] | None = None) -> Path:
        if extra is not None:
            self._health_extra.update(extra)
        payload = self.health.to_json() | {"extra": dict(self._health_extra)}
        target = self.directory / f"health-{self.session_id}.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temporary, target)
        return target

    def close(self) -> Path:
        if not self._handle.closed:
            self._handle.close()
        return self.write_health()

    def __enter__(self) -> "AppendOnlyRecorder":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if exc_value is not None:
            self.note_error(f"uncaught_{exc_value.__class__.__name__.lower()}")
        self.close()
