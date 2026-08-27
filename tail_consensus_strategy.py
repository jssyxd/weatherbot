"""Fail-closed tail-end consensus strategy for YES outcome tokens.

The module is intentionally transport-agnostic: callers supply fresh local CLOB
snapshots from the public market stream.  It never reads a wallet, signs data,
or submits, cancels, or amends a real order.  Its outputs are auditable paper
intents only.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as local_time, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from edge_engine import local_market_date, observed_temperature_native
from local_order_book import LocalBookSnapshot


DECIMAL_ZERO = Decimal("0")
SIZE_QUANTUM = Decimal("0.01")


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _decimal(value: Any, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"invalid_{field}") from exc
    if not parsed.is_finite() or parsed < DECIMAL_ZERO:
        raise ValueError(f"invalid_{field}")
    return parsed


def _time(value: Any, field: str) -> local_time:
    try:
        parsed = local_time.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"invalid_{field}") from exc
    if parsed.tzinfo is not None or parsed.second != 0 or parsed.microsecond != 0:
        raise ValueError(f"invalid_{field}")
    return parsed


@dataclass(frozen=True)
class TailConsensusConfig:
    enabled: bool
    high_start: local_time
    high_end: local_time
    low_start: local_time
    low_end: local_time
    stability_seconds: int
    stable_min_price: Decimal
    entry_min_price: Decimal
    entry_max_price: Decimal
    target_shares: Decimal
    max_open_positions: int
    market_alert_bid: Decimal
    rotation_multiplier: Decimal
    max_rotations: int
    price_mode: str
    max_book_age_seconds: float

    @classmethod
    def from_mapping(cls, config: dict[str, Any]) -> "TailConsensusConfig":
        high_start = _time(config.get("tail_consensus_high_start_local", "12:00"), "tail_consensus_high_start_local")
        high_end = _time(config.get("tail_consensus_high_end_local", "17:00"), "tail_consensus_high_end_local")
        low_start = _time(config.get("tail_consensus_low_start_local", "01:00"), "tail_consensus_low_start_local")
        low_end = _time(config.get("tail_consensus_low_end_local", "05:00"), "tail_consensus_low_end_local")
        if high_start >= high_end or low_start >= low_end:
            raise ValueError("tail_consensus_tail_window_must_increase")
        stability = int(config.get("tail_consensus_stability_seconds", 1800))
        if stability < 1800:
            raise ValueError("tail_consensus_stability_seconds_must_be_at_least_1800")
        stable_min = _decimal(config.get("tail_consensus_stable_min_price", "0.90"), "tail_consensus_stable_min_price")
        entry_min = _decimal(config.get("tail_consensus_entry_min_price", "0.92"), "tail_consensus_entry_min_price")
        entry_max = _decimal(config.get("tail_consensus_entry_max_price", "0.98"), "tail_consensus_entry_max_price")
        alert_bid = _decimal(config.get("tail_consensus_market_alert_bid", "0.85"), "tail_consensus_market_alert_bid")
        if not DECIMAL_ZERO < stable_min <= entry_min <= entry_max < Decimal("1"):
            raise ValueError("tail_consensus_price_thresholds_invalid")
        if not DECIMAL_ZERO < alert_bid <= entry_max:
            raise ValueError("tail_consensus_market_alert_bid_invalid")
        target_shares = _decimal(config.get("tail_consensus_target_shares", "5"), "tail_consensus_target_shares")
        if target_shares < Decimal("5"):
            raise ValueError("tail_consensus_target_shares_must_be_at_least_5")
        multiplier = _decimal(config.get("tail_consensus_rotation_multiplier", "3"), "tail_consensus_rotation_multiplier")
        if multiplier != Decimal("3"):
            raise ValueError("tail_consensus_rotation_multiplier_must_be_3")
        max_positions = int(config.get("tail_consensus_max_open_positions", 10))
        max_rotations = int(config.get("tail_consensus_max_rotations", 1))
        if max_positions < 1 or max_rotations != 1:
            raise ValueError("tail_consensus_position_or_rotation_limit_invalid")
        mode = str(config.get("tail_consensus_price_mode", "best_ask_plus_one_tick")).strip()
        if mode not in {"best_ask", "best_ask_plus_one_tick"}:
            raise ValueError("tail_consensus_price_mode_invalid")
        max_book_age = float(config.get("local_book_max_age_seconds", 3))
        if max_book_age <= 0:
            raise ValueError("local_book_max_age_seconds_must_be_positive")
        return cls(
            enabled=bool(config.get("tail_consensus_enabled", True)), high_start=high_start, high_end=high_end,
            low_start=low_start, low_end=low_end, stability_seconds=stability, stable_min_price=stable_min,
            entry_min_price=entry_min, entry_max_price=entry_max, target_shares=target_shares,
            max_open_positions=max_positions, market_alert_bid=alert_bid, rotation_multiplier=multiplier,
            max_rotations=max_rotations, price_mode=mode, max_book_age_seconds=max_book_age,
        )


def position_key(city_id: str, market_local_date: str, direction: str) -> str:
    return f"{city_id}|{market_local_date}|{direction}"


def stability_key(city_id: str, market_local_date: str, direction: str, token_id: str) -> str:
    return f"{position_key(city_id, market_local_date, direction)}|{token_id}"


def all_yes_token_ids(rules: Iterable[dict[str, Any]]) -> list[str]:
    """Return canonical, de-duplicated YES token IDs for public-stream subscription."""
    return list(dict.fromkeys(
        str(bucket.get("yes_token_id"))
        for rule in rules
        for bucket in rule.get("buckets", [])
        if isinstance(bucket, dict) and str(bucket.get("yes_token_id") or "")
    ))


def _is_in_window(now: datetime, city: dict[str, Any], direction: str, config: TailConsensusConfig) -> bool:
    local_now = now.astimezone(ZoneInfo(str(city["timezone"]))).time().replace(tzinfo=None)
    start, end = (config.high_start, config.high_end) if direction == "high" else (config.low_start, config.low_end)
    return start <= local_now <= end


def _book_is_usable(book: LocalBookSnapshot | None, config: TailConsensusConfig, now: datetime) -> bool:
    return bool(
        book and book.is_fresh(config.max_book_age_seconds, now=now.timestamp())
        and book.ready and book.tick_size and book.tick_size > DECIMAL_ZERO
        and book.min_order_size and book.min_order_size > DECIMAL_ZERO
    )


def _fill_buy(book: LocalBookSnapshot, target_shares: Decimal, config: TailConsensusConfig) -> dict[str, Any]:
    """Build a bounded, full-size paper FAK BUY_YES intent from ask depth."""
    if book.best_ask is None:
        return {"status": "paper_buy_yes_rejected_empty_ask", "side": "BUY_YES"}
    if not config.entry_min_price <= book.best_ask <= config.entry_max_price:
        return {
            "status": "paper_buy_yes_rejected_best_ask_outside_gate", "side": "BUY_YES",
            "best_ask": str(book.best_ask), "entry_price_min": str(config.entry_min_price),
            "entry_price_max": str(config.entry_max_price),
        }
    limit = book.best_ask
    if config.price_mode == "best_ask_plus_one_tick":
        limit = min(book.best_ask + (book.tick_size or DECIMAL_ZERO), config.entry_max_price)
    fills: list[dict[str, str]] = []
    filled = DECIMAL_ZERO
    principal = DECIMAL_ZERO
    for level in sorted(book.asks, key=lambda item: item["price"]):
        price, size = level["price"], level["size"]
        if price > limit or size <= DECIMAL_ZERO:
            break
        quantity = min(size, target_shares - filled).quantize(SIZE_QUANTUM, rounding=ROUND_DOWN)
        if quantity <= DECIMAL_ZERO:
            continue
        filled += quantity
        principal += quantity * price
        fills.append({"price": str(price), "shares": str(quantity)})
        if filled >= target_shares:
            break
    minimum = book.min_order_size or DECIMAL_ZERO
    if filled < target_shares or filled < minimum:
        return {
            "status": "paper_buy_yes_rejected_insufficient_ask_depth", "side": "BUY_YES",
            "best_ask": str(book.best_ask), "limit_price": str(limit), "required_shares": str(target_shares),
            "available_shares": str(filled), "min_order_size": str(minimum),
        }
    return {
        "status": "paper_buy_yes_estimate", "side": "BUY_YES", "order_type": "FAK", "token_id": book.token_id,
        "execution_source": "websocket_local", "best_ask": str(book.best_ask), "limit_price": str(limit),
        "filled_shares": str(filled), "average_price": str(principal / filled), "principal_usdc": str(principal),
        "book_hash": book.book_hash, "book_timestamp": book.exchange_timestamp, "book_version": book.version,
        "fills": fills,
    }


def _fill_sell(book: LocalBookSnapshot, target_shares: Decimal) -> dict[str, Any]:
    """Build a full-size paper FAK SELL_YES intent from executable bid depth."""
    if book.best_bid is None:
        return {"status": "paper_sell_yes_rejected_empty_bid", "side": "SELL_YES"}
    fills: list[dict[str, str]] = []
    filled = DECIMAL_ZERO
    proceeds = DECIMAL_ZERO
    for level in sorted(book.bids, key=lambda item: item["price"], reverse=True):
        price, size = level["price"], level["size"]
        if price <= DECIMAL_ZERO or size <= DECIMAL_ZERO:
            continue
        quantity = min(size, target_shares - filled).quantize(SIZE_QUANTUM, rounding=ROUND_DOWN)
        if quantity <= DECIMAL_ZERO:
            continue
        filled += quantity
        proceeds += quantity * price
        fills.append({"price": str(price), "shares": str(quantity)})
        if filled >= target_shares:
            break
    minimum = book.min_order_size or DECIMAL_ZERO
    if filled < target_shares or filled < minimum:
        return {
            "status": "paper_sell_yes_rejected_insufficient_bid_depth", "side": "SELL_YES",
            "best_bid": str(book.best_bid), "required_shares": str(target_shares),
            "available_shares": str(filled), "min_order_size": str(minimum),
        }
    return {
        "status": "paper_sell_yes_estimate", "side": "SELL_YES", "order_type": "FAK", "token_id": book.token_id,
        "execution_source": "websocket_local", "best_bid": str(book.best_bid), "limit_price": str(book.best_bid),
        "filled_shares": str(filled), "average_price": str(proceeds / filled), "gross_proceeds_usdc": str(proceeds),
        "book_hash": book.book_hash, "book_timestamp": book.exchange_timestamp, "book_version": book.version,
        "fills": fills,
    }


def _bucket_is_still_possible(state: dict[str, Any], city: dict[str, Any], local_date: str, direction: str, bucket: dict[str, Any]) -> bool:
    extrema = state.get("daily_extrema", {}).get(f"{city['city_id']}|{local_date}")
    if not isinstance(extrema, dict):
        return False
    if direction == "high":
        hi = bucket.get("hi")
        return hi is None or float(extrema.get("high", float("inf"))) < float(hi)
    lo = bucket.get("lo")
    return lo is None or float(extrema.get("low", -float("inf"))) >= float(lo)


def _eligible_buckets(
    state: dict[str, Any], config: TailConsensusConfig, city: dict[str, Any], rule: dict[str, Any],
    books: dict[str, LocalBookSnapshot], now: datetime, *, require_tail_window: bool, exclude_token_id: str | None = None,
) -> tuple[list[tuple[dict[str, Any], LocalBookSnapshot]], list[dict[str, Any]]]:
    local_date = str(rule.get("market_local_date") or "")
    direction = str(rule.get("direction") or "")
    rejections: list[dict[str, Any]] = []
    if require_tail_window and not _is_in_window(now, city, direction, config):
        return [], [{"reason": "outside_tail_time_window"}]
    candidates: list[tuple[dict[str, Any], LocalBookSnapshot]] = []
    stability = state.setdefault("tail_consensus", {})
    for raw_bucket in rule.get("buckets", []):
        if not isinstance(raw_bucket, dict):
            continue
        bucket = dict(raw_bucket)
        token = str(bucket.get("yes_token_id") or "")
        if not token:
            rejections.append({"bucket": bucket, "reason": "missing_yes_token"})
            continue
        if token == exclude_token_id:
            continue
        book = books.get(token)
        key = stability_key(city["city_id"], local_date, direction, token)
        if not _book_is_usable(book, config, now) or book is None or book.best_ask is None:
            stability.pop(key, None)
            rejections.append({"bucket": bucket, "reason": "stale_or_missing_local_book"})
            continue
        if book.best_ask < config.stable_min_price:
            stability.pop(key, None)
            rejections.append({"bucket": bucket, "reason": "consensus_below_stability_floor", "best_ask": str(book.best_ask)})
            continue
        record = stability.get(key)
        since = _parse_utc(record.get("above_threshold_since_utc")) if isinstance(record, dict) else None
        if since is None or now < since:
            since = now
        stability[key] = {"above_threshold_since_utc": _utc_iso(since), "last_seen_utc": _utc_iso(now), "last_best_ask": str(book.best_ask)}
        stable_seconds = (now - since).total_seconds()
        if stable_seconds < config.stability_seconds:
            rejections.append({"bucket": bucket, "reason": "consensus_not_stable_long_enough", "stable_seconds": round(stable_seconds, 3)})
            continue
        if not config.entry_min_price <= book.best_ask <= config.entry_max_price:
            rejections.append({"bucket": bucket, "reason": "best_ask_outside_entry_gate", "best_ask": str(book.best_ask)})
            continue
        if book.best_bid is None or book.best_bid < config.market_alert_bid:
            rejections.append({"bucket": bucket, "reason": "best_bid_below_protection_floor", "best_bid": str(book.best_bid) if book.best_bid is not None else None})
            continue
        if not _bucket_is_still_possible(state, city, local_date, direction, bucket):
            rejections.append({"bucket": bucket, "reason": "bucket_already_temperature_invalid"})
            continue
        candidates.append((bucket, book))
    return candidates, rejections


def evaluate_tail_entries(
    state: dict[str, Any], config: TailConsensusConfig, cities: dict[str, dict[str, Any]],
    rules: Iterable[dict[str, Any]], books: dict[str, LocalBookSnapshot], now: datetime,
) -> list[dict[str, Any]]:
    """Update stability state and create at most one paper BUY_YES per city/date/direction."""
    if not config.enabled:
        return []
    signals: list[dict[str, Any]] = []
    positions = state.setdefault("tail_positions", {})
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("enabled", True) is not True:
            continue
        city = cities.get(str(rule.get("icao") or ""))
        direction = str(rule.get("direction") or "")
        local_date = str(rule.get("market_local_date") or "")
        if city is None or direction not in {"high", "low"} or local_date != local_market_date(now, city):
            continue
        key = position_key(city["city_id"], local_date, direction)
        if key in positions:
            continue
        candidates, rejections = _eligible_buckets(state, config, city, rule, books, now, require_tail_window=True)
        if len(candidates) != 1:
            if len(candidates) > 1:
                signals.append({
                    "signal_type": "no_signal", "reason": "multiple_executable_consensus_buckets", "city_id": city["city_id"],
                    "icao": city["icao"], "market_local_date": local_date, "direction": direction,
                    "candidate_yes_token_ids": [book.token_id for _, book in candidates],
                })
            elif rejections:
                signals.append({
                    "signal_type": "no_signal", "reason": rejections[0]["reason"], "city_id": city["city_id"],
                    "icao": city["icao"], "market_local_date": local_date, "direction": direction,
                    "details": rejections[0],
                })
            continue
        if len(positions) >= config.max_open_positions:
            signals.append({"signal_type": "no_signal", "reason": "max_open_positions_reached", "city_id": city["city_id"], "market_local_date": local_date, "direction": direction})
            continue
        bucket, book = candidates[0]
        execution = _fill_buy(book, config.target_shares, config)
        if execution["status"] != "paper_buy_yes_estimate":
            signals.append({"signal_type": "no_signal", "reason": execution["status"], "city_id": city["city_id"], "market_local_date": local_date, "direction": direction, "bucket": bucket, "execution": execution})
            continue
        positions[key] = {
            "position_key": key, "city_id": city["city_id"], "icao": city["icao"], "market_local_date": local_date,
            "direction": direction, "token_id": book.token_id, "bucket": bucket, "shares": execution["filled_shares"],
            "base_shares": execution["filled_shares"], "entry_average_price": execution["average_price"],
            "entered_at_utc": _utc_iso(now), "rotation_count": 0, "pending_temperature_break": None,
            "market_alert_active": False,
        }
        signals.append({
            "signal_type": "tail_yes_entry", "candidate_status": "single_stable_executable_tail_consensus", "city_id": city["city_id"],
            "icao": city["icao"], "market_local_date": local_date, "direction": direction, "market_unit": city["market_unit"],
            "bucket": bucket, "execution": execution, "disclaimer": "Paper-only YES intent; no wallet, signing, order submission, cancellation, or amendment occurs.",
        })
    return signals


def mark_temperature_breaks(
    state: dict[str, Any], config: TailConsensusConfig, event: dict[str, Any], city: dict[str, Any], now: datetime,
) -> list[dict[str, Any]]:
    """Update daily extrema and mark held YES buckets as pending exit only on fresh observed temperature evidence."""
    report_time = _parse_utc(event.get("report_time_utc"))
    fetched_at = _parse_utc(event.get("fetched_at_utc")) or now
    if event.get("is_correction") is True:
        return []
    temperature = observed_temperature_native(event, city)
    if report_time is None or temperature is None:
        return []
    value, precision = temperature
    # Integer-C METAR body temperatures map to 1.8°F increments and cannot
    # safely establish a Fahrenheit contract-boundary break without RMK tenths.
    if city.get("market_unit") == "F" and precision != "metar_remark_tenths_c":
        return []
    local_date = local_market_date(report_time, city)
    day_key = f"{city['city_id']}|{local_date}"
    day = state.setdefault("daily_extrema", {}).get(day_key)
    if not isinstance(day, dict):
        return []
    old_high, old_low = float(day.get("high", value)), float(day.get("low", value))
    is_new_high, is_new_low = value > old_high, value < old_low
    if is_new_high:
        day["high"] = value
    if is_new_low:
        day["low"] = value
    if is_new_high or is_new_low:
        day["updated_at_utc"] = _utc_iso(fetched_at)
    signals: list[dict[str, Any]] = []
    for key, position in list(state.get("tail_positions", {}).items()):
        if not isinstance(position, dict) or position.get("city_id") != city["city_id"] or position.get("market_local_date") != local_date:
            continue
        direction = str(position.get("direction") or "")
        bucket = position.get("bucket") if isinstance(position.get("bucket"), dict) else {}
        invalid = False
        if direction == "high" and is_new_high and bucket.get("hi") is not None:
            invalid = value >= float(bucket["hi"])
        elif direction == "low" and is_new_low and bucket.get("lo") is not None:
            invalid = value < float(bucket["lo"])
        if not invalid or position.get("pending_temperature_break"):
            continue
        position["pending_temperature_break"] = {
            "event_id": event.get("event_id"), "report_time_utc": _utc_iso(report_time), "fetched_at_utc": _utc_iso(fetched_at),
            "temperature_native": round(value, 4), "temperature_precision": precision, "previous_high": old_high,
            "previous_low": old_low, "marked_at_utc": _utc_iso(now),
        }
        signals.append({
            "signal_type": "temperature_break_pending_exit", "reason": "held_yes_bucket_invalidated_by_fresh_observed_temperature",
            "city_id": city["city_id"], "icao": city["icao"], "market_local_date": local_date, "direction": direction,
            "position_key": key, "token_id": position.get("token_id"), "bucket": bucket,
            "temperature_evidence": position["pending_temperature_break"],
        })
    return signals


def _find_rule(rules: Iterable[dict[str, Any]], position: dict[str, Any]) -> dict[str, Any] | None:
    for rule in rules:
        if isinstance(rule, dict) and rule.get("city_id") == position.get("city_id") and rule.get("market_local_date") == position.get("market_local_date") and rule.get("direction") == position.get("direction"):
            return rule
    return None


def monitor_tail_positions(
    state: dict[str, Any], config: TailConsensusConfig, cities: dict[str, dict[str, Any]],
    rules: Iterable[dict[str, Any]], books: dict[str, LocalBookSnapshot], now: datetime,
) -> list[dict[str, Any]]:
    """Issue non-trading market alerts and execute paper exits/one permitted paper rotation after temperature proof."""
    if not config.enabled:
        return []
    positions = state.setdefault("tail_positions", {})
    signals: list[dict[str, Any]] = []
    rule_list = list(rules)
    for key, position in list(positions.items()):
        if not isinstance(position, dict):
            continue
        book = books.get(str(position.get("token_id") or ""))
        if _book_is_usable(book, config, now) and book is not None and book.best_bid is not None:
            if book.best_bid < config.market_alert_bid and not position.get("market_alert_active", False):
                position["market_alert_active"] = True
                position["market_alerted_at_utc"] = _utc_iso(now)
                signals.append({
                    "signal_type": "market_reversal_alert", "reason": "held_yes_best_bid_below_85_cents",
                    "position_key": key, "city_id": position.get("city_id"), "market_local_date": position.get("market_local_date"),
                    "direction": position.get("direction"), "token_id": position.get("token_id"), "best_bid": str(book.best_bid),
                    "action": "alert_only_no_exit_or_rotation_without_temperature_evidence",
                })
            elif book.best_bid >= config.market_alert_bid:
                position["market_alert_active"] = False
        if not position.get("pending_temperature_break"):
            continue
        if not _book_is_usable(book, config, now) or book is None:
            signals.append({"signal_type": "no_signal", "reason": "exit_unavailable_stale_or_missing_local_book", "position_key": key})
            continue
        shares = _decimal(position.get("shares"), "position_shares")
        exit_execution = _fill_sell(book, shares)
        if exit_execution["status"] != "paper_sell_yes_estimate":
            signals.append({"signal_type": "no_signal", "reason": "exit_unavailable", "position_key": key, "exit_execution": exit_execution})
            continue
        city = cities.get(str(position.get("icao") or ""))
        rule = _find_rule(rule_list, position)
        rotation_count = int(position.get("rotation_count", 0))
        evidence = position.get("pending_temperature_break")
        del positions[key]
        if rotation_count >= config.max_rotations:
            signals.append({
                "signal_type": "tail_yes_exit", "reason": "rotation_limit_reached", "position_key": key,
                "exit_execution": exit_execution, "temperature_evidence": evidence,
            })
            continue
        if city is None or rule is None:
            signals.append({"signal_type": "tail_yes_exit", "reason": "no_current_rule_for_rotation", "position_key": key, "exit_execution": exit_execution, "temperature_evidence": evidence})
            continue
        candidates, rejections = _eligible_buckets(
            state, config, city, rule, books, now, require_tail_window=False, exclude_token_id=str(position.get("token_id") or ""),
        )
        if len(candidates) != 1:
            reason = "multiple_executable_replacement_buckets" if len(candidates) > 1 else (rejections[0]["reason"] if rejections else "no_executable_replacement_bucket")
            signals.append({"signal_type": "tail_yes_exit", "reason": reason, "position_key": key, "exit_execution": exit_execution, "temperature_evidence": evidence})
            continue
        bucket, replacement_book = candidates[0]
        base_shares = _decimal(position.get("base_shares", shares), "base_shares")
        replacement_shares = base_shares * config.rotation_multiplier
        entry_execution = _fill_buy(replacement_book, replacement_shares, config)
        if entry_execution["status"] != "paper_buy_yes_estimate":
            signals.append({"signal_type": "tail_yes_exit", "reason": "replacement_buy_unavailable", "position_key": key, "exit_execution": exit_execution, "entry_execution": entry_execution, "temperature_evidence": evidence})
            continue
        positions[key] = {
            **position, "token_id": replacement_book.token_id, "bucket": bucket, "shares": entry_execution["filled_shares"],
            "entry_average_price": entry_execution["average_price"], "entered_at_utc": _utc_iso(now),
            "rotation_count": rotation_count + 1, "pending_temperature_break": None, "market_alert_active": False,
            "rotated_from_token_id": position.get("token_id"), "last_rotation_at_utc": _utc_iso(now),
        }
        signals.append({
            "signal_type": "tail_yes_rotation", "reason": "temperature_evidence_confirmed_one_time_rotation",
            "position_key": key, "city_id": city["city_id"], "market_local_date": position["market_local_date"],
            "direction": position["direction"], "old_token_id": position.get("token_id"), "new_bucket": bucket,
            "exit_execution": exit_execution, "entry_execution": entry_execution, "temperature_evidence": evidence,
            "disclaimer": "Paper-only sell-old-YES then buy-new-YES rotation; no live order is submitted.",
        })
    return signals
