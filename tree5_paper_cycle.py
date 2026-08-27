"""One deterministic, paper-only tree5 policy cycle.

The coordinator joins independently recorded data for one cycle. It performs no
I/O and no execution. A scheduler may call it after collecting frozen TAF,
market L2, calibration, cost and position evidence. Missing fields deliberately
produce blocked outcomes rather than optimistic guesses.
"""
from __future__ import annotations

from typing import Any

from tree5_ev_model import EVModelInputError, evaluate_paper_entry, evaluate_taf_market_alignment
from tree5_risk_state import RiskStateInputError, plan_position_risk


class PaperCycleInputError(ValueError):
    pass


def evaluate_paper_cycle(cycle: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(cycle, dict) or not isinstance(policy, dict):
        raise PaperCycleInputError("cycle_and_policy_must_be_objects")
    t0 = cycle.get("t0_monotonic_ns")
    taf = cycle.get("latest_visible_taf")
    market = cycle.get("market")
    if not isinstance(t0, int) or t0 <= 0 or not isinstance(taf, dict) or not isinstance(market, dict):
        raise PaperCycleInputError("t0_latest_visible_taf_and_market_required")
    common = {
        "cycle_id": cycle.get("cycle_id"), "t0_monotonic_ns": t0, "paper_only": True,
        "orders_submitted": 0, "credentials_loaded": False,
    }
    try:
        alignment = evaluate_taf_market_alignment(
            taf_token_id=str(taf.get("token_id") or ""), market_id=str(market.get("market_id") or ""),
            bucket_ids=market.get("bucket_ids"), snapshots=cycle.get("pre_t0_l2_snapshots", []),
            t0_monotonic_ns=t0, policy=policy,
        )
    except EVModelInputError as exc:
        alignment = {"status": "BLOCKED_ALIGNMENT_INPUT", "message": str(exc), "paper_only": True, "orders_submitted": 0}
    entry = evaluate_paper_entry(
        alignment=alignment, entry_snapshot=cycle.get("post_t0_entry_snapshot"), calibration=cycle.get("calibration"),
        costs=cycle.get("costs"), policy=policy, t0_monotonic_ns=t0,
    )
    position_results: list[dict[str, Any]] = []
    positions = cycle.get("confirmed_positions", [])
    if not isinstance(positions, list):
        raise PaperCycleInputError("confirmed_positions_must_be_array")
    evidence_by_position = cycle.get("position_evidence", {})
    if not isinstance(evidence_by_position, dict):
        raise PaperCycleInputError("position_evidence_must_be_object")
    for position in positions:
        if not isinstance(position, dict):
            continue
        position_id = str(position.get("position_id") or "")
        evidence = evidence_by_position.get(position_id, {})
        if not isinstance(evidence, dict):
            evidence = {}
        try:
            risk = plan_position_risk(
                position=position, observed_extreme=evidence.get("observed_extreme"), consensus=evidence.get("consensus"),
                time_closure_triggered=bool(evidence.get("time_closure_triggered", False)),
                alternative_entry=evidence.get("alternative_entry"), policy=policy,
            )
        except (RiskStateInputError, EVModelInputError) as exc:
            risk = {"risk_state": "BLOCKED_RISK_INPUT", "message": str(exc), "paper_only": True, "orders_submitted": 0}
        position_results.append({"position_id": position_id or None, "risk": risk})
    return {"status": entry.get("status"), "latest_visible_taf": taf, "market_alignment": alignment, "entry": entry, "position_risks": position_results, **common}
