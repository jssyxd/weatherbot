from __future__ import annotations

import unittest

from tree3_runtime import Tree3MarketRuntime


class Tree3RuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = [100.0]
        self.runtime = Tree3MarketRuntime(["no-1"], max_book_age_seconds=3, clock=lambda: self.now[0])

    def test_runtime_requires_book_before_local_snapshot(self) -> None:
        self.runtime.connect()
        self.runtime.subscribed()
        self.assertIsNone(self.runtime.local_snapshot("no-1"))
        self.runtime.on_message({"type": "book", "payload": {
            "market": "m", "tokenId": "no-1", "timestamp": "1", "hash": "h",
            "minOrderSize": "5", "tickSize": "0.01", "negRisk": False,
            "bids": [], "asks": [{"price": "0.98", "size": "5"}]
        }})
        self.assertEqual(self.runtime.state, "running")
        self.assertIsNotNone(self.runtime.local_snapshot("no-1"))

    def test_disconnect_invalidates_and_health_reports(self) -> None:
        self.runtime.connect()
        self.runtime.subscribed()
        self.runtime.disconnect("network")
        health = self.runtime.health()
        self.assertEqual(health["state"], "reconnecting")
        self.assertEqual(health["last_error"], "network")
        self.assertFalse(health["connected"])

    def test_stale_snapshot_is_not_exposed(self) -> None:
        self.runtime.connect()
        self.runtime.subscribed()
        self.runtime.on_message({"type": "book", "payload": {
            "market": "m", "tokenId": "no-1", "timestamp": "1", "hash": "h",
            "minOrderSize": "5", "tickSize": "0.01", "negRisk": False,
            "bids": [], "asks": [{"price": "0.98", "size": "5"}]
        }})
        self.now[0] = 104.0
        self.assertIsNone(self.runtime.local_snapshot("no-1"))


if __name__ == "__main__":
    unittest.main()
