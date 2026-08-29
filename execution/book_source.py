"""Local-order-book bridge: the paper execution path reads books from the WS
local-order-book state machine, with the REST snapshot as the priming source
and the fallback.

Before this module, the main loop only consumed REST ``/books`` snapshots
(``fetch_tree5_books``) and the WebSocket/local-order-book layer was dead code
(audit-b: "WS 层未接入主循环").  ``LocalBookSource`` wires the existing
``MarketStream``/``LocalOrderBook`` state machine into the data path:

- ``prime()`` feeds every REST snapshot into the local book as a full book
  event, so the local book always has a baseline.
- ``books_for()`` returns a local book when it is ready and fresh, otherwise
  falls back to the REST snapshot (fail-closed, never guesses).
- ``disconnect()`` invalidates every local book so a lost stream can never be
  mistaken for a live one; the next ``prime()`` re-establishes the baseline.

The transport stays injected (as the existing WS layer documents): this module
never opens a socket itself.  A real transport can feed the same
``MarketStream`` later without changing the consumer.
"""
from __future__ import annotations

import time
from typing import Any, Iterable

from local_order_book import LocalOrderBook
from tree3_runtime import Tree3MarketRuntime

MAX_AGE_DEFAULT = 3.0


class LocalBookSource:
    """Owns one Tree3 market runtime and merges local + REST books."""

    def __init__(
        self,
        token_ids: Iterable[str],
        *,
        max_book_age_seconds: float = MAX_AGE_DEFAULT,
        clock=None,
    ) -> None:
        self.max_book_age_seconds = float(max_book_age_seconds)
        self.runtime = Tree3MarketRuntime(
            token_ids, max_book_age_seconds=self.max_book_age_seconds, clock=clock
        )
        self.primed_at_epoch: float | None = None
        self.last_error: str | None = None

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> None:
        self.runtime.connect()
        self.runtime.subscribed()

    def ensure_tokens(self, token_ids: Iterable[str]) -> None:
        """Extend the runtime with books for tokens that appeared after startup.

        The main loop's candidate token set changes every scan, so the bridge
        grows the local book map instead of forcing a fixed subscription list.
        """
        stream = self.runtime.stream
        for token_id in token_ids:
            key = str(token_id)
            if key and key not in stream.books:
                stream.books[key] = LocalOrderBook(key, clock=self.runtime.clock)

    def prime(self, books_by_token: dict[str, Any]) -> None:
        """Feed REST snapshots into the local books as full book events."""
        fed = 0
        for token_id, snapshot in books_by_token.items():
            event = _rest_to_book_event(str(token_id), snapshot)
            if event is None:
                continue
            try:
                self.runtime.on_message({"type": "book", "payload": event})
                fed += 1
            except Exception as exc:  # one bad token must not kill the batch
                self.last_error = f"{type(exc).__name__}: {exc}"
        if fed:
            self.primed_at_epoch = float(self.runtime.clock())
            self.last_error = None

    def disconnect(self, reason: str = "socket_lost") -> None:
        self.runtime.disconnect(reason)
        self.primed_at_epoch = None

    # -- read path ---------------------------------------------------------

    def local_snapshot(self, token_id: str) -> Any | None:
        """Fresh, ready local book for a token, else None."""
        snapshot = self.runtime.local_snapshot(str(token_id))
        if snapshot is None:
            return None
        return _local_to_dict(snapshot)

    def books_for(
        self,
        token_ids: Iterable[str],
        rest_books: dict[str, Any],
        *,
        max_book_age_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Local book first, REST fallback, always fail-closed.

        A token with a fresh ready local book is served from the local book;
        otherwise the REST snapshot (if any) is used.  A token with neither is
        simply absent from the result.
        """
        max_age = self.max_book_age_seconds if max_book_age_seconds is None else float(max_book_age_seconds)
        merged: dict[str, Any] = {}
        for token_id in token_ids:
            key = str(token_id)
            local = self._fresh_local(key, max_age)
            if local is not None:
                merged[key] = local
                continue
            rest = rest_books.get(key)
            if rest is not None:
                merged[key] = rest
        return merged

    def _fresh_local(self, token_id: str, max_age: float) -> Any | None:
        book = self.runtime.stream.books.get(token_id)
        if book is None or not book.ready:
            return None
        if not book.is_fresh(max_age, now=self.runtime.clock()):
            return None
        return _local_to_dict(book.snapshot())

    def health(self) -> dict[str, Any]:
        return {
            **self.runtime.health(),
            "primed_at_epoch": self.primed_at_epoch,
            "last_error": self.last_error,
            "max_book_age_seconds": self.max_book_age_seconds,
        }


# -- shape adapters --------------------------------------------------------


def _rest_to_book_event(token_id: str, snapshot: Any) -> dict[str, Any] | None:
    """Convert a REST BookSnapshot (or dict) into a WS book event payload."""
    if snapshot is None:
        return None
    asks = _levels_of(snapshot, "asks")
    bids = _levels_of(snapshot, "bids")
    if asks is None or bids is None:
        return None
    event: dict[str, Any] = {
        "market": _field(snapshot, "market"),
        "tokenId": token_id,
        "timestamp": _field(snapshot, "timestamp"),
        "hash": _field(snapshot, "book_hash") or _field(snapshot, "hash"),
        "minOrderSize": _str_field(snapshot, "min_order_size"),
        "tickSize": _str_field(snapshot, "tick_size"),
        "negRisk": _field(snapshot, "neg_risk"),
        "lastTradePrice": None,
        "bids": bids,
        "asks": asks,
    }
    return event


def _levels_of(snapshot: Any, name: str) -> list[dict[str, str]] | None:
    if isinstance(snapshot, dict):
        raw = snapshot.get(name)
    else:
        raw = getattr(snapshot, name, None)
    if not isinstance(raw, (list, tuple)):
        return None
    levels: list[dict[str, str]] = []
    for level in raw:
        if not isinstance(level, dict):
            continue
        price = level.get("price")
        size = level.get("size")
        if price is None or size is None:
            continue
        levels.append({"price": str(price), "size": str(size)})
    return levels


def _field(snapshot: Any, name: str) -> Any:
    if isinstance(snapshot, dict):
        return snapshot.get(name)
    return getattr(snapshot, name, None)


def _str_field(snapshot: Any, name: str) -> str | None:
    value = _field(snapshot, name)
    return str(value) if value is not None else None


def _local_to_dict(snapshot: Any) -> dict[str, Any]:
    """LocalBookSnapshot -> consumer-friendly dict with REST-compatible keys."""
    asks = [{"price": str(level["price"]), "size": str(level["size"])} for level in snapshot.asks]
    bids = [{"price": str(level["price"]), "size": str(level["size"])} for level in snapshot.bids]
    return {
        "token_id": snapshot.token_id,
        "best_ask": str(snapshot.best_ask) if snapshot.best_ask is not None else None,
        "best_bid": str(snapshot.best_bid) if snapshot.best_bid is not None else None,
        "asks": asks,
        "bids": bids,
        "tick_size": str(snapshot.tick_size) if snapshot.tick_size is not None else None,
        "min_order_size": str(snapshot.min_order_size) if snapshot.min_order_size is not None else None,
        "book_hash": snapshot.book_hash,
        "timestamp": snapshot.exchange_timestamp,
        "fetched_at_epoch": snapshot.received_at_epoch,
        "source": "websocket_local",
    }
