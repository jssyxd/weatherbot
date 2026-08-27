"""Tree5 deterministic TAF-entry and invalid-position exit policy.

This module has no wallet, private key, CLOB credential, signing, submission, or
cancellation HTTP code.  It only produces auditable actions from explicit state,
TAF/METAR observations, and executable order-book snapshots.  A future live
executor must reconcile positions and confirm every action before mutating order
state; otherwise the default path remains observe-only and fails closed.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any
from zoneinfo import ZoneInfo

from edge_engine import celsius_to_native, local_market_date, parse_utc

TAF_EXTREME_RE = re.compile(r"\b(TX|TN)(M?)(\d{2})/(\d{2})(\d{2})Z\b")
ZERO = Decimal("0")
DEFAULT_RETRY_SECONDS = (0, 5, 20, 60, 120)
DEFAULT_EXIT_SLIPPAGE = (Decimal("0.10"), Decimal("0.20"), Decimal("0.35"), Decimal("0.60"), Decimal("0.90"))


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except Exception:
        return None
    return parsed if parsed.is_finite() else None


def _book_value(book: Any, name: str) -> Any:
    if isinstance(book, dict):
        return book.get(name)
    return getattr(book, name, None)


def _snapshot_summary(book: Any) -> dict[str, Any]:
    if book is None:
        return {"available": False}
    result = {"available": True}
    for key in ("token_id", "timestamp", "book_hash", "source", "best_ask", "best_bid", "tick_size", "min_order_size"):
        value = _book_value(book, key)
        result[key] = str(value) if isinstance(value, Decimal) else value
    return result


def ensure_tree5_state(state: dict[str, Any]) -> dict[str, Any]:
    """Initialise JSON-serialisable Tree5 state without assuming any fills."""
    tree = state.setdefault("tree5", {})
    if not isinstance(tree, dict):
        raise ValueError("tree5 状态必须为对象")
    for name, default in (
        ("taf_fetches", {}),
        ("taf_forecasts", {}),
        ("entries", {}),
        ("confirmed_positions", {}),
        ("exit_chases", {}),
        ("temperature_history", {}),
        ("last_closure_check_utc", None),
    ):
        tree.setdefault(name, default)
        if name != "last_closure_check_utc" and not isinstance(tree[name], dict):
            raise ValueError(f"tree5.{name} 状态必须为对象")
    return tree


def _month_shift(year: int, month: int, offset: int) -> tuple[int, int]:
    absolute = year * 12 + month - 1 + offset
    return absolute // 12, absolute % 12 + 1


def _resolve_taf_day_hour(reference_utc: datetime, day: int, hour: int) -> datetime | None:
    """Resolve a TAF DDHHZ group against issuance time without calendar guessing."""
    choices: list[datetime] = []
    for shift in (-1, 0, 1):
        year, month = _month_shift(reference_utc.year, reference_utc.month, shift)
        try:
            choices.append(datetime(year, month, day, hour, tzinfo=timezone.utc))
        except ValueError:
            continue
    if not choices:
        return None
    return min(choices, key=lambda candidate: abs((candidate - reference_utc).total_seconds()))


def parse_taf_extremes_for_local_day(raw_taf: Any, issued: Any, city: dict[str, Any], market_local_date: str) -> dict[str, dict[str, Any]]:
    """Extract TX/TN groups whose forecast time belongs to one IANA market day.

    TAF extrema are Celsius.  Returned values are converted to the contract's
    native C/F unit and include the forecast-time provenance required for audit.
    """
    raw = str(raw_taf or "").upper()
    issued_utc = parse_utc(issued)
    if not raw.startswith("TAF") or issued_utc is None:
        return {}
    parsed: dict[str, dict[str, Any]] = {}
    for kind, minus, digits, day, hour in TAF_EXTREME_RE.findall(raw):
        forecast_at = _resolve_taf_day_hour(issued_utc, int(day), int(hour))
        if forecast_at is None or local_market_date(forecast_at, city) != market_local_date:
            continue
        celsius = float(int(digits))
        if minus == "M":
            celsius *= -1.0
        direction = "high" if kind == "TX" else "low"
        value_native = celsius_to_native(celsius, city["market_unit"])
        candidate = {
            "direction": direction,
            "value_c": celsius,
            "value_native": value_native,
            "market_unit": city["market_unit"],
            "forecast_time_utc": iso_utc(forecast_at),
            "issued_utc": iso_utc(issued_utc),
            "raw_group": f"{kind}{minus}{digits}/{day}{hour}Z",
        }
        # One issued TAF should have one TX and one TN for a local day.  If a
        # malformed bulletin repeats a group, retain the later valid time.
        previous = parsed.get(direction)
        if previous is None or candidate["forecast_time_utc"] >= previous["forecast_time_utc"]:
            parsed[direction] = candidate
    return parsed


def tree5_day_key(city: dict[str, Any], market_local_date: str) -> str:
    return f"{city['city_id']}|{market_local_date}"


def forecast_key(city: dict[str, Any], market_local_date: str, direction: str) -> str:
    return f"{tree5_day_key(city, market_local_date)}|{direction}"


def entry_key(market_rule_id: Any, bucket_id: Any) -> str:
    return f"{market_rule_id}|{bucket_id}"


def bucket_contains(bucket: dict[str, Any], value: float) -> bool:
    lo, hi = bucket.get("lo"), bucket.get("hi")
    return (lo is None or value >= float(lo)) and (hi is None or value < float(hi))


def select_forecast_bucket(rules: list[dict[str, Any]], city: dict[str, Any], market_local_date: str, direction: str, value_native: float) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Find the unique tradable bucket containing a forecast temperature."""
    for rule in rules:
        if not (
            rule.get("enabled", True) is True
            and rule.get("city_id") == city["city_id"]
            and rule.get("market_local_date") == market_local_date
            and rule.get("direction") == direction
            and rule.get("market_unit") == city["market_unit"]
        ):
            continue
        matches = [bucket for bucket in rule.get("buckets", []) if bucket_contains(bucket, value_native)]
        if len(matches) == 1:
            selected = dict(matches[0])
            selected["market_rule_id"] = rule.get("market_rule_id")
            return rule, selected
    return None


