from __future__ import annotations

import unittest
from decimal import Decimal

from local_order_book import OrderBookStateError
from websocket_market_data import MarketStream, MarketStreamError


class Tree3MarketStreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = [100.0]
        self.stream = MarketStream(["no-1"], clock=lambda: self.now[0])
        self.stream.mark_connected()
        self.stream.mark_subscribed()

    def book(self, asks=None, bids=None):
        return {
            "market": "market-1", "tokenId": "no-1", "timestamp": "100000",
            "hash": "h1", "minOrderSize": "5", "tickSize": "0.01", "negRisk": True,
            "asks": asks if asks is not None else [{"price": "0.10", "size": "5"}],
            "bids": bids if bids is not None else [{"price": "0.09", "size": "5"}],
        }

    def test_subscription_message_is_market_stream(self) -> None:
        self.assertEqual(self.stream.subscription_message()["type"], "market")
        self.assertEqual(self.stream.subscription_message()["assets_ids"], ["no-1"])

    def test_book_then_price_changes_update_local_depth(self) -> None:
        first = self.stream.handle_message({"type": "book", "payload": self.book()})
        self.assertEqual(first.best_ask, Decimal("0.10"))
        self.now[0] = 101.0
        change = {"type": "price_change", "payload": {"timestamp": "100100", "priceChanges": [
            {"tokenId": "no-1", "price": "0.10", "size": "0", "side": "SELL", "hash": "h2"},
            {"tokenId": "no-1", "price": "0.11", "size": "5", "side": "SELL", "hash": "h2"},
        ]}}
        result = self.stream.handle_message(change)
        self.assertEqual(result[-1].best_ask, Decimal("0.11"))
        self.assertEqual(result[-1].version, 3)
        self.assertEqual(result[-1].book_hash, "h2")

    def test_official_event_type_and_asset_id_fields_are_supported(self) -> None:
        first = self.stream.handle_message({"event_type": "book", "payload": self.book() | {"tokenId": None, "asset_id": "no-1"}})
        self.assertEqual(first.best_ask, Decimal("0.10"))
        result = self.stream.handle_message({"event_type": "price_change", "payload": {"timestamp": "100100", "price_changes": [
            {"asset_id": "no-1", "price": "0.10", "size": "0", "side": "SELL", "hash": "h2"},
            {"asset_id": "no-1", "price": "0.11", "size": "5", "side": "SELL", "hash": "h2"},
        ]}})
        self.assertEqual(result[-1].best_ask, Decimal("0.11"))

    def test_increment_before_baseline_is_rejected(self) -> None:
        with self.assertRaisesRegex(OrderBookStateError, "book_baseline_required"):
            self.stream.handle_message({"type": "price_change", "payload": {
                "priceChanges": [{"tokenId": "no-1", "price": "0.10", "size": "5", "side": "SELL"}]
            }})

    def test_disconnect_invalidates_snapshot(self) -> None:
        self.stream.handle_message({"type": "book", "payload": self.book()})
        self.stream.mark_disconnected("socket_lost")
        self.assertIsNone(self.stream.snapshot("no-1", max_age_seconds=10, now=100.0))
        self.assertEqual(self.stream.last_error, "socket_lost")

    def test_tick_size_change_updates_metadata(self) -> None:
        self.stream.handle_message({"type": "book", "payload": self.book()})
        result = self.stream.handle_message({"type": "tick_size_change", "payload": {
            "tokenId": "no-1", "newTickSize": "0.001"
        }})
        self.assertEqual(result.tick_size, Decimal("0.001"))

    def test_wrong_token_and_unknown_event_fail_closed(self) -> None:
        with self.assertRaisesRegex(OrderBookStateError, "unknown_token_id"):
            self.stream.handle_message({"type": "book", "payload": self.book() | {"tokenId": "no-2"}})
        with self.assertRaisesRegex(MarketStreamError, "unsupported_event"):
            self.stream.handle_message({"type": "surprise", "payload": {}})


if __name__ == "__main__":
    unittest.main()
