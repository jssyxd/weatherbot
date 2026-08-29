from __future__ import annotations

import unittest
from unittest.mock import patch
from decimal import Decimal

from clob_market_data import CLOBMarketData
from tree2_execution import build_fak_intent, simulate


class Tree2ExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.signal = {
            "city_id": "shanghai", "market_local_date": "2026-08-26",
            "bucket": {"no_token_id": "no-1"},
        }
        self.book = {
            "asset_id": "no-1", "market": "market-1", "timestamp": "123",
            "hash": "hash-1", "min_order_size": "1", "tick_size": "0.01",
            "asks": [{"price": "0.50", "size": "5"}], "bids": [],
        }

    def test_fixed_one_fak_uses_last_required_ask_as_worst_price(self) -> None:
        data = CLOBMarketData(max_snapshot_age_seconds=10)
        multi = dict(self.book, asks=[
            {"price": "0.50", "size": "2"},
            {"price": "0.51", "size": "4"},
        ])
        snapshot = data.snapshot_from_raw("no-1", multi)
        intent = build_fak_intent(snapshot, {"base_fee": 500})
        self.assertEqual(intent.executable_shares, 1)
        self.assertEqual(intent.limit_price, Decimal("0.50"))
        self.assertEqual(len(intent.fills), 1)
        self.assertEqual(intent.fills[0]["shares"], Decimal("1"))

    def test_fixed_one_fak_is_partial_when_depth_is_short(self) -> None:
        data = CLOBMarketData(max_snapshot_age_seconds=10)
        short = dict(self.book, asks=[{"price": "0.50", "size": "4.99"}])
        snapshot = data.snapshot_from_raw("no-1", short)
        intent = build_fak_intent(snapshot, {"base_fee": 500})
        self.assertEqual(intent.executable_shares, Decimal("1.00"))
        self.assertEqual(intent.limit_price, Decimal("0.50"))

    def test_fixed_one_fak_excludes_ask_at_protection_cap(self) -> None:
        data = CLOBMarketData(max_snapshot_age_seconds=10)
        high = dict(self.book, asks=[{"price": "1.00", "size": "10"}])
        snapshot = data.snapshot_from_raw("no-1", high)
        intent = build_fak_intent(snapshot, {"base_fee": 500})
        self.assertEqual(intent.executable_shares, 0)
        self.assertIsNone(intent.limit_price)

    def test_paper_fill_requires_actual_ask_depth(self) -> None:
        data = CLOBMarketData(max_snapshot_age_seconds=10)
        with patch.object(data, "fetch_books", return_value={"no-1": data.snapshot_from_raw("no-1", self.book)}), \
             patch.object(data, "fetch_fee_rate", return_value={"base_fee": 500}):
            result = simulate(self.signal, {}, data)
        self.assertEqual(result["status"], "paper_fill_estimate")
        self.assertEqual(result["estimated_shares"], "1.00")

    def test_empty_ask_does_not_use_bid_as_liquidity(self) -> None:
        data = CLOBMarketData(max_snapshot_age_seconds=10)
        empty = dict(self.book, asks=[])
        with patch.object(data, "fetch_books", return_value={"no-1": data.snapshot_from_raw("no-1", empty)}), \
             patch.object(data, "fetch_fee_rate", return_value={"base_fee": 500}):
            result = simulate(self.signal, {}, data)
        self.assertEqual(result["status"], "paper_fill_rejected_no_asks")
        self.assertEqual(result["decision"]["code"], "EMPTY_ASK")

    def test_ask_at_or_above_limit_is_rejected(self) -> None:
        data = CLOBMarketData(max_snapshot_age_seconds=10)
        high = dict(self.book, asks=[{"price": "1.00", "size": "5"}])
        with patch.object(data, "fetch_books", return_value={"no-1": data.snapshot_from_raw("no-1", high)}), \
             patch.object(data, "fetch_fee_rate", return_value={"base_fee": 500}):
            result = simulate(self.signal, {}, data)
        self.assertEqual(result["status"], "paper_fill_rejected_best_ask_outside_gate")


if __name__ == "__main__":
    unittest.main()
