"""tree12-allno: self-contained early NO-bucket layout with strict filters and position management.

Default is paper / observe-only. Live submission requires reconciled positions.
This module is a standalone strategy: it does not import tree5 code. TAF parsing,
bucket containment and UTC helpers are implemented locally.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any
from zoneinfo import ZoneInfo

from edge_engine import celsius_to_native, local_market_date, parse_utc
from paper_capital import remaining_capital_usdc, reserve, release
from execution.paper_executor import match_gtc
from execution.order_intent import OrderIntent, OrderType, Side
from execution.position import realized_pnl_for_exit
from adapters.polymarket.orderbook import from_any

TREE12_TARGET_SHARES = Decimal("5")
TREE12_MIN_NO_ASK = Decimal("0.85")
TREE12_MAX_NO_ASK = Decimal("0.95")
# Entry window: (18, 30] hours before local day 00:00 (backtest optimal 24-30h + buffer)
TREE12_LEAD_HOURS_MIN = 18
TREE12_LEAD_HOURS_MAX = 30
TREE12_WS_VWAP_HOURS = 6
TREE12_REQUOTE_TICKS = 2
# Fast cut-loss ladder: mild absolute-style discounts, short intervals, hard floor
TREE12_DEFAULT_EXIT_RETRY_SECONDS = (0, 3, 8, 15, 30)
TREE12_DEFAULT_EXIT_SLIPPAGE = (Decimal("0.03"), Decimal("0.07"), Decimal("0.12"), Decimal("0.20"), Decimal("0.30"))
TREE12_EXIT_HARD_FLOOR = Decimal("0.05")
TAF_EXTREME_RE = re.compile(r"\b(TX|TN)(M?)(\d{2})/(\d{2})(\d{2})Z\b")
TREE12_PAPER_FEE_RATE = Decimal("0.05")
ZERO = Decimal("0")


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def new_order_id() -> str:
    """Unified order identifier spanning SIGNAL→…→PNL for audit (PRD Step 7)."""
    return f"t12-{uuid.uuid4().hex[:12]}"


def bucket_contains(bucket: dict[str, Any], value: float) -> bool:
    lo, hi = bucket.get("lo"), bucket.get("hi")
    return (lo is None or value >= float(lo)) and (hi is None or value < float(hi))


def _month_shift(year: int, month: int, offset: int) -> tuple[int, int]:
    absolute = year * 12 + month - 1 + offset
    return absolute // 12, absolute % 12 + 1


def _resolve_taf_day_hour(reference_utc: datetime, day: int, hour: int) -> datetime | None:
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

    Returned values are converted to the contract's native C/F unit. This is a
    local, tree12-owned copy so the strategy never depends on tree5 TAF code.
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
        previous = parsed.get(direction)
        if previous is None or candidate["forecast_time_utc"] >= previous["forecast_time_utc"]:
            parsed[direction] = candidate
    return parsed


def tree12_day_key(city: dict[str, Any], market_local_date: str) -> str:
    return f"{city['city_id']}|{market_local_date}"


def due_tree12_taf_cities(state: dict[str, Any], cities: dict[str, dict[str, Any]], now_utc: datetime, config: dict[str, Any]) -> list[dict[str, Any]]:
    """Return cities whose first post-01:00 TAF fetch (or bounded retry) is due."""
    tree = ensure_tree12_state(state)
    fetch_hour = int(config.get("tree12_taf_fetch_local_hour", 1))
    retry_seconds = int(config.get("tree12_taf_retry_seconds", 900))
    due: list[dict[str, Any]] = []
    for city in cities.values():
        local_now = now_utc.astimezone(ZoneInfo(city["timezone"]))
        if local_now.hour < fetch_hour:
            continue
        market_date = local_now.date().isoformat()
        key = tree12_day_key(city, market_date)
        prior = tree["taf_fetches"].get(key, {})
        if prior.get("status") == "complete" and prior.get("market_local_date") == market_date:
            continue
        last_attempt = parse_utc(prior.get("last_attempt_utc"))
        if last_attempt is not None and (now_utc - last_attempt).total_seconds() < retry_seconds:
            continue
        due.append(city)
    return due


def record_tree12_taf_reports(state: dict[str, Any], reports: list[dict[str, Any]], cities: dict[str, dict[str, Any]], now_utc: datetime, source_endpoint: str) -> list[dict[str, Any]]:
    """Persist tree12-owned daily TAF extrema. Missing TX/TN is explicit and does not trade."""
    tree = ensure_tree12_state(state)
    by_icao = {str(report.get("icao", "")).upper(): report for report in reports if isinstance(report, dict)}
    actions: list[dict[str, Any]] = []
    for city in cities.values():
        local_date = now_utc.astimezone(ZoneInfo(city["timezone"])).date().isoformat()
        day_key = tree12_day_key(city, local_date)
        report = by_icao.get(city["icao"])
        fetch_record = {
            "city_id": city["city_id"], "icao": city["icao"], "market_local_date": local_date,
            "last_attempt_utc": iso_utc(now_utc), "source_endpoint": source_endpoint,
        }
        if report is None:
            tree["taf_fetches"][day_key] = {**fetch_record, "status": "failed_missing_station_taf"}
            actions.append({"action_type": "tree12_taf_fetch", "status": "failed_missing_station_taf", **fetch_record})
            continue
        parsed = parse_taf_extremes_for_local_day(report.get("raw_text"), report.get("issued"), city, local_date)
        missing = sorted({"high", "low"} - set(parsed))
        if missing:
            tree["taf_fetches"][day_key] = {**fetch_record, "status": "failed_missing_local_day_extreme", "missing_directions": missing, "taf_issued_utc": report.get("issued")}
            actions.append({"action_type": "tree12_taf_fetch", "status": "failed_missing_local_day_extreme", "missing_directions": missing, **fetch_record})
            continue
        tree["taf_fetches"][day_key] = {**fetch_record, "status": "complete", "taf_issued_utc": report.get("issued")}
        for direction, detail in parsed.items():
            key = f"{day_key}|{direction}"
            tree["taf_forecasts"][key] = {
                **detail, "city_id": city["city_id"], "icao": city["icao"], "market_local_date": local_date,
                "raw_taf": str(report.get("raw_text") or ""), "source_endpoint": source_endpoint,
                "fetched_at_utc": iso_utc(now_utc),
            }
            actions.append({"action_type": "tree12_taf_forecast_recorded", "status": "recorded", "forecast_key": key,
                            "city_id": city["city_id"], "market_local_date": local_date, **detail})
    return actions


def ensure_tree12_state(state: dict[str, Any]) -> dict[str, Any]:
    tree = state.setdefault("tree12", {})
    if not isinstance(tree, dict):
        raise ValueError("tree12 状态必须为对象")
    for name, default in (
        ("working_orders", {}),
        ("positions", {}),
        ("exit_chases", {}),
        ("ws_ask_samples", {}),
        ("taf_fetches", {}),
        ("taf_forecasts", {}),
        ("last_scan_utc", None),
        ("rejects", {}),
    ):
        tree.setdefault(name, default)
        if name != "last_scan_utc" and not isinstance(tree[name], dict):
            raise ValueError(f"tree12.{name} 状态必须为对象")
    return tree


def _dec(value: Any, default: str | None = None) -> Decimal | None:
    if value is None:
        return Decimal(default) if default is not None else None
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default) if default is not None else None


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def local_day_start_utc(city: dict[str, Any], market_local_date: str) -> datetime:
    from zoneinfo import ZoneInfo
    y, m, d = (int(x) for x in market_local_date.split("-"))
    local = datetime(y, m, d, 0, 0, 0, tzinfo=ZoneInfo(city["timezone"]))
    return local.astimezone(timezone.utc)


def hours_before_local_day(city: dict[str, Any], market_local_date: str, now_utc: datetime) -> float:
    start = local_day_start_utc(city, market_local_date)
    return (start - now_utc.astimezone(timezone.utc)).total_seconds() / 3600.0


def allow_new_entries(city: dict[str, Any], market_local_date: str, now_utc: datetime) -> bool:
    """Open new buckets only when local-day start is (18, 30] hours ahead."""
    hours = hours_before_local_day(city, market_local_date, now_utc)
    return TREE12_LEAD_HOURS_MIN < hours <= TREE12_LEAD_HOURS_MAX


def position_key(city_id: str, market_local_date: str, direction: str, bucket_id: Any) -> str:
    return f"{city_id}|{market_local_date}|{direction}|{bucket_id}"


def record_ws_ask_sample(state: dict[str, Any], token_id: str, price: Decimal, size: Decimal, now_utc: datetime) -> None:
    tree = ensure_tree12_state(state)
    samples = tree["ws_ask_samples"].setdefault(str(token_id), [])
    samples.append({"ts": _iso(now_utc), "price": str(price), "size": str(size)})
    cutoff = now_utc.astimezone(timezone.utc) - timedelta(hours=TREE12_WS_VWAP_HOURS + 1)
    kept = []
    for row in samples:
        try:
            ts = datetime.fromisoformat(str(row["ts"]).replace("Z", "+00:00"))
        except Exception:
            continue
        if ts >= cutoff:
            kept.append(row)
    tree["ws_ask_samples"][str(token_id)] = kept[-500:]


def ws_ask_vwap_6h(state: dict[str, Any], token_id: str, now_utc: datetime) -> Decimal | None:
    tree = ensure_tree12_state(state)
    samples = tree["ws_ask_samples"].get(str(token_id)) or []
    cutoff = now_utc.astimezone(timezone.utc) - timedelta(hours=TREE12_WS_VWAP_HOURS)
    num = Decimal("0")
    den = Decimal("0")
    for row in samples:
        try:
            ts = datetime.fromisoformat(str(row["ts"]).replace("Z", "+00:00"))
            px = Decimal(str(row["price"]))
            sz = Decimal(str(row.get("size") or "1"))
        except Exception:
            continue
        if ts < cutoff or px <= 0 or sz <= 0:
            continue
        num += px * sz
        den += sz
    if den <= 0:
        return None
    return (num / den).quantize(Decimal("0.0001"))


def hybrid_limit_price(state: dict[str, Any], token_id: str, best_ask: Decimal, tick: Decimal, now_utc: datetime) -> Decimal:
    vwap = ws_ask_vwap_6h(state, token_id, now_utc)
    fair = best_ask if vwap is None else (vwap + best_ask) / Decimal("2")
    limit = min(fair, best_ask)
    floor = TREE12_MIN_NO_ASK + tick
    if limit < floor:
        limit = floor
    if tick > 0:
        steps = (limit / tick).to_integral_value(rounding=ROUND_DOWN)
        limit = steps * tick
    if limit > best_ask:
        limit = best_ask
    return limit


def no_token_id(bucket: dict[str, Any]) -> str | None:
    """Align with market_adapter / edge_engine field names."""
    for key in ("no_token_id", "token_id_no", "noTokenId", "no_token", "clob_no_token_id"):
        value = bucket.get(key)
        if value:
            return str(value)
    # Parent rule may embed tokens on the bucket after enrichment
    for key in ("tokens", "clob_token_ids", "clobTokenIds"):
        tokens = bucket.get(key)
        if isinstance(tokens, str):
            try:
                import json
                tokens = json.loads(tokens)
            except Exception:
                tokens = None
        if isinstance(tokens, (list, tuple)) and len(tokens) >= 2:
            # Prefer explicit outcomes if present
            outcomes = bucket.get("outcomes")
            if isinstance(outcomes, list) and len(outcomes) == len(tokens):
                for index, outcome in enumerate(outcomes):
                    if str(outcome).lower() in {"no", "n"}:
                        return str(tokens[index])
            return str(tokens[1])
    return None


def best_ask_of(book: Any) -> Decimal | None:
    if book is None:
        return None
    if isinstance(book, dict):
        raw = book.get("best_ask")
        if raw is None and book.get("asks"):
            asks = book["asks"]
            if asks:
                raw = asks[0].get("price") if isinstance(asks[0], dict) else asks[0]
    else:
        raw = getattr(book, "best_ask", None)
    return _dec(raw)


def best_bid_of(book: Any) -> Decimal | None:
    if book is None:
        return None
    if isinstance(book, dict):
        raw = book.get("best_bid")
        if raw is None and book.get("bids"):
            bids = book["bids"]
            if bids:
                raw = bids[0].get("price") if isinstance(bids[0], dict) else bids[0]
    else:
        raw = getattr(book, "best_bid", None)
    return _dec(raw)


def tick_of(book: Any) -> Decimal:
    if isinstance(book, dict):
        return _dec(book.get("tick_size"), "0.01") or Decimal("0.01")
    return _dec(getattr(book, "tick_size", None), "0.01") or Decimal("0.01")


def list_no_buckets(
    rules: list[dict[str, Any]],
    city_id: str,
    market_local_date: str,
    direction: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for rule in rules:
        if not (
            rule.get("enabled", True) is True
            and rule.get("city_id") == city_id
            and rule.get("market_local_date") == market_local_date
            and rule.get("direction") == direction
        ):
            continue
        for bucket in rule.get("buckets") or []:
            if not isinstance(bucket, dict):
                continue
            # Enrich from parent rule if needed
            merged = dict(bucket)
            if not no_token_id(merged) and rule.get("no_token_id"):
                merged["no_token_id"] = rule.get("no_token_id")
            token = no_token_id(merged)
            if not token:
                continue
            merged["_rule"] = rule
            merged["_no_token_id"] = token
            merged["_direction"] = direction
            merged["_city_id"] = city_id
            merged["_market_local_date"] = market_local_date
            out.append(merged)
    return out


def consensus_top3_token_ids(
    buckets: list[dict[str, Any]],
    books_by_token: dict[str, Any],
) -> set[str]:
    """Exclude the three cheapest distinct ask levels (same price ties excluded together)."""
    ranked: list[tuple[Decimal, str]] = []
    for bucket in buckets:
        token = bucket["_no_token_id"]
        ask = best_ask_of(books_by_token.get(token))
        if ask is None:
            continue
        ranked.append((ask, token))
    if not ranked:
        return set()
    ranked.sort(key=lambda row: (row[0], row[1]))
    excluded: set[str] = set()
    distinct_levels = 0
    i = 0
    while i < len(ranked) and distinct_levels < 3:
        level = ranked[i][0]
        while i < len(ranked) and ranked[i][0] == level:
            excluded.add(ranked[i][1])
            i += 1
        distinct_levels += 1
    return excluded


# Backward-compatible alias (tests / callers that still import old name)
def consensus_top2_token_ids(
    buckets: list[dict[str, Any]],
    books_by_token: dict[str, Any],
) -> set[str]:
    return consensus_top3_token_ids(buckets, books_by_token)


def taf_forbidden_bucket_ids(
    state: dict[str, Any],
    city: dict[str, Any],
    market_local_date: str,
    direction: str,
    rules: list[dict[str, Any]],
) -> set[str]:
    tree = ensure_tree12_state(state)
    forbidden: set[str] = set()
    for forecast in tree.get("taf_forecasts", {}).values():
        if not isinstance(forecast, dict):
            continue
        if (
            forecast.get("city_id") != city["city_id"]
            or forecast.get("market_local_date") != market_local_date
            or forecast.get("direction") != direction
        ):
            continue
        value = float(forecast["value_native"])
        for rule in rules:
            if not (
                rule.get("city_id") == city["city_id"]
                and rule.get("market_local_date") == market_local_date
                and rule.get("direction") == direction
            ):
                continue
            for bucket in rule.get("buckets") or []:
                if bucket_contains(bucket, value):
                    bid = str(bucket.get("bucket_id") or bucket.get("id") or "")
                    if bid:
                        forbidden.add(bid)
    return forbidden


def bucket_hit_by_observation(bucket: dict[str, Any], observed_temp: float | None) -> bool:
    if observed_temp is None:
        return False
    return bucket_contains(bucket, float(observed_temp))


def _position_shares(tree: dict[str, Any], key: str) -> Decimal:
    pos = tree["positions"].get(key) or {}
    return _dec(pos.get("shares"), "0") or ZERO


def start_tree12_exit_chase(
    state: dict[str, Any],
    key: str,
    token_id: str,
    shares: Decimal,
    trigger: str,
    now_utc: datetime,
) -> dict[str, Any] | None:
    """Start SELL NO FAK ladder (paper: planned only; live needs reconciliation)."""
    tree = ensure_tree12_state(state)
    existing = tree["exit_chases"].get(key)
    if isinstance(existing, dict) and existing.get("status") in {"active", "awaiting_reconciliation"}:
        return None
    if shares <= ZERO:
        return None
    chase = {
        "key": key,
        "order_id": str(tree["positions"].get(key, {}).get("order_id") or ""),
        "status": "active",
        "trigger": trigger,
        "token_id": str(token_id),
        "side": "SELL",
        "outcome": "NO",
        "order_type": "FAK",
        "remaining_shares": str(shares),
        "triggered_at_utc": _iso(now_utc),
        "attempt_index": 0,
        "attempts": [],
        "next_attempt_utc": _iso(now_utc),
    }
    tree["exit_chases"][key] = chase
    return chase



def settle_tree12_paper_exit(
    state: dict[str, Any],
    key: str,
    sale_price: Decimal,
    shares: Decimal,
    now_utc: datetime,
    *,
    reason: str = "paper_fak_fill",
) -> dict[str, Any] | None:
    """Paper-only: realize exit, credit cash, remove position, mark chase settled.

    Live path must use real FAK fills + reconciliation instead of this helper.
    """
    tree = ensure_tree12_state(state)
    pos = tree["positions"].get(key)
    if not isinstance(pos, dict):
        return None
    shares = shares if shares > ZERO else (_dec(pos.get("shares"), "0") or ZERO)
    if shares <= ZERO:
        tree["positions"].pop(key, None)
        return None
    avg = _dec(pos.get("avg_price"), "0") or ZERO
    sale_price = max(sale_price, ZERO)
    proceeds = (sale_price * shares).quantize(Decimal("0.00001"))
    pnl = ((sale_price - avg) * shares).quantize(Decimal("0.0001"))
    release(state, proceeds)
    prior_realized = _dec(pos.get("realized_pnl_usdc"), "0") or ZERO
    pos["realized_pnl_usdc"] = str((prior_realized + pnl).quantize(Decimal("0.0001")))
    pos["shares"] = "0"
    tree["positions"].pop(key, None)
    chase = tree["exit_chases"].get(key)
    if isinstance(chase, dict):
        chase["status"] = "settled"
        chase["settled_at_utc"] = _iso(now_utc)
        chase["sale_price"] = str(sale_price)
        chase["realized_pnl_usdc"] = str(pnl)
        chase["proceeds_usdc"] = str(proceeds)
        chase["remaining_shares"] = "0"
        chase["settle_reason"] = reason
    return {
        "key": key,
        "sale_price": sale_price,
        "shares": shares,
        "proceeds": proceeds,
        "realized_pnl_usdc": pnl,
        "avg_price": avg,
    }


def plan_tree12_due_exit_faks(
    state: dict[str, Any],
    books_by_token: dict[str, Any],
    now_utc: datetime,
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Exit ladder with paper settlement: mild slippage, short retries, hard floor.

    Paper mode immediately settles on first usable bid (conservative limit price),
    releases capital, realizes PnL, and removes the position so equity is truthful.
    Live mode still only plans FAK attempts and waits for reconciliation.
    """
    config = config or {}
    tree = ensure_tree12_state(state)
    mode = str(config.get("mode", "paper")).lower().strip()
    paper_mode = mode in {"paper", "observe"}
    seconds = tuple(int(x) for x in config.get("tree12_exit_retry_seconds", TREE12_DEFAULT_EXIT_RETRY_SECONDS))
    slippages = tuple(
        Decimal(str(x)) for x in config.get("tree12_exit_slippage", [str(s) for s in TREE12_DEFAULT_EXIT_SLIPPAGE])
    )
    min_price = _dec(config.get("tree12_exit_min_price"), str(TREE12_EXIT_HARD_FLOOR)) or TREE12_EXIT_HARD_FLOOR
    hard_floor = _dec(config.get("tree12_exit_hard_floor"), str(TREE12_EXIT_HARD_FLOOR)) or TREE12_EXIT_HARD_FLOOR
    if min_price < hard_floor:
        min_price = hard_floor
    actions: list[dict[str, Any]] = []
    for key, chase in list(tree["exit_chases"].items()):
        if not isinstance(chase, dict) or chase.get("status") != "active":
            continue
        next_at = parse_utc(chase.get("next_attempt_utc"))
        if next_at is not None and now_utc < next_at:
            continue
        index = int(chase.get("attempt_index") or 0)
        if index >= len(seconds):
            # Exhausted ladder: in paper, force-settle at hard floor so books stay honest
            shares = _dec(chase.get("remaining_shares"), "0") or ZERO
            if paper_mode and shares > ZERO:
                settled = settle_tree12_paper_exit(
                    state, key, hard_floor, shares, now_utc, reason="ladder_exhausted_hard_floor"
                )
                actions.append({
                    "action_type": "tree12_exit_fak",
                    "status": "paper_settled_hard_floor",
                    "key": key,
                    "sale_price": str(hard_floor),
                    "requested_shares": str(shares),
                    "realized_pnl_usdc": str(settled["realized_pnl_usdc"]) if settled else None,
                    "proceeds_usdc": str(settled["proceeds"]) if settled else None,
                    "trigger": chase.get("trigger"),
                })
            else:
                chase["status"] = "awaiting_reconciliation"
                chase["next_attempt_utc"] = None
            continue
        token = str(chase.get("token_id"))
        book = books_by_token.get(token)
        bid = best_bid_of(book)
        tick = tick_of(book)
        shares = _dec(chase.get("remaining_shares"), "0") or ZERO
        if shares <= ZERO:
            chase["status"] = "done"
            tree["positions"].pop(key, None)
            continue
        if bid is None or bid <= ZERO:
            attempt = {
                "attempt_index": index,
                "status": "skipped_no_bid",
                "at_utc": _iso(now_utc),
            }
            chase["attempts"].append(attempt)
            actions.append({
                "action_type": "tree12_exit_fak",
                "status": "planned_skip_no_bid",
                "key": key,
                **attempt,
            })
        else:
            slip = slippages[min(index, len(slippages) - 1)]
            limit = bid * (Decimal("1") - slip)
            if tick and tick > 0:
                limit = (limit / tick).to_integral_value(rounding=ROUND_DOWN) * tick
            if limit < min_price:
                limit = min_price
            # If market already dead, do not pretend deeper discounts help
            if bid <= hard_floor:
                limit = hard_floor
            attempt = {
                "attempt_index": index,
                "status": "planned_observe_only" if not paper_mode else "paper_settled",
                "at_utc": _iso(now_utc),
                "best_bid": str(bid),
                "slippage": str(slip),
                "limit_price": str(limit),
                "requested_shares": str(shares),
            }
            chase["attempts"].append(attempt)
            action: dict[str, Any] = {
                "action_type": "tree12_exit_fak",
                "status": attempt["status"],
                "key": key,
                "order_id": chase.get("order_id"),
                "token_id": token,
                "side": "SELL",
                "outcome": "NO",
                "order_type": "FAK",
                "limit_price": str(limit),
                "requested_shares": str(shares),
                "trigger": chase.get("trigger"),
                **attempt,
            }
            pos = tree["positions"].get(key) or {}
            avg_price = _dec(pos.get("avg_price"))
            if avg_price is not None and avg_price > 0:
                action["position_avg_price"] = str(avg_price)
                action["estimated_realized_pnl_usdc"] = str(
                    realized_pnl_for_exit(avg_price, limit, shares).quantize(Decimal("0.0001"))
                )
            if paper_mode:
                settled = settle_tree12_paper_exit(
                    state, key, limit, shares, now_utc, reason=str(chase.get("trigger") or "paper_fak")
                )
                if settled:
                    action["status"] = "paper_settled"
                    action["sale_price"] = str(settled["sale_price"])
                    action["realized_pnl_usdc"] = str(settled["realized_pnl_usdc"])
                    action["proceeds_usdc"] = str(settled["proceeds"])
                    chase["remaining_shares"] = "0"
            actions.append(action)
            if paper_mode:
                # Settled on this attempt; do not schedule further retries
                continue
        chase["attempt_index"] = index + 1
        triggered = parse_utc(chase.get("triggered_at_utc")) or now_utc
        if index + 1 < len(seconds):
            chase["next_attempt_utc"] = _iso(triggered + timedelta(seconds=seconds[index + 1]))
        else:
            if paper_mode:
                shares_left = _dec(chase.get("remaining_shares"), "0") or ZERO
                if shares_left > ZERO:
                    settled = settle_tree12_paper_exit(
                        state, key, hard_floor, shares_left, now_utc, reason="ladder_end_hard_floor"
                    )
                    actions.append({
                        "action_type": "tree12_exit_fak",
                        "status": "paper_settled_hard_floor",
                        "key": key,
                        "sale_price": str(hard_floor),
                        "requested_shares": str(shares_left),
                        "realized_pnl_usdc": str(settled["realized_pnl_usdc"]) if settled else None,
                        "proceeds_usdc": str(settled["proceeds"]) if settled else None,
                        "trigger": chase.get("trigger"),
                    })
            else:
                chase["status"] = "awaiting_reconciliation"
                chase["next_attempt_utc"] = None
    return actions


