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

    def raw_book(self, asks=None, bids=None):
        return {
            "event_type": "book", "market": "market-1", "asset_id": "no-1", "timestamp": "100000",
            "hash": "h1", "min_order_size": "1", "tick_size": "0.01", "neg_risk": True,
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

    def test_raw_event_type_asset_id_and_snake_case_price_changes_replay(self) -> None:
        raw_stream = MarketStream(["no-1"], clock=lambda: self.now[0])
        raw_stream.mark_connected()
        events = [
            self.raw_book(),
            {
                "event_type": "price_change", "timestamp": "100100", "price_changes": [
                    {"asset_id": "no-1", "price": "0.10", "size": "0", "side": "SELL", "hash": "h2"},
                    {"asset_id": "no-1", "price": "0.12", "size": "7", "side": "SELL", "hash": "h2"},
                ],
            },
            {"event_type": "tick_size_change", "asset_id": "no-1", "new_tick_size": "0.001"},
            {"event_type": "last_trade_price", "asset_id": "no-1", "price": "0.11"},
        ]
        results = raw_stream.replay(events)
        final = results[-1]
        self.assertTrue(raw_stream.subscribed)
        self.assertEqual(len(results), 4)
        self.assertEqual(final.best_ask, Decimal("0.12"))
        self.assertEqual(final.tick_size, Decimal("0.001"))
        self.assertEqual(final.last_trade_price, Decimal("0.11"))
        self.assertEqual(final.book_hash, "h2")

    def test_direct_initial_book_array_pong_and_complement_increment_are_safe(self) -> None:
        raw_stream = MarketStream(["no-1"], clock=lambda: self.now[0])
        raw_stream.mark_connected()
        result = raw_stream.handle_message([self.raw_book()])
        self.assertEqual(result[0].best_ask, Decimal("0.10"))
        self.assertIsNone(raw_stream.handle_message("PONG"))
        updates = raw_stream.handle_message({"event_type": "price_change", "timestamp": "100100", "price_changes": [
            {"asset_id": "yes-1", "price": "0.90", "size": "5", "side": "BUY"},
            {"asset_id": "no-1", "price": "0.11", "size": "5", "side": "SELL"},
        ]})
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].best_ask, Decimal("0.10"))
        self.assertTrue(raw_stream.subscribed)

    def test_raw_book_marks_subscription_only_after_connection(self) -> None:
        disconnected = MarketStream(["no-1"], clock=lambda: self.now[0])
        with self.assertRaisesRegex(MarketStreamError, "connection_required"):
            disconnected.handle_message(self.raw_book())
        self.assertFalse(disconnected.subscribed)

    def test_increment_before_baseline_is_rejected(self) -> None:
        with self.assertRaisesRegex(OrderBookStateError, "book_baseline_required"):
            self.stream.handle_message({"type": "price_change", "payload": {
                "priceChanges": [{"tokenId": "no-1", "price": "0.10", "size": "5", "side": "SELL"}]
            }})

    def test_raw_increment_before_baseline_is_rejected(self) -> None:
        with self.assertRaisesRegex(OrderBookStateError, "book_baseline_required"):
            self.stream.handle_message({"event_type": "price_change", "price_changes": [
                {"asset_id": "no-1", "price": "0.10", "size": "5", "side": "SELL"}
            ]})

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
            self.stream.handle_message({"event_type": "surprise", "payload": {}})


if __name__ == "__main__":
    unittest.main()
