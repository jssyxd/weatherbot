"""Offline price-and-depth consensus gate for tree11 paper YES candidates.

No WebSocket connection, public HTTP request, account operation, wallet,
credential, signing, order placement, cancellation or position lookup exists in
this module. It operates only on immutable snapshot dictionaries collected by a
separate recorder before ``t0``.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, ROUND_DOWN
from typing import Any


class ConsensusInputError(ValueError):
    pass


def D(value: Any, name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except Exception as exc:
        raise ConsensusInputError(f"invalid_{name}") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ConsensusInputError(f"invalid_{name}")
    return parsed


def I(value: Any, name: str) -> int:
    if not isinstance(value, int) or value <= 0:
        raise ConsensusInputError(f"invalid_{name}")
    return value


def level_summary(levels: Any, *, descending: bool, target_shares: Decimal, limit_price: Decimal | None = None) -> dict[str, Decimal | None]:
    if not isinstance(levels, (list, tuple)):
        raise ConsensusInputError("invalid_l2_levels")
    parsed: list[tuple[Decimal, Decimal]] = []
    for row in levels:
        if not isinstance(row, dict):
            raise ConsensusInputError("invalid_l2_level")
        price, shares = D(row.get("price"), "level_price"), D(row.get("size"), "level_size")
        if price <= 0 or shares <= 0:
            continue
        if limit_price is not None:
            if descending and price < limit_price:
                continue
            if not descending and price > limit_price:
                continue
        parsed.append((price, shares))
    parsed.sort(reverse=descending)
    total_shares = sum((shares for _, shares in parsed), Decimal("0"))
    total_notional = sum((price * shares for price, shares in parsed), Decimal("0"))
    remaining = target_shares
    walked_shares = Decimal("0")
    walked_notional = Decimal("0")
    worst = None
    for price, shares in parsed:
        take = min(shares, remaining)
        if take <= 0:
            break
        walked_shares += take
        walked_notional += take * price
        remaining -= take
        worst = price
    return {
        "levels": tuple({"price": price, "size": shares} for price, shares in parsed),
        "best_price": parsed[0][0] if parsed else None,
        "total_shares": total_shares,
        "total_notional": total_notional,
        "target_walked_shares": walked_shares,
        "target_walked_notional": walked_notional,
        "target_vwap": (walked_notional / walked_shares) if walked_shares > 0 else None,
        "target_worst_price": worst,
    }


def snapshot_quote(snapshot: dict[str, Any], target_shares: Decimal) -> dict[str, Any]:
    """Validate one recorded snapshot and compute visible executable depth."""
    if not isinstance(snapshot, dict) or snapshot.get("ready") is not True:
        raise ConsensusInputError("snapshot_not_ready")
    timestamp = I(snapshot.get("received_monotonic_ns"), "snapshot_received_monotonic_ns")
    token_id = str(snapshot.get("token_id") or "")
    bucket_id = str(snapshot.get("bucket_id") or "")
    market_rule_id = str(snapshot.get("market_rule_id") or "")
    if not token_id or not bucket_id or not market_rule_id:
        raise ConsensusInputError("snapshot_identity_required")
    tick = D(snapshot.get("tick_size"), "tick_size")
    minimum = D(snapshot.get("min_order_size"), "min_order_size")
    if tick <= 0 or minimum <= 0:
        raise ConsensusInputError("snapshot_trading_metadata_required")
    bids = level_summary(snapshot.get("bids"), descending=True, target_shares=target_shares)
    asks = level_summary(snapshot.get("asks"), descending=False, target_shares=target_shares)
    return {
        "received_monotonic_ns": timestamp, "market_rule_id": market_rule_id, "bucket_id": bucket_id,
        "token_id": token_id, "tick_size": tick, "min_order_size": minimum,
        "book_hash": snapshot.get("book_hash"), "bids": bids, "asks": asks,
        "raw_bids": bids["levels"], "raw_asks": asks["levels"],
    }


def weighted_median(values: list[tuple[Decimal, int]]) -> Decimal:
    filtered = [(value, weight) for value, weight in values if weight > 0]
    if not filtered:
        raise ConsensusInputError("no_weighted_values")
    total = sum(weight for _, weight in filtered)
    accumulated = 0
    for value, weight in sorted(filtered, key=lambda item: item[0]):
        accumulated += weight
        if accumulated * 2 >= total:
            return value
    return filtered[-1][0]


def quote_history_stats(quotes: list[dict[str, Any]], *, start_ns: int, end_ns: int, policy: dict[str, Any]) -> dict[str, Any]:
    if not quotes:
        return {"status": "BLOCKED_NO_SNAPSHOTS"}
    quotes = sorted(quotes, key=lambda quote: quote["received_monotonic_ns"])
    count = len(quotes)
    latest = quotes[-1]["received_monotonic_ns"]
    earliest = quotes[0]["received_monotonic_ns"]
    minimum_count = int(policy["minimum_snapshot_count"])
    maximum_age_ns = int(policy["maximum_final_snapshot_age_seconds"]) * 1_000_000_000
    min_coverage_ratio = D(policy.get("minimum_coverage_ratio", "0.75"), "minimum_coverage_ratio")
    coverage_ns = max(0, latest - earliest)
    window_ns = end_ns - start_ns
    if count < minimum_count:
        return {"status": "BLOCKED_INSUFFICIENT_SNAPSHOT_COUNT", "snapshot_count": count, "required": minimum_count}
    if latest < end_ns - maximum_age_ns:
        return {"status": "BLOCKED_STALE_FINAL_SNAPSHOT", "last_age_ns": end_ns - latest, "maximum_age_ns": maximum_age_ns}
    if Decimal(coverage_ns) < Decimal(window_ns) * min_coverage_ratio:
        return {"status": "BLOCKED_INSUFFICIENT_WINDOW_COVERAGE", "coverage_ns": coverage_ns, "window_ns": window_ns, "minimum_coverage_ratio": str(min_coverage_ratio)}

    weighted_prices: list[tuple[Decimal, int]] = []
    weighted_share_depth: list[tuple[Decimal, int]] = []
    weighted_notional_depth: list[tuple[Decimal, int]] = []
    for index, quote in enumerate(quotes):
        bid = quote["bids"]
        price = bid["best_price"]
        if price is None:
            continue
        next_ns = quotes[index + 1]["received_monotonic_ns"] if index + 1 < len(quotes) else end_ns
        weight = max(0, min(end_ns, next_ns) - max(start_ns, quote["received_monotonic_ns"]))
        if weight <= 0:
            continue
        weighted_prices.append((price, weight))
        weighted_share_depth.append((bid["total_shares"], weight))
        weighted_notional_depth.append((bid["total_notional"], weight))
    if not weighted_prices:
        return {"status": "BLOCKED_NO_EXECUTABLE_BID_COVERAGE"}
    return {
        "status": "READY", "snapshot_count": count, "earliest_snapshot_ns": earliest, "latest_snapshot_ns": latest,
        "coverage_ns": coverage_ns, "time_weighted_bid_median": weighted_median(weighted_prices),
        "time_weighted_bid_depth_shares_median": weighted_median(weighted_share_depth),
        "time_weighted_bid_depth_usdc_median": weighted_median(weighted_notional_depth),
    }


def price_with_ticks(best_ask: Decimal, tick: Decimal, ticks: int) -> Decimal:
    if ticks < 0:
        raise ConsensusInputError("negative_ticks_not_allowed")
    return ((best_ask + tick * ticks) / tick).to_integral_value(rounding=ROUND_DOWN) * tick


def _bucket_ids(signal: dict[str, Any]) -> tuple[str, str, str]:
    old_bucket = signal.get("old_bucket")
    new_bucket = signal.get("new_bucket")
    if not isinstance(old_bucket, dict) or not isinstance(new_bucket, dict):
        raise ConsensusInputError("signal_bucket_required")
    old_id, new_id = str(old_bucket.get("bucket_id") or ""), str(new_bucket.get("bucket_id") or "")
    rule_id = str(signal.get("old_market_rule_id") or signal.get("market_rule_id") or "")
    if not old_id or not new_id or not rule_id:
        raise ConsensusInputError("signal_bucket_identity_required")
    return rule_id, old_id, new_id


def evaluate_price_depth_consensus(signal: dict[str, Any], snapshots: list[dict[str, Any]], policy: dict[str, Any], t0_monotonic_ns: int, entry_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    """Apply pre-t0 consensus evidence and output paper-price profiles.

    The function refuses a partial cross-bucket comparison: every active bucket
    in the same market rule needs enough valid snapshots to rank the old bucket.
    """
    if signal.get("status") != "PENDING_CONSENSUS":
        return {"status": "BLOCKED_SIGNAL_NOT_PENDING", "signal_id": signal.get("signal_id")}
    t0 = I(t0_monotonic_ns, "t0_monotonic_ns")
    consensus = policy.get("consensus")
    entry = policy.get("new_bucket_entry")
    if not isinstance(consensus, dict) or not isinstance(entry, dict):
        raise ConsensusInputError("policy_sections_required")
    target = D(entry.get("target_shares"), "target_shares")
    if target <= 0:
        raise ConsensusInputError("target_shares_must_be_positive")
    rule_id, old_id, new_id = _bucket_ids(signal)
    window_ns = int(consensus.get("lookback_window_seconds", 0)) * 1_000_000_000
    if window_ns <= 0:
        raise ConsensusInputError("invalid_lookback_window_seconds")
    start_ns = t0 - window_ns
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    listed_bucket_ids = signal.get("market_bucket_ids")
    expected_bucket_ids = set(str(bucket_id) for bucket_id in listed_bucket_ids) if isinstance(listed_bucket_ids, list) else {old_id, new_id}
    expected_bucket_ids.update({old_id, new_id})
    for raw in snapshots:
        try:
            quote = snapshot_quote(raw, target)
        except ConsensusInputError:
            continue
        if quote["market_rule_id"] != rule_id or not start_ns <= quote["received_monotonic_ns"] < t0:
            continue
        grouped[quote["bucket_id"]].append(quote)

    stats = {bucket_id: quote_history_stats(quotes, start_ns=start_ns, end_ns=t0, policy=consensus) for bucket_id, quotes in grouped.items()}
    missing = [bucket_id for bucket_id in expected_bucket_ids if stats.get(bucket_id, {}).get("status") != "READY"]
    base = {"signal_id": signal.get("signal_id"), "t0_monotonic_ns": t0, "market_rule_id": rule_id, "old_bucket_id": old_id, "new_bucket_id": new_id, "window_start_monotonic_ns": start_ns, "window_end_monotonic_ns": t0, "paper_only": True, "orders_submitted": 0}
    if missing:
        return {"status": "BLOCKED_INCOMPLETE_CONSENSUS_COVERAGE", "missing_or_unready_bucket_ids": sorted(missing), "bucket_stats": _json_stats(stats), **base}

    rankings = sorted(((bucket_id, value["time_weighted_bid_median"]) for bucket_id, value in stats.items()), key=lambda item: item[1], reverse=True)
    old_rank = next((index + 1 for index, (bucket_id, _) in enumerate(rankings) if bucket_id == old_id), None)
    old_stat = stats[old_id]
    runner_up = next((price for bucket_id, price in rankings if bucket_id != old_id), Decimal("0"))
    lead = old_stat["time_weighted_bid_median"] - runner_up
    min_lead = D(consensus.get("minimum_price_lead"), "minimum_price_lead")
    min_depth_usdc = D(consensus.get("minimum_old_bucket_depth_usdc"), "minimum_old_bucket_depth_usdc")
    min_depth_shares = target * D(consensus.get("minimum_old_bucket_depth_share_multiple"), "minimum_old_bucket_depth_share_multiple")
    if old_rank != int(consensus.get("old_bucket_required_rank", 1)) or lead < min_lead:
        return {"status": "BLOCKED_OLD_BUCKET_NOT_PRICE_CONSENSUS", "old_bucket_rank": old_rank, "old_bucket_price_lead": str(lead), "minimum_price_lead": str(min_lead), "bucket_stats": _json_stats(stats), **base}
    if old_stat["time_weighted_bid_depth_usdc_median"] < min_depth_usdc or old_stat["time_weighted_bid_depth_shares_median"] < min_depth_shares:
        return {"status": "BLOCKED_OLD_BUCKET_INSUFFICIENT_DEPTH_CONSENSUS", "old_depth_usdc": str(old_stat["time_weighted_bid_depth_usdc_median"]), "old_depth_shares": str(old_stat["time_weighted_bid_depth_shares_median"]), "required_depth_usdc": str(min_depth_usdc), "required_depth_shares": str(min_depth_shares), "bucket_stats": _json_stats(stats), **base}

    # Entry quality must be measured from a separately captured post-signal L2
    # snapshot. The old-bucket consensus window deliberately ends at t0; using a
    # post-t0 quote there would introduce look-ahead bias, while using only a
    # pre-t0 new-bucket quote would invent a speed result.
    if entry_snapshot is None:
        return {"status": "BLOCKED_MISSING_POST_SIGNAL_ENTRY_SNAPSHOT", **base}
    try:
        current = snapshot_quote(entry_snapshot, target)
    except ConsensusInputError as exc:
        return {"status": "BLOCKED_INVALID_POST_SIGNAL_ENTRY_SNAPSHOT", "message": str(exc), **base}
    if current["market_rule_id"] != rule_id or current["bucket_id"] != new_id:
        return {"status": "BLOCKED_POST_SIGNAL_ENTRY_SNAPSHOT_MISMATCH", **base}
    entry_delay_ns = current["received_monotonic_ns"] - t0
    max_entry_delay_ns = int(entry.get("maximum_entry_delay_seconds", entry.get("maximum_book_age_seconds", 0))) * 1_000_000_000
    if entry_delay_ns < 0 or entry_delay_ns > max_entry_delay_ns:
        return {"status": "BLOCKED_POST_SIGNAL_ENTRY_DELAY", "entry_delay_ns": entry_delay_ns, "maximum_entry_delay_ns": max_entry_delay_ns, **base}
    if current["min_order_size"] > target:
        return {"status": "BLOCKED_NEW_BUCKET_MIN_ORDER_SIZE", "min_order_size": str(current["min_order_size"]), "target_shares": str(target), **base}
    best_ask = current["asks"]["best_price"]
    if best_ask is None:
        return {"status": "BLOCKED_NEW_BUCKET_EMPTY_ASK", **base}

    profiles: list[dict[str, Any]] = []
    for profile in entry.get("pricing_profiles", []):
        if not isinstance(profile, dict):
            continue
        profile_id = str(profile.get("id") or "")
        if profile.get("limit_kind") == "best_ask":
            limit = best_ask
        elif profile.get("limit_kind") == "best_ask_plus_ticks":
            limit = price_with_ticks(best_ask, current["tick_size"], int(profile.get("ticks", 0)))
        else:
            profiles.append({"id": profile_id or None, "status": "BLOCKED_UNKNOWN_LIMIT_KIND"})
            continue
        max_price = D(profile.get("maximum_price"), "maximum_price")
        if limit > max_price:
            profiles.append({"id": profile_id, "status": "BLOCKED_PRICE_PROTECTION", "limit_price": str(limit), "maximum_price": str(max_price)})
            continue
        ask_walk = level_summary(current_raw_levels(current, "asks"), descending=False, target_shares=target, limit_price=limit)
        if ask_walk["target_walked_shares"] < target:
            profiles.append({"id": profile_id, "status": "BLOCKED_INSUFFICIENT_VISIBLE_ASK_DEPTH", "limit_price": str(limit), "visible_shares": str(ask_walk["target_walked_shares"]), "required_shares": str(target)})
            continue
        profiles.append({"id": profile_id, "status": "PAPER_INTENT_READY", "side": "BUY", "outcome": "YES", "order_type": "FAK_REPLAY_ASSUMPTION", "token_id": current["token_id"], "limit_price": str(limit), "requested_shares": str(target), "estimated_visible_fill_shares": str(ask_walk["target_walked_shares"]), "estimated_visible_vwap": str(ask_walk["target_vwap"]), "estimated_worst_ask": str(ask_walk["target_worst_price"]), "book_hash": current["book_hash"], "entry_delay_ns": entry_delay_ns, "safety": {"paper_only": True, "orders_submitted": 0, "credentials_loaded": False}})
    ready_profiles = [profile for profile in profiles if profile.get("status") == "PAPER_INTENT_READY"]
    return {"status": "PAPER_INTENT_READY" if ready_profiles else "BLOCKED_NEW_BUCKET_EXECUTION", "old_bucket_rank": old_rank, "old_bucket_price_lead": str(lead), "bucket_stats": _json_stats(stats), "new_bucket_profiles": profiles, **base}


def current_raw_levels(quote: dict[str, Any], side: str) -> tuple[dict[str, Decimal], ...]:
    """Quote summaries intentionally do not retain L2. This guard prevents use.

    Callers should attach immutable ``raw_bids`` / ``raw_asks`` from the same
    validated snapshot before current-book execution research. The separate
    helper makes that requirement explicit rather than silently using summary
    depth as fictitious individual orders.
    """
    raw_levels = quote.get(f"raw_{side}")
    if not isinstance(raw_levels, tuple):
        raise ConsensusInputError("current_quote_raw_l2_required")
    return raw_levels


def _json_stats(stats: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for bucket_id, value in stats.items():
        output[bucket_id] = {key: (str(item) if isinstance(item, Decimal) else item) for key, item in value.items()}
    return output
