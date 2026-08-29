"""Read-only Polymarket CLOB paper-fill simulator.

This module only performs public GET requests. It never loads credentials, reads a
wallet, signs typed data, or sends POST/DELETE requests to the CLOB.
"""
from __future__ import annotations

import json
import math
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any

from paper_capital import remaining_capital_usdc, reserve

CLOB_BOOK_ENDPOINT = "https://clob.polymarket.com/book"
CLOB_FEE_RATE_ENDPOINT = "https://clob.polymarket.com/fee-rate"
MIN_PRICE_INCLUSIVE = Decimal("0.40")
MAX_PRICE_INCLUSIVE = Decimal("0.98")
# Per-user spec: every paper order is a FIXED quantity of 5 shares (the
# exchange minimum order size). The paper account starts with 1000 USDC and
# every simulated fill reserves cash from that global ledger. The optional
# per-city-day debit cap remains configurable via state for extra safety.
TARGET_ORDER_SHARES = Decimal("5")
CITY_DAY_MAX_TOTAL_DEBIT_DEFAULT = Decimal("20.00")


def utc_now_string() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(url: str) -> tuple[dict[str, Any], str]:
    request = urllib.request.Request(url, headers={"User-Agent": "weatherbot-paper-execution/1.0 (+https://github.com/jssyxd/weatherbot)"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = response.read().decode("utf-8")
            final_url = response.geturl()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"CLOB 只读请求失败（HTTP {exc.code}）") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"CLOB 只读网络请求失败: {exc.reason}") from exc
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError("CLOB 返回了不可解析的 JSON") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("CLOB 返回格式异常")
    return parsed, final_url


def fetch_order_book(no_token_id: str) -> tuple[dict[str, Any], str]:
    return _read_json(f"{CLOB_BOOK_ENDPOINT}?{urllib.parse.urlencode({'token_id': no_token_id})}")


def fetch_fee_rate(no_token_id: str) -> tuple[dict[str, Any], str]:
    return _read_json(f"{CLOB_FEE_RATE_ENDPOINT}?{urllib.parse.urlencode({'token_id': no_token_id})}")


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:  # Decimal's exception hierarchy is intentionally broad here.
        raise RuntimeError(f"CLOB 字段 {field} 不是十进制数") from exc
    if not result.is_finite() or result < 0:
        raise RuntimeError(f"CLOB 字段 {field} 无效")
    return result


def _floor_size(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_DOWN)


def _strict_price_cap(tick_size: Decimal) -> Decimal:
    if tick_size <= 0:
        raise RuntimeError("CLOB tick_size 必须大于零")
    quotient = (MAX_PRICE_INCLUSIVE / tick_size).to_integral_value(rounding=ROUND_DOWN)
    cap = quotient * tick_size
    if cap <= MIN_PRICE_INCLUSIVE:
        raise RuntimeError("CLOB tick_size 无法构造不超过 0.98 的可交易价格")
    return cap


def _base_fee_rate(fee_payload: dict[str, Any]) -> Decimal:
    if "base_fee" not in fee_payload:
        raise RuntimeError("CLOB fee-rate 返回缺少 base_fee")
    # Official endpoint reports basis points; fee rate is therefore base_fee / 10,000.
    return _decimal(fee_payload["base_fee"], "base_fee") / Decimal("10000")


def _rejected(status: str, message: str, **details: Any) -> dict[str, Any]:
    return {"mode": "paper", "status": status, "message": message, "order_type": "FAK", "side": "BUY_NO", **details}


