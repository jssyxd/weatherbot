"""Tree3 market runtime: WS market data before signal and execution.

Transport is intentionally injected. This keeps the deterministic core
replayable and prevents accidental network/order side effects in tests.
"""
from __future__ import annotations

import time
from typing import Any, Iterable

from local_order_book import LocalBookSnapshot
from websocket_market_data import MarketStream


class Tree3MarketRuntime:
    def __init__(self, token_ids: Iterable[str], *, max_book_age_seconds: float = 3.0, clock=None) -> None:
        self.clock = clock or time.time
        self.max_book_age_seconds = max_book_age_seconds
        self.stream = MarketStream(token_ids, clock=self.clock)
        self.state = "disconnected"
        self.reconnect_count = 0
        self.last_event_at: float | None = None

    def connect(self) -> dict[str, Any]:
        self.state = "connected"
        self.reconnect_count += 1
        return self.stream.mark_connected()

    def subscribed(self) -> None:
        self.stream.mark_subscribed()
        self.state = "ready_for_book"

    def on_message(self, message: str | bytes | dict[str, Any]) -> Any:
        result = self.stream.handle_message(message)
        self.last_event_at = self.clock()
        if isinstance(result, LocalBookSnapshot) and result.ready:
            self.state = "running"
        elif isinstance(result, tuple) and result and all(item.ready for item in result):
            self.state = "running"
        return result

    def disconnect(self, reason: str = "socket_lost") -> None:
        self.stream.mark_disconnected(reason)
        self.state = "reconnecting"

    def local_snapshot(self, token_id: str) -> LocalBookSnapshot | None:
        snapshot = self.stream.snapshot(token_id, max_age_seconds=self.max_book_age_seconds, now=self.clock())
        if snapshot is None:
            return None
        return snapshot

    def health(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "connected": self.stream.connected,
            "subscribed": self.stream.subscribed,
            "reconnect_count": self.reconnect_count,
            "event_count": self.stream.event_count,
            "last_event_at": self.last_event_at,
            "last_error": self.stream.last_error,
            "ready_tokens": sum(1 for book in self.stream.books.values() if book.ready),
            "token_count": len(self.stream.books),
        }
