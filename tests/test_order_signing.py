from __future__ import annotations

import unittest
from decimal import Decimal

from eth_account import Account

from clob_market_data import CLOBMarketData
from order_signing import (
    NEG_RISK_EXCHANGE,
    STANDARD_EXCHANGE,
    OrderEncodingError,
    build_unsigned_buy_order,
    encode_buy_amounts,
    exchange_for_neg_risk,
    sign_order,
)


TEST_KEY = "0x0123456789012345678901234567890123456789012345678901234567890123"
TEST_ACCOUNT = Account.from_key(TEST_KEY).address


class OrderSigningTests(unittest.TestCase):
    def test_buy_precision_and_six_decimal_encoding(self) -> None:
        amounts = encode_buy_amounts(price="0.5237", size="5.999", tick_size="0.01", min_order_size="5")
        self.assertEqual(amounts.price, Decimal("0.52"))
        self.assertEqual(amounts.size, Decimal("5.99"))
        self.assertEqual(amounts.amount, Decimal("3.1148"))
        self.assertEqual(amounts.maker_amount, 3114800)
        self.assertEqual(amounts.taker_amount, 5990000)

    def test_amount_rounding_never_underfunds(self) -> None:
        amounts = encode_buy_amounts(price="0.333", size="5", tick_size="0.001", min_order_size="5")
        self.assertEqual(amounts.price, Decimal("0.333"))
        self.assertGreaterEqual(amounts.amount, amounts.price * amounts.size)
        self.assertEqual(amounts.maker_amount, int(amounts.amount * Decimal(10**6)))

    def test_all_supported_tick_rules(self) -> None:
        for tick in ("0.1", "0.01", "0.005", "0.0025", "0.001", "0.0001"):
            amounts = encode_buy_amounts(price=tick, size="5", tick_size=tick, min_order_size="5")
            self.assertGreater(amounts.maker_amount, 0)
            self.assertEqual(amounts.taker_amount, 5_000_000)

    def test_invalid_precision_and_minimums_are_rejected(self) -> None:
        with self.assertRaisesRegex(OrderEncodingError, "unsupported_tick_size"):
            encode_buy_amounts(price="0.52", size="5", tick_size="0.03", min_order_size="5")
        with self.assertRaisesRegex(OrderEncodingError, "size_below_min"):
            encode_buy_amounts(price="0.52", size="4.999", tick_size="0.01", min_order_size="5")
        with self.assertRaisesRegex(OrderEncodingError, "price_out_of_range"):
            encode_buy_amounts(price="1.01", size="5", tick_size="0.01", min_order_size="5")

    def test_book_metadata_drives_neg_risk_route(self) -> None:
        raw = {
            "asset_id": "123", "market": "market-1", "timestamp": "1", "hash": "h",
            "min_order_size": "5", "tick_size": "0.01", "neg_risk": True,
            "asks": [{"price": "0.52", "size": "5"}], "bids": [],
        }
        snapshot = CLOBMarketData().snapshot_from_raw("123", raw)
        from order_signing import build_unsigned_buy_order_from_book
        order = build_unsigned_buy_order_from_book(
            snapshot=snapshot, price="0.52", size="5", maker=TEST_ACCOUNT,
            signer=TEST_ACCOUNT, salt=1, expiration=2_000_000_000,
            timestamp=1_713_398_400_000,
        )
        self.assertEqual(order.exchange_address, NEG_RISK_EXCHANGE)

    def test_neg_risk_selects_exchange_verifying_contract(self) -> None:
        self.assertEqual(exchange_for_neg_risk(False), STANDARD_EXCHANGE)
        self.assertEqual(exchange_for_neg_risk(True), NEG_RISK_EXCHANGE)
        standard = build_unsigned_buy_order(
            token_id="123", price="0.52", size="5", tick_size="0.01", min_order_size="5",
            maker=TEST_ACCOUNT, signer=TEST_ACCOUNT, salt=1, expiration=2_000_000_000,
            timestamp=1_713_398_400_000, neg_risk=False,
        )
        negative = build_unsigned_buy_order(
            token_id="123", price="0.52", size="5", tick_size="0.01", min_order_size="5",
            maker=TEST_ACCOUNT, signer=TEST_ACCOUNT, salt=1, expiration=2_000_000_000,
            timestamp=1_713_398_400_000, neg_risk=True,
        )
        self.assertEqual(standard.exchange_address, STANDARD_EXCHANGE)
        self.assertEqual(negative.exchange_address, NEG_RISK_EXCHANGE)
        self.assertNotEqual(standard.domain["verifyingContract"], negative.domain["verifyingContract"])

    def test_eip712_signature_recovers_explicit_signer(self) -> None:
        order = build_unsigned_buy_order(
            token_id="123456789", price="0.52", size="5", tick_size="0.01", min_order_size="5",
            maker=TEST_ACCOUNT, signer=TEST_ACCOUNT, salt=999, expiration=2_000_000_000,
            timestamp=1_713_398_400_000, neg_risk=False, signature_type=0,
        )
        signature, recovered = sign_order(order, TEST_KEY)
        self.assertTrue(signature.startswith("0x"))
        self.assertEqual(recovered, TEST_ACCOUNT)
        self.assertEqual(order.domain["version"], "2")
        self.assertNotIn("expiration", order.message)
        self.assertEqual(order.wire_fields["expiration"], "2000000000")

    def test_signed_message_changes_when_neg_risk_changes(self) -> None:
        common = dict(
            token_id="123456789", price="0.52", size="5", tick_size="0.01", min_order_size="5",
            maker=TEST_ACCOUNT, signer=TEST_ACCOUNT, salt=999, expiration=2_000_000_000,
            timestamp=1_713_398_400_000,
        )
        standard_sig, _ = sign_order(build_unsigned_buy_order(**common, neg_risk=False), TEST_KEY)
        negative_sig, _ = sign_order(build_unsigned_buy_order(**common, neg_risk=True), TEST_KEY)
        self.assertNotEqual(standard_sig, negative_sig)


if __name__ == "__main__":
    unittest.main()
