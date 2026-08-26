"""Tree2 read-only execution path.

It estimates paper FAK fills only from a validated, timestamped CLOB snapshot.
It never signs or submits an order. The function is intentionally separate from
legacy paper_execution.py so deployments can canary it and compare decisions.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
from typing import Any

from clob_market_data import CLOBDataError, CLOBMarketData
from execution_policy import decide_buy_no

TARGET_ORDER_SHARES = Decimal("5")
CITY_DAY_MAX_TOTAL_DEBIT = Decimal("20.00")
MIN_PRICE_EXCLUSIVE = Decimal("0.05")
MAX_PRICE_EXCLUSIVE = Decimal("0.95")


def _fee_rate(payload: dict[str, Any]) -> Decimal:
    try:
        return Decimal(str(payload["base_fee"])) / Decimal("10000")
    except Exception as exc:
        raise CLOBDataError("invalid_fee_rate") from exc


def _quantity_quantum(tick_size: Decimal | None) -> Decimal:
    # CLOB currently exposes share quantities to two decimal places. Keep this
    # explicit and isolated so a future exchange metadata change is testable.
    return Decimal("0.01")


def simulate(signal: dict[str, Any], state: dict[str, Any], market_data: CLOBMarketData | None = None) -> dict[str, Any]:
    token_id = str(signal.get("bucket", {}).get("no_token_id") or "")
    base = {"mode": "paper", "order_type": "FAK", "side": "BUY_NO", "no_token_id": token_id}
    if not token_id:
        return {**base, "status": "paper_fill_rejected_missing_no_token", "decision_code": "MISSING_NO_TOKEN"}
    data = market_data or CLOBMarketData()
    try:
        books = data.fetch_books([token_id])
        snapshot = books[token_id]
        fee_payload = data.fetch_fee_rate(token_id)
        summary = data.executable_summary(token_id)
    except (CLOBDataError, KeyError) as exc:
        return {**base, "status": "paper_fill_unavailable", "decision_code": str(exc), "message": "CLOB snapshot unavailable"}

    min_order_size = snapshot.min_order_size or Decimal("0")
    decision = decide_buy_no(
        mode="paper", token_id=token_id, book_summary=summary,
        target_shares=TARGET_ORDER_SHARES, min_order_size=min_order_size,
        max_price=MAX_PRICE_EXCLUSIVE,
    )
    result = {**base, "decision": decision.as_dict(), "book": snapshot.to_json(), "fee": fee_payload}
    if not decision.allowed:
        mapping = {
            "EMPTY_ASK": "paper_fill_rejected_no_asks",
            "ASK_OUTSIDE_LIMIT": "paper_fill_rejected_best_ask_outside_gate",
            "DEPTH_LT_MIN_ORDER": "paper_fill_rejected_below_min_order_size",
            "DEPTH_LT_TARGET": "paper_fill_rejected_below_target_depth",
        }
        return {**result, "status": mapping.get(decision.code, "paper_fill_rejected_pretrade_gate")}

    ledger = state.setdefault("paper_city_day_total_debit", {})
    key = f"{signal.get('city_id')}|{signal.get('market_local_date')}"
    already_spent = Decimal(str(ledger.get(key, 0)))
    remaining = CITY_DAY_MAX_TOTAL_DEBIT - already_spent
    if remaining <= 0:
        return {**result, "status": "paper_fill_rejected_city_day_cap", "decision_code": "CITY_DAY_CAP"}

    fee_rate = _fee_rate(fee_payload)
    quantum = _quantity_quantum(snapshot.tick_size)
    filled = Decimal("0")
    principal = Decimal("0")
    fees = Decimal("0")
    fills: list[dict[str, str]] = []
    cap = MAX_PRICE_EXCLUSIVE
    for level in sorted(snapshot.asks, key=lambda x: x["price"]):
        price, size = level["price"], level["size"]
        if not MIN_PRICE_EXCLUSIVE < price < cap:
            continue
        quantity = min(size, TARGET_ORDER_SHARES - filled).quantize(quantum, rounding=ROUND_DOWN)
        if quantity <= 0:
            continue
        level_fee = quantity * fee_rate * price * (Decimal("1") - price)
        filled += quantity
        principal += quantity * price
        fees += level_fee
        fills.append({"price": str(price), "shares": str(quantity)})
        if filled >= TARGET_ORDER_SHARES:
            break
    if filled < min_order_size:
        return {**result, "status": "paper_fill_rejected_below_min_order_size", "estimated_shares": str(filled), "fills": fills}
    total = principal + fees
    if total > remaining:
        return {**result, "status": "paper_fill_rejected_city_day_cap", "total_debit_usdc": str(total), "fills": fills}
    ledger[key] = float((already_spent + total).quantize(Decimal("0.00001")))
    return {
        **result, "status": "paper_fill_estimate", "target_shares": str(TARGET_ORDER_SHARES),
        "estimated_shares": str(filled), "principal_usdc": str(principal),
        "estimated_fee_usdc": str(fees), "total_debit_usdc": str(total),
        "average_price": str(principal / filled), "fills": fills,
    }