def _quantize_down(value: Decimal, tick_size: Decimal) -> Decimal | None:
    if value <= ZERO or tick_size <= ZERO:
        return None
    return (value / tick_size).to_integral_value(rounding=ROUND_DOWN) * tick_size


def discounted_limit(reference_price: Any, tick_size: Any, discount: Any) -> Decimal | None:
    reference, tick, pct = _decimal(reference_price), _decimal(tick_size), _decimal(discount)
    if reference is None or tick is None or pct is None or not ZERO <= pct < Decimal("1"):
        return None
    return _quantize_down(reference * (Decimal("1") - pct), tick)


def _config_decimal(config: dict[str, Any], name: str, default: Decimal) -> Decimal:
    value = _decimal(config.get(name, default))
    return default if value is None else value


def due_taf_cities(state: dict[str, Any], cities: dict[str, dict[str, Any]], now_utc: datetime, config: dict[str, Any]) -> list[dict[str, Any]]:
    """Return cities whose first post-01:00 TAF fetch (or bounded retry) is due."""
    tree = ensure_tree5_state(state)
    fetch_hour = int(config.get("tree5_taf_fetch_local_hour", 1))
    retry_seconds = int(config.get("tree5_taf_retry_seconds", 900))
    due: list[dict[str, Any]] = []
    for city in cities.values():
        local_now = now_utc.astimezone(ZoneInfo(city["timezone"]))
        if local_now.hour < fetch_hour:
            continue
        market_date = local_now.date().isoformat()
        key = tree5_day_key(city, market_date)
        prior = tree["taf_fetches"].get(key, {})
        if prior.get("status") == "complete" and prior.get("market_local_date") == market_date:
            continue
        last_attempt = parse_utc(prior.get("last_attempt_utc"))
        if last_attempt is not None and (now_utc - last_attempt).total_seconds() < retry_seconds:
            continue
        due.append(city)
    return due


