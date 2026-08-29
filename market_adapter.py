"""Public Polymarket Gamma adapter for temperature-market rule discovery.

The adapter reads public event metadata only. It never authenticates, signs, reads a
wallet, or sends an order. Parsed rules are inputs to the local paper signal engine.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
import concurrent.futures
from datetime import date
from typing import Any

GAMMA_EVENT_ENDPOINT = "https://gamma-api.polymarket.com/events/slug/"
MONTHS = {month.casefold(): index for index, month in enumerate(("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"), start=1)}
QUESTION_RE = re.compile(r"^Will the (highest|lowest) temperature in .+? be (.+?) on ([A-Za-z]+) (\d+)\?$")
RANGE_RE = re.compile(r"^between (-?\d+(?:\.\d+)?)-(-?\d+(?:\.\d+)?)°([CF])$")
EXACT_RE = re.compile(r"^(-?\d+(?:\.\d+)?)°([CF])$")
BELOW_RE = re.compile(r"^(-?\d+(?:\.\d+)?)°([CF]) or below$")
ABOVE_RE = re.compile(r"^(-?\d+(?:\.\d+)?)°([CF]) or higher$")


def event_slug(market_city_slug: str, local_date: str, direction: str) -> str:
    parsed = date.fromisoformat(local_date)
    direction_word = "highest" if direction == "high" else "lowest"
    return f"{direction_word}-temperature-in-{market_city_slug}-on-{parsed.strftime('%B').lower()}-{parsed.day}-{parsed.year}"


def _fetch_json(url: str, timeout_seconds: float = 5.0) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "weatherbot-market-adapter/1.1 (+https://github.com/jssyxd/weatherbot)"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {}
        raise RuntimeError(f"Gamma 请求失败（HTTP {exc.code}）") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Gamma 网络请求失败: {exc.reason}") from exc
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise RuntimeError("Gamma 事件返回格式异常")
    return parsed


def parse_bucket(outcome_text: str) -> tuple[float | None, float | None, str] | None:
    """Parse contract question interval as a half-open numeric bucket [lo, hi)."""
    for pattern, kind in ((RANGE_RE, "range"), (EXACT_RE, "exact"), (BELOW_RE, "below"), (ABOVE_RE, "above")):
        match = pattern.match(outcome_text)
        if not match:
            continue
        if kind == "range":
            lo, upper, unit = float(match.group(1)), float(match.group(2)), match.group(3)
            return lo, upper + 1.0, unit
        if kind == "exact":
            value, unit = float(match.group(1)), match.group(2)
            return value, value + 1.0, unit
        if kind == "below":
            value, unit = float(match.group(1)), match.group(2)
            return None, value + 1.0, unit
        value, unit = float(match.group(1)), match.group(2)
        return value, None, unit
    return None


def _bucket_sort_key(bucket: dict[str, Any]) -> tuple[float, float]:
    return (
        -float("inf") if bucket.get("lo") is None else float(bucket["lo"]),
        float("inf") if bucket.get("hi") is None else float(bucket["hi"]),
    )


def parse_event_rules(event: dict[str, Any], city: dict[str, Any], local_date: str, direction: str) -> list[dict[str, Any]]:
    """Parse one active event into one rule with all its selectable temperature buckets."""
    expected_slug = event_slug(str(city.get("market_city_slug") or city["city_id"]), local_date, direction)
    if event.get("slug") != expected_slug:
        return []
    buckets: list[dict[str, Any]] = []
    for market in event.get("markets", []):
        if not isinstance(market, dict):
            continue
        if not (market.get("active") is True and market.get("closed") is False and market.get("acceptingOrders") is True and market.get("enableOrderBook") is True):
            continue
        question = str(market.get("question") or "")
        question_match = QUESTION_RE.match(question)
        if not question_match:
            continue
        wording_direction, outcome_text, month_name, day_text = question_match.groups()
        question_date = date(int(local_date[:4]), MONTHS.get(month_name.casefold(), 0), int(day_text)).isoformat() if month_name.casefold() in MONTHS else None
        if question_date != local_date or (wording_direction == "highest") != (direction == "high"):
            continue
        parsed_bucket = parse_bucket(outcome_text)
        if parsed_bucket is None:
            continue
        lo, hi, unit = parsed_bucket
        if unit != city["market_unit"]:
            continue
        try:
            outcomes = json.loads(market.get("outcomes", "[]"))
            token_ids = json.loads(market.get("clobTokenIds", "[]"))
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(outcomes, list) or not isinstance(token_ids, list) or len(outcomes) != len(token_ids):
            continue
        no_token = next((str(token_ids[index]) for index, outcome in enumerate(outcomes) if outcome == "No"), None)
        yes_token = next((str(token_ids[index]) for index, outcome in enumerate(outcomes) if outcome == "Yes"), None)
        if not no_token or not yes_token:
            continue
        buckets.append({
            "bucket_id": str(market.get("id")), "label": outcome_text, "lo": lo, "hi": hi,
            "market_id": str(market.get("id")), "yes_token_id": yes_token, "no_token_id": no_token,
        })
    if not buckets:
        return []
    buckets.sort(key=_bucket_sort_key)
    return [{
        "market_rule_id": f"{event.get('id')}|{local_date}|{direction}",
        "event_id": str(event.get("id")), "event_slug": expected_slug,
        "city_id": city["city_id"], "icao": city["icao"], "market_local_date": local_date,
        "direction": direction, "market_unit": city["market_unit"], "enabled": True,
        "source": "Polymarket Gamma public event metadata", "buckets": buckets,
    }]


def refresh_market_rules(cities: dict[str, dict[str, Any]], local_dates: dict[str, str], timeout_seconds: float = 10.0, total_deadline_seconds: float = 120.0) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Fetch public Gamma metadata concurrently (per-request timeout).

    A failed remote market-data endpoint must not prevent weather observation or
    warm-up progress. Failed city/direction pairs are marked so callers remain
    fail-closed instead of treating partial market rules as complete.

    Concurrent fetch (up to 16 workers) replaces the old serial loop, so the
    ~98 city/direction requests finish in a few seconds instead of hitting a
    30s serial deadline with only ~10 fetches done.
    """
    rules: list[dict[str, Any]] = []
    failures: dict[str, str] = {}
    work: list[tuple[str, dict[str, Any], str, str, str]] = []
    for city in cities.values():
        local_date = local_dates[city["icao"]]
        for direction in ("high", "low"):
            key = f"{city['city_id']}|{local_date}|{direction}"
            slug = event_slug(str(city.get("market_city_slug") or city["city_id"]), local_date, direction)
            work.append((key, city, local_date, direction, slug))

    def fetch_one(item: tuple[str, dict[str, Any], str, str, str]) -> tuple[str, list[dict[str, Any]] | None, str | None]:
        key, city, local_date, direction, slug = item
        try:
            event = _fetch_json(GAMMA_EVENT_ENDPOINT + slug, timeout_seconds=timeout_seconds)
            if not event:
                return key, None, "event_not_found"
            parsed = parse_event_rules(event, city, local_date, direction)
            if not parsed:
                return key, None, "no_trade_ready_parsed_rules"
            return key, parsed, None
        except (RuntimeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            return key, None, f"market_discovery_failed:{type(exc).__name__}"

    if work:
        max_workers = min(16, len(work))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            for key, parsed, err in pool.map(fetch_one, work):
                if err is not None:
                    failures[key] = err
                else:
                    rules.extend(parsed or [])
    return rules, failures