def simulate_paper_fak(signal: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Estimate a FAK BUY_NO fill from public book levels.

    Intent size is a FIXED 5 shares (the exchange minimum order size).
    Cash is reserved from the global 1000-USDC paper account; an optional
    per-city-day debit cap remains configurable via state.
    """
    no_token_id = str(signal.get("bucket", {}).get("no_token_id") or "")
    city_day_key = f"{signal.get('city_id')}|{signal.get('market_local_date')}"
    ledger: dict[str, float] = state.setdefault("paper_city_day_total_debit", {})
    already_spent = Decimal(str(ledger.get(city_day_key, 0.0)))
    city_day_max = Decimal(str(state.get("paper_city_day_max_debit_usdc", CITY_DAY_MAX_TOTAL_DEBIT_DEFAULT)))
    if not no_token_id:
        return _rejected("paper_fill_rejected_missing_no_token", "候选桶没有有效 NO token；未读取订单簿。")
    try:
        book, book_endpoint = fetch_order_book(no_token_id)
        fee, fee_endpoint = fetch_fee_rate(no_token_id)
        asks = book.get("asks")
        if not isinstance(asks, list) or not asks:
            return _rejected("paper_fill_rejected_no_asks", "NO token 没有可用的公开 ask 深度。", book_endpoint=book_endpoint, fee_endpoint=fee_endpoint)
        tick_size = _decimal(book.get("tick_size"), "tick_size")
        min_order_size = _decimal(book.get("min_order_size"), "min_order_size")
        price_cap = _strict_price_cap(tick_size)
        fee_rate = _base_fee_rate(fee)
        levels = sorted(
            ({"price": _decimal(level.get("price"), "ask.price"), "size": _decimal(level.get("size"), "ask.size")} for level in asks if isinstance(level, dict)),
            key=lambda level: level["price"],
        )
        if not levels:
            return _rejected("paper_fill_rejected_invalid_book", "订单簿 ask 档位不可解析。", book_endpoint=book_endpoint, fee_endpoint=fee_endpoint)
        best_ask = levels[0]["price"]
        if not MIN_PRICE_INCLUSIVE <= best_ask <= MAX_PRICE_INCLUSIVE:
            return _rejected(
                "paper_fill_rejected_best_ask_outside_gate",
                "最优 ask 不在包含端点的价格门槛 [0.40, 0.98] 内。",
                best_ask=float(best_ask), book_endpoint=book_endpoint, fee_endpoint=fee_endpoint,
            )
        remaining_city_budget = city_day_max - already_spent
        if remaining_city_budget <= 0:
            return _rejected(
                "paper_fill_rejected_city_day_cap",
                "该城市当地日的含费用总现金余量已用尽。",
                required_intent_total_debit_usdc=float(TARGET_ORDER_SHARES),
                total_debit_budget_usdc=float(city_day_max), spent_city_day_total_debit_usdc=float(already_spent),
            )
        target_shares = TARGET_ORDER_SHARES
        filled_shares = Decimal("0")
        principal = Decimal("0")
        fees = Decimal("0")
        fills: list[dict[str, float]] = []
        for level in levels:
            price, size = level["price"], level["size"]
            if price > price_cap:
                break
            if not MIN_PRICE_INCLUSIVE <= price <= MAX_PRICE_INCLUSIVE or size <= 0:
                continue
            per_share_fee = fee_rate * price * (Decimal("1") - price)
            per_share_total = price + per_share_fee
            # Fixed 5-share intent: walk levels until 5 shares are filled.
            quantity = min(size, target_shares - filled_shares)
            quantity = _floor_size(quantity)
            if quantity <= 0:
                break
            level_principal = quantity * price
            level_fee = quantity * per_share_fee
            filled_shares += quantity
            principal += level_principal
            fees += level_fee
            fills.append({"price": float(price), "shares": float(quantity), "principal_usdc": float(level_principal), "estimated_fee_usdc": float(level_fee)})
            if filled_shares >= target_shares:
                break
        total_debit = principal + fees
        if filled_shares < min_order_size:
            return _rejected(
                "paper_fill_rejected_below_min_order_size",
                "订单簿在价格门槛内的累计深度不足 5 股（最小订单规模）。",
                best_ask=float(best_ask), min_order_size=float(min_order_size), estimated_shares=float(filled_shares),
                book_endpoint=book_endpoint, fee_endpoint=fee_endpoint,
            )
        if total_debit > remaining_city_budget:
            return _rejected(
                "paper_fill_rejected_city_day_cap",
                "5 股订单含费用总成本超过该城市当地日的剩余现金余量。",
                required_shares=float(target_shares), required_total_debit_usdc=float(total_debit),
                total_debit_budget_usdc=float(city_day_max), spent_city_day_total_debit_usdc=float(already_spent),
            )
        if total_debit <= 0:
            return _rejected("paper_fill_rejected_no_affordable_depth", "没有能在价格门槛内成交的有效深度。", book_endpoint=book_endpoint, fee_endpoint=fee_endpoint)
        if reserve(state, total_debit) is None:
            return _rejected(
                "paper_fill_rejected_insufficient_capital",
                "5 股订单含费用总成本超过纸面账户剩余资金。",
                required_total_debit_usdc=float(total_debit),
                remaining_capital_usdc=float(remaining_capital_usdc(state)),
                paper_initial_capital_usdc=float(state.get("paper_initial_capital_usdc", 1000)),
            )
        ledger[city_day_key] = float((already_spent + total_debit).quantize(Decimal("0.00001")))
        return {
            "mode": "paper", "status": "paper_fill_estimate", "message": "公开订单簿快照的纸面 FAK 估算；不保证真实可成交，且未提交订单。",
            "order_type": "FAK", "side": "BUY_NO", "no_token_id": no_token_id,
            "book_endpoint": book_endpoint, "fee_endpoint": fee_endpoint, "book_timestamp": book.get("timestamp"),
            "book_hash": book.get("hash"), "tick_size": float(tick_size), "min_order_size": float(min_order_size),
            "base_fee_bps": float(_decimal(fee["base_fee"], "base_fee")), "price_cap_inclusive": float(MAX_PRICE_INCLUSIVE),
            "effective_fak_limit_price": float(price_cap), "target_shares": float(target_shares),
            "principal_usdc": float(principal), "estimated_fee_usdc": float(fees), "total_debit_usdc": float(total_debit),
            "average_price": float(principal / filled_shares), "estimated_shares": float(filled_shares),
            "unspent_city_day_budget_usdc": float(remaining_city_budget - total_debit),
            "spent_city_day_total_debit_usdc_before": float(already_spent),
            "spent_city_day_total_debit_usdc_after": ledger[city_day_key], "fills": fills,
            "paper_initial_capital_usdc": float(state.get("paper_initial_capital_usdc", 1000)),
            "remaining_capital_usdc": float(remaining_capital_usdc(state)),
            "retrieved_at_utc": utc_now_string(),
        }
    except RuntimeError as exc:
        return _rejected("paper_fill_unavailable", str(exc), no_token_id=no_token_id)