def record_taf_reports(state: dict[str, Any], reports: list[dict[str, Any]], cities: dict[str, dict[str, Any]], now_utc: datetime, source_endpoint: str) -> list[dict[str, Any]]:
    """Persist daily TAF extrema. Missing TX/TN is explicit and does not trade."""
    tree = ensure_tree5_state(state)
    by_icao = {str(report.get("icao", "")).upper(): report for report in reports if isinstance(report, dict)}
    actions: list[dict[str, Any]] = []
    for city in cities.values():
        local_date = now_utc.astimezone(ZoneInfo(city["timezone"])).date().isoformat()
        day_key = tree5_day_key(city, local_date)
        report = by_icao.get(city["icao"])
        fetch_record = {
            "city_id": city["city_id"], "icao": city["icao"], "market_local_date": local_date,
            "last_attempt_utc": iso_utc(now_utc), "source_endpoint": source_endpoint,
        }
        if report is None:
            tree["taf_fetches"][day_key] = {**fetch_record, "status": "failed_missing_station_taf"}
            actions.append({"action_type": "tree5_taf_fetch", "status": "failed_missing_station_taf", **fetch_record})
            continue
        parsed = parse_taf_extremes_for_local_day(report.get("raw_text"), report.get("issued"), city, local_date)
        missing = sorted({"high", "low"} - set(parsed))
        if missing:
            tree["taf_fetches"][day_key] = {**fetch_record, "status": "failed_missing_local_day_extreme", "missing_directions": missing,
                                             "taf_issued_utc": report.get("issued")}
            actions.append({"action_type": "tree5_taf_fetch", "status": "failed_missing_local_day_extreme", "missing_directions": missing, **fetch_record})
            continue
        tree["taf_fetches"][day_key] = {**fetch_record, "status": "complete", "taf_issued_utc": report.get("issued")}
        for direction, detail in parsed.items():
            key = forecast_key(city, local_date, direction)
            tree["taf_forecasts"][key] = {
                **detail, "city_id": city["city_id"], "icao": city["icao"], "market_local_date": local_date,
                "raw_taf": str(report.get("raw_text") or ""), "source_endpoint": source_endpoint,
                "fetched_at_utc": iso_utc(now_utc),
            }
            actions.append({"action_type": "tree5_taf_forecast_recorded", "status": "recorded", "forecast_key": key,
                            "city_id": city["city_id"], "market_local_date": local_date, **detail})
    return actions


def _current_extreme(state: dict[str, Any], city: dict[str, Any], market_local_date: str, direction: str) -> float | None:
    day = state.get("daily_extrema", {}).get(tree5_day_key(city, market_local_date))
    if not isinstance(day, dict):
        return None
    value = day.get(direction)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def bucket_proven_impossible(bucket: dict[str, Any], direction: str, observed_extreme: float | None) -> bool:
    """Only an interval-boundary cross is a 100%-wrong trigger.

    A raw observation merely exceeding a TAF point forecast is not enough for a
    range contract.  For a high bucket [lo, hi), the running high must be >= hi;
    for a low bucket [lo, hi), the running low must be < lo.
    """
    if observed_extreme is None:
        return False
    if direction == "high":
        hi = bucket.get("hi")
        return hi is not None and observed_extreme >= float(hi)
    lo = bucket.get("lo")
    return lo is not None and observed_extreme < float(lo)


