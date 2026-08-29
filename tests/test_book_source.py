from __future__ import annotations

import unittest
from decimal import Decimal

from execution.book_source import LocalBookSource


def rest_book(token, ask="0.90", ask_size="5", bid=None, bid_size="0"):
    return {
        "token_id": token,
        "best_ask": ask,
        "best_bid": bid,
        "asks": [{"price": ask, "size": ask_size}],
        "bids": [{"price": bid, "size": bid_size}] if bid else [],
        "tick_size": "0.01",
        "min_order_size": "5",
        "timestamp": "123",
        "book_hash": f"h-{token}",
        "source": "rest_batch",
    }


class LocalBookSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = [100.0]
        self.source = LocalBookSource(["no-1"], max_book_age_seconds=3.0, clock=lambda: self.now[0])
        self.source.connect()

    def test_prime_then_books_for_serves_local_book(self) -> None:
        self.source.prime({"no-1": rest_book("no-1")})
        merged = self.source.books_for(["no-1"], {"no-1": rest_book("no-1")})
        self.assertIn("no-1", merged)
        self.assertEqual(merged["no-1"]["source"], "websocket_local")
        self.assertEqual(merged["no-1"]["best_ask"], "0.90")
        self.assertEqual(Decimal(merged["no-1"]["best_ask"]), Decimal("0.90"))
        # local book also exposes depth for match_l2
        self.assertEqual(merged["no-1"]["asks"][0]["price"], "0.90")

    def test_stale_local_book_falls_back_to_rest(self) -> None:
        self.source.prime({"no-1": rest_book("no-1", ask="0.90")})
        self.now[0] = 104.0  # older than 3s max age
        merged = self.source.books_for(["no-1"], {"no-1": rest_book("no-1", ask="0.91")})
        self.assertEqual(merged["no-1"]["source"], "rest_batch")
        self.assertEqual(merged["no-1"]["best_ask"], "0.91")

    def test_disconnect_invalidates_local_books(self) -> None:
        self.source.prime({"no-1": rest_book("no-1")})
        self.source.disconnect("socket_lost")
        merged = self.source.books_for(["no-1"], {"no-1": rest_book("no-1", ask="0.92")})
        self.assertEqual(merged["no-1"]["source"], "rest_batch")
        self.assertFalse(self.source.runtime.stream.connected)

    def test_ensure_tokens_grows_the_book_map(self) -> None:
        self.source.ensure_tokens(["no-2", "no-3"])
        self.assertIn("no-2", self.source.runtime.stream.books)
        self.assertIn("no-3", self.source.runtime.stream.books)
        self.source.prime({"no-2": rest_book("no-2"), "no-3": rest_book("no-3")})
        merged = self.source.books_for(["no-2", "no-3"], {})
        self.assertEqual(merged["no-2"]["source"], "websocket_local")
        self.assertEqual(merged["no-3"]["source"], "websocket_local")

    def test_unprimed_token_without_rest_is_absent(self) -> None:
        merged = self.source.books_for(["no-9"], {})
        self.assertNotIn("no-9", merged)

    def test_bad_token_does_not_kill_prime_batch(self) -> None:
        self.source.prime({"no-1": rest_book("no-1"), "no-bad": None})
        merged = self.source.books_for(["no-1"], {})
        self.assertEqual(merged["no-1"]["source"], "websocket_local")

    def test_health_reports_runtime_state(self) -> None:
        self.source.prime({"no-1": rest_book("no-1")})
        health = self.source.health()
        self.assertEqual(health["state"], "running")
        self.assertTrue(health["connected"])
        self.assertEqual(health["ready_tokens"], 1)
        self.assertEqual(health["max_book_age_seconds"], 3.0)

    def test_rest_failure_disconnects_and_returns_error(self) -> None:
        # simulate the metar_observer contract: error + empty books
        books, error = {"no-1": rest_book("no-1")}, None
        self.source.prime(books)
        # REST fails: bridge disconnects, local invalidated, error surfaces
        self.source.disconnect("rest_fetch_failed")
        merged = self.source.books_for(["no-1"], {})
        self.assertNotIn("no-1", merged)


if __name__ == "__main__":
    unittest.main()
