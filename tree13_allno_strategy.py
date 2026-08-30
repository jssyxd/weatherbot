"""Deterministic, paper-only all-NO weather-market strategy for tree13-allno.

This module never loads credentials, signs orders, calls the CLOB, or assumes a
fill. It consumes explicit market snapshots and weather events and emits
idempotent, auditable intents.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any

from edge_engine import local_market_date, parse_utc
from tree5_strategy import bucket_contains, parse_taf_extremes_for_local_day, select_forecast_bucket, tree5_day_key
from paper_capital import remaining_capital_usdc, reserve, release

SCHEMA_VERSION = "1.0"
ENTRY_SHARES = Decimal("5")
ENTRY_MIN_ASK = Decimal("0.85")
ENTRY_MAX_LIMIT = Decimal("0.98")
ENTRY_ASK_DISCOUNT = Decimal("0.95")
CONSENSUS_EXCLUDED = 3  # exclude cheapest 3 ask levels
ENTRY_LEAD_HOURS_MIN = 18
ENTRY_LEAD_HOURS_MAX = 30


class AllNoInputError(ValueError):
    pass

def _decimal(value: Any, name: str, *, nonnegative: bool = True) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise AllNoInputError(f"invalid_{name}") from exc
    if not result.is_finite() or (nonnegative and result < 0):
        raise AllNoInputError(f"invalid_{name}")
    return result

def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

def _hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]

def ensure_allno_state(state: dict[str, Any]) -> dict[str, Any]:
    tree = state.setdefault("tree13_allno", {})
    if not isinstance(tree, dict):
        raise AllNoInputError("tree13_allno_state_must_be_object")
    for key, default in (("taf_versions", {}), ("daily_extrema", {}), ("orders", {}), ("positions", {}), ("processed_event_ids", {}), ("closed_positions", {}), ("exit_chases", {}), ("realized_pnl_events", {})):
        tree.setdefault(key, default)
        if not isinstance(tree[key], dict):
            raise AllNoInputError(f"tree13_{key}_must_be_object")
    return tree

def _market_key(city: dict[str, Any], local_date: str, direction: str) -> str:
    return f"{tree5_day_key(city, local_date)}|{direction}"

def record_taf_version(state: dict[str, Any], *, city: dict[str, Any], report: dict[str, Any], visible_at_utc: datetime, visible_at_monotonic_ns: int) -> list[dict[str, Any]]:
    if visible_at_monotonic_ns <= 0:
        raise AllNoInputError("taf_visible_monotonic_ns_required")
    tree = ensure_allno_state(state)
    local_date = local_market_date(visible_at_utc, city)
    parsed = parse_taf_extremes_for_local_day(report.get("raw_text"), report.get("issued"), city, local_date)
    actions = []
    for direction, forecast in parsed.items():
        key = _market_key(city, local_date, direction)
        version = {"city_id": city["city_id"], "icao": city["icao"], "market_local_date": local_date, "direction": direction, "taf_issued_utc": forecast["issued_utc"], "forecast_time_utc": forecast["forecast_time_utc"], "value_native": forecast["value_native"], "value_c": forecast["value_c"], "raw_group": forecast["raw_group"], "raw_taf": str(report.get("raw_text") or ""), "visible_at_utc": _iso(visible_at_utc), "visible_at_monotonic_ns": visible_at_monotonic_ns, "amd_or_cor": any(x in str(report.get("raw_text") or "").upper() for x in (" AMD", " COR"))}
        version["taf_version_id"] = _hash(version)
        versions = tree["taf_versions"].setdefault(key, [])
        if not any(isinstance(v, dict) and v.get("taf_version_id") == version["taf_version_id"] for v in versions):
            versions.append(version)
            versions.sort(key=lambda v: int(v.get("visible_at_monotonic_ns", 0)))
            actions.append({"action_type": "tree13_taf_update", "status": "recorded", **version})
        else:
            actions.append({"action_type": "tree13_taf_update", "status": "duplicate", "taf_version_id": version["taf_version_id"], "market_key": key})
    return actions

def latest_taf(state: dict[str, Any], city: dict[str, Any], local_date: str, direction: str, now_monotonic_ns: int) -> dict[str, Any] | None:
    versions = ensure_allno_state(state)["taf_versions"].get(_market_key(city, local_date, direction), [])
    eligible = [v for v in versions if int(v.get("visible_at_monotonic_ns", 0)) <= now_monotonic_ns]
    return max(eligible, key=lambda v: int(v.get("visible_at_monotonic_ns", 0))) if eligible else None

def update_daily_extreme(state: dict[str, Any], *, city: dict[str, Any], report_time_utc: datetime, temperature_native: Any) -> tuple[dict[str, float], bool]:
    tree = ensure_allno_state(state)
    value = float(_decimal(temperature_native, "temperature_native", nonnegative=False))
    key = tree5_day_key(city, local_market_date(report_time_utc, city))
    old = tree["daily_extrema"].get(key)
    if old is None:
        tree["daily_extrema"][key] = {"high": value, "low": value, "last_report_time_utc": _iso(report_time_utc)}
        return {"high": value, "low": value}, True
    previous = dict(old)
    changed = value > float(old["high"]) or value < float(old["low"])
    old["high"] = max(float(old["high"]), value)
    old["low"] = min(float(old["low"]), value)
    old["last_report_time_utc"] = _iso(report_time_utc)
    return {"high": float(old["high"]), "low": float(old["low"])}, changed

def _twap_ask(snapshots: list[dict[str, Any]], now_ns: int, lookback_seconds: int = 21600) -> Decimal | None:
    start = now_ns - lookback_seconds * 1_000_000_000
    rows = []
    for row in snapshots:
        ts = int(row.get("monotonic_ns", 0))
        ask = row.get("best_ask")
        if start <= ts <= now_ns and ask is not None:
            rows.append((ts, _decimal(ask, "best_ask")))
    rows.sort()
    if len(rows) < 2:
        return None
    weighted = Decimal(0); duration = 0
    for (t0, p0), (t1, _) in zip(rows, rows[1:]):
        dt = max(0, t1 - t0)
        weighted += p0 * dt; duration += dt
    if duration <= 0:
        return None
    return weighted / duration

def _floor_tick(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_DOWN) * tick

def conservative_limit(best_ask: Any, twap_ask: Any, tick_size: Any) -> Decimal | None:
    ask = _decimal(best_ask, "best_ask"); twap = _decimal(twap_ask, "twap_ask"); tick = _decimal(tick_size, "tick_size")
    if ask < ENTRY_MIN_ASK or tick <= 0:
        return None
    value = min(ENTRY_ASK_DISCOUNT * ask, (twap + ask) / 2)
    value = min(value, ENTRY_MAX_LIMIT)
    result = _floor_tick(value, tick)
    return result if result >= ENTRY_MIN_ASK else None

def plan_entries(*, state: dict[str, Any], city: dict[str, Any], local_date: str, direction: str, rules: list[dict[str, Any]], books: dict[str, dict[str, Any]], ask_history: dict[str, list[dict[str, Any]]], now_monotonic_ns: int, taf_required: bool = False, max_usdc: Decimal = Decimal("40"), global_reserved_usdc: Decimal = Decimal("0"), global_max_usdc: Decimal = Decimal("1000")) -> list[dict[str, Any]]:
    tree = ensure_allno_state(state); rule = next((r for r in rules if r.get("city_id") == city["city_id"] and r.get("market_local_date") == local_date and r.get("direction") == direction), None)
    if not rule or not rule.get("buckets"):
        return [{"action_type": "tree13_entry", "status": "blocked_missing_market_rules", "city_id": city["city_id"], "market_local_date": local_date, "direction": direction}]
    taf = latest_taf(state, city, local_date, direction, now_monotonic_ns)
    if taf_required and taf is None:
        return [{"action_type": "tree13_entry", "status": "blocked_no_taf"}]
    quotes = []
    for bucket in rule["buckets"]:
        token = str(bucket.get("no_token_id") or "")
        book = books.get(token, {})
        ask = book.get("best_ask")
        if token and ask is not None:
            quotes.append((token, _decimal(ask, "best_ask"), bucket, book))
    if len(quotes) < len(rule["buckets"]):
        return [{"action_type": "tree13_entry", "status": "blocked_incomplete_market_coverage", "covered": len(quotes), "total": len(rule["buckets"])}]
    excluded = {token for token, _, _, _ in sorted(quotes, key=lambda x: x[1])[:CONSENSUS_EXCLUDED]}
    reserved = _decimal(global_reserved_usdc, "global_reserved_usdc")
    actions = []
    for token, ask, bucket, book in quotes:
        key = f"{rule.get('market_rule_id')}|{bucket.get('bucket_id')}"
        if key in tree["orders"] or key in tree["closed_positions"]:
            continue
        reasons = []
        if token in excluded: reasons.append("consensus_top3")
        if taf and bucket_contains(bucket, float(taf["value_native"])): reasons.append("taf_bucket")
        if ask < ENTRY_MIN_ASK: reasons.append("ask_below_0.85")
        twap = _twap_ask(ask_history.get(token, []), now_monotonic_ns)
        if twap is None: reasons.append("insufficient_6h_ask_history")
        limit = conservative_limit(ask, twap, book.get("tick_size")) if twap is not None else None
        if limit is None: reasons.append("missing_or_invalid_limit")
        notional = limit * ENTRY_SHARES if limit is not None else Decimal(0)
        if notional + reserved > global_max_usdc: reasons.append("global_cap")
        if notional + sum(Decimal(str(o.get("reserved_usdc", "0"))) for o in tree["orders"].values() if o.get("city_id") == city["city_id"] and o.get("market_local_date") == local_date and o.get("direction") == direction) > max_usdc: reasons.append("city_day_direction_cap")
        if reasons:
            actions.append({"action_type": "tree13_entry", "status": "blocked", "token_id": token, "bucket_id": bucket.get("bucket_id"), "reasons": reasons, "best_ask": str(ask), "twap_6h": str(twap) if twap is not None else None})
            continue
        order = {"order_key": key, "action_type": "tree13_paper_buy_no", "status": "PENDING_GTC", "city_id": city["city_id"], "market_local_date": local_date, "direction": direction, "bucket_id": bucket.get("bucket_id"), "token_id": token, "outcome": "NO", "side": "BUY", "order_type": "GTC", "requested_shares": str(ENTRY_SHARES), "best_ask": str(ask), "ask_twap_6h": str(twap), "limit_price": str(limit), "reserved_usdc": str(notional), "safety": {"paper_only": True, "orders_submitted": 0, "credentials_loaded": False}}
        tree["orders"][key] = order; actions.append(order)
    return actions

def classify_metar_for_position(position: dict[str, Any], running_extreme: Any) -> str:
    value = float(_decimal(running_extreme, "running_extreme", nonnegative=False)); bucket = position.get("bucket", {}); lo, hi = bucket.get("lo"), bucket.get("hi")
    if position.get("direction") == "high":
        if hi is not None and value >= float(hi): return "PROVEN_IMPOSSIBLE_HOLD"
        if (lo is None or value >= float(lo)) and (hi is None or value < float(hi)): return "FACT_INVALIDATED_EXIT"
    else:
        if lo is not None and value < float(lo): return "PROVEN_IMPOSSIBLE_HOLD"
        if (lo is None or value >= float(lo)) and (hi is None or value < float(hi)): return "FACT_INVALIDATED_EXIT"
    return "NO_ACTION"

EXIT_RETRY_SECONDS = (0, 3, 8, 15, 30)
EXIT_SLIPPAGE = (Decimal("0.03"), Decimal("0.07"), Decimal("0.12"), Decimal("0.20"), Decimal("0.30"))
EXIT_HARD_FLOOR = Decimal("0.05")

def plan_exit(*, position: dict[str, Any], reason: str, best_bid: Any, remaining_shares: Any, attempt: int = 0) -> dict[str, Any]:
    """Fast mild FAK ladder; hard floor 0.05. Paper callers should settle fills into closed_positions."""
    shares = _decimal(remaining_shares, "remaining_shares")
    if shares <= 0:
        return {"action_type": "tree13_exit", "status": "blocked_no_reconciled_shares"}
    bid = _decimal(best_bid, "best_bid")
    slip = EXIT_SLIPPAGE[min(max(attempt, 0), len(EXIT_SLIPPAGE) - 1)]
    minimum = max(EXIT_HARD_FLOOR, bid * (Decimal(1) - slip))
    if bid <= EXIT_HARD_FLOOR:
        minimum = EXIT_HARD_FLOOR
    return {
        "action_type": "tree13_paper_sell_no",
        "status": "PENDING_FAK",
        "reason": reason,
        "token_id": position.get("token_id"),
        "outcome": "NO",
        "side": "SELL",
        "order_type": "FAK",
        "remaining_shares": str(shares),
        "best_bid": str(bid),
        "minimum_price": str(minimum),
        "slippage": str(slip),
        "attempt": attempt,
        "retry_seconds": EXIT_RETRY_SECONDS[min(max(attempt, 0), len(EXIT_RETRY_SECONDS) - 1)],
        "requires_reconciled_order_id": True,
        "safety": {"paper_only": True, "orders_submitted": 0, "credentials_loaded": False},
    }


def _position_key(position: dict[str, Any]) -> str:
    """Stable key for a tree13 position (order_key preferred)."""
    for candidate in (
        position.get("order_key"),
        position.get("position_key"),
        position.get("key"),
    ):
        if candidate:
            return str(candidate)
    city = position.get("city_id") or "unknown"
    local_date = position.get("market_local_date") or "unknown"
    direction = position.get("direction") or "unknown"
    bucket = position.get("bucket_id") or (position.get("bucket") or {}).get("bucket_id") or "unknown"
    token = position.get("token_id") or "unknown"
    return f"{city}|{local_date}|{direction}|{bucket}|{token}"


def paper_fill_entry(
    state: dict[str, Any],
    order_key: str,
    *,
    fill_price: Any | None = None,
    now_utc: datetime | None = None,
) -> dict[str, Any] | None:
    """Paper-fill a PENDING_GTC buy-NO order into positions and reserve cash.

    Idempotent: if already a position or closed, returns None / status only.
    """
    tree = ensure_allno_state(state)
    order = tree["orders"].get(order_key)
    if not isinstance(order, dict):
        return None
    if order.get("status") not in {"PENDING_GTC", "WORKING_GTC"}:
        return {"action_type": "tree13_paper_fill_entry", "status": "skipped_not_pending", "order_key": order_key}
    if order_key in tree["positions"] or order_key in tree["closed_positions"]:
        return {"action_type": "tree13_paper_fill_entry", "status": "skipped_already_position", "order_key": order_key}

    shares = _decimal(order.get("requested_shares") or ENTRY_SHARES, "shares")
    price = _decimal(fill_price if fill_price is not None else order.get("limit_price"), "fill_price")
    notional = (shares * price).quantize(Decimal("0.00001"))
    reserved = reserve(state, notional)
    if reserved is None:
        return {
            "action_type": "tree13_paper_fill_entry",
            "status": "blocked_insufficient_capital",
            "order_key": order_key,
            "notional": str(notional),
            "remaining_capital_usdc": str(remaining_capital_usdc(state)),
        }

    now = now_utc or datetime.now(timezone.utc)
    pos = {
        "order_key": order_key,
        "position_key": order_key,
        "city_id": order.get("city_id"),
        "market_local_date": order.get("market_local_date"),
        "direction": order.get("direction"),
        "bucket_id": order.get("bucket_id"),
        "bucket": order.get("bucket") or {"bucket_id": order.get("bucket_id")},
        "token_id": order.get("token_id"),
        "outcome": "NO",
        "side": "BUY",
        "shares": str(shares),
        "avg_price": str(price),
        "cost_usdc": str(notional),
        "realized_pnl_usdc": "0",
        "opened_at_utc": _iso(now),
        "status": "open",
    }
    tree["positions"][order_key] = pos
    order["status"] = "FILLED_PAPER"
    order["filled_at_utc"] = _iso(now)
    order["fill_price"] = str(price)
    order["filled_shares"] = str(shares)
    return {
        "action_type": "tree13_paper_fill_entry",
        "status": "filled",
        "order_key": order_key,
        "token_id": pos["token_id"],
        "shares": str(shares),
        "fill_price": str(price),
        "cost_usdc": str(notional),
        "paper_total_debit_usdc": str(reserved),
    }


def settle_paper_exit(
    state: dict[str, Any],
    position_key: str,
    sale_price: Any,
    *,
    shares: Any | None = None,
    reason: str = "FACT_INVALIDATED_EXIT",
    now_utc: datetime | None = None,
) -> dict[str, Any] | None:
    """Paper-settle an open position: release cash, realize PnL, move to closed_positions.

    Idempotent — if position already closed/missing, returns skipped status.
    """
    tree = ensure_allno_state(state)
    if position_key in tree["closed_positions"] and position_key not in tree["positions"]:
        return {
            "action_type": "tree13_paper_exit_settled",
            "status": "skipped_already_closed",
            "position_key": position_key,
        }
    pos = tree["positions"].get(position_key)
    if not isinstance(pos, dict):
        return None

    qty = _decimal(shares if shares is not None else pos.get("shares") or "0", "shares")
    if qty <= 0:
        tree["positions"].pop(position_key, None)
        return {
            "action_type": "tree13_paper_exit_settled",
            "status": "skipped_zero_shares",
            "position_key": position_key,
        }

    avg = _decimal(pos.get("avg_price") or "0", "avg_price")
    px = _decimal(sale_price, "sale_price")
    if px < 0:
        px = Decimal("0")
    # Never settle below hard floor when a positive bid path was intended;
    # caller may pass hard floor explicitly for dead books.
    proceeds = (px * qty).quantize(Decimal("0.00001"))
    pnl = ((px - avg) * qty).quantize(Decimal("0.0001"))
    release(state, proceeds)

    now = now_utc or datetime.now(timezone.utc)
    closed = {
        **pos,
        "shares": "0",
        "closed_shares": str(qty),
        "sale_price": str(px),
        "proceeds_usdc": str(proceeds),
        "realized_pnl_usdc": str(pnl),
        "status": "closed",
        "close_reason": reason,
        "closed_at_utc": _iso(now),
    }
    tree["closed_positions"][position_key] = closed
    tree["positions"].pop(position_key, None)

    chase = tree["exit_chases"].get(position_key)
    if isinstance(chase, dict):
        chase["status"] = "settled"
        chase["settled_at_utc"] = _iso(now)
        chase["sale_price"] = str(px)
        chase["realized_pnl_usdc"] = str(pnl)
        chase["proceeds_usdc"] = str(proceeds)
        chase["remaining_shares"] = "0"

    event_id = f"{position_key}|{reason}|{_iso(now)}"
    tree["realized_pnl_events"][event_id] = {
        "position_key": position_key,
        "reason": reason,
        "sale_price": str(px),
        "shares": str(qty),
        "avg_price": str(avg),
        "proceeds_usdc": str(proceeds),
        "realized_pnl_usdc": str(pnl),
        "at_utc": _iso(now),
    }

    # Clear related open order if still present
    order = tree["orders"].get(position_key)
    if isinstance(order, dict) and order.get("status") in {"PENDING_GTC", "WORKING_GTC"}:
        order["status"] = "CANCELLED_FOR_EXIT"

    return {
        "action_type": "tree13_paper_exit_settled",
        "status": "settled",
        "position_key": position_key,
        "token_id": pos.get("token_id"),
        "reason": reason,
        "sale_price": str(px),
        "shares": str(qty),
        "avg_price": str(avg),
        "proceeds_usdc": str(proceeds),
        "realized_pnl_usdc": str(pnl),
        "remaining_capital_usdc": str(remaining_capital_usdc(state)),
    }


def process_metar_paper_exits(
    state: dict[str, Any],
    *,
    running_extremes_by_position_key: dict[str, Any] | None = None,
    books_by_token: dict[str, Any] | None = None,
    now_utc: datetime | None = None,
    attempt: int = 0,
) -> list[dict[str, Any]]:
    """Scan open positions; on FACT_INVALIDATED_EXIT settle paper exit immediately.

    ``running_extremes_by_position_key`` maps position_key -> native temp extreme
    relevant to that position's direction. If omitted, uses each position's
    ``running_extreme`` field when present.

    Paper settlement uses plan_exit minimum_price (bid with mild slippage / hard floor).
    """
    tree = ensure_allno_state(state)
    books_by_token = books_by_token or {}
    extremes = running_extremes_by_position_key or {}
    now = now_utc or datetime.now(timezone.utc)
    actions: list[dict[str, Any]] = []

    for key, pos in list(tree["positions"].items()):
        if not isinstance(pos, dict):
            continue
        extreme = extremes.get(key, pos.get("running_extreme"))
        if extreme is None:
            # Derive from daily_extrema when city/date known
            city_id = pos.get("city_id")
            local_date = pos.get("market_local_date")
            direction = pos.get("direction")
            if city_id and local_date and direction:
                day_key = f"{city_id}|{local_date}"
                ext = tree["daily_extrema"].get(day_key) or {}
                extreme = ext.get("high") if direction == "high" else ext.get("low")
        if extreme is None:
            continue

        classification = classify_metar_for_position(pos, extreme)
        if classification != "FACT_INVALIDATED_EXIT":
            if classification == "PROVEN_IMPOSSIBLE_HOLD":
                actions.append({
                    "action_type": "tree13_metar_classify",
                    "status": "hold_proven_impossible",
                    "position_key": key,
                    "classification": classification,
                    "running_extreme": extreme,
                })
            continue

        token = str(pos.get("token_id") or "")
        book = books_by_token.get(token) or {}
        best_bid = book.get("best_bid")
        if best_bid is None:
            best_bid = pos.get("mark_bid")
        if best_bid is None:
            # Dead / unknown book: settle at hard floor so equity is not inflated
            sale = EXIT_HARD_FLOOR
            planned = {
                "action_type": "tree13_paper_sell_no",
                "status": "PENDING_FAK",
                "reason": "FACT_INVALIDATED_EXIT",
                "token_id": token,
                "minimum_price": str(sale),
                "best_bid": None,
                "attempt": attempt,
            }
        else:
            planned = plan_exit(
                position=pos,
                reason="FACT_INVALIDATED_EXIT",
                best_bid=best_bid,
                remaining_shares=pos.get("shares"),
                attempt=attempt,
            )
            sale = _decimal(planned.get("minimum_price") or EXIT_HARD_FLOOR, "minimum_price")

        # Record chase then settle immediately in paper
        chase = tree["exit_chases"].setdefault(key, {
            "position_key": key,
            "token_id": token,
            "status": "active",
            "trigger": "FACT_INVALIDATED_EXIT",
            "triggered_at_utc": _iso(now),
            "attempts": [],
        })
        if isinstance(chase, dict):
            chase.setdefault("attempts", []).append({
                "attempt": attempt,
                "at_utc": _iso(now),
                "planned": planned,
            })

        actions.append(planned)
        settled = settle_paper_exit(
            state,
            key,
            sale,
            shares=pos.get("shares"),
            reason="FACT_INVALIDATED_EXIT",
            now_utc=now,
        )
        if settled:
            actions.append(settled)
    return actions


def process_taf_paper_exits(
    state: dict[str, Any],
    *,
    books_by_token: dict[str, Any] | None = None,
    now_utc: datetime | None = None,
    attempt: int = 0,
) -> list[dict[str, Any]]:
    """If latest TAF value falls inside an open NO bucket, paper-settle the exit."""
    tree = ensure_allno_state(state)
    books_by_token = books_by_token or {}
    now = now_utc or datetime.now(timezone.utc)
    actions: list[dict[str, Any]] = []

    for key, pos in list(tree["positions"].items()):
        if not isinstance(pos, dict):
            continue
        city_id = pos.get("city_id")
        local_date = pos.get("market_local_date")
        direction = pos.get("direction")
        if not (city_id and local_date and direction):
            continue
        market_key = f"{city_id}|{local_date}|{direction}"
        versions = tree["taf_versions"].get(market_key) or []
        if not versions:
            continue
        latest = max(versions, key=lambda v: int(v.get("visible_at_monotonic_ns") or 0))
        value = latest.get("value_native")
        bucket = pos.get("bucket") or {}
        if value is None or not bucket_contains(bucket, float(value)):
            continue

        token = str(pos.get("token_id") or "")
        book = books_by_token.get(token) or {}
        best_bid = book.get("best_bid", pos.get("mark_bid"))
        if best_bid is None:
            sale = EXIT_HARD_FLOOR
            planned = {
                "action_type": "tree13_paper_sell_no",
                "status": "PENDING_FAK",
                "reason": "TAF_REVISION_HIT_BUCKET",
                "token_id": token,
                "minimum_price": str(sale),
            }
        else:
            planned = plan_exit(
                position=pos,
                reason="TAF_REVISION_HIT_BUCKET",
                best_bid=best_bid,
                remaining_shares=pos.get("shares"),
                attempt=attempt,
            )
            sale = _decimal(planned.get("minimum_price") or EXIT_HARD_FLOOR, "minimum_price")

        actions.append(planned)
        settled = settle_paper_exit(
            state,
            key,
            sale,
            shares=pos.get("shares"),
            reason="TAF_REVISION_HIT_BUCKET",
            now_utc=now,
        )
        if settled:
            actions.append(settled)
    return actions