def plan_taf_entries(state: dict[str, Any], cities: dict[str, dict[str, Any]], rules: list[dict[str, Any]], books_by_token: dict[str, Any], now_utc: datetime, config: dict[str, Any]) -> list[dict[str, Any]]:
    """Create one five-share GTC BUY-YES intent per parsed daily TAF bucket.

    The limit is the contemporaneous executable best ask discounted by 5% by
    default.  It is intentionally a GTC order: FAK and GTC cannot be combined.
    """
    tree = ensure_tree5_state(state)
    actions: list[dict[str, Any]] = []
    target = _config_decimal(config, "target_order_shares", Decimal("5"))
    entry_discount = _config_decimal(config, "tree5_entry_price_discount", Decimal("0.05"))
    min_price = _config_decimal(config, "min_execution_price", Decimal("0.05"))
    max_price = _config_decimal(config, "max_execution_price", Decimal("0.98"))
    if target != Decimal("5"):
        raise ValueError("Tree5 只允许固定 5 shares")
    for forecast in list(tree["taf_forecasts"].values()):
        city = next((candidate for candidate in cities.values() if candidate["city_id"] == forecast.get("city_id")), None)
        if city is None:
            continue
        direction, local_date = str(forecast.get("direction")), str(forecast.get("market_local_date"))
        selected = select_forecast_bucket(rules, city, local_date, direction, float(forecast["value_native"]))
        if selected is None:
            actions.append({"action_type": "tree5_taf_entry", "status": "blocked_no_unique_forecast_bucket", "city_id": city["city_id"],
                            "market_local_date": local_date, "direction": direction, "forecast_native": forecast["value_native"]})
            continue
        rule, bucket = selected
        key = entry_key(bucket.get("market_rule_id"), bucket.get("bucket_id"))
        if key in tree["entries"]:
            continue
        current = _current_extreme(state, city, local_date, direction)
        if bucket_proven_impossible(bucket, direction, current):
            actions.append({"action_type": "tree5_taf_entry", "status": "blocked_bucket_already_impossible", "entry_key": key,
                            "city_id": city["city_id"], "market_local_date": local_date, "direction": direction,
                            "observed_extreme": current, "bucket": bucket})
            continue
        yes_token = str(bucket.get("yes_token_id") or "")
        book = books_by_token.get(yes_token)
        ask, tick, minimum = _decimal(_book_value(book, "best_ask")), _decimal(_book_value(book, "tick_size")), _decimal(_book_value(book, "min_order_size"))
        if not yes_token or ask is None or tick is None or minimum is None or target < minimum:
            actions.append({"action_type": "tree5_taf_entry", "status": "blocked_missing_executable_yes_ask", "entry_key": key,
                            "city_id": city["city_id"], "market_local_date": local_date, "direction": direction,
                            "bucket": bucket, "book": _snapshot_summary(book)})
            continue
        limit = discounted_limit(ask, tick, entry_discount)
        if limit is None or not min_price <= limit <= max_price:
            actions.append({"action_type": "tree5_taf_entry", "status": "blocked_entry_price_outside_gate", "entry_key": key,
                            "limit_price": str(limit) if limit is not None else None, "book": _snapshot_summary(book), "bucket": bucket})
            continue
        entry = {
            "entry_key": key, "status": "planned_gtc_entry", "city_id": city["city_id"], "icao": city["icao"],
            "market_local_date": local_date, "direction": direction, "market_rule_id": rule.get("market_rule_id"),
            "token_id": yes_token, "side": "BUY", "outcome": "YES", "order_type": "GTC", "requested_shares": str(target),
            "remaining_shares": str(target), "limit_price": str(limit), "entry_reference_best_ask": str(ask),
            "book": _snapshot_summary(book), "bucket": bucket, "forecast": forecast, "planned_at_utc": iso_utc(now_utc),
            "external_order_id": None, "confirmed_filled_shares": "0",
        }
        tree["entries"][key] = entry
        actions.append({"action_type": "tree5_submit_entry", "status": "planned_observe_only", **entry})
    return actions


