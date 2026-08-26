"""Tree3 execution boundary: signal -> local book -> FAK/FOK paper intent."""
from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from typing import Any

from clob_market_data import BookSnapshot
from local_order_book import LocalBookSnapshot
from tree2_execution import build_fixed_five_fak, TARGET_ORDER_SHARES, MAX_EXECUTION_PRICE

DEFAULT_MAX_SLIPPAGE = Decimal("0.10")


def _book_from_local(local: LocalBookSnapshot) -> BookSnapshot:
    return BookSnapshot(
        token_id=local.token_id, fetched_at_epoch=local.received_at_epoch,
        timestamp=local.exchange_timestamp, book_hash=local.book_hash,
        asset_id=local.token_id, market=local.market,
        min_order_size=local.min_order_size, tick_size=local.tick_size,
        neg_risk=local.neg_risk, asks=local.asks, bids=local.bids, source="websocket_local",
    )


def simulate_local_fak(signal: dict[str, Any], local: LocalBookSnapshot | None, fee_payload: dict[str, Any], state: dict[str, Any] | None = None, *, max_age_seconds: float = 3.0, max_price: Decimal = MAX_EXECUTION_PRICE, max_slippage: Decimal = Decimal("0.10"), now: float | None = None) -> dict[str, Any]:
    """Return an auditable FAK estimate without any REST request."""
    base = {"mode": "paper", "order_type": "FAK", "side": "BUY_NO", "execution_source": "websocket_local", "no_token_id": str(signal.get("bucket", {}).get("no_token_id") or "")}
    if local is None or not local.ready:
        return {**base, "status": "paper_fill_rejected_stale_local_book", "decision_code": "LOCAL_BOOK_NOT_READY"}
    if not local.is_fresh(max_age_seconds, now=now):
        return {**base, "status": "paper_fill_rejected_stale_local_book", "decision_code": "STALE_LOCAL_BOOK", "book_age_seconds": max(0, (now if now is not None else __import__("time").time()) - local.received_at_epoch)}
    if local.neg_risk is None or local.tick_size is None or local.min_order_size is None:
        return {**base, "status": "paper_fill_rejected_missing_book_metadata", "decision_code": "BOOK_METADATA_INCOMPLETE"}
    if max_slippage < 0:
        return {**base, "status": "paper_fill_rejected_invalid_slippage", "decision_code": "NEGATIVE_SLIPPAGE"}
    snapshot = _book_from_local(local)
    effective_max_price = min(max_price, local.best_ask + max_slippage) if local.best_ask is not None else max_price
    try:
        intent = build_fixed_five_fak(snapshot, fee_payload, target_shares=TARGET_ORDER_SHARES, max_price=effective_max_price)
    except Exception as exc:
        return {**base, "status": "paper_fill_unavailable", "decision_code": type(exc).__name__, "message": str(exc), "book_version": local.version}
    if intent.executable_shares < snapshot.min_order_size:
        return {**base, "status": "paper_fill_rejected_below_min_order_size", "decision_code": "DEPTH_LT_MIN_ORDER", "book_version": local.version, "effective_max_price": str(effective_max_price), "intent": intent.as_dict()}
    if intent.executable_shares < TARGET_ORDER_SHARES:
        return {**base, "status": "paper_fill_partial_fak", "decision_code": "PARTIAL_FILL_REMAINDER_CANCELLED", "book_version": local.version, "effective_max_price": str(effective_max_price), "intent": intent.as_dict()}
    return {**base, "status": "paper_fill_estimate", "decision_code": "FAK_FULL_TARGET", "book_version": local.version, "book_hash": local.book_hash, "effective_max_price": str(effective_max_price), "intent": intent.as_dict()}


def build_execution_intent(signal: dict[str, Any], local: LocalBookSnapshot | None, fee_payload: dict[str, Any], *, order_type: str = "FAK", max_price: Decimal = MAX_EXECUTION_PRICE, max_slippage: Decimal = Decimal("0.10"), now: float | None = None) -> dict[str, Any]:
    order_type = order_type.upper()
    if order_type not in {"FAK", "FOK"}:
        raise ValueError("order_type_must_be_FAK_or_FOK")
    result = simulate_local_fak(signal, local, fee_payload, max_price=max_price, max_slippage=max_slippage, now=now)
    intent = result.get("intent")
    if order_type == "FOK" and (not intent or Decimal(str(intent["executable_shares"])) < TARGET_ORDER_SHARES):
        return {**result, "order_type": "FOK", "status": "paper_fill_rejected_fok_insufficient_depth", "decision_code": "FOK_NOT_FULL"}
    return {**result, "order_type": order_type}
