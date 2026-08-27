from __future__ import annotations

import unittest
from decimal import Decimal

from local_order_book import LocalOrderBook
from tree11_l2_evidence import L2EvidenceError, snapshot_evidence, yes_bucket_index


class Tree11L2EvidenceTests(unittest.TestCase):
    def rules(self) -> list[dict]:
        return [{"market_rule_id": "high-rule", "city_id": "shanghai", "market_local_date": "2026-08-27", "direction": "high", "enabled": True, "buckets": [
            {"bucket_id": "h30", "yes_token_id": "yes-30"}, {"bucket_id": "h31", "yes_token_id": "yes-31"},
        ]}]

    def test_index_and_snapshot_preserve_identity_and_l2(self) -> None:
        index = yes_bucket_index(self.rules())
        book = LocalOrderBook("yes-31", clock=lambda: 1.0)
        snapshot = book.apply_book({"asset_id": "yes-31", "market": "market", "timestamp": "1", "hash": "h", "tick_size": "0.01", "min_order_size": "5", "bids": [{"price": "0.20", "size": "10"}], "asks": [{"price": "0.22", "size": "8"}]})
        evidence = snapshot_evidence(snapshot, received_monotonic_ns=123, token_index=index, received_at_utc="2026-08-27T00:00:00Z", source_session_id="session")
        self.assertEqual(evidence["market_rule_id"], "high-rule")
        self.assertEqual(evidence["bucket_id"], "h31")
        self.assertEqual(evidence["bids"], [{"price": "0.20", "size": "10"}])
        self.assertEqual(evidence["asks"], [{"price": "0.22", "size": "8"}])
        self.assertEqual(evidence["min_order_size"], "5")
        self.assertEqual(evidence["safety"]["orders_submitted"], 0)

    def test_unmapped_or_not_ready_book_emits_no_evidence(self) -> None:
        index = yes_bucket_index(self.rules())
        unmapped = LocalOrderBook("other")
        self.assertIsNone(snapshot_evidence(unmapped.snapshot(), received_monotonic_ns=1, token_index=index))

    def test_duplicate_yes_token_is_removed_from_index(self) -> None:
        rules = self.rules() + [{"market_rule_id": "low-rule", "city_id": "shanghai", "market_local_date": "2026-08-27", "direction": "low", "enabled": True, "buckets": [{"bucket_id": "l27", "yes_token_id": "yes-31"}]}]
        self.assertNotIn("yes-31", yes_bucket_index(rules))

    def test_monotonic_time_is_mandatory(self) -> None:
        book = LocalOrderBook("yes-31", clock=lambda: 1.0)
        snapshot = book.apply_book({"asset_id": "yes-31", "bids": [], "asks": [], "tick_size": "0.01", "min_order_size": "5"})
        with self.assertRaisesRegex(L2EvidenceError, "received_monotonic_ns_required"):
            snapshot_evidence(snapshot, received_monotonic_ns=0, token_index=yes_bucket_index(self.rules()))


if __name__ == "__main__":
    unittest.main()
