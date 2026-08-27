#!/usr/bin/env python3
"""Record and replay one public Polymarket market stream for smoke testing.

This is deliberately a read-only test utility. It discovers a public active
asset, subscribes to the public market WebSocket, sends only PING heartbeats,
writes raw messages to JSONL, and feeds messages into MarketStream. It never
loads configuration, credentials, private keys, or an order client.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import websocket
except ImportError as exc:  # pragma: no cover - depends on environment
    raise SystemExit("websocket-client is required: pip install websocket-client") from exc

# Running this file directly places only scripts/ on sys.path. Add the repository
# root explicitly so the smoke test imports the local adapter under test.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from local_order_book import OrderBookStateError
from websocket_market_data import MARKET_WS_URL, MarketStream, MarketStreamError

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100"
USER_AGENT = "weatherbot-tree6-readonly-ws-smoke/1.0"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def first_active_token() -> str:
    request = urllib.request.Request(GAMMA_MARKETS_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, list):
        raise RuntimeError("gamma_market_list_invalid")
    for market in payload:
        if not isinstance(market, dict):
            continue
        token_ids: Any = market.get("clobTokenIds")
        if isinstance(token_ids, str):
            try:
                token_ids = json.loads(token_ids)
            except json.JSONDecodeError:
                continue
        if isinstance(token_ids, list) and token_ids and isinstance(token_ids[0], str) and token_ids[0]:
            return token_ids[0]
    raise RuntimeError("no_active_clob_token_found")


def classify_payload(payload: Any) -> list[str]:
    if isinstance(payload, dict):
        return [str(payload.get("event_type", payload.get("type", "unknown"))).lower()]
    if isinstance(payload, list) and payload and all(isinstance(item, dict) for item in payload):
        # Official initial subscriptions are an array of bare book objects.
        return ["book"] * len(payload)
    return ["invalid_raw_shape"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only public WSS smoke test")
    parser.add_argument("--duration-seconds", type=int, default=300)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--token-id", help="optional public token id; otherwise discover one through Gamma")
    args = parser.parse_args()
    if not 5 <= args.duration_seconds <= 900:
        print("duration must be between 5 and 900 seconds", file=sys.stderr)
        return 2

    started_at_utc = utc_now()
    token_id = args.token_id or first_active_token()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    raw_path = args.output_dir / "market_ws_raw.jsonl"
    summary_path = args.output_dir / "summary.json"
    stream = MarketStream([token_id])
    event_counts: Counter[str] = Counter()
    parse_errors: Counter[str] = Counter()
    expected_controls: Counter[str] = Counter()
    received = 0
    ping_count = 0
    started = time.monotonic()
    last_ping = started
    socket = None

    try:
        socket = websocket.create_connection(MARKET_WS_URL, timeout=5, origin="https://polymarket.com")
        socket.settimeout(2)
        subscription = stream.mark_connected()
        socket.send(json.dumps(subscription))
        with raw_path.open("w", encoding="utf-8") as handle:
            while time.monotonic() - started < args.duration_seconds:
                now = time.monotonic()
                if now - last_ping >= 10:
                    socket.send("PING")
                    last_ping = now
                    ping_count += 1
                try:
                    message = socket.recv()
                except Exception as exc:
                    timeout_name = exc.__class__.__name__.lower()
                    if "timeout" in timeout_name:
                        continue
                    raise
                received += 1
                handle.write(json.dumps({"received_utc": utc_now(), "message": message}, ensure_ascii=False) + "\n")
                if isinstance(message, bytes):
                    message = message.decode("utf-8", errors="strict")
                if isinstance(message, str) and message.strip().upper() in {"PING", "PONG"}:
                    event_counts[message.strip().lower()] += 1
                    stream.handle_message(message)
                    continue
                try:
                    payload = json.loads(message) if isinstance(message, str) else message
                    for kind in classify_payload(payload):
                        event_counts[kind] += 1
                    if event_counts["invalid_raw_shape"]:
                        parse_errors["invalid_raw_shape"] += 1
                        continue
                    stream.handle_message(payload)
                except OrderBookStateError as exc:
                    # The public channel can emit an incremental update ahead of
                    # its initial book array. It is intentionally non-tradable
                    # until a baseline arrives, so record it but do not call it a
                    # schema incompatibility.
                    if str(exc) == "book_baseline_required":
                        expected_controls["increment_before_baseline"] += 1
                    else:
                        parse_errors[str(exc)] += 1
                except (MarketStreamError, ValueError, json.JSONDecodeError) as exc:
                    parse_errors[str(exc)] += 1
        final = stream.snapshot(token_id, max_age_seconds=30)
        status = "PASS" if received > 0 and event_counts["book"] > 0 and final and final.ready and not parse_errors else "BLOCKED"
    except Exception as exc:  # network errors must be a visible test failure
        final = None
        status = "BLOCKED"
        parse_errors[f"network_or_runtime:{exc.__class__.__name__}"] += 1
    finally:
        if socket is not None:
            try:
                socket.close()
            except Exception:
                pass

    summary = {
        "schema_version": "1.0",
        "purpose": "read_only_market_wss_protocol_smoke",
        "status": status,
        "started_at_utc": started_at_utc,
        "ended_at_utc": utc_now(),
        "duration_seconds_requested": args.duration_seconds,
        "token_id": token_id,
        "received_messages": received,
        "event_counts": dict(event_counts),
        "expected_controls": dict(expected_controls),
        "parse_errors": dict(parse_errors),
        "ping_count": ping_count,
        "stream_connected": stream.connected,
        "stream_subscribed": stream.subscribed,
        "final_book_ready": bool(final and final.ready),
        "final_book_version": final.version if final else None,
        "raw_file": str(raw_path),
        "safety": {"read_only": True, "orders_submitted": 0, "credentials_loaded": False},
    }
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if status == "PASS" else 3


if __name__ == "__main__":
    raise SystemExit(main())
