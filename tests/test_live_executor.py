"""Tests for adapters.polymarket.live_executor: fail-closed + body construction.

No network, wallet, or real order submission is exercised here. The tests only
assert the safety gate and the pure wire-body builders.
"""
from __future__ import annotations

import unittest
from decimal import Decimal

from adapters.polymarket.live_executor import (
    LiveExecutor,
    LiveExecutorDisabledError,
    LiveExecutorError,
    LiveOrderNotSupported,
    build_order_wire,
    build_send_order,
)
from execution.order_intent import OrderIntent


def _intent(**overrides):
    base = dict(
        order_id="o1",
        token_id="1234567890",
        side="BUY",
        outcome="NO",
        price=Decimal("0.50"),
        quantity=Decimal("5"),
        order_type="FAK",
        strategy="test",
        signal_reason="test",
        created_at="2026-08-29T00:00:00Z",
    )
    base.update(overrides)
    return OrderIntent(**base)


class LiveExecutorSafetyTests(unittest.TestCase):
    def test_disabled_by_default_raises_on_submit(self) -> None:
        executor = LiveExecutor({})
        self.assertFalse(executor.enabled)
        with self.assertRaises(LiveExecutorDisabledError):
            executor.submit(_intent(), tick_size="0.01", min_order_size="5", neg_risk=False, salt=1)

    def test_disabled_by_default_raises_on_cancel(self) -> None:
        with self.assertRaises(LiveExecutorDisabledError):
            LiveExecutor({}).cancel("0xabc")

    def test_enabled_but_sell_unsupported(self) -> None:
        executor = LiveExecutor({"live_enabled": True})
        with self.assertRaises(LiveOrderNotSupported):
            executor.submit(_intent(side="SELL"), tick_size="0.01", min_order_size="5", neg_risk=False, salt=1)

    def test_enabled_but_no_credentials_fails_closed(self) -> None:
        executor = LiveExecutor({"live_enabled": True})
        with self.assertRaises(LiveExecutorError):
            executor.submit(_intent(), tick_size="0.01", min_order_size="5", neg_risk=False, salt=1)


class LiveExecutorWireTests(unittest.TestCase):
    def test_build_send_order_fak(self) -> None:
        body = build_send_order(_intent(), {"salt": 1}, owner="0xowner")
        self.assertEqual(body["orderType"], "FAK")
        self.assertEqual(body["owner"], "0xowner")
        self.assertEqual(body["order"], {"salt": 1})

    def test_build_send_order_maps_gtc_and_fok(self) -> None:
        self.assertEqual(build_send_order(_intent(order_type="GTC"), {}, "o")["orderType"], "GTC")
        self.assertEqual(build_send_order(_intent(order_type="FOK"), {}, "o")["orderType"], "FOK")

    def test_build_send_order_unsupported_type(self) -> None:
        with self.assertRaises(LiveOrderNotSupported):
            build_send_order(_intent(order_type="NOPE"), {}, "o")

    def test_build_order_wire_adds_signature_and_stringifies_token(self) -> None:
        class FakeUnsigned:
            wire_fields = {"salt": 1, "tokenId": 1234567890, "maker": "0xM"}

        wire = build_order_wire(FakeUnsigned(), "0xsig")
        self.assertEqual(wire["signature"], "0xsig")
        self.assertEqual(wire["tokenId"], "1234567890")


if __name__ == "__main__":
    unittest.main()
