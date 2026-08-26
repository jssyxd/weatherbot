from __future__ import annotations

import tempfile
import time
import unittest
from unittest.mock import patch
from decimal import Decimal
from pathlib import Path

from audit_store import AuditStore
from clob_market_data import CLOBDataError, CLOBMarketData
from execution_policy import decide_buy_no


class Tree2SafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw_book = {
            "asset_id": "no-1", "market": "market-1", "timestamp": "123",
            "hash": "hash-1", "min_order_size": "5", "tick_size": "0.01",
            "asks": [{"price": "0.10", "size": "5"}],
            "bids": [{"price": "0.09", "size": "10"}],
        }

    def test_batch_books_uses_post_payload_and_parses_tokens(self) -> None:
        raw = [self.raw_book]
        data = CLOBMarketData(max_snapshot_age_seconds=10)
        with patch.object(data, "_request_json", return_value=raw) as request:
            books = data.fetch_books(["no-1"])
        request.assert_called_once()
        args, kwargs = request.call_args
        self.assertTrue(args[0].endswith("/books"))
        self.assertEqual(kwargs["payload"], [{"token_id": "no-1"}])
        self.assertEqual(books["no-1"].best_ask, Decimal("0.10"))

    def test_book_parser_rejects_token_mismatch(self) -> None:
        with self.assertRaises(CLOBDataError):
            CLOBMarketData().snapshot_from_raw("other", self.raw_book)

    def test_empty_ask_is_not_executable_even_with_bid(self) -> None:
        raw = dict(self.raw_book, asks=[])
        data = CLOBMarketData(max_snapshot_age_seconds=10)
        data.snapshot_from_raw("no-1", raw)
        summary = data.executable_summary("no-1")
        decision = decide_buy_no(
            mode="paper", token_id="no-1", book_summary=summary,
            target_shares=Decimal("5"), min_order_size=Decimal("5"),
        )
        self.assertEqual(summary["status"], "EMPTY_ASK")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "EMPTY_ASK")

    def test_live_is_fail_closed(self) -> None:
        data = CLOBMarketData(max_snapshot_age_seconds=10)
        data.snapshot_from_raw("no-1", self.raw_book)
        decision = decide_buy_no(
            mode="live", token_id="no-1", book_summary=data.executable_summary("no-1"),
            target_shares=Decimal("5"), min_order_size=Decimal("5"),
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "LIVE_EXECUTOR_DISABLED")

    def test_stale_book_is_rejected(self) -> None:
        data = CLOBMarketData(max_snapshot_age_seconds=0.01)
        data.snapshot_from_raw("no-1", self.raw_book)
        time.sleep(0.02)
        decision = decide_buy_no(
            mode="paper", token_id="no-1", book_summary=data.executable_summary("no-1"),
            target_shares=Decimal("5"), min_order_size=Decimal("5"),
        )
        self.assertEqual(decision.code, "STALE_OR_MISSING_BOOK")

    def test_sqlite_audit_is_append_only_and_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.sqlite3"
            store = AuditStore(path)
            event_id = store.append(
                created_at_utc="2026-08-26T00:00:00Z", event_type="decision",
                payload={"code": "EMPTY_ASK"}, token_id="no-1", mode="paper",
            )
            self.assertGreater(event_id, 0)
            store.set_ledger("city|date", "1.25", "2026-08-26T00:00:00Z")
            self.assertEqual(store.get_ledger("city|date"), "1.25")
            store.close()


if __name__ == "__main__":
    unittest.main()
