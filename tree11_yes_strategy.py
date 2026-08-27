"""Tree11 paper-only TAF fact-reversal signal policy.

The module is deterministic and network-free. It stores every TAF version when
it becomes visible to the program, updates independent daily high/low extrema
from supplied METAR/SPECI/COR events, and emits only *pending* paper YES
candidates. Market-consensus and paper-execution checks are intentionally a
separate phase; no wallet, credential, order, cancellation or position code is
present here.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from edge_engine import local_market_date, parse_utc
from tree5_strategy import bucket_contains, parse_taf_extremes_for_local_day, select_forecast_bucket, tree5_day_key

SCHEMA_VERSION = "1.0"


class Tree11InputError(ValueError):
    pass


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _as_float(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise Tree11InputError(f"invalid_{name}") from exc
    if result != result or result in (float("inf"), float("-inf")):
        raise Tree11InputError(f"invalid_{name}")
    return result


def ensure_tree11_state(state: dict[str, Any]) -> dict[str, Any]:
    tree = state.setdefault("tree11", {})
    if not isinstance(tree, dict):
        raise Tree11InputError("tree11_state_must_be_object")
    for key, default in (
        ("taf_versions", {}),
        ("daily_extrema", {}),
        ("signals", {}),
        ("processed_event_ids", {}),
    ):
        tree.setdefault(key, default)
        if not isinstance(tree[key], dict):
            raise Tree11InputError(f"tree11_{key}_must_be_object")
    return tree


def _forecast_key(city: dict[str, Any], market_local_date: str, direction: str) -> str:
    return f"{tree5_day_key(city, market_local_date)}|{direction}"


def _stable_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def record_taf_versions(
    state: dict[str, Any], reports: list[dict[str, Any]], cities: dict[str, dict[str, Any]],
    visible_at_utc: datetime, visible_at_monotonic_ns: int, source_endpoint: str,
) -> list[dict[str, Any]]:
    """Persist all TAF extreme versions visible at a known program time.

    Only versions supplied by the caller are recorded. A later run must never
    rewrite a prior version's visibility timestamp or retroactively replace the
    version selected by a signal at t0.
    """
    if visible_at_monotonic_ns <= 0:
        raise Tree11InputError("taf_visible_monotonic_ns_required")
    tree = ensure_tree11_state(state)
    reports_by_icao = {str(report.get("icao") or "").upper(): report for report in reports if isinstance(report, dict)}
    actions: list[dict[str, Any]] = []
    for city in cities.values():
        report = reports_by_icao.get(str(city.get("icao") or "").upper())
        if report is None:
            continue
        local_date = local_market_date(visible_at_utc, city)
        parsed = parse_taf_extremes_for_local_day(report.get("raw_text"), report.get("issued"), city, local_date)
        for direction, forecast in parsed.items():
            key = _forecast_key(city, local_date, direction)
            versions = tree["taf_versions"].setdefault(key, [])
            if not isinstance(versions, list):
                raise Tree11InputError("taf_version_list_must_be_array")
            provenance = {
                "city_id": city["city_id"], "icao": city["icao"], "market_local_date": local_date,
                "direction": direction, "taf_issued_utc": forecast["issued_utc"],
                "forecast_time_utc": forecast["forecast_time_utc"], "value_c": forecast["value_c"],
                "value_native": forecast["value_native"], "market_unit": forecast["market_unit"],
                "raw_group": forecast["raw_group"], "raw_taf": str(report.get("raw_text") or ""),
                "source_endpoint": source_endpoint, "visible_at_utc": iso_utc(visible_at_utc),
                "visible_at_monotonic_ns": visible_at_monotonic_ns,
            }
            provenance["taf_version_id"] = _stable_hash(provenance)
            if any(isinstance(old, dict) and old.get("taf_version_id") == provenance["taf_version_id"] for old in versions):
                actions.append({"action_type": "tree11_taf_version", "status": "duplicate_visible_version", "taf_version_id": provenance["taf_version_id"], "forecast_key": key, "safety": {"paper_only": True, "orders_submitted": 0, "credentials_loaded": False}})
                continue
            versions.append(provenance)
            versions.sort(key=lambda value: (int(value.get("visible_at_monotonic_ns", 0)), str(value.get("taf_issued_utc") or "")))
            actions.append({"action_type": "tree11_taf_version", "status": "recorded", "taf_version_id": provenance["taf_version_id"], "forecast_key": key, **provenance, "safety": {"paper_only": True, "orders_submitted": 0, "credentials_loaded": False}})
    return actions


def latest_visible_taf(state: dict[str, Any], city: dict[str, Any], market_local_date: str, direction: str, t0_monotonic_ns: int) -> dict[str, Any] | None:
    tree = ensure_tree11_state(state)
    versions = tree["taf_versions"].get(_forecast_key(city, market_local_date, direction), [])
    if not isinstance(versions, list):
        raise Tree11InputError("taf_version_list_must_be_array")
    eligible = [version for version in versions if isinstance(version, dict) and int(version.get("visible_at_monotonic_ns", 0)) <= t0_monotonic_ns]
    return max(eligible, key=lambda value: (int(value["visible_at_monotonic_ns"]), str(value.get("taf_issued_utc") or ""))) if eligible else None


def _current_extreme(tree: dict[str, Any], city: dict[str, Any], market_local_date: str, direction: str) -> float | None:
    values = tree["daily_extrema"].get(tree5_day_key(city, market_local_date), {})
    if not isinstance(values, dict):
        return None
    value = values.get(direction)
    return _as_float(value, "daily_extreme") if value is not None else None


def _update_extrema(tree: dict[str, Any], city: dict[str, Any], market_local_date: str, temperature_native: float, report_time_utc: datetime) -> dict[str, float]:
    key = tree5_day_key(city, market_local_date)
    day = tree["daily_extrema"].setdefault(key, {"high": temperature_native, "low": temperature_native, "last_report_time_utc": iso_utc(report_time_utc)})
    if not isinstance(day, dict):
        raise Tree11InputError("daily_extrema_entry_must_be_object")
    previous_high = _as_float(day.get("high", temperature_native), "previous_high")
    previous_low = _as_float(day.get("low", temperature_native), "previous_low")
    day["high"] = max(previous_high, temperature_native)
    day["low"] = min(previous_low, temperature_native)
    day["last_report_time_utc"] = iso_utc(report_time_utc)
    return {"high": float(day["high"]), "low": float(day["low"])}


def _bucket_for_value(rules: list[dict[str, Any]], city: dict[str, Any], market_local_date: str, direction: str, value_native: float) -> tuple[dict[str, Any], dict[str, Any]] | None:
    return select_forecast_bucket(rules, city, market_local_date, direction, value_native)


def _is_fact_reversal(old_bucket: dict[str, Any], direction: str, observed_extreme: float) -> bool:
    if direction == "high":
        high = old_bucket.get("hi")
        return high is not None and observed_extreme >= float(high)
    if direction == "low":
        low = old_bucket.get("lo")
        return low is not None and observed_extreme < float(low)
    raise Tree11InputError("invalid_direction")


def _report_age_seconds(event: dict[str, Any]) -> float | None:
    observed = parse_utc(event.get("report_time_utc") or event.get("observed_at_utc"))
    fetched = parse_utc(event.get("fetched_at_utc") or event.get("received_at_utc"))
    if observed is None or fetched is None:
        return None
    return (fetched - observed).total_seconds()


def evaluate_fact_reversal(
    state: dict[str, Any], event: dict[str, Any], city: dict[str, Any], rules: list[dict[str, Any]],
    t0_utc: datetime, t0_monotonic_ns: int, *, warmup_complete: bool, max_report_age_seconds: int = 300,
) -> list[dict[str, Any]]:
    """Update independent extrema and emit pending paper YES candidates.

    The returned items deliberately stop at ``PENDING_CONSENSUS``. They contain
    no order price and are not considered executable until a later module joins
    the pre-t0 L2 snapshots and checks the policy manifest.
    """
    if t0_monotonic_ns <= 0:
        raise Tree11InputError("t0_monotonic_ns_required")
    tree = ensure_tree11_state(state)
    event_id = str(event.get("event_id") or "")
    report_time = parse_utc(event.get("report_time_utc") or event.get("observed_at_utc"))
    temperature = _as_float(event.get("temperature_native"), "temperature_native")
    if report_time is None:
        raise Tree11InputError("report_time_utc_required")
    local_date = local_market_date(report_time, city)
    age_seconds = _report_age_seconds(event)
    base = {
        "schema_version": SCHEMA_VERSION, "event_id": event_id or None, "city_id": city["city_id"], "icao": city["icao"],
        "market_local_date": local_date, "report_time_utc": iso_utc(report_time), "fetched_at_utc": event.get("fetched_at_utc") or event.get("received_at_utc"),
        "t0_utc": iso_utc(t0_utc), "t0_monotonic_ns": t0_monotonic_ns, "temperature_native": temperature,
        "report_age_seconds": age_seconds, "safety": {"paper_only": True, "orders_submitted": 0, "credentials_loaded": False},
    }
    if event_id and event_id in tree["processed_event_ids"]:
        return [{"action_type": "tree11_fact_reversal", "status": "duplicate_event", **base}]
    if event_id:
        tree["processed_event_ids"][event_id] = iso_utc(t0_utc)
    extrema = _update_extrema(tree, city, local_date, temperature, report_time)
    if not warmup_complete:
        return [{"action_type": "tree11_fact_reversal", "status": "blocked_warmup_incomplete", "extrema": extrema, **base}]
    if age_seconds is None or age_seconds < 0 or age_seconds >= max_report_age_seconds:
        return [{"action_type": "tree11_fact_reversal", "status": "blocked_report_age", "max_report_age_seconds": max_report_age_seconds, "extrema": extrema, **base}]

    actions: list[dict[str, Any]] = []
    for direction in ("high", "low"):
        taf = latest_visible_taf(state, city, local_date, direction, t0_monotonic_ns)
        if taf is None:
            actions.append({"action_type": "tree11_fact_reversal", "status": "blocked_no_t0_visible_taf", "direction": direction, "extrema": extrema, **base})
            continue
        old_selection = _bucket_for_value(rules, city, local_date, direction, _as_float(taf.get("value_native"), "taf_value_native"))
        observed_extreme = extrema[direction]
        new_selection = _bucket_for_value(rules, city, local_date, direction, observed_extreme)
        if old_selection is None or new_selection is None:
            actions.append({"action_type": "tree11_fact_reversal", "status": "blocked_missing_unique_bucket", "direction": direction, "taf_version": taf, "observed_extreme": observed_extreme, **base})
            continue
        old_rule, old_bucket = old_selection
        new_rule, new_bucket = new_selection
        if str(old_bucket.get("bucket_id")) == str(new_bucket.get("bucket_id")):
            actions.append({"action_type": "tree11_fact_reversal", "status": "no_crossed_contract_bucket", "direction": direction, "taf_version": taf, "old_bucket": old_bucket, "new_bucket": new_bucket, "observed_extreme": observed_extreme, **base})
            continue
        if not _is_fact_reversal(old_bucket, direction, observed_extreme):
            actions.append({"action_type": "tree11_fact_reversal", "status": "no_fact_reversal", "direction": direction, "taf_version": taf, "old_bucket": old_bucket, "new_bucket": new_bucket, "observed_extreme": observed_extreme, **base})
            continue
        # A fact reversal is a transition of a specific visible TAF bucket into
        # a specific observed bucket. Do not include the raw event id: later
        # unrelated reports must not repeatedly create the same trade candidate.
        # If the observed extreme later reaches a *different* bucket, that is a
        # new transition and will receive a distinct signal id for later review.
        signal_basis = {
            "city_id": city["city_id"], "market_local_date": local_date, "direction": direction,
            "taf_version_id": taf["taf_version_id"], "old_bucket_id": old_bucket.get("bucket_id"), "new_bucket_id": new_bucket.get("bucket_id"),
        }
        signal_id = f"tree11-{_stable_hash(signal_basis)}"
        if signal_id in tree["signals"]:
            actions.append({"action_type": "tree11_fact_reversal", "status": "duplicate_signal", "signal_id": signal_id, **base})
            continue
        candidate = {
            "signal_id": signal_id, "status": "PENDING_CONSENSUS", "action_type": "tree11_paper_yes_intent", "city_id": city["city_id"],
            "icao": city["icao"], "market_local_date": local_date, "direction": direction, "market_rule_id": new_rule.get("market_rule_id"),
            "old_market_rule_id": old_rule.get("market_rule_id"), "old_bucket": old_bucket, "new_bucket": new_bucket,
            "market_bucket_ids": [str(bucket.get("bucket_id")) for bucket in new_rule.get("buckets", []) if isinstance(bucket, dict) and bucket.get("bucket_id")],
            "token_id": new_bucket.get("yes_token_id"), "side": "BUY", "outcome": "YES", "requested_shares": "5",
            "report": base, "taf_at_t0": taf, "observed_extreme": observed_extreme, "consensus_status": "NOT_EVALUATED",
            "execution_status": "NOT_EVALUATED", "safety": {"paper_only": True, "orders_submitted": 0, "credentials_loaded": False},
        }
        tree["signals"][signal_id] = candidate
        actions.append(candidate)
    return actions
