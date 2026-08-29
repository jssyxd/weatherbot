"""Tree2 read-only execution path.

It estimates a fixed 5-share BUY_NO FAK from a validated order-book snapshot.
It never signs or submits an order. The order-intent builder is deliberately
pure and can be replay-tested before a future authenticated adapter is added.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from decimal import Decimal, ROUND_DOWN
from typing import Any, Iterable

from clob_market_data import CLOBDataError, CLOBMarketData, BookSnapshot
from execution_policy import decide_buy_no

TARGET_ORDER_SHARES = Decimal("1")
CITY_DAY_MAX_TOTAL_DEBIT = Decimal("20.00")
MIN_PRICE_EXCLUSIVE = Decimal("0.05")
MAX_PRICE_EXCLUSIVE = Decimal("0.95")


@dataclass(frozen=True)
class FAKIntent:
    token_id: str
    side: str
    order_type: str
    target_shares: Decimal
    executable_shares: Decimal
    limit_price: Decimal | None
    principal_usdc: Decimal
    estimated_fee_usdc: Decimal
    average_price: Decimal | None
    fills: tuple[dict[str, Decimal], ...]

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for key in ("target_shares", "executable_shares", "limit_price", "principal_usdc", "estimated_fee_usdc", "average_price"):
            if value[key] is not None:
                value[key] = str(value[key])
        value["fills"] = [{k: str(v) for k, v in fill.items()} for fill in self.fills]
        return value


def _fee_rate(payload: dict[str, Any]) -> Decimal:
    try:
        return Decimal(str(payload["base_fee"])) / Decimal("10000")
    except Exception as exc:
        raise CLOBDataError("invalid_fee_rate") from exc


def _size_quantum(_tick_size: Decimal | None) -> Decimal:
    return Decimal("0.01")


def _price_quantum(tick_size: Decimal | None) -> Decimal:
    if tick_size is None or tick_size <= 0:
        raise CLOBDataError("invalid_tick_size")
    return tick_size


def build_fak_intent(
    snapshot: BookSnapshot,
    fee_payload: dict[str, Any],
    *,
    target_shares: Decimal = TARGET_ORDER_SHARES,
    max_price: Decimal = MAX_PRICE_EXCLUSIVE,
) -> FAKIntent:
    """Build a fixed-share FAK intent using asks up to the worst fill price.

    The returned ``limit_price`` is a worst acceptable price, not a promise
    that every share fills at that price. Unfilled quantity is cancelled by FAK.
    """
    if target_shares <= 0:
        raise CLOBDataError("invalid_target_shares")
    fee_rate = _fee_rate(fee_payload)
    price_quantum = _price_quantum(snapshot.tick_size)
    size_quantum = _size_quantum(snapshot.tick_size)
    filled = Decimal("0")
    principal = Decimal("0")
    fees = Decimal("0")
    fills: list[dict[str, Decimal]] = []
    for level in sorted(snapshot.asks, key=lambda x: x["price"]):
        price, size = level["price"], level["size"]
        if not MIN_PRICE_EXCLUSIVE < price < max_price or size <= 0:
            continue
        quantity = min(size, target_shares - filled).quantize(size_quantum, rounding=ROUND_DOWN)
        if quantity <= 0:
            continue
        level_fee = quantity * fee_rate * price * (Decimal("1") - price)
        filled += quantity
        principal += quantity * price
        fees += level_fee
        fills.append({"price": price, "shares": quantity})
        if filled >= target_shares:
            break
    worst = fills[-1]["price"] if fills else None
    if worst is not None:
        worst = (worst / price_quantum).to_integral_value(rounding=ROUND_DOWN) * price_quantum
    return FAKIntent(
        token_id=snapshot.token_id, side="BUY", order_type="FAK",
        target_shares=target_shares, executable_shares=filled,
        limit_price=worst, principal_usdc=principal,
        estimated_fee_usdc=fees,
        average_price=(principal / filled if filled > 0 else None),
        fills=tuple(fills),
    )


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
        summary = data.executable_summary(token_id, max_price=MAX_PRICE_EXCLUSIVE)
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

    intent = build_fak_intent(snapshot, fee_payload)
    if intent.executable_shares < Decimal("1"):
        return {**result, "status": "paper_fill_rejected_below_min_order_size", "intent": intent.as_dict()}
    total = intent.principal_usdc + intent.estimated_fee_usdc
    if total > remaining:
        return {**result, "status": "paper_fill_rejected_city_day_cap", "total_debit_usdc": str(total), "intent": intent.as_dict()}
    ledger[key] = float((already_spent + total).quantize(Decimal("0.00001")))
    return {
        **result, "status": "paper_fill_estimate", "intent": intent.as_dict(),
        "target_shares": str(TARGET_ORDER_SHARES), "estimated_shares": str(intent.executable_shares),
        "principal_usdc": str(intent.principal_usdc), "estimated_fee_usdc": str(intent.estimated_fee_usdc),
        "total_debit_usdc": str(total), "average_price": str(intent.average_price),
        "spent_city_day_total_debit_usdc_after": str(ledger[key]),
    }