def planned_entry_token_ids(state: dict[str, Any], cities: dict[str, dict[str, Any]], rules: list[dict[str, Any]]) -> set[str]:
    """Return only YES tokens needed to price unplanned daily TAF entries."""
    tree = ensure_tree5_state(state)
    tokens: set[str] = set()
    for forecast in tree["taf_forecasts"].values():
        if not isinstance(forecast, dict):
            continue
        city = next((candidate for candidate in cities.values() if candidate["city_id"] == forecast.get("city_id")), None)
        if city is None:
            continue
        selection = select_forecast_bucket(
            rules, city, str(forecast.get("market_local_date")), str(forecast.get("direction")), float(forecast.get("value_native")),
        )
        if selection is None:
            continue
        _rule, bucket = selection
        key = entry_key(bucket.get("market_rule_id"), bucket.get("bucket_id"))
        if key not in tree["entries"] and bucket.get("yes_token_id"):
            tokens.add(str(bucket["yes_token_id"]))
    return tokens


def record_temperature(state: dict[str, Any], city: dict[str, Any], report_time_utc: datetime, temperature_native: float, event_id: str | None) -> None:
    tree = ensure_tree5_state(state)
    local_date = local_market_date(report_time_utc, city)
    key = tree5_day_key(city, local_date)
    history = tree["temperature_history"].setdefault(key, [])
    history.append({"report_time_utc": iso_utc(report_time_utc), "temperature_native": round(float(temperature_native), 4), "event_id": event_id})
    # A compact history is enough for 13:00-17:00 / 01:00-05:00 trend checks.
    del history[:-24]


def _confirmed_position_shares(tree: dict[str, Any], token_id: str) -> Decimal:
    position = tree["confirmed_positions"].get(token_id, {})
    if not isinstance(position, dict):
        return ZERO
    shares = _decimal(position.get("shares"))
    return shares if shares is not None and shares > ZERO else ZERO


def start_exit_chase(state: dict[str, Any], entry: dict[str, Any], trigger: str, now_utc: datetime) -> dict[str, Any] | None:
    """Start a sell-FAK ladder only against a reconciled, non-zero YES position."""
    tree = ensure_tree5_state(state)
    key = str(entry["entry_key"])
    existing = tree["exit_chases"].get(key)
    if isinstance(existing, dict) and existing.get("status") in {"active", "awaiting_reconciliation"}:
        return None
    shares = _confirmed_position_shares(tree, str(entry["token_id"]))
    if shares <= ZERO:
        return None
    chase = {
        "entry_key": key, "status": "active", "trigger": trigger, "token_id": str(entry["token_id"]),
        "side": "SELL", "outcome": "YES", "remaining_shares": str(shares), "triggered_at_utc": iso_utc(now_utc),
        "attempt_index": 0, "attempts": [], "next_attempt_utc": iso_utc(now_utc),
    }
    tree["exit_chases"][key] = chase
    return chase


def invalidate_entries_from_observation(state: dict[str, Any], city: dict[str, Any], market_local_date: str, now_utc: datetime, observed_temperature: float | None = None) -> list[dict[str, Any]]:
    """Cancel invalid GTC entries and start FAK exit ladders for known positions."""
    tree = ensure_tree5_state(state)
    actions: list[dict[str, Any]] = []
    for entry in tree["entries"].values():
        if not isinstance(entry, dict) or entry.get("city_id") != city["city_id"] or entry.get("market_local_date") != market_local_date:
            continue
        if entry.get("invalidated_at_utc"):
            continue
        direction = str(entry.get("direction"))
        extreme = _current_extreme(state, city, market_local_date, direction)
        proof_value = observed_temperature if observed_temperature is not None else extreme
        if not bucket_proven_impossible(entry.get("bucket", {}), direction, proof_value):
            continue
        entry["invalidated_at_utc"] = iso_utc(now_utc)
        entry["invalidation_reason"] = "metar_bucket_interval_boundary_crossed"
        entry["status"] = "invalidated_by_metar"
        external_order_id = entry.get("external_order_id")
        if external_order_id:
            actions.append({"action_type": "tree5_cancel_entry_gtc", "status": "planned_observe_only", "entry_key": entry["entry_key"],
                            "external_order_id": external_order_id, "reason": entry["invalidation_reason"], "observed_extreme": proof_value})
        else:
            actions.append({"action_type": "tree5_cancel_entry_gtc", "status": "not_submitted_or_no_external_order", "entry_key": entry["entry_key"],
                            "reason": entry["invalidation_reason"], "observed_extreme": proof_value})
        chase = start_exit_chase(state, entry, "metar_bucket_proven_impossible", now_utc)
        if chase is not None:
            actions.append({"action_type": "tree5_exit_chase_started", "status": "pending_fak", **chase})
    return actions


