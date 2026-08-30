"""Shared paper-trading cash ledger for the weatherbot paper simulators.

A single paper account starts with ``paper_initial_capital_usdc`` (default
1000 USDC). Every simulated fill reserves cash from that account. The ledger
lives in the observer ``state`` dict and is therefore serialised to
``data/state.json`` exactly like the rest of the deterministic state.

No real money, no wallet, no CLOB credentials, no order submission.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

DEFAULT_INITIAL_CAPITAL_USDC = Decimal("1000.00")


def _decimal(value: Any, default: Decimal) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except Exception:
        return default
    return parsed if parsed.is_finite() and parsed >= 0 else default


def initial_capital_usdc(state: dict[str, Any]) -> Decimal:
    """Return the paper account's starting capital, defaulting to 1000 USDC."""
    return _decimal(state.get("paper_initial_capital_usdc"), DEFAULT_INITIAL_CAPITAL_USDC)


def total_debit_usdc(state: dict[str, Any]) -> Decimal:
    """Return the running paper cash outflow (principal + fees) so far."""
    return _decimal(state.get("paper_total_debit_usdc"), Decimal("0"))


def remaining_capital_usdc(state: dict[str, Any]) -> Decimal:
    """Return the unused paper cash still available for simulated fills."""
    return initial_capital_usdc(state) - total_debit_usdc(state)


def reserve(state: dict[str, Any], amount_usdc: Any) -> Decimal | None:
    """Reserve ``amount_usdc`` of paper cash; return new total debit or None.

    ``None`` means the account does not have enough remaining capital, in which
    case the caller must fail closed and never mutate position state.
    """
    amount = Decimal(str(amount_usdc))
    if amount < 0:
        raise ValueError("paper debit must be non-negative")
    initial = initial_capital_usdc(state)
    if amount > initial - total_debit_usdc(state):
        return None
    state["paper_initial_capital_usdc"] = float(initial)
    new_total = (total_debit_usdc(state) + amount).quantize(Decimal("0.00001"))
    state["paper_total_debit_usdc"] = float(new_total)
    return new_total


def release(state: dict[str, Any], amount_usdc: Any) -> Decimal:
    """Credit paper cash back after a simulated sell (reduce total debit).

    Used by paper exit settlement so overturned positions free capital and
    realized PnL is reflected in the ledger. Never goes below zero debit.
    """
    amount = Decimal(str(amount_usdc))
    if amount < 0:
        raise ValueError("paper credit must be non-negative")
    initial = initial_capital_usdc(state)
    new_total = (total_debit_usdc(state) - amount).quantize(Decimal("0.00001"))
    if new_total < 0:
        new_total = Decimal("0")
    state["paper_initial_capital_usdc"] = float(initial)
    state["paper_total_debit_usdc"] = float(new_total)
    return new_total

