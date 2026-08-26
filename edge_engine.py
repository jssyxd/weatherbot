"""Deterministic local-day dead-bucket engine (tree1).

This module deliberately has no LLM dependency and never submits a real order.
It turns published METAR/SPECI observations into auditable *candidate* signals.

tree1 strategy (user-confirmed, observation-only):
  * A market day is the airport city's IANA-local natural day 00:00-24:00
    (report_time_utc -> ZoneInfo(city tz) -> local date). Nothing is traded on
    the day's first report: it only initialises the daily baseline.
  * Every later METAR/SPECI is recorded; when a report sets a new daily
    extreme (new high above the previous high, or new low below the previous
    low), the temperature bucket that held the previous extreme value is now
    provably impossible -> candidate BUY_NO on that bucket (e.g. 12°C dropping
    to 11°C kills the "lowest 12°C" bucket; 12°C rising to 13°C kills the
    "highest 12°C" bucket). No forecast edge is involved.
  * No forecast model (TAF TX/TN, ECMWF, Wunderground) is fetched or used.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# Standard METAR RMK temperature group, 9 chars: T[sign][TTT][DDHH] e.g.
# T02430138 = +24.3°C (sign 0=+ 1=-, TTT = tenths of °C, trailing DDHH ignored).
REMARK_TEMPERATURE_RE = re.compile(r"\bT([01])(\d{3})(\d{4})\b")
# Legacy 4-char form: T[sign][TTT].
REMARK_TEMPERATURE_LEGACY_RE = re.compile(r"\bT([01])(\d{3})\b")
# Standard body temperature/dewpoint group, e.g. 23/19 or M04/M08.
BODY_TEMPERATURE_RE = re.compile(r"\b(M?\d{2})/(M?\d{2})\b")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc_string(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def atomic_json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_contract_cities(path: Path) -> dict[str, dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("contract_cities 配置必须为数组")
    by_icao: dict[str, dict[str, Any]] = {}
    for city in raw:
        if not isinstance(city, dict):
            raise ValueError("contract_cities 每项必须为对象")
        required = (
            "city_id", "name", "icao", "timezone", "market_unit", "wu_forecast_url",
            "latitude", "longitude", "coordinate_source",
        )
        missing = [field for field in required if not city.get(field)]
        if missing:
            raise ValueError(f"合同城市配置缺少字段: {', '.join(missing)}")
        icao = str(city["icao"]).upper()
        if len(icao) != 4 or not icao.isalnum():
            raise ValueError(f"合同城市配置的 ICAO 无效: {icao!r}")
        if city["market_unit"] not in {"C", "F"}:
            raise ValueError(f"{icao} 的 market_unit 必须为 C 或 F")
        latitude, longitude = city["latitude"], city["longitude"]
        if not isinstance(latitude, (int, float)) or not -90.0 <= float(latitude) <= 90.0:
            raise ValueError(f"{icao} 的 latitude 必须为 -90 至 90 的数值")
        if not isinstance(longitude, (int, float)) or not -180.0 <= float(longitude) <= 180.0:
            raise ValueError(f"{icao} 的 longitude 必须为 -180 至 180 的数值")
        ZoneInfo(str(city["timezone"]))
        if icao in by_icao:
            raise ValueError(f"合同城市配置 ICAO 重复: {icao}")
        normalized = dict(city)
        normalized["icao"] = icao
        normalized.setdefault("market_city_slug", str(normalized["city_id"]))
        by_icao[icao] = normalized
    if len(by_icao) != 49:
        raise ValueError(f"严格 METAR 范围应有 49 城，当前为 {len(by_icao)}")
    return by_icao


def local_market_date(report_time_utc: datetime, city: dict[str, Any]) -> str:
    """IANA-local market day: observed UTC -> airport timezone -> date.

    The day window is the city's local 00:00-24:00. A 16:00Z report for a
    UTC+8 city belongs to the *next* local day, never to the fetch/receipt
    time.
    """
    return report_time_utc.astimezone(ZoneInfo(city["timezone"])).date().isoformat()


def celsius_to_native(value_c: float, unit: str) -> float:
    return value_c if unit == "C" else value_c * 9.0 / 5.0 + 32.0


def observed_temperature_native(event: dict[str, Any], city: dict[str, Any]) -> tuple[float, str] | None:
    raw = str(event.get("raw_metar") or "")
    remark = REMARK_TEMPERATURE_RE.search(raw) or REMARK_TEMPERATURE_LEGACY_RE.search(raw)
    if remark:
        value_c = int(remark.group(2)) / 10.0
        if remark.group(1) == "1":
            value_c = -value_c
        return celsius_to_native(value_c, city["market_unit"]), "metar_remark_tenths_c"
    body_temperature = BODY_TEMPERATURE_RE.search(raw)
    if body_temperature:
        encoded = body_temperature.group(1)
        value_c = float(encoded[1:]) if encoded.startswith("M") else float(encoded)
        if encoded.startswith("M"):
            value_c *= -1
        return celsius_to_native(value_c, city["market_unit"]), "metar_body_integer_c"
    value = event.get("temperature_c")
    if isinstance(value, (int, float)):
        return celsius_to_native(float(value), city["market_unit"]), "metar_body_integer_c"
    return None


def _day_state_key(city: dict[str, Any], local_date: str) -> str:
    return f"{city['city_id']}|{local_date}"


def _bucket_newly_invalidated(bucket: dict[str, Any], direction: str, previous: float | None, current: float) -> bool:
    """A bucket is newly dead when the current extreme crossed its boundary.

    high: dead when current >= hi (the observed high is at/above the bucket's
          top) while previous was still below hi.
    low:  dead when current <  lo (the observed low is below the bucket's
          bottom) while previous was still at/above lo.
    Open-ended buckets (lo=None or hi=None) are never invalidated by this rule.
    """
    lo = bucket.get("lo")
    hi = bucket.get("hi")
    if direction == "high":
        if hi is None:
            return False
        return current >= float(hi) and (previous is None or previous < float(hi))
    if lo is None:
        return False
    return current < float(lo) and (previous is None or previous >= float(lo))


def select_dead_buckets(buckets: list[dict[str, Any]], direction: str, previous: float | None, current: float) -> list[dict[str, Any]]:
    """Return EVERY bucket newly invalidated by the new extreme (tree1 v2).

    User rule: on a new daily high/low, ALL buckets that just became
    impossible are tradeable, not only the one holding the previous extreme.
    A fast 2-3 degree move kills 2-3 buckets at once (e.g. 24 -> 27 kills
    [24,25), [25,26) and [26,27)). Order is deterministic: high sorted by hi
    ascending (coldest dead bucket first), low sorted by lo ascending.
    """
    invalidated = [bucket for bucket in buckets if _bucket_newly_invalidated(bucket, direction, previous, current)]
    # Half-open tail buckets ("X°C or below" for high, "X°C or higher" for low)
    # ARE killable in the opposite direction and carry a None bound on the
    # sorting side; never call float() on None (2026-08-23 v2 review catch).
    def _key_hi(bucket: dict[str, Any]) -> float:
        hi = bucket.get("hi")
        return float(hi) if hi is not None else float("inf")

    def _key_lo(bucket: dict[str, Any]) -> float:
        lo = bucket.get("lo")
        return float(lo) if lo is not None else -float("inf")

    if direction == "high":
        invalidated.sort(key=lambda bucket: (_key_hi(bucket), _key_lo(bucket)))
    else:
        invalidated.sort(key=lambda bucket: (_key_lo(bucket), _key_hi(bucket)))
    return invalidated


def market_rules_for(rules: list[dict[str, Any]], city_id: str, local_date: str, direction: str) -> list[dict[str, Any]]:
    return [
        rule for rule in rules
        if rule.get("city_id") == city_id
        and rule.get("market_local_date") == local_date
        and rule.get("direction") == direction
        and rule.get("enabled", True) is True
    ]


def evaluate_observation(
    state: dict[str, Any],
    event: dict[str, Any],
    city: dict[str, Any],
    market_rules: list[dict[str, Any]],
    max_latency_seconds: int = 900,
) -> list[dict[str, Any]]:
    report_time = parse_utc(event.get("report_time_utc"))
    fetched_at = parse_utc(event.get("fetched_at_utc")) or utc_now()
    if report_time is None:
        return [{"signal_type": "no_signal", "reason": "missing_report_time", "event_id": event.get("event_id"), "city_id": city["city_id"]}]
    # CheckWX short responses expose the observation time but no ingestion or
    # receipt timestamp. Gate deterministically on absolute age from observed
    # UTC to local fetch UTC; this avoids provider-specific latency assumptions.
    age = (fetched_at - report_time).total_seconds()
    if age > max_latency_seconds:
        return [{"signal_type": "no_signal", "reason": "report_too_old", "event_id": event.get("event_id"), "age_seconds": round(age, 3), "city_id": city["city_id"]}]
    if event.get("is_correction") is True:
        return [{
            "signal_type": "no_signal", "reason": "correction_requires_full_day_rebuild",
            "event_id": event.get("event_id"), "city_id": city["city_id"],
            "disclaimer": "COR is retained for audit but cannot produce a candidate until full corrected-day replay is implemented.",
        }]
    temperature = observed_temperature_native(event, city)
    if temperature is None:
        return [{"signal_type": "no_signal", "reason": "missing_temperature", "event_id": event.get("event_id"), "city_id": city["city_id"]}]
    value, precision = temperature
    local_date = local_market_date(report_time, city)
    day_key = _day_state_key(city, local_date)
    extrema = state.setdefault("daily_extrema", {})
    day = extrema.get(day_key)
    if day is None:
        # First report of the IANA-local day: record only, never trade.
        extrema[day_key] = {
            "city_id": city["city_id"], "icao": city["icao"], "market_local_date": local_date,
            "market_unit": city["market_unit"], "high": value, "low": value,
            "initialized_by_event_id": event.get("event_id"), "updated_at_utc": event.get("fetched_at_utc"),
        }
        return [{"signal_type": "no_signal", "reason": "daily_baseline_initialized", "event_id": event.get("event_id"), "local_date": local_date, "city_id": city["city_id"]}]

    previous_high, previous_low = float(day["high"]), float(day["low"])
    is_new_high, is_new_low = value > previous_high, value < previous_low
    if is_new_high:
        day["high"] = value
    if is_new_low:
        day["low"] = value
    day["updated_at_utc"] = event.get("fetched_at_utc")

    signals: list[dict[str, Any]] = []
    for direction, previous, is_new in (("high", previous_high, is_new_high), ("low", previous_low, is_new_low)):
        if not is_new:
            signals.append({"signal_type": "no_signal", "reason": f"not_new_daily_{direction}", "event_id": event.get("event_id"), "local_date": local_date, "city_id": city["city_id"]})
            continue
        rules = market_rules_for(market_rules, city["city_id"], local_date, direction)
        bucket_candidates: list[dict[str, Any]] = []
        for rule in rules:
            if rule.get("market_unit") != city["market_unit"]:
                continue
            for selected in select_dead_buckets(rule.get("buckets", []), direction, previous, value):
                candidate = dict(selected)
                candidate["market_rule_id"] = rule.get("market_rule_id")
                candidate["market_id"] = candidate.get("market_id")
                candidate["no_token_id"] = candidate.get("no_token_id")
                bucket_candidates.append(candidate)
        if not bucket_candidates:
            signals.append({
                "signal_type": "no_signal", "reason": f"no_dead_{direction}_bucket_in_market_rules",
                "event_id": event.get("event_id"), "local_date": local_date, "city_id": city["city_id"],
                "value": round(value, 4), "previous_extreme": round(previous, 4),
            })
            continue
        if city["market_unit"] == "F" and precision != "metar_remark_tenths_c":
            signals.append({
                "signal_type": "no_signal", "reason": "f_unit_precision_ambiguous",
                "event_id": event.get("event_id"), "local_date": local_date, "city_id": city["city_id"],
                "temperature_native": round(value, 4), "temperature_precision": precision,
                "disclaimer": "An integer-C METAR body temperature cannot safely invalidate a Fahrenheit contract bucket without an RMK T-group.",
            })
            continue
        handled = state.setdefault("handled_candidate_buckets", {})
        for selected_bucket in bucket_candidates:
            handle_key = "|".join((str(selected_bucket.get("market_rule_id")), str(selected_bucket.get("bucket_id"))))
            if handle_key in handled:
                signals.append({"signal_type": "no_signal", "reason": "candidate_bucket_already_handled", "event_id": event.get("event_id"), "local_date": local_date, "city_id": city["city_id"], "handle_key": handle_key})
                continue
            handled[handle_key] = event.get("fetched_at_utc")
            signals.append({
                "signal_type": "candidate_no_signal",
                "candidate_status": "dead_bucket_invalidated_by_metar",
                "event_id": event.get("event_id"),
                "city_id": city["city_id"], "icao": city["icao"], "market_local_date": local_date,
                "direction": direction, "market_unit": city["market_unit"],
                "temperature_native": round(value, 4), "temperature_precision": precision,
                "previous_candidate_extreme": round(previous, 4),
                "bucket": selected_bucket, "handle_key": handle_key,
                "disclaimer": "METAR/SPECI candidate signal only; not settlement-source confirmation.",
            })
    return signals


def append_jsonl(path: Path, records: list[dict[str, Any]]) -> Path | None:
    if not records:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def load_market_rules(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("market_rules 必须为数组")
    return [item for item in raw if isinstance(item, dict)]