def _retry_schedule(config: dict[str, Any]) -> tuple[tuple[int, ...], tuple[Decimal, ...]]:
    seconds = tuple(int(value) for value in config.get("tree5_exit_retry_seconds", DEFAULT_RETRY_SECONDS))
    slippage = tuple(_decimal(value) for value in config.get("tree5_exit_slippage", DEFAULT_EXIT_SLIPPAGE))
    if seconds != tuple(sorted(seconds)) or seconds[0] != 0 or len(seconds) != len(slippage) or any(value is None or value < ZERO or value >= Decimal("1") for value in slippage):
        raise ValueError("tree5 退出追价配置无效：秒数须以 0 开始递增，折价须为 [0,1) 且数量一致")
    return seconds, tuple(value for value in slippage if value is not None)


def plan_due_exit_faks(state: dict[str, Any], books_by_token: dict[str, Any], now_utc: datetime, config: dict[str, Any]) -> list[dict[str, Any]]:
    """Plan due FAK sells at 0/5/20/60/120 seconds after a trigger.

    FAK cancels unfilled residual automatically, so replacement FAKs do *not*
    cancel a prior exit order.  Before every retry a real executor must reconcile
    the position; this planner reads only ``confirmed_positions``.
    """
    tree = ensure_tree5_state(state)
    seconds, slippages = _retry_schedule(config)
    min_price = _config_decimal(config, "tree5_exit_min_price", Decimal("0.01"))
    actions: list[dict[str, Any]] = []
    for chase in tree["exit_chases"].values():
        if not isinstance(chase, dict) or chase.get("status") != "active":
            continue
        index = int(chase.get("attempt_index", 0))
        due_at = parse_utc(chase.get("next_attempt_utc"))
        if index >= len(seconds) or due_at is None or now_utc < due_at:
            continue
        token_id = str(chase.get("token_id") or "")
        shares = _confirmed_position_shares(tree, token_id)
        if shares <= ZERO:
            chase["status"] = "closed_no_confirmed_position"
            actions.append({"action_type": "tree5_exit_fak", "status": "skipped_no_confirmed_position", "entry_key": chase["entry_key"], "token_id": token_id})
            continue
        book = books_by_token.get(token_id)
        bid, tick, minimum = _decimal(_book_value(book, "best_bid")), _decimal(_book_value(book, "tick_size")), _decimal(_book_value(book, "min_order_size"))
        attempt = {"attempt_index": index, "scheduled_after_seconds": seconds[index], "at_utc": iso_utc(now_utc),
                   "slippage": str(slippages[index]), "book": _snapshot_summary(book)}
        if bid is None or tick is None or minimum is None or shares < minimum:
            attempt["status"] = "blocked_missing_executable_yes_bid"
            actions.append({"action_type": "tree5_exit_fak", "status": attempt["status"], "entry_key": chase["entry_key"],
                            "token_id": token_id, "remaining_shares": str(shares), **attempt})
        else:
            limit = discounted_limit(bid, tick, slippages[index])
            if limit is None or limit < min_price:
                attempt["status"] = "blocked_exit_price_below_floor"
                attempt["limit_price"] = str(limit) if limit is not None else None
                actions.append({"action_type": "tree5_exit_fak", "status": attempt["status"], "entry_key": chase["entry_key"], **attempt})
            else:
                attempt.update({"status": "planned_observe_only", "limit_price": str(limit), "requested_shares": str(shares)})
                actions.append({"action_type": "tree5_submit_exit_fak", "status": "planned_observe_only", "entry_key": chase["entry_key"],
                                "token_id": token_id, "side": "SELL", "outcome": "YES", "order_type": "FAK",
                                "limit_price": str(limit), "requested_shares": str(shares), **attempt})
        chase["attempts"].append(attempt)
        chase["attempt_index"] = index + 1
        triggered = parse_utc(chase.get("triggered_at_utc")) or now_utc
        if index + 1 < len(seconds):
            chase["next_attempt_utc"] = iso_utc(triggered + timedelta(seconds=seconds[index + 1]))
        else:
            chase["status"] = "awaiting_reconciliation"
            chase["next_attempt_utc"] = None
    return actions


