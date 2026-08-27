"""Translate deterministic local-order-book snapshots into tree11 evidence.

This module is intentionally transport-free. A separate public WebSocket
recorder supplies ``LocalBookSnapshot`` objects; this adapter binds each YES
token to the contemporaneously captured Gamma market-rule identity before
writing a paper-replay record. It cannot submit, cancel, or query orders.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from local_order_book import LocalBookSnapshot


class L2EvidenceError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def yes_bucket_index(rules: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """Build an unambiguous YES token -> market-rule/bucket map.

    A duplicate token across two buckets is an identity failure, not a choice.
    Such a token is omitted so that downstream paper research fails closed.
    """
    index: dict[str, dict[str, str]] = {}
    duplicates: set[str] = set()
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("enabled", True) is not True:
            continue
        rule_id = str(rule.get("market_rule_id") or "")
        direction = str(rule.get("direction") or "")
        city_id = str(rule.get("city_id") or "")
        local_date = str(rule.get("market_local_date") or "")
        if not rule_id or direction not in {"high", "low"} or not city_id or not local_date:
            continue
        for bucket in rule.get("buckets", []):
            if not isinstance(bucket, dict):
                continue
            token = str(bucket.get("yes_token_id") or "")
            bucket_id = str(bucket.get("bucket_id") or "")
            if not token or not bucket_id:
                continue
            identity = {"market_rule_id": rule_id, "bucket_id": bucket_id, "city_id": city_id, "market_local_date": local_date, "direction": direction}
            if token in index and index[token] != identity:
                duplicates.add(token)
            else:
                index[token] = identity
    for token in duplicates:
        index.pop(token, None)
    return index


def _level(value: tuple[dict[str, Decimal], ...]) -> list[dict[str, str]]:
    return [{"price": str(row["price"]), "size": str(row["size"])} for row in value]


def snapshot_evidence(
    snapshot: LocalBookSnapshot, *, received_monotonic_ns: int, token_index: dict[str, dict[str, str]],
    received_at_utc: str | None = None, source_session_id: str | None = None,
) -> dict[str, Any] | None:
    """Convert a ready mapped book into one immutable paper-replay record.

    The caller must supply the recorder's monotonic receive timestamp, not the
    market's exchange timestamp. Missing tick/min-order metadata is represented
    faithfully; the consensus/execution stage will reject rather than infer it.
    """
    if not isinstance(received_monotonic_ns, int) or received_monotonic_ns <= 0:
        raise L2EvidenceError("received_monotonic_ns_required")
    identity = token_index.get(str(snapshot.token_id))
    if identity is None or not snapshot.ready:
        return None
    return {
        "schema_version": "1.0", "event_type": "tree11_l2_snapshot", "received_at_utc": received_at_utc or utc_now(),
        "received_monotonic_ns": received_monotonic_ns, "source_session_id": source_session_id,
        "token_id": str(snapshot.token_id), "market_rule_id": identity["market_rule_id"], "bucket_id": identity["bucket_id"],
        "city_id": identity["city_id"], "market_local_date": identity["market_local_date"], "direction": identity["direction"],
        "ready": True, "book_hash": snapshot.book_hash, "exchange_timestamp": snapshot.exchange_timestamp,
        "book_version": snapshot.version, "tick_size": str(snapshot.tick_size) if snapshot.tick_size is not None else None,
        "min_order_size": str(snapshot.min_order_size) if snapshot.min_order_size is not None else None,
        "bids": _level(snapshot.bids), "asks": _level(snapshot.asks),
        "safety": {"paper_only": True, "orders_submitted": 0, "credentials_loaded": False},
    }
