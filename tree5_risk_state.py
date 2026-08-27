"""Paper-only risk transitions for a tree5 temperature-bucket position.

This module has no network, wallet, credential, order, cancellation, account,
or balance dependencies. It produces auditable `PAPER_*` decisions only. A
position is never assumed to exist merely because an entry was intended: exit
candidates require an explicitly supplied confirmed share count.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from tree5_ev_model import EVModelInputError, dec


class RiskStateInputError(ValueError):
    pass


PRIORITY = {
    "FACT_INVALIDATED": 3,
    "TIME_CLOSURE_AND_CONSENSUS_REVERSAL": 2,
    "CONSENSUS_REVERSAL": 1,
    "ACTIVE": 0,
}


def _bucket(position: dict[str, Any]) -> dict[str, Any]:
    value = position.get("bucket")
    if not isinstance(value, dict) or not str(value.get("bucket_id") or ""):
        raise RiskStateInputError("position_bucket_required")
    return value


def _shares(position: dict[str, Any]) -> Decimal:
    return dec(position.get("confirmed_shares", "0"), "confirmed_shares")


def fact_invalidated(position: dict[str, Any], observed_extreme: Any) -> bool:
    """Apply contract-range logic, not a point forecast comparison."""
    direction = str(position.get("direction") or "")
    bucket = _bucket(position)
    extreme = dec(observed_extreme, "observed_extreme")
    if direction == "high":
        hi = bucket.get("hi")
        return hi is not None and extreme >= dec(hi, "bucket_hi")
    if direction == "low":
        lo = bucket.get("lo")
        return lo is not None and extreme < dec(lo, "bucket_lo")
    raise RiskStateInputError("invalid_direction")


def consensus_reversed(position: dict[str, Any], consensus: dict[str, Any] | None, policy: dict[str, Any]) -> dict[str, Any]:
    """Evaluate a complete, executable-price market-consensus evidence record."""
    if not isinstance(consensus, dict):
        return {"status": "NOT_EVALUATED_NO_CONSENSUS"}
    if consensus.get("status") != "PAPER_MARKET_CONSENSUS_READY" or consensus.get("complete_market_coverage") is not True:
        return {"status": "NOT_EVALUATED_INCOMPLETE_CONSENSUS"}
    bucket_id = str(_bucket(position).get("bucket_id"))
    leader_bucket = str(consensus.get("leader_bucket_id") or "")
    prices = consensus.get("bucket_prices")
    if not leader_bucket or not isinstance(prices, dict) or bucket_id not in prices or leader_bucket not in prices:
        return {"status": "NOT_EVALUATED_INVALID_CONSENSUS"}
    held_price = dec(prices[bucket_id], "held_bucket_price", upper=Decimal("1"))
    leader_price = dec(prices[leader_bucket], "leader_bucket_price", upper=Decimal("1"))
    config = policy.get("risk_reversal")
    if not isinstance(config, dict):
        raise RiskStateInputError("risk_reversal_policy_required")
    minimum_absolute = dec(config.get("minimum_absolute_price_lead"), "minimum_absolute_price_lead", upper=Decimal("1"))
    minimum_relative = dec(config.get("minimum_relative_price_lead"), "minimum_relative_price_lead")
    absolute = leader_price - held_price
    relative = leader_price / held_price - 1 if held_price > 0 else None
    reversed_now = leader_bucket != bucket_id and absolute >= minimum_absolute and ((relative is not None and relative >= minimum_relative) or (held_price == 0 and leader_price >= minimum_absolute))
    return {
        "status": "CONSENSUS_REVERSAL" if reversed_now else "NO_CONSENSUS_REVERSAL",
        "held_bucket_id": bucket_id, "leader_bucket_id": leader_bucket, "held_executable_bid": str(held_price),
        "leader_executable_bid": str(leader_price), "absolute_lead": str(absolute),
        "relative_lead": str(relative) if relative is not None else None,
        "minimum_absolute_lead": str(minimum_absolute), "minimum_relative_lead": str(minimum_relative),
    }


def _safe_position_id(position: dict[str, Any]) -> str:
    identifier = str(position.get("position_id") or position.get("entry_id") or "")
    if not identifier:
        raise RiskStateInputError("position_id_required")
    return identifier


def _base(position: dict[str, Any], reason: str, priority: int) -> dict[str, Any]:
    bucket = _bucket(position)
    return {
        "position_id": _safe_position_id(position), "market_id": position.get("market_id"), "direction": position.get("direction"),
        "held_bucket_id": bucket.get("bucket_id"), "token_id": position.get("token_id"), "reason": reason, "priority": priority,
        "paper_only": True, "orders_submitted": 0, "credentials_loaded": False,
    }


def plan_position_risk(*, position: dict[str, Any], observed_extreme: Any | None, consensus: dict[str, Any] | None, time_closure_triggered: bool, alternative_entry: dict[str, Any] | None, policy: dict[str, Any]) -> dict[str, Any]:
    """Create non-executable risk and possible new-research actions.

    `alternative_entry` is trusted only when an independent EV pipeline has
    already returned `PAPER_ENTRY_READY`. Even then it creates a separate paper
    candidate, never an atomic sell-and-buy instruction.
    """
    confirmed = _shares(position)
    pending_entry = bool(position.get("pending_entry_id"))
    base_actions: list[dict[str, Any]] = []
    state = "ACTIVE"
    fact = observed_extreme is not None and fact_invalidated(position, observed_extreme)
    reversal = consensus_reversed(position, consensus, policy)
    if fact:
        state = "FACT_INVALIDATED"
    elif reversal.get("status") == "CONSENSUS_REVERSAL" and time_closure_triggered:
        state = "TIME_CLOSURE_AND_CONSENSUS_REVERSAL"
    elif reversal.get("status") == "CONSENSUS_REVERSAL":
        state = "CONSENSUS_REVERSAL"
    priority = PRIORITY[state]
    base = _base(position, state, priority)
    if state != "ACTIVE":
        base_actions.append({"action_type": "PAPER_STOP_NEW_ENTRIES", "status": "PAPER_RISK_ACTIVE", **base})
        if pending_entry:
            base_actions.append({"action_type": "PAPER_CANCEL_CANDIDATE", "status": "PAPER_CANCEL_PENDING_ENTRY", "pending_entry_id": position.get("pending_entry_id"), **base})
        if confirmed > 0:
            base_actions.append({"action_type": "PAPER_EXIT_CANDIDATE", "status": "PAPER_EXIT_FACT_INVALIDATED" if state == "FACT_INVALIDATED" else "PAPER_EXIT_CONSENSUS_REVERSAL", "confirmed_shares": str(confirmed), **base})
            base_actions.append({"action_type": "PAPER_ROUTE_COMPARISON_REQUIRED", "status": "PAPER_COMPARE_SELL_YES_VS_BUY_NO_OR_MERGE", "confirmed_shares": str(confirmed), **base})
    if state != "FACT_INVALIDATED" and isinstance(alternative_entry, dict) and alternative_entry.get("status") == "PAPER_ENTRY_READY":
        base_actions.append({"action_type": "PAPER_INDEPENDENT_NEW_ENTRY_CANDIDATE", "status": "PAPER_NEW_ENTRY_REQUIRES_SEPARATE_RECONCILIATION", "source_position_id": _safe_position_id(position), "alternative_entry": alternative_entry, "paper_only": True, "orders_submitted": 0, "credentials_loaded": False})
    return {"risk_state": state, "priority": priority, "fact_invalidated": fact, "consensus": reversal, "actions": base_actions, "paper_only": True, "orders_submitted": 0}