def due_tree12_exit_token_ids(state: dict[str, Any], now_utc: datetime) -> set[str]:
    tree = ensure_tree12_state(state)
    tokens: set[str] = set()
    for chase in tree["exit_chases"].values():
        if not isinstance(chase, dict) or chase.get("status") != "active":
            continue
        next_at = parse_utc(chase.get("next_attempt_utc"))
        if next_at is not None and now_utc < next_at:
            continue
        token = chase.get("token_id")
        if token:
            tokens.add(str(token))
    return tokens


def plan_tree12_entries(
    state: dict[str, Any],
    cities: dict[str, dict[str, Any]],
    rules: list[dict[str, Any]],
    books_by_token: dict[str, Any],
    now_utc: datetime,
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    config = config or {}
    tree = ensure_tree12_state(state)
    actions: list[dict[str, Any]] = []
    target = _dec(config.get("target_order_shares"), str(TREE12_TARGET_SHARES)) or TREE12_TARGET_SHARES

    for city in cities.values():
        dates = sorted({
            str(rule.get("market_local_date"))
            for rule in rules
            if rule.get("city_id") == city["city_id"] and rule.get("market_local_date")
        })
        for local_date in dates:
            if not allow_new_entries(city, local_date, now_utc):
                actions.append({
                    "action_type": "tree12_entry_window",
                    "status": "blocked_inside_24h_lead",
                    "city_id": city["city_id"],
                    "market_local_date": local_date,
                    "hours_before_local_day": round(hours_before_local_day(city, local_date, now_utc), 3),
                })
                continue
            for direction in ("high", "low"):
                buckets = list_no_buckets(rules, city["city_id"], local_date, direction)
                if not buckets:
                    continue
                top3 = consensus_top3_token_ids(buckets, books_by_token)
                forbidden = taf_forbidden_bucket_ids(state, city, local_date, direction, rules)
                for bucket in buckets:
                    token = bucket["_no_token_id"]
                    bid = str(bucket.get("bucket_id") or bucket.get("id") or "")
                    key = position_key(city["city_id"], local_date, direction, bid)
                    ask = best_ask_of(books_by_token.get(token))
                    tick = tick_of(books_by_token.get(token))
                    if ask is not None:
                        record_ws_ask_sample(state, token, ask, Decimal("1"), now_utc)
                    pos_shares = _position_shares(tree, key)
                    working = tree["working_orders"].get(key) or {}
                    need = target - pos_shares
                    if need <= 0:
                        continue
                    if bid in forbidden:
                        actions.append({"action_type": "tree12_entry", "status": "blocked_taf_predicted_bucket", "key": key})
                        continue
                    if token in top3:
                        actions.append({"action_type": "tree12_entry", "status": "blocked_consensus_top3", "key": key, "best_ask": str(ask) if ask else None})
                        continue
                    if ask is None or ask < TREE12_MIN_NO_ASK or ask > TREE12_MAX_NO_ASK:
                        actions.append({"action_type": "tree12_entry", "status": "blocked_no_ask_or_outside_085_095", "key": key, "best_ask": str(ask) if ask else None})
                        continue
                    limit = hybrid_limit_price(state, token, ask, tick, now_utc)
                    if limit < TREE12_MIN_NO_ASK or limit > TREE12_MAX_NO_ASK:
                        actions.append({"action_type": "tree12_entry", "status": "blocked_limit_outside_085_095", "key": key, "limit": str(limit)})
                        continue
                    paper_mode = config.get("mode") in {"paper", "observe"}
                    estimated_debit = _tree12_estimated_debit(need, limit)
                    if paper_mode and remaining_capital_usdc(state) < estimated_debit:
                        actions.append({"action_type": "tree12_entry", "status": "blocked_insufficient_capital", "key": key,
                                        "required_debit_usdc": str(estimated_debit), "remaining_capital_usdc": str(remaining_capital_usdc(state))})
                        continue
                    if working and working.get("status") == "working_gtc_buy_no":
                        old_limit = _dec(working.get("limit_price"))
                        if old_limit is not None and abs(old_limit - limit) >= tick * TREE12_REQUOTE_TICKS:
                            actions.append({
                                "action_type": "tree12_requote",
                                "status": "planned_cancel_replace",
                                "key": key,
                                "old_limit": str(old_limit),
                                "new_limit": str(limit),
                            })
                            working["limit_price"] = str(limit)
                            working["updated_at_utc"] = _iso(now_utc)
                            tree["working_orders"][key] = working
                        continue
                    order = {
                        "key": key,
                        "order_id": new_order_id(),
                        "status": "working_gtc_buy_no",
                        "city_id": city["city_id"],
                        "icao": city.get("icao"),
                        "market_local_date": local_date,
                        "direction": direction,
                        "bucket_id": bid,
                        "lo": bucket.get("lo"),
                        "hi": bucket.get("hi"),
                        "token_id": token,
                        "side": "BUY",
                        "outcome": "NO",
                        "order_type": "GTC",
                        "target_shares": str(target),
                        "remaining_shares": str(need),
                        "limit_price": str(limit),
                        "reference_best_ask": str(ask),
                        "created_at_utc": _iso(now_utc),
                        "updated_at_utc": _iso(now_utc),
                    }
                    tree["working_orders"][key] = order
                    actions.append({"action_type": "tree12_submit_entry", "status": "planned_observe_only", **order})
                    if paper_mode:
                        actions.append(tree12_paper_fill(state, key, need, books_by_token.get(token), now_utc))
    tree["last_scan_utc"] = _iso(now_utc)
    return actions


def manage_open_orders_inside_lead_window(
    state: dict[str, Any],
    cities: dict[str, dict[str, Any]],
    rules: list[dict[str, Any]],
    books_by_token: dict[str, Any],
    now_utc: datetime,
) -> list[dict[str, Any]]:
    tree = ensure_tree12_state(state)
    actions: list[dict[str, Any]] = []
    for key, order in list(tree["working_orders"].items()):
        if not isinstance(order, dict) or order.get("status") != "working_gtc_buy_no":
            continue
        city = next((c for c in cities.values() if c["city_id"] == order.get("city_id")), None)
        if city is None:
            continue
        local_date = str(order.get("market_local_date"))
        if allow_new_entries(city, local_date, now_utc):
            continue
        token = str(order.get("token_id"))
        ask = best_ask_of(books_by_token.get(token))
        tick = tick_of(books_by_token.get(token))
        direction = str(order.get("direction"))
        bid = str(order.get("bucket_id"))
        buckets = list_no_buckets(rules, city["city_id"], local_date, direction)
        top3 = consensus_top3_token_ids(buckets, books_by_token)
        forbidden = taf_forbidden_bucket_ids(state, city, local_date, direction, rules)
        if bid in forbidden or token in top3 or ask is None or ask < TREE12_MIN_NO_ASK or ask > TREE12_MAX_NO_ASK:
            order["status"] = "cancelled_filter_break"
            order["updated_at_utc"] = _iso(now_utc)
            actions.append({"action_type": "tree12_cancel", "status": "planned_cancel_filter_break", "key": key})
            continue
        limit = hybrid_limit_price(state, token, ask, tick, now_utc)
        old_limit = _dec(order.get("limit_price"))
        if old_limit is not None and abs(old_limit - limit) >= tick * TREE12_REQUOTE_TICKS:
            actions.append({
                "action_type": "tree12_requote",
                "status": "planned_cancel_replace",
                "key": key,
                "old_limit": str(old_limit),
                "new_limit": str(limit),
            })
            order["limit_price"] = str(limit)
            order["updated_at_utc"] = _iso(now_utc)
    return actions


def plan_tree12_exits_from_metar(
    state: dict[str, Any],
    city: dict[str, Any],
    market_local_date: str,
    observed_temp_native: float | None,
    now_utc: datetime,
) -> list[dict[str, Any]]:
    tree = ensure_tree12_state(state)
    actions: list[dict[str, Any]] = []
    if observed_temp_native is None:
        return actions
    for key, pos in list(tree["positions"].items()):
        if not isinstance(pos, dict):
            continue
        if pos.get("city_id") != city["city_id"] or pos.get("market_local_date") != market_local_date:
            continue
        shares = _dec(pos.get("shares"), "0") or ZERO
        if shares <= ZERO:
            continue
        bucket = pos.get("bucket") or {}
        if not bucket_hit_by_observation(bucket, observed_temp_native):
            continue
        token = str(pos.get("token_id"))
        chase = start_tree12_exit_chase(state, key, token, shares, "metar_hit_no_bucket", now_utc)
        if chase is None:
            continue
        working = tree["working_orders"].get(key)
        if working and working.get("status") == "working_gtc_buy_no":
            working["status"] = "cancelled_metar_exit"
            actions.append({"action_type": "tree12_cancel", "status": "planned_cancel_for_exit", "key": key})
        actions.append({
            "action_type": "tree12_exit",
            "status": "chase_started",
            "key": key,
            "trigger": "metar_hit_no_bucket",
            "token_id": token,
            "shares": str(shares),
            "observed_temp_native": observed_temp_native,
        })
    return actions


def plan_tree12_exits_from_taf_revision(
    state: dict[str, Any],
    cities: dict[str, dict[str, Any]],
    rules: list[dict[str, Any]],
    now_utc: datetime,
) -> list[dict[str, Any]]:
    tree = ensure_tree12_state(state)
    actions: list[dict[str, Any]] = []
    for key, pos in list(tree["positions"].items()):
        if not isinstance(pos, dict):
            continue
        shares = _dec(pos.get("shares"), "0") or ZERO
        if shares <= ZERO:
            continue
        city = next((c for c in cities.values() if c["city_id"] == pos.get("city_id")), None)
        if city is None:
            continue
        local_date = str(pos.get("market_local_date"))
        direction = str(pos.get("direction"))
        forbidden = taf_forbidden_bucket_ids(state, city, local_date, direction, rules)
        bid = str(pos.get("bucket_id"))
        if bid not in forbidden:
            continue
        token = str(pos.get("token_id"))
        chase = start_tree12_exit_chase(state, key, token, shares, "taf_revision_predicted_bucket", now_utc)
        if chase is None:
            continue
        working = tree["working_orders"].get(key)
        if working and working.get("status") == "working_gtc_buy_no":
            working["status"] = "cancelled_taf_exit"
            actions.append({"action_type": "tree12_cancel", "status": "planned_cancel_for_exit", "key": key})
        actions.append({
            "action_type": "tree12_exit",
            "status": "chase_started",
            "key": key,
            "trigger": "taf_revision_predicted_bucket",
            "token_id": token,
            "shares": str(shares),
        })
    return actions


def _tree12_estimated_debit(shares: Decimal, price: Decimal) -> Decimal:
    fee = shares * TREE12_PAPER_FEE_RATE * price * (Decimal("1") - price)
    return shares * price + fee


def tree12_paper_fill(
    state: dict[str, Any],
    key: str,
    shares: Decimal,
    book: Any,
    now_utc: datetime,
) -> dict[str, Any]:
    """Simulate a tree12 NO GTC fill against real L2 ask depth (paper only).

    The hybrid limit is the ceiling; fills walk the actual visible ask levels
    (price-time priority), so a thin book produces a partial fill and the
    remainder stays as a resting GTC order instead of inventing a full fill.
    Cash is reserved from the shared 1000-USDC paper account.
    """
    tree = ensure_tree12_state(state)
    order = tree["working_orders"].get(key)
    if not isinstance(order, dict) or order.get("status") != "working_gtc_buy_no":
        return {"action_type": "tree12_paper_fill", "status": "no_working_order", "key": key,
                "order_id": order.get("order_id") if isinstance(order, dict) else None}
    shares = _dec(shares, "0") or ZERO
    limit_price = _dec(order.get("limit_price"))
    best_ask = best_ask_of(book)
    if shares <= ZERO or limit_price is None or limit_price <= ZERO:
        return {"action_type": "tree12_paper_fill", "status": "invalid_fill", "key": key,
                "order_id": order.get("order_id")}
    if best_ask is None or best_ask > limit_price:
        return {
            "action_type": "tree12_paper_fill", "status": "resting_above_limit", "key": key,
            "order_id": order.get("order_id"),
            "token_id": order.get("token_id"), "limit_price": str(limit_price),
            "best_ask": str(best_ask) if best_ask is not None else None,
        }
    book_view = from_any(book, token_id=order.get("token_id"))
    intent = OrderIntent.new(
        token_id=order.get("token_id"), side=Side.BUY, price=limit_price,
        quantity=shares, order_type=OrderType.GTC, strategy="tree12",
        signal_reason="allno", order_id=order.get("order_id"),
    )
    match = match_gtc(intent, book_view, fee_rate=TREE12_PAPER_FEE_RATE)
    filled = match.filled_shares
    if filled <= ZERO:
        return {"action_type": "tree12_paper_fill", "status": "resting_no_depth", "key": key,
                "order_id": order.get("order_id"),
                "token_id": order.get("token_id"), "limit_price": str(limit_price)}
    avg = match.average_price or best_ask
    total_debit = match.principal_usdc + match.fee_usdc
    if reserve(state, total_debit) is None:
        order["status"] = "blocked_insufficient_capital"
        order["updated_at_utc"] = _iso(now_utc)
        tree["working_orders"][key] = order
        return {
            "action_type": "tree12_paper_fill", "status": "blocked_insufficient_capital", "key": key,
            "order_id": order.get("order_id"),
            "token_id": order.get("token_id"), "required_debit_usdc": str(total_debit),
            "remaining_capital_usdc": str(remaining_capital_usdc(state)),
        }
    result = paper_fill_working_order(state, key, filled, avg, now_utc)
    result["action_type"] = "tree12_paper_fill"
    result["order_id"] = order.get("order_id")
    result["match"] = match.as_dict()
    result["principal_usdc"] = str(match.principal_usdc)
    result["estimated_fee_usdc"] = str(match.fee_usdc)
    result["total_debit_usdc"] = str(total_debit)
    result["remaining_capital_usdc"] = str(remaining_capital_usdc(state))
    return result


def paper_fill_working_order(
    state: dict[str, Any],
    key: str,
    fill_shares: Decimal,
    fill_price: Decimal,
    now_utc: datetime,
) -> dict[str, Any]:
    tree = ensure_tree12_state(state)
    order = tree["working_orders"].get(key)
    if not order:
        return {"status": "no_working_order", "key": key}
    remaining = _dec(order.get("remaining_shares"), "0") or ZERO
    filled = min(remaining, fill_shares)
    if filled <= 0:
        return {"status": "nothing_to_fill", "key": key}
    order["remaining_shares"] = str(remaining - filled)
    order["updated_at_utc"] = _iso(now_utc)
    if _dec(order["remaining_shares"]) == 0:
        order["status"] = "filled"
    pos = tree["positions"].setdefault(key, {
        "key": key,
        "order_id": order.get("order_id"),
        "city_id": order.get("city_id"),
        "market_local_date": order.get("market_local_date"),
        "direction": order.get("direction"),
        "bucket_id": order.get("bucket_id"),
        "token_id": order.get("token_id"),
        "bucket": {
            "bucket_id": order.get("bucket_id"),
            "lo": order.get("lo"),
            "hi": order.get("hi"),
        },
        "shares": "0",
    })
    for field in ("lo", "hi"):
        if order.get(field) is not None:
            pos.setdefault("bucket", {})[field] = order.get(field)
    prev_shares = _dec(pos.get("shares"), "0") or ZERO
    prev_avg = _dec(pos.get("avg_price"), "0") or ZERO
    total_shares = prev_shares + filled
    weighted_avg = (prev_avg * prev_shares + fill_price * filled) / total_shares
    pos["shares"] = str(total_shares)
    pos["avg_price"] = str(weighted_avg)
    pos["updated_at_utc"] = _iso(now_utc)
    # Minimal PnL snapshot: cost basis + realized (0 on entry fills).
    cost_basis = weighted_avg * total_shares
    pos["cost_basis_usdc"] = str(cost_basis)
    pos["realized_pnl_usdc"] = "0"
    return {
        "status": "paper_filled", "key": key, "filled": str(filled),
        "position_shares": pos["shares"],
        "position_avg_price": pos["avg_price"],
        "position_cost_basis_usdc": pos["cost_basis_usdc"],
        "position_realized_pnl_usdc": pos["realized_pnl_usdc"],
    }


def collect_tree12_book_token_ids(state: dict[str, Any], rules: list[dict[str, Any]], cities: dict[str, dict[str, Any]], now_utc: datetime) -> set[str]:
    """Tokens needed for entry planning + active exits."""
    tokens = due_tree12_exit_token_ids(state, now_utc)
    for city in cities.values():
        dates = sorted({
            str(rule.get("market_local_date"))
            for rule in rules
            if rule.get("city_id") == city["city_id"] and rule.get("market_local_date")
        })
        for local_date in dates:
            # tree12 only opens new buckets > 24h before the local day. Fetching
            # books for already-inside-the-window days is wasteful and, with a
            # 49-city x multi-day universe, makes the CLOB batch request too
            # large to complete through a proxy.
            if not allow_new_entries(city, local_date, now_utc):
                continue
            for direction in ("high", "low"):
                for bucket in list_no_buckets(rules, city["city_id"], local_date, direction):
                    tokens.add(bucket["_no_token_id"])
    # Also working order tokens
    tree = ensure_tree12_state(state)
    for order in tree["working_orders"].values():
        if isinstance(order, dict) and order.get("token_id"):
            tokens.add(str(order["token_id"]))
    return tokens


def run_tree12_cycle(
    state: dict[str, Any],
    cities: dict[str, dict[str, Any]],
    rules: list[dict[str, Any]],
    books_by_token: dict[str, Any],
    now_utc: datetime,
    config: dict[str, Any] | None = None,
    observations_by_city: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    actions.extend(plan_tree12_exits_from_taf_revision(state, cities, rules, now_utc))
    if observations_by_city:
        for city in cities.values():
            temp = observations_by_city.get(city["city_id"])
            dates = sorted({
                str(r.get("market_local_date"))
                for r in rules
                if r.get("city_id") == city["city_id"] and r.get("market_local_date")
            })
            for local_date in dates:
                actions.extend(plan_tree12_exits_from_metar(state, city, local_date, temp, now_utc))
    actions.extend(plan_tree12_due_exit_faks(state, books_by_token, now_utc, config))
    actions.extend(manage_open_orders_inside_lead_window(state, cities, rules, books_by_token, now_utc))
    actions.extend(plan_tree12_entries(state, cities, rules, books_by_token, now_utc, config))
    return actions