def due_exit_token_ids(state: dict[str, Any], now_utc: datetime) -> set[str]:
    tree = ensure_tree5_state(state)
    due: set[str] = set()
    for chase in tree["exit_chases"].values():
        if not isinstance(chase, dict) or chase.get("status") != "active":
            continue
        due_at = parse_utc(chase.get("next_attempt_utc"))
        if due_at is not None and now_utc >= due_at and chase.get("token_id"):
            due.add(str(chase["token_id"]))
    return due


def _trend_supports_closure(history: list[dict[str, Any]], direction: str, min_move: float) -> bool:
    if len(history) < 3:
        return False
    recent = history[-3:]
    values = [float(item["temperature_native"]) for item in recent if item.get("temperature_native") is not None]
    if len(values) < 3:
        return False
    return values[-1] <= max(values[:-1]) - min_move if direction == "high" else values[-1] >= min(values[:-1]) + min_move


def evaluate_time_closure(state: dict[str, Any], cities: dict[str, dict[str, Any]], books_by_token: dict[str, Any], now_utc: datetime, config: dict[str, Any]) -> list[dict[str, Any]]:
    """Apply conservative high/low time-closure rules to existing TAF entries.

    This is probabilistic, not a settlement proof.  It requires all of: local
    timing window, a one-native-degree shortfall, a three-observation reversal,
    and executable-bid deterioration versus the entry reference.  It therefore
    cancels stale GTC intent only after weather *and* market evidence align.
    """
    tree = ensure_tree5_state(state)
    actions: list[dict[str, Any]] = []
    price_decline = _config_decimal(config, "tree5_closure_price_decline", Decimal("0.20"))
    shortfall = float(config.get("tree5_closure_shortfall_native", 1.0))
    trend_move = float(config.get("tree5_closure_trend_move_native", 0.5))
    windows = {"high": (int(config.get("tree5_high_closure_start_hour", 13)), int(config.get("tree5_high_closure_end_hour", 17))),
               "low": (int(config.get("tree5_low_closure_start_hour", 1)), int(config.get("tree5_low_closure_end_hour", 5)))}
    for entry in tree["entries"].values():
        if not isinstance(entry, dict) or entry.get("closure_at_utc") or entry.get("invalidated_at_utc"):
            continue
        city = next((value for value in cities.values() if value["city_id"] == entry.get("city_id")), None)
        if city is None:
            continue
        local_now = now_utc.astimezone(ZoneInfo(city["timezone"]))
        if local_now.date().isoformat() != entry.get("market_local_date"):
            continue
        direction = str(entry.get("direction"))
        start_hour, end_hour = windows.get(direction, (99, -1))
        if not start_hour <= local_now.hour <= end_hour:
            continue
        bucket = entry.get("bucket", {})
        extreme = _current_extreme(state, city, str(entry["market_local_date"]), direction)
        if extreme is None or bucket_proven_impossible(bucket, direction, extreme):
            continue
        lo, hi = bucket.get("lo"), bucket.get("hi")
        weather_shortfall = (lo is not None and extreme <= float(lo) - shortfall) if direction == "high" else (hi is not None and extreme >= float(hi) + shortfall)
        history = tree["temperature_history"].get(tree5_day_key(city, str(entry["market_local_date"])), [])
        trend = _trend_supports_closure(history if isinstance(history, list) else [], direction, trend_move)
        book = books_by_token.get(str(entry.get("token_id") or ""))
        current_bid = _decimal(_book_value(book, "best_bid"))
        entry_reference = _decimal(entry.get("entry_reference_best_ask"))
        consensus = current_bid is not None and entry_reference is not None and current_bid <= entry_reference * (Decimal("1") - price_decline)
        if not (weather_shortfall and trend and consensus):
            continue
        entry["closure_at_utc"] = iso_utc(now_utc)
        entry["closure_reason"] = "probabilistic_time_closure_weather_trend_and_market_decline"
        entry["status"] = "closure_exit_requested"
        external_order_id = entry.get("external_order_id")
        actions.append({"action_type": "tree5_time_closure", "status": "triggered", "entry_key": entry["entry_key"], "city_id": city["city_id"],
                        "direction": direction, "local_hour": local_now.hour, "observed_extreme": extreme, "book": _snapshot_summary(book),
                        "weather_shortfall": weather_shortfall, "trend_reversal": trend, "market_decline_confirmed": consensus})
        if external_order_id:
            actions.append({"action_type": "tree5_cancel_entry_gtc", "status": "planned_observe_only", "entry_key": entry["entry_key"],
                            "external_order_id": external_order_id, "reason": entry["closure_reason"]})
        chase = start_exit_chase(state, entry, "probabilistic_time_closure", now_utc)
        if chase is not None:
            actions.append({"action_type": "tree5_exit_chase_started", "status": "pending_fak", **chase})
    tree["last_closure_check_utc"] = iso_utc(now_utc)
    return actions


