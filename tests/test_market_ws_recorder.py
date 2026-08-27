from __future__ import annotations

import json
import unittest

from market_ws_recorder import describe_message, parse_tokens


class MarketWsRecorderTests(unittest.TestCase):
    def test_parse_tokens_deduplicates_and_rejects_empty(self) -> None:
        self.assertEqual(parse_tokens("token-a, token-b, token-a"), ["token-a", "token-b"])
        self.assertEqual(parse_tokens(None), [])
        with self.assertRaisesRegex(ValueError, "no_valid_token_ids"):
            parse_tokens(" , , ")

    def test_describe_control_and_raw_book_array(self) -> None:
        self.assertEqual(describe_message("PONG"), ("pong", "PONG"))
        book = [{"asset_id": "token-a", "bids": [], "asks": []}]
        self.assertEqual(describe_message(json.dumps(book))[0], "book_array")

    def test_describe_raw_price_change(self) -> None:
        message = {"event_type": "price_change", "price_changes": []}
        kind, payload = describe_message(json.dumps(message))
        self.assertEqual(kind, "price_change")
        self.assertEqual(payload, message)


if __name__ == "__main__":
    unittest.main()
