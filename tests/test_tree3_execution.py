from __future__ import annotations

import unittest
from decimal import Decimal
from tree3_execution import build_execution_intent, simulate_local_fak
from websocket_market_data import MarketStream


class Tree3ExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = [100.0]
        self.stream = MarketStream(["no-1"], clock=lambda: self.now[0])
        self.stream.mark_connected()
        self.stream.mark_subscribed()
        self.stream.handle_message({"type": "book", "payload": {
            "market": "m", "tokenId": "no-1", "timestamp": "100000", "hash": "h",
            "minOrderSize": "5", "tickSize": "0.01", "negRisk": False,
            "asks": [{"price": "0.96", "size": "5"}], "bids": []
        }})
        self.signal = {"bucket": {"no_token_id": "no-1"}}

    def test_local_fak_uses_ws_snapshot_and_wide_cap(self) -> None:
        local = self.stream.snapshot("no-1", max_age_seconds=3, now=100.0)
        result = simulate_local_fak(self.signal, local, {"base_fee": 0}, max_price=Decimal("0.98"), now=100.0)
        self.assertEqual(result["status"], "paper_fill_estimate")
        self.assertEqual(result["execution_source"], "websocket_local")
        self.assertEqual(Decimal(result["intent"]["executable_shares"]), Decimal("5"))

    def test_fok_rejects_partial_depth(self) -> None:
        self.stream.handle_message({"type": "book", "payload": {
            "market": "m", "tokenId": "no-1", "timestamp": "100001", "hash": "h2",
            "minOrderSize": "5", "tickSize": "0.01", "negRisk": False,
            "asks": [{"price": "0.96", "size": "4.99"}], "bids": []
        }})
        local = self.stream.snapshot("no-1", max_age_seconds=3, now=100.0)
        result = build_execution_intent(self.signal, local, {"base_fee": 0}, order_type="FOK", max_price=Decimal("0.98"))
        self.assertEqual(result["status"], "paper_fill_rejected_fok_insufficient_depth")
        self.assertEqual(result["order_type"], "FOK")

    def test_price_gate_includes_both_endpoints(self) -> None:
        for price in ("0.05", "0.98"):
            self.stream.handle_message({"type": "book", "payload": {
                "market": "m", "tokenId": "no-1", "timestamp": "100002", "hash": price,
                "minOrderSize": "5", "tickSize": "0.01", "negRisk": False,
                "asks": [{"price": price, "size": "5"}], "bids": []
            }})
            local = self.stream.snapshot("no-1", max_age_seconds=3, now=100.0)
            result = simulate_local_fak(self.signal, local, {"base_fee": 0}, max_price=Decimal("0.98"), now=100.0)
            self.assertEqual(result["status"], "paper_fill_estimate", msg=price)

    def test_stale_local_book_fails_closed(self) -> None:
        local = self.stream.snapshot("no-1", max_age_seconds=3, now=100.0)
        self.now[0] = 104.0
        result = simulate_local_fak(self.signal, local, {"base_fee": 0}, max_age_seconds=3, now=104.0)
        self.assertEqual(result["decision_code"], "STALE_LOCAL_BOOK")


if __name__ == "__main__":
    unittest.main()