def due_time_closure_token_ids(state: dict[str, Any], cities: dict[str, dict[str, Any]], now_utc: datetime, config: dict[str, Any]) -> set[str]:
    """Return only positions that are currently in a local closure window."""
    tree = ensure_tree5_state(state)
    interval = int(config.get("tree5_closure_check_seconds", 60))
    previous = parse_utc(tree.get("last_closure_check_utc"))
    if previous is not None and (now_utc - previous).total_seconds() < interval:
        return set()
    tokens: set[str] = set()
    for entry in tree["entries"].values():
        if not isinstance(entry, dict) or entry.get("closure_at_utc") or entry.get("invalidated_at_utc"):
            continue
        city = next((value for value in cities.values() if value["city_id"] == entry.get("city_id")), None)
        if city is None:
            continue
        local_now = now_utc.astimezone(ZoneInfo(city["timezone"]))
        if local_now.date().isoformat() != entry.get("market_local_date"):
            continue
        direction = str(entry.get("direction"))
        start_hour = int(config.get("tree5_high_closure_start_hour", 13)) if direction == "high" else int(config.get("tree5_low_closure_start_hour", 1))
        end_hour = int(config.get("tree5_high_closure_end_hour", 17)) if direction == "high" else int(config.get("tree5_low_closure_end_hour", 5))
        if start_hour <= local_now.hour <= end_hour and entry.get("token_id"):
            tokens.add(str(entry["token_id"]))
    return tokens


def active_entry_token_ids(state: dict[str, Any]) -> set[str]:
    tree = ensure_tree5_state(state)
    return {str(entry["token_id"]) for entry in tree["entries"].values() if isinstance(entry, dict) and entry.get("token_id")}


def attach_confirmed_position_for_replay(state: dict[str, Any], token_id: str, shares: Any, now_utc: datetime) -> None:
    """Test/replay helper. Production callers must populate this from CLOB reconciliation."""
    parsed = _decimal(shares)
    if parsed is None or parsed < ZERO:
        raise ValueError("持仓股数必须为非负有限数")
    tree = ensure_tree5_state(state)
    tree["confirmed_positions"][str(token_id)] = {"shares": str(parsed), "source": "replay_or_reconciled", "updated_at_utc": iso_utc(now_utc)}
