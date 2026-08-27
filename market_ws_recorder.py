#!/usr/bin/env python3
"""Paper-only Polymarket market WebSocket recorder.

The recorder stores raw public market-channel frames and transport-health
metadata. It cannot load user configuration, sign, submit, cancel, or query
account orders. It is suitable for collecting future L2 replay evidence.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    import websocket
except ImportError as exc:  # pragma: no cover - environment dependent
    raise SystemExit("websocket-client is required: pip install websocket-client") from exc

from data_recorder import AppendOnlyRecorder
from local_order_book import OrderBookStateError
from websocket_market_data import MARKET_WS_URL, MarketStream, MarketStreamError

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100"
USER_AGENT = "weatherbot-tree6-paper-recorder/1.0"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def session_id() -> str:
    # PID avoids collision if a supervisor restarts this recorder within one second.
    return f"ws-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{os.getpid()}"


def parse_tokens(value: str | None) -> list[str]:
    if not value:
        return []
    tokens = list(dict.fromkeys(token.strip() for token in value.split(",") if token.strip()))
    if not tokens:
        raise ValueError("no_valid_token_ids")
    return tokens


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


def describe_message(raw: str | bytes) -> tuple[str, Any]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="strict")
    if raw.strip().upper() in {"PING", "PONG"}:
        return raw.strip().lower(), raw
    payload = json.loads(raw)
    if isinstance(payload, list):
        return "book_array" if payload and all(isinstance(item, dict) for item in payload) else "invalid_array", payload
    if isinstance(payload, dict):
        return str(payload.get("event_type", payload.get("type", "unknown"))).lower(), payload
    return "invalid_shape", payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Paper-only public market WebSocket recorder")
    parser.add_argument("--duration-seconds", type=int, default=300)
    parser.add_argument("--data-lake-root", type=Path, required=True)
    parser.add_argument("--token-ids", help="comma-separated public token ids; default discovers one active token")
    parser.add_argument("--reconnect-delay-seconds", type=float, default=2.0)
    args = parser.parse_args()
    if not 5 <= args.duration_seconds <= 86_400:
        parser.error("duration must be between 5 and 86400 seconds")
    if not 0.1 <= args.reconnect_delay_seconds <= 60:
        parser.error("reconnect delay must be between 0.1 and 60 seconds")

    started_wall = datetime.now(timezone.utc)
    if (started_wall + timedelta(seconds=args.duration_seconds)).date() != started_wall.date():
        parser.error("duration would cross a UTC data partition; restart the recorder after midnight")
    tokens = parse_tokens(args.token_ids) or [first_active_token()]
    stream = MarketStream(tokens)
    recorder = AppendOnlyRecorder(
        args.data_lake_root, date_utc=started_wall.strftime("%Y-%m-%d"), stream="market_ws",
        source="polymarket", session_id=session_id(),
    )
    counts: Counter[str] = Counter()
    expected_controls: Counter[str] = Counter()
    parse_errors: Counter[str] = Counter()
    reconnect_count = 0
    ping_count = 0
    current_socket = None
    deadline = time.monotonic() + args.duration_seconds

    try:
        while time.monotonic() < deadline:
            try:
                current_socket = websocket.create_connection(MARKET_WS_URL, timeout=5, origin="https://polymarket.com")
                current_socket.settimeout(2)
                stream.mark_connected()
                current_socket.send(json.dumps(stream.subscription_message()))
                reconnect_count += 1
                recorder.append(
                    {"kind": "connected", "subscription": stream.subscription_message(), "socket_epoch": reconnect_count},
                    event_type="transport_connected", metadata={"socket_epoch": reconnect_count},
                )
                last_ping = time.monotonic()
                while time.monotonic() < deadline:
                    now = time.monotonic()
                    if now - last_ping >= 10:
                        current_socket.send("PING")
                        last_ping = now
                        ping_count += 1
                    try:
                        raw = current_socket.recv()
                    except Exception as exc:
                        if "timeout" in exc.__class__.__name__.lower():
                            continue
                        raise
                    kind, decoded = describe_message(raw)
                    counts[kind] += 1
                    recorder.append(
                        {"raw_message": raw, "decoded": decoded}, event_type=kind,
                        metadata={"socket_epoch": reconnect_count, "token_ids": tokens},
                    )
                    try:
                        stream.handle_message(raw)
                    except OrderBookStateError as exc:
                        if str(exc) == "book_baseline_required":
                            expected_controls["increment_before_baseline"] += 1
                        else:
                            parse_errors[str(exc)] += 1
                    except (MarketStreamError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                        parse_errors[str(exc)] += 1
                break
            except Exception as exc:
                stream.mark_disconnected(f"transport_{exc.__class__.__name__.lower()}")
                recorder.note_error(f"transport_{exc.__class__.__name__.lower()}")
                recorder.append(
                    {"kind": "disconnected", "error_class": exc.__class__.__name__, "socket_epoch": reconnect_count},
                    event_type="transport_disconnected", metadata={"socket_epoch": reconnect_count},
                )
                if time.monotonic() < deadline:
                    time.sleep(args.reconnect_delay_seconds)
            finally:
                if current_socket is not None:
                    try:
                        current_socket.close()
                    except Exception:
                        pass
                    current_socket = None

        final_books = {token: stream.snapshot(token, max_age_seconds=30) for token in tokens}
        ready_count = sum(snapshot is not None for snapshot in final_books.values())
        recorder.write_health({
            "duration_seconds_requested": args.duration_seconds,
            "token_ids": tokens,
            "event_counts": dict(counts),
            "expected_controls": dict(expected_controls),
            "parse_errors": dict(parse_errors),
            "socket_epochs": reconnect_count,
            "ping_count": ping_count,
            "ready_book_count": ready_count,
            "all_books_ready": ready_count == len(tokens),
            "stream_connected": stream.connected,
            "stream_subscribed": stream.subscribed,
            "protocol_status": "PASS" if ready_count == len(tokens) and not parse_errors else "BLOCKED",
        })
    finally:
        health_path = recorder.close()

    health = json.loads(health_path.read_text(encoding="utf-8"))
    print(json.dumps({"status": health["extra"].get("protocol_status", "BLOCKED"), "part": str(recorder.part_path), "health": str(health_path), "records": health["records_written"], "safety": health["safety"]}, ensure_ascii=False))
    return 0 if health["extra"].get("protocol_status") == "PASS" else 3


if __name__ == "__main__":
    raise SystemExit(main())
