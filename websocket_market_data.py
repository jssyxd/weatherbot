"""Polymarket market-stream adapter with deterministic event dispatch.

The transport is deliberately injected: this module can consume direct raw
market-channel messages or prior recorded JSON payloads, but never connects to
an exchange and never submits orders. A production collector belongs in a
separate paper-only recorder process.
"""
from __future__ import annotations

import json
from typing import Any, Iterable

from local_order_book import LocalOrderBook, LocalBookSnapshot, OrderBookStateError

MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class MarketStreamError(RuntimeError):
    pass


StreamResult = LocalBookSnapshot | tuple[LocalBookSnapshot, ...] | None


class MarketStream:
    def __init__(self, token_ids: Iterable[str], *, clock=None) -> None:
        ids = tuple(dict.fromkeys(str(x) for x in token_ids if str(x)))
        if not ids:
            raise ValueError("at_least_one_token_required")
        self.token_ids = ids
        self.books = {token: LocalOrderBook(token, clock=clock) for token in ids}
        self.connected = False
        self.subscribed = False
        self.last_error: str | None = None
        self.event_count = 0

    def subscription_message(self) -> dict[str, Any]:
        return {"type": "market", "assets_ids": list(self.token_ids), "custom_feature_enabled": True}

    def mark_connected(self) -> dict[str, Any]:
        self.connected = True
        self.subscribed = False
        self.last_error = None
        return self.subscription_message()

    def mark_disconnected(self, reason: str = "disconnected") -> None:
        self.connected = False
        self.subscribed = False
        self.last_error = reason
        for book in self.books.values():
            book.invalidate()

    def mark_subscribed(self) -> None:
        if not self.connected:
            raise MarketStreamError("connection_required")
        self.subscribed = True

    @staticmethod
    def _decode(message: str | bytes | dict[str, Any] | list[Any]) -> dict[str, Any] | list[Any] | None:
        if isinstance(message, bytes):
            message = message.decode("utf-8", errors="strict")
        if isinstance(message, str):
            # The market channel returns a bare PONG in response to the required
            # application-level heartbeat. It is transport control, not JSON data.
            if message.strip().upper() in {"PING", "PONG"}:
                return None
            try:
                payload = json.loads(message)
            except json.JSONDecodeError as exc:
                raise MarketStreamError("invalid_json") from exc
        else:
            payload = message
        if not isinstance(payload, (dict, list)):
            raise MarketStreamError("invalid_event_shape")
        return payload

    @staticmethod
    def _event_and_body(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Normalize direct market WSS and existing SDK-wrapper shapes.

        Official raw incremental messages use ``event_type``. The initial
        subscription snapshot is a JSON array whose entries are bare L2 book
        objects without an event field. Existing tests/SDK wrappers use ``type``
        plus an optional ``payload`` body.
        """
        body = payload.get("payload", payload)
        if not isinstance(body, dict):
            raise MarketStreamError("invalid_event_payload")
        event_type = payload.get("event_type", payload.get("type"))
        if event_type is None and all(key in body for key in ("asset_id", "bids", "asks")):
            event_type = "book"
        return str(event_type or "").lower(), body

    @staticmethod
    def _token(payload: dict[str, Any]) -> str:
        return str(payload.get("tokenId") or payload.get("asset_id") or "")

    def handle_message(self, message: str | bytes | dict[str, Any] | list[Any]) -> StreamResult:
        payload = self._decode(message)
        if payload is None:
            return None
        if isinstance(payload, list):
            # Direct market WSS sends its initial L2 baseline as an array. A
            # malformed item invalidates the entire array rather than silently
            # accepting an incomplete order book sequence.
            if not payload or not all(isinstance(item, dict) for item in payload):
                raise MarketStreamError("invalid_book_array")
            results: list[LocalBookSnapshot] = []
            for item in payload:
                result = self.handle_message(item)
                if isinstance(result, LocalBookSnapshot):
                    results.append(result)
                elif isinstance(result, tuple):
                    results.extend(result)
            return tuple(results)

        event_type, body = self._event_and_body(payload)
        if event_type in {"subscribe", "subscribed", "ack"}:
            self.mark_subscribed()
            return None
        if event_type == "book":
            # A book snapshot is both the baseline required for increments and
            # evidence that the server accepted the market subscription.
            self.mark_subscribed()
            return self._apply_book(body)
        if event_type == "price_change":
            return self._apply_price_changes(body)
        if event_type == "tick_size_change":
            normalized = {**body, "tokenId": self._token(body), "newTickSize": body.get("newTickSize", body.get("new_tick_size"))}
            return self._book(normalized["tokenId"]).apply_tick_size_change(normalized)
        if event_type == "last_trade_price":
            token = self._token(body)
            book = self._book(token)
            book.last_trade_price = book._optional_decimal(body.get("price"), "last_trade_price")
            return book.snapshot()
        if event_type in {"best_bid_ask", "new_market", "market_resolved", "heartbeat", "ping", "pong"}:
            return None
        raise MarketStreamError(f"unsupported_event:{event_type}")

    def replay(self, messages: Iterable[str | bytes | dict[str, Any] | list[Any]]) -> tuple[StreamResult, ...]:
        """Replay a finite recorded message sequence without network access."""
        return tuple(self.handle_message(message) for message in messages)

    def _book(self, token: str) -> LocalOrderBook:
        if token not in self.books:
            raise OrderBookStateError("unknown_token_id")
        return self.books[token]

    def _apply_book(self, body: dict[str, Any]) -> LocalBookSnapshot:
        result = self._book(self._token(body)).apply_book(body)
        self.event_count += 1
        return result

    def _apply_price_changes(self, body: dict[str, Any]) -> tuple[LocalBookSnapshot, ...]:
        changes = body.get("priceChanges", body.get("price_changes"))
        if not isinstance(changes, list):
            raise MarketStreamError("price_changes_required")
        results = []
        for change in changes:
            if not isinstance(change, dict):
                raise MarketStreamError("invalid_price_change")
            token = self._token(change)
            # Raw events may include both complement tokens of a binary market
            # even when only one asset was subscribed. Ignore the irrelevant
            # complement, but never manufacture a book for it.
            if token not in self.books:
                continue
            normalized = {**change, "tokenId": token}
            if body.get("timestamp") is not None:
                normalized["timestamp"] = body["timestamp"]
            results.append(self._book(token).apply_price_change(normalized))
        self.event_count += 1
        return tuple(results)

    def snapshot(self, token_id: str, *, max_age_seconds: float, now: float | None = None) -> LocalBookSnapshot | None:
        book = self._book(str(token_id))
        return book.snapshot() if book.is_fresh(max_age_seconds, now=now) else None
