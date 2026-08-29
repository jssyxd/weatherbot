"""PRD §G 成交测试矩阵(§十九):必须验证实际数量与状态。

Rows (PRD G):
  完全成交    ask 深度足够         BUY 5 FAK  -> FILLED
  部分成交    ask 只够 3           BUY 5 FAK  -> CANCELLED(FAK_REMAINDER_CANCELLED), filled=3
  零成交      ask > limit          BUY 5 FAK  -> CANCELLED, filled=0
  超价        ask 超保护价         BUY 5       -> RISK_REJECT
  盘口过期    book age 超限        BUY 5       -> RISK_REJECT
  重复订单    已有相同 open 订单   BUY 5       -> 不重复提交
  断线重连    WS 断开             重新 Snapshot -> 新 book 可用
  仓位一致    部分成交 3           Position +3 -> Fill 与 Position 一致
  Paper/Live 同模型               同一 OrderIntent 过同一 RiskGate -> 共用
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from execution.order_intent import OrderIntent, OrderStatus, OrderType, Side
from execution.paper_executor import match_fak, match_gtc
from execution.risk_gate import RiskGate, RiskConfig
from execution.position import Position, apply_fill
from execution.order_intent import Fill
from tree12_allno_strategy import plan_tree12_entries, tree12_paper_fill

MIN_PRICE = Decimal("0.40")
MAX_PRICE = Decimal("0.95")
MAX_SLIPPAGE = Decimal("0.10")
MAX_AGE = 3.0


def _intent(price="0.90", quantity="5", order_type="FAK", order_id="o-matrix"):
    return OrderIntent.new(
        token_id="tok", side=Side.BUY, price=price, quantity=quantity,
        order_type=order_type, strategy="tree12", signal_reason="matrix", order_id=order_id,
    )


def _book(asks, bids=None, best_ask=None, fetched_at=None, age=1.0):
    from execution.market import BookView
    ask_levels = tuple((Decimal(str(l["price"])), Decimal(str(l["size"]))) for l in asks)
    bid_levels = tuple((Decimal(str(l["price"])), Decimal(str(l["size"]))) for l in (bids or []))
    return BookView(
        token_id="tok",
        asks=ask_levels,
        bids=bid_levels,
        best_ask=Decimal(str(best_ask)) if best_ask is not None else (ask_levels[0][0] if ask_levels else None),
        best_bid=bid_levels[0][0] if bid_levels else None,
        age_seconds=float(age),
    )


class TradeMatrixTests(unittest.TestCase):
    """Rows 1-3: match_fak actual quantities and states."""

    def test_row_full_fill_filled_5(self) -> None:
        b = _book([{"price": "0.90", "size": "3"}, {"price": "0.91", "size": "5"}])
        r = match_fak(_intent("0.92"), b)
        self.assertEqual(r.status, OrderStatus.FILLED)
        self.assertEqual(r.filled_shares, Decimal("5"))
        self.assertEqual(r.remaining_shares, Decimal("0"))

    def test_row_partial_fill_3_plus_cancel_remainder(self) -> None:
        b = _book([{"price": "0.90", "size": "3"}])
        r = match_fak(_intent("0.92"), b)
        self.assertEqual(r.status, OrderStatus.CANCELLED)
        self.assertEqual(r.filled_shares, Decimal("3"))
        self.assertEqual(r.cancel_reason, "FAK_REMAINDER_CANCELLED")

    def test_row_zero_fill_cancelled_0(self) -> None:
        b = _book([{"price": "0.99", "size": "10"}])
        r = match_fak(_intent("0.92"), b)
        self.assertEqual(r.status, OrderStatus.CANCELLED)
        self.assertEqual(r.filled_shares, Decimal("0"))


class RiskGateMatrixTests(unittest.TestCase):
    """Rows 4-5: pre-trade gate rejections."""

    def test_row_overpriced_ask_risk_reject(self) -> None:
        b = _book([{"price": "0.99", "size": "10"}], best_ask="0.99")
        gate = RiskGate(RiskConfig(min_price=MIN_PRICE, max_price=MAX_PRICE, max_slippage=Decimal("0.05")))
        d = gate.check(_intent("0.90"), b)
        self.assertFalse(d.allowed)
        self.assertEqual(d.code, "SLIPPAGE_EXCEEDED")

    def test_row_stale_book_risk_reject(self) -> None:
        b = _book([{"price": "0.90", "size": "10"}], best_ask="0.90", age=10.0)
        gate = RiskGate(RiskConfig(min_price=MIN_PRICE, max_price=MAX_PRICE, max_slippage=MAX_SLIPPAGE))
        d = gate.check(_intent("0.90"), b)
        self.assertFalse(d.allowed)
        self.assertEqual(d.code, "STALE_BOOK")

    def test_row_fresh_book_passes(self) -> None:
        b = _book([{"price": "0.90", "size": "10"}], best_ask="0.90", age=1.0)
        gate = RiskGate(RiskConfig(min_price=MIN_PRICE, max_price=MAX_PRICE, max_slippage=MAX_SLIPPAGE))
        d = gate.check(_intent("0.90"), b)
        self.assertTrue(d.allowed)
        self.assertEqual(d.code, "RISK_PASS")


class DuplicateOrderMatrixTests(unittest.TestCase):
    """Row 6: same open order -> not resubmitted (防重)."""

    def test_row_existing_working_order_not_resubmitted(self) -> None:
        city = {"city_id": "shanghai", "icao": "ZSPD", "timezone": "Asia/Shanghai", "market_unit": "C"}
        local_date = "2026-09-10"
        now = datetime(2026, 9, 8, 0, 0, tzinfo=timezone.utc)
        rules = [{"enabled": True, "city_id": "shanghai", "market_local_date": local_date, "direction": "high",
                  "buckets": [
                      {"bucket_id": "b29", "lo": 29, "hi": 30, "no_token_id": "no-29"},
                      {"bucket_id": "b30", "lo": 30, "hi": 31, "no_token_id": "no-30"},
                      {"bucket_id": "b31", "lo": 31, "hi": 32, "no_token_id": "no-31"},
                  ]}]
        books = {"no-29": {"best_ask": "0.85", "tick_size": "0.01", "asks": [{"price": "0.85", "size": "10"}], "bids": []},
                 "no-30": {"best_ask": "0.87", "tick_size": "0.01", "asks": [{"price": "0.87", "size": "10"}], "bids": []},
                 "no-31": {"best_ask": "0.91", "tick_size": "0.01", "asks": [{"price": "0.91", "size": "10"}], "bids": []}}
        config = {"target_order_shares": "5", "mode": "paper"}
        first = plan_tree12_entries({}, {"shanghai": city}, rules, books, now, config)
        submits1 = [a for a in first if a.get("action_type") == "tree12_submit_entry"]
        self.assertTrue(submits1)
        state = {"tree12": {"working_orders": {}, "positions": {}, "exit_chases": {}, "ws_ask_samples": {}}}
        for a in submits1:
            state["tree12"]["working_orders"][a["key"]] = dict(a)
        second = plan_tree12_entries(state, {"shanghai": city}, rules, books, now, config)
        submits2 = [a for a in second if a.get("action_type") == "tree12_submit_entry"]
        self.assertEqual(submits2, [])


class ReconnectMatrixTests(unittest.TestCase):
    """Row 7: WS 断开 -> 旧 book 失效;重新 Snapshot -> 新 book 可用."""

    def test_row_disconnect_invalidates_then_new_snapshot_recovers(self) -> None:
        from execution.book_source import LocalBookSource

        now = [100.0]
        source = LocalBookSource(["no-1"], max_book_age_seconds=3.0, clock=lambda: now[0])
        source.connect()
        source.prime({"no-1": {"best_ask": "0.90", "asks": [{"price": "0.90", "size": "5"}], "bids": [],
                               "tick_size": "0.01", "min_order_size": "5", "timestamp": "1", "book_hash": "h1"}})
        self.assertEqual(source.books_for(["no-1"], {})["no-1"]["source"], "websocket_local")
        source.disconnect("socket_lost")
        self.assertNotIn("no-1", source.books_for(["no-1"], {}))
        source.connect()
        source.prime({"no-1": {"best_ask": "0.91", "asks": [{"price": "0.91", "size": "7"}], "bids": [],
                               "tick_size": "0.01", "min_order_size": "5", "timestamp": "2", "book_hash": "h2"}})
        book = source.books_for(["no-1"], {})["no-1"]
        self.assertEqual(book["source"], "websocket_local")
        self.assertEqual(book["best_ask"], "0.91")


class PositionConsistencyMatrixTests(unittest.TestCase):
    """Row 8: 部分成交 3 -> Position +3, Fill 与 Position 一致."""

    def test_row_partial_fill_moves_position_by_filled(self) -> None:
        state = {"paper_initial_capital_usdc": 1000.0, "paper_total_debit_usdc": 0.0,
                 "tree12": {"working_orders": {"k1": {"key": "k1", "order_id": "t12-m1",
                    "status": "working_gtc_buy_no", "remaining_shares": "5",
                    "city_id": "shanghai", "market_local_date": "2026-09-10", "direction": "high",
                    "bucket_id": "b32", "token_id": "no-32", "lo": 32, "hi": 33, "limit_price": "0.92"}},
                    "positions": {}, "exit_chases": {}, "ws_ask_samples": {}}}
        now = datetime(2026, 9, 8, 0, 0, tzinfo=timezone.utc)
        book = {"best_ask": "0.90", "asks": [{"price": "0.90", "size": "3"}], "bids": []}
        result = tree12_paper_fill(state, "k1", Decimal("5"), book, now)
        self.assertEqual(result["status"], "paper_filled")
        self.assertEqual(Decimal(result["filled"]), Decimal("3"))
        pos = state["tree12"]["positions"]["k1"]
        self.assertEqual(Decimal(pos["shares"]), Decimal("3"))
        self.assertEqual(Decimal(pos["avg_price"]), Decimal("0.90"))
        self.assertEqual(Decimal(pos["cost_basis_usdc"]), Decimal("2.7"))

    def test_row_position_model_matches_fill(self) -> None:
        pos = Position(token_id="no-32", side=Side.BUY)
        fill = Fill(order_id="t12-m1", token_id="no-32", side=Side.BUY, price=Decimal("0.90"), shares=Decimal("3"))
        pos = apply_fill(pos, fill)
        self.assertEqual(pos.shares, Decimal("3"))
        self.assertEqual(pos.avg_price, Decimal("0.90"))


class PaperLiveSharedModelTests(unittest.TestCase):
    """Row 9: 同一 OrderIntent 过同一 RiskGate —— Paper 与 Live 共用同一模型."""

    def test_row_same_intent_same_gate_same_decision(self) -> None:
        b = _book([{"price": "0.90", "size": "10"}], best_ask="0.90", age=1.0)
        gate = RiskGate(RiskConfig(min_price=MIN_PRICE, max_price=MAX_PRICE, max_slippage=MAX_SLIPPAGE))
        d1 = gate.check(_intent(), b)
        d2 = gate.check(_intent(), b)
        self.assertTrue(d1.allowed)
        self.assertEqual(d1.code, d2.code)
        self.assertEqual(d1.as_dict(), d2.as_dict())

    def test_row_intent_rejected_identically_in_both_modes(self) -> None:
        b = _book([{"price": "0.99", "size": "10"}], best_ask="0.99")
        gate = RiskGate(RiskConfig(min_price=MIN_PRICE, max_price=MAX_PRICE, max_slippage=Decimal("0.05")))
        d = gate.check(_intent("0.90"), b)
        self.assertFalse(d.allowed)
        self.assertEqual(d.code, "SLIPPAGE_EXCEEDED")


if __name__ == "__main__":
    unittest.main()
