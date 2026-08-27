"""Deterministic paper-only EV and TAF/market-consensus evaluation for tree5.

The module accepts previously recorded market snapshots and calibration output.
It intentionally has no HTTP, WebSocket, wallet, credential, CLOB order,
account, cancellation, or position dependency. Missing evidence blocks a paper
candidate instead of being replaced with an optimistic default.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, ROUND_DOWN
from typing import Any


class EVModelInputError(ValueError):
    pass


def dec(value: Any, name: str, *, upper: Decimal | None = None) -> Decimal:
    try:
        output = Decimal(str(value))
    except Exception as exc:
        raise EVModelInputError(f"invalid_{name}") from exc
    if not output.is_finite() or output < 0 or (upper is not None and output > upper):
        raise EVModelInputError(f"invalid_{name}")
    return output


def positive_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or value <= 0:
        raise EVModelInputError(f"invalid_{name}")
    return value


def parse_levels(levels: Any, *, descending: bool, target_shares: Decimal, limit_price: Decimal | None = None) -> dict[str, Any]:
    if not isinstance(levels, (list, tuple)):
        raise EVModelInputError("invalid_l2_levels")
    parsed: list[tuple[Decimal, Decimal]] = []
    for level in levels:
        if not isinstance(level, dict):
            raise EVModelInputError("invalid_l2_level")
        price = dec(level.get("price"), "level_price", upper=Decimal("1"))
        size = dec(level.get("size"), "level_size")
        if price <= 0 or size <= 0:
            continue
        if limit_price is not None and ((descending and price < limit_price) or (not descending and price > limit_price)):
            continue
        parsed.append((price, size))
    parsed.sort(reverse=descending)
    remaining = target_shares
    filled, notional, total_depth = Decimal("0"), Decimal("0"), Decimal("0")
    for price, size in parsed:
        total_depth += price * size
        fill = min(remaining, size)
        if fill > 0:
            filled += fill
            notional += fill * price
            remaining -= fill
    return {
        "levels": tuple({"price": price, "size": size} for price, size in parsed),
        "best_price": parsed[0][0] if parsed else None,
        "target_filled_shares": filled,
        "target_vwap": notional / filled if filled > 0 else None,
        "total_notional_depth": total_depth,
    }


def weighted_median(values: list[tuple[Decimal, int]]) -> Decimal:
    filtered = sorted((value, duration) for value, duration in values if duration > 0)
    if not filtered:
        raise EVModelInputError("no_weighted_values")
    total_duration = sum(duration for _, duration in filtered)
    elapsed = 0
    for value, duration in filtered:
        elapsed += duration
        if elapsed * 2 >= total_duration:
            return value
    return filtered[-1][0]


def _quote(raw: dict[str, Any], target_shares: Decimal) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("ready") is not True:
        raise EVModelInputError("snapshot_not_ready")
    timestamp = positive_int(raw.get("received_monotonic_ns"), "received_monotonic_ns")
    bucket_id = str(raw.get("bucket_id") or "")
    token_id = str(raw.get("token_id") or "")
    market_id = str(raw.get("market_id") or raw.get("market_rule_id") or "")
    if not bucket_id or not token_id or not market_id:
        raise EVModelInputError("snapshot_identity_required")
    return {
        "received_monotonic_ns": timestamp, "bucket_id": bucket_id, "token_id": token_id, "market_id": market_id,
        "bids": parse_levels(raw.get("bids"), descending=True, target_shares=target_shares),
        "asks": parse_levels(raw.get("asks"), descending=False, target_shares=target_shares),
        "book_hash": raw.get("book_hash"), "tick_size": raw.get("tick_size"), "min_order_size": raw.get("min_order_size"),
    }


def _bucket_statistics(quotes: list[dict[str, Any]], start_ns: int, t0_ns: int, minimum_count: int, min_coverage: Decimal, final_max_age_ns: int) -> dict[str, Any]:
    if len(quotes) < minimum_count:
        return {"status": "BLOCKED_INSUFFICIENT_SNAPSHOT_COUNT", "snapshot_count": len(quotes), "required": minimum_count}
    quotes = sorted(quotes, key=lambda value: value["received_monotonic_ns"])
    first, last = quotes[0]["received_monotonic_ns"], quotes[-1]["received_monotonic_ns"]
    if t0_ns - last > final_max_age_ns:
        return {"status": "BLOCKED_STALE_FINAL_SNAPSHOT", "last_age_ns": t0_ns - last, "maximum_age_ns": final_max_age_ns}
    total_window = t0_ns - start_ns
    coverage = max(0, last - first)
    if Decimal(coverage) < Decimal(total_window) * min_coverage:
        return {"status": "BLOCKED_INSUFFICIENT_COVERAGE", "coverage_ns": coverage, "window_ns": total_window, "minimum_coverage_ratio": str(min_coverage)}
    weighted_bids: list[tuple[Decimal, int]] = []
    for index, quote in enumerate(quotes):
        bid = quote["bids"]["best_price"]
        if bid is None:
            continue
        next_time = quotes[index + 1]["received_monotonic_ns"] if index + 1 < len(quotes) else t0_ns
        duration = max(0, min(t0_ns, next_time) - max(start_ns, quote["received_monotonic_ns"]))
        if duration:
            weighted_bids.append((bid, duration))
    if not weighted_bids:
        return {"status": "BLOCKED_NO_EXECUTABLE_BID"}
    return {"status": "READY", "snapshot_count": len(quotes), "coverage_ns": coverage, "last_snapshot_ns": last, "tw_executable_bid_median": weighted_median(weighted_bids)}


def evaluate_taf_market_alignment(
    *, taf_token_id: str, market_id: str, bucket_ids: list[str], snapshots: list[dict[str, Any]], t0_monotonic_ns: int, policy: dict[str, Any],
) -> dict[str, Any]:
    """Verify that t0's latest TAF bucket was the adequately supported market leader.

    The assessment consumes only snapshots in [t0-window, t0); all active
    buckets supplied by the immutable market-rule snapshot need coverage.
    """
    t0 = positive_int(t0_monotonic_ns, "t0_monotonic_ns")
    if not taf_token_id or not market_id or not isinstance(bucket_ids, list) or not bucket_ids:
        raise EVModelInputError("market_identity_required")
    alignment = policy.get("taf_market_alignment")
    if not isinstance(alignment, dict):
        raise EVModelInputError("taf_market_alignment_policy_required")
    target = dec(policy.get("net_expected_value", {}).get("target_shares"), "target_shares")
    window_ns = positive_int(alignment.get("lookback_window_seconds"), "lookback_window_seconds") * 1_000_000_000
    start = t0 - window_ns
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    token_by_bucket: dict[str, str] = {}
    for raw in snapshots:
        try:
            quote = _quote(raw, target)
        except EVModelInputError:
            continue
        if quote["market_id"] != market_id or not start <= quote["received_monotonic_ns"] < t0:
            continue
        grouped[quote["bucket_id"]].append(quote)
        token_by_bucket[quote["bucket_id"]] = quote["token_id"]
    expected = {str(bucket) for bucket in bucket_ids}
    if taf_token_id not in token_by_bucket.values():
        return {"status": "BLOCKED_TAF_TOKEN_NOT_IN_PRE_T0_MARKET", "paper_only": True, "orders_submitted": 0}
    minimum_count = positive_int(alignment.get("minimum_snapshot_count"), "minimum_snapshot_count")
    min_coverage = dec(alignment.get("minimum_coverage_ratio"), "minimum_coverage_ratio", upper=Decimal("1"))
    final_age_ns = positive_int(alignment.get("maximum_final_snapshot_age_seconds"), "maximum_final_snapshot_age_seconds") * 1_000_000_000
    statistics = {bucket: _bucket_statistics(grouped.get(bucket, []), start, t0, minimum_count, min_coverage, final_age_ns) for bucket in expected}
    unavailable = sorted(bucket for bucket, stat in statistics.items() if stat["status"] != "READY")
    common = {"paper_only": True, "orders_submitted": 0, "market_id": market_id, "taf_token_id": taf_token_id, "t0_monotonic_ns": t0, "bucket_statistics": _json_safe_statistics(statistics)}
    if unavailable:
        return {"status": "BLOCKED_INCOMPLETE_MARKET_COVERAGE", "unavailable_bucket_ids": unavailable, **common}
    ranking = sorted(((bucket, stat["tw_executable_bid_median"]) for bucket, stat in statistics.items()), key=lambda row: row[1], reverse=True)
    leader_bucket, leader_price = ranking[0]
    taf_bucket = next(bucket for bucket, token in token_by_bucket.items() if token == taf_token_id)
    if taf_bucket != leader_bucket:
        return {"status": "BLOCKED_TAF_NOT_MARKET_LEADER", "taf_bucket_id": taf_bucket, "market_leader_bucket_id": leader_bucket, "market_leader_price": str(leader_price), **common}
    runner_price = ranking[1][1] if len(ranking) > 1 else Decimal("0")
    absolute_lead = leader_price - runner_price
    relative_lead = (leader_price / runner_price - 1) if runner_price > 0 else None
    min_absolute = dec(alignment.get("minimum_absolute_price_lead"), "minimum_absolute_price_lead", upper=Decimal("1"))
    min_relative = dec(alignment.get("minimum_relative_price_lead"), "minimum_relative_price_lead")
    zero_minimum = dec(alignment.get("zero_runner_up_minimum_leader_price"), "zero_runner_up_minimum_leader_price", upper=Decimal("1"))
    sufficient = absolute_lead >= min_absolute and ((relative_lead is not None and relative_lead >= min_relative) or (runner_price == 0 and leader_price >= zero_minimum))
    if not sufficient:
        return {"status": "BLOCKED_CONSENSUS_LEAD_INSUFFICIENT", "taf_bucket_id": taf_bucket, "absolute_lead": str(absolute_lead), "relative_lead": str(relative_lead) if relative_lead is not None else None, "minimum_absolute_lead": str(min_absolute), "minimum_relative_lead": str(min_relative), **common}
    return {"status": "PAPER_ALIGNMENT_READY", "taf_bucket_id": taf_bucket, "leader_price": str(leader_price), "runner_up_price": str(runner_price), "absolute_lead": str(absolute_lead), "relative_lead": str(relative_lead) if relative_lead is not None else None, **common}


def walk_post_signal_ask(*, entry_snapshot: dict[str, Any], target_shares: Decimal, t0_monotonic_ns: int, max_delay_seconds: int) -> dict[str, Any]:
    quote = _quote(entry_snapshot, target_shares)
    t0 = positive_int(t0_monotonic_ns, "t0_monotonic_ns")
    delay = quote["received_monotonic_ns"] - t0
    maximum = positive_int(max_delay_seconds, "maximum_post_signal_entry_delay_seconds") * 1_000_000_000
    if delay < 0 or delay > maximum:
        return {"status": "BLOCKED_POST_SIGNAL_ENTRY_DELAY", "entry_delay_ns": delay, "maximum_delay_ns": maximum}
    if quote["asks"]["target_filled_shares"] < target_shares:
        return {"status": "BLOCKED_INSUFFICIENT_VISIBLE_ASK_DEPTH", "visible_shares": str(quote["asks"]["target_filled_shares"]), "required_shares": str(target_shares)}
    return {"status": "READY", "entry_delay_ns": delay, "vwap_ask": quote["asks"]["target_vwap"], "target_shares": target_shares, "token_id": quote["token_id"], "book_hash": quote["book_hash"]}


def ev_net_lower_bound(*, p_lower: Any, calibration_oos_sample_count: int, q_fill: Any, target_shares: Any, executable_vwap_ask: Any, entry_fee_full_fill: Any, expected_exit_cost: Any, latency_slippage_reserve: Any, policy: dict[str, Any]) -> dict[str, Any]:
    """Calculate the conservative net-EV lower bound from fully supplied inputs."""
    ev_policy = policy.get("net_expected_value")
    if not isinstance(ev_policy, dict):
        raise EVModelInputError("net_expected_value_policy_required")
    required_samples = positive_int(ev_policy.get("minimum_oos_sample_count"), "minimum_oos_sample_count")
    if calibration_oos_sample_count < required_samples:
        return {"status": "BLOCKED_INSUFFICIENT_OOS_CALIBRATION", "oos_sample_count": calibration_oos_sample_count, "required_oos_sample_count": required_samples, "paper_only": True, "orders_submitted": 0}
    probability = dec(p_lower, "p_lower", upper=Decimal("1"))
    fill_probability = dec(q_fill, "q_fill", upper=Decimal("1"))
    shares = dec(target_shares, "target_shares")
    vwap = dec(executable_vwap_ask, "executable_vwap_ask", upper=Decimal("1"))
    entry_fee = dec(entry_fee_full_fill, "entry_fee_full_fill")
    exit_cost = dec(expected_exit_cost, "expected_exit_cost")
    reserve = dec(latency_slippage_reserve, "latency_slippage_reserve")
    gross_edge = fill_probability * shares * (probability - vwap)
    expected_entry_fee = fill_probability * entry_fee
    ev_lower = gross_edge - expected_entry_fee - exit_cost - reserve
    return {"status": "PAPER_EV_POSITIVE" if ev_lower > 0 else "BLOCKED_EV_LOWER_BOUND_NONPOSITIVE", "p_lower": str(probability), "q_fill": str(fill_probability), "target_shares": str(shares), "executable_vwap_ask": str(vwap), "gross_edge_usdc": str(gross_edge), "expected_entry_fee_usdc": str(expected_entry_fee), "expected_exit_cost_usdc": str(exit_cost), "latency_slippage_reserve_usdc": str(reserve), "ev_net_lower_usdc": str(ev_lower), "paper_only": True, "orders_submitted": 0}


def evaluate_paper_entry(*, alignment: dict[str, Any], entry_snapshot: dict[str, Any] | None, calibration: dict[str, Any] | None, costs: dict[str, Any] | None, policy: dict[str, Any], t0_monotonic_ns: int) -> dict[str, Any]:
    """Join alignment, post-signal L2 and calibration evidence into a paper result."""
    if alignment.get("status") != "PAPER_ALIGNMENT_READY":
        return {"status": "BLOCKED_ALIGNMENT", "alignment": alignment, "paper_only": True, "orders_submitted": 0}
    if not isinstance(entry_snapshot, dict) or not isinstance(calibration, dict) or not isinstance(costs, dict):
        return {"status": "BLOCKED_MISSING_EV_INPUT", "paper_only": True, "orders_submitted": 0}
    ev_policy = policy.get("net_expected_value", {})
    shares = dec(ev_policy.get("target_shares"), "target_shares")
    entry = walk_post_signal_ask(entry_snapshot=entry_snapshot, target_shares=shares, t0_monotonic_ns=t0_monotonic_ns, max_delay_seconds=int(ev_policy.get("maximum_post_signal_entry_delay_seconds", 0)))
    if entry.get("status") != "READY":
        return {"status": entry["status"], "alignment": alignment, "entry": _decimal_strings(entry), "paper_only": True, "orders_submitted": 0}
    ev = ev_net_lower_bound(p_lower=calibration.get("p_lower"), calibration_oos_sample_count=calibration.get("oos_sample_count"), q_fill=calibration.get("q_fill"), target_shares=shares, executable_vwap_ask=entry["vwap_ask"], entry_fee_full_fill=costs.get("entry_fee_full_fill"), expected_exit_cost=costs.get("expected_exit_cost"), latency_slippage_reserve=costs.get("latency_slippage_reserve", ev_policy.get("latency_slippage_reserve_usdc")), policy=policy)
    return {"status": "PAPER_ENTRY_READY" if ev["status"] == "PAPER_EV_POSITIVE" else ev["status"], "alignment": alignment, "entry": _decimal_strings(entry), "ev": ev, "paper_only": True, "orders_submitted": 0, "credentials_loaded": False}


def _decimal_strings(value: dict[str, Any]) -> dict[str, Any]:
    return {key: str(item) if isinstance(item, Decimal) else item for key, item in value.items()}


def _json_safe_statistics(statistics: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {bucket: _decimal_strings(statistic) for bucket, statistic in statistics.items()}
