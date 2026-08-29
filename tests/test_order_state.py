"""Tests for execution.order_state: transitions + FAK cancel-remainder."""
from __future__ import annotations

import unittest
from decimal import Decimal

from execution.order_intent import OrderStatus
from execution.order_state import (
    FAK_REMAINDER_CANCELLED,
    InvalidTransitionError,
    OrderState,
    OrderStateMachine,
)


class OrderStateTests(unittest.TestCase):
    def test_created_initializes_zero_fill(self) -> None:
        state = OrderState.created("o1", Decimal("5"))
        self.assertEqual(state.status, OrderStatus.CREATED)
        self.assertEqual(state.filled_size, Decimal("0"))
        self.assertEqual(state.remaining_size, Decimal("5"))
        self.assertFalse(state.terminal)

    def test_valid_submit_chain(self) -> None:
        machine = OrderStateMachine(OrderState.created("o1", Decimal("5")))
        machine.transition(OrderStatus.SUBMITTING)
        machine.transition(OrderStatus.SUBMITTED)
        machine.transition(OrderStatus.ACKED)
        self.assertEqual(machine.state.status, OrderStatus.ACKED)

    def test_illegal_transition_raises(self) -> None:
        machine = OrderStateMachine(OrderState.created("o1", Decimal("5")))
        with self.assertRaises(InvalidTransitionError):
            machine.transition(OrderStatus.ACKED)  # CREATED -> ACKED is illegal

    def test_risk_reject_is_terminal(self) -> None:
        machine = OrderStateMachine(OrderState.created("o1", Decimal("5")))
        machine.transition(OrderStatus.RISK_REJECTED, reason="price too high")
        self.assertTrue(machine.state.terminal)
        self.assertEqual(machine.state.reject_reason, "price too high")

    def test_full_fill_is_filled(self) -> None:
        machine = OrderStateMachine(OrderState.created("o1", Decimal("5")))
        machine.apply_match(Decimal("5"), order_type="FAK")
        self.assertEqual(machine.state.status, OrderStatus.FILLED)
        self.assertEqual(machine.state.filled_size, Decimal("5"))
        self.assertEqual(machine.state.remaining_size, Decimal("0"))

    def test_fak_partial_cancels_remainder_explicitly(self) -> None:
        # Audit §5 example: BUY 5, only 3 available -> filled 3, cancel 2.
        machine = OrderStateMachine(OrderState.created("o1", Decimal("5")))
        machine.apply_match(Decimal("3"), order_type="FAK")
        self.assertEqual(machine.state.status, OrderStatus.CANCELLED)
        self.assertEqual(machine.state.cancel_reason, FAK_REMAINDER_CANCELLED)
        self.assertEqual(machine.state.filled_size, Decimal("3"))
        self.assertEqual(machine.state.remaining_size, Decimal("0"))

    def test_fak_zero_fill_cancels_with_no_fill(self) -> None:
        machine = OrderStateMachine(OrderState.created("o1", Decimal("5")))
        machine.apply_match(Decimal("0"), order_type="FAK")
        self.assertEqual(machine.state.status, OrderStatus.CANCELLED)
        self.assertEqual(machine.state.filled_size, Decimal("0"))

    def test_gtc_partial_rests(self) -> None:
        machine = OrderStateMachine(OrderState.created("o1", Decimal("5")))
        machine.apply_match(Decimal("2"), order_type="GTC")
        self.assertEqual(machine.state.status, OrderStatus.PARTIALLY_FILLED)
        self.assertEqual(machine.state.filled_size, Decimal("2"))
        self.assertEqual(machine.state.remaining_size, Decimal("3"))
        self.assertIsNone(machine.state.cancel_reason)

    def test_cancel_open_order(self) -> None:
        machine = OrderStateMachine(OrderState.created("o1", Decimal("5")))
        machine.cancel(reason="manual_cancel")
        self.assertEqual(machine.state.status, OrderStatus.CANCELLED)
        self.assertEqual(machine.state.cancel_reason, "manual_cancel")

    def test_cancel_terminal_raises(self) -> None:
        state = OrderState.created("o1", Decimal("5"))
        state.status = OrderStatus.FILLED
        with self.assertRaises(InvalidTransitionError):
            OrderStateMachine(state).cancel(reason="late")


if __name__ == "__main__":
    unittest.main()
