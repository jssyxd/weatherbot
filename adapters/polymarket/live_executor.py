"""Official Polymarket CLOB order submission adapter (PREPARE ONLY, default OFF).

Built against the current official CLOB OpenAPI (verified 2026-08-29):

- ``POST /order``   body ``SendOrder{order, owner, orderType ∈ GTC|FOK|GTD|FAK}``
- ``POST /orders``  batch (not used here; single orders only for now)
- ``DELETE /order`` body ``{orderID}``
- ``GET /data/orders``  query open orders

L2 authenticated requests require the ``POLY_*`` headers. Credentials are read
ONLY from environment variables, never from config files, URLs, audit records,
or source control:

- ``POLY_ADDRESS``       wallet address (maker/signer)
- ``POLY_PRIVATE_KEY``   used only to EIP-712 sign the order itself
- ``POLY_API_KEY``       L2 API key (operator derives once via /auth/derive-api-key)
- ``POLY_API_SECRET``    L2 API secret (HMAC request signing)
- ``POLY_PASSPHRASE``    L2 passphrase

Hard safety gate: :class:`LiveExecutor` refuses to sign or submit anything
unless ``live_enabled`` is explicitly ``true`` in the config it is constructed
with. There is no code path that flips this on automatically, and the main loop
(`metar_observer.py`) still reports ``live_executor: not_present`` until an
operator wires this in after human confirmation.

Scope note: order signing is BUY-only today because ``order_signing`` exposes a
BUY builder. SELL raises :class:`LiveOrderNotSupported`. This is intentional and
fail-closed; do not add SELL until the signed-order builder is extended and
verified against the current CLOB OpenAPI.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from execution.order_intent import OrderIntent
from order_signing import (
    OrderEncodingError,
    build_unsigned_buy_order,
    sign_order,
)

CLOB_BASE = "https://clob.polymarket.com"
ORDER_ENDPOINT = f"{CLOB_BASE}/order"
ORDERS_ENDPOINT = f"{CLOB_BASE}/orders"
DATA_ORDERS_ENDPOINT = f"{CLOB_BASE}/data/orders"

# Map the internal order_type string to the CLOB wire orderType (audit §21:
# FAK/FOK are the top-level time-in-force flag; GTC is the default limit order).
_CLOB_ORDER_TYPES = {"GTC": "GTC", "FAK": "FAK", "FOK": "FOK", "GTD": "GTD"}


class LiveExecutorError(RuntimeError):
    pass


class LiveExecutorDisabledError(LiveExecutorError):
    pass


class LiveOrderNotSupported(LiveExecutorError):
    pass


@dataclass
class LiveSubmitResult:
    order_id: str
    status: str            # "live" | "matched" | "delayed"
    making_amount: str | None = None
    taking_amount: str | None = None
    transaction_hashes: tuple[str, ...] = ()
    trade_ids: tuple[str, ...] = ()
    error_msg: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "status": self.status,
            "making_amount": self.making_amount,
            "taking_amount": self.taking_amount,
            "transaction_hashes": list(self.transaction_hashes),
            "trade_ids": list(self.trade_ids),
            "error_msg": self.error_msg,
        }


def build_order_wire(unsigned: Any, signature: str) -> dict[str, Any]:
    """Assemble the ``order`` object for a ``SendOrder`` body.

    Reuses ``order_signing``'s wire fields (which already carry the documented
    precision-encoded amounts) and adds the EIP-712 signature + string token id.
    """
    wire = dict(unsigned.wire_fields)
    wire["tokenId"] = str(wire.get("tokenId"))
    wire["signature"] = signature
    return wire


def build_send_order(intent: OrderIntent, order_wire: dict[str, Any], owner: str) -> dict[str, Any]:
    """Assemble the ``SendOrder`` body per the official CLOB OpenAPI."""
    if intent.order_type not in _CLOB_ORDER_TYPES:
        raise LiveOrderNotSupported(f"unsupported order_type: {intent.order_type}")
    return {
        "order": order_wire,
        "owner": owner,
        "orderType": _CLOB_ORDER_TYPES[intent.order_type],
    }


def _hmac_sha256_base64(secret: str, message: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


class LiveExecutor:
    """Thin, fail-closed CLOB order submitter. Default OFF."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        config = config or {}
        self.live_enabled = bool(config.get("live_enabled", False))
        self.address = os.environ.get("POLY_ADDRESS", "")
        self.private_key = os.environ.get("POLY_PRIVATE_KEY", "")
        self.api_key = os.environ.get("POLY_API_KEY", "")
        self.api_secret = os.environ.get("POLY_API_SECRET", "")
        self.passphrase = os.environ.get("POLY_PASSPHRASE", "")

    @property
    def enabled(self) -> bool:
        return self.live_enabled

    def _assert_enabled(self) -> None:
        if not self.live_enabled:
            raise LiveExecutorDisabledError(
                "live executor is disabled; set live_enabled=true explicitly to enable"
            )

    def _signed_headers(self, method: str, path: str, body: str) -> dict[str, str]:
        timestamp = str(int(time.time()))
        signature = _hmac_sha256_base64(self.api_secret, f"{timestamp}{method}{path}{body}")
        return {
            "POLY_ADDRESS": self.address,
            "POLY_API_KEY": self.api_key,
            "POLY_PASSPHRASE": self.passphrase,
            "POLY_TIMESTAMP": timestamp,
            "POLY_SIGNATURE": signature,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        self._assert_enabled()
        payload = json.dumps(body) if body is not None else ""
        request = urllib.request.Request(
            url,
            data=payload.encode("utf-8") if body is not None else None,
            method=method,
        )
        for key, value in self._signed_headers(method, url.removeprefix(CLOB_BASE), payload).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise LiveExecutorError(f"clob_http_{exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise LiveExecutorError(f"clob_network_error: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise LiveExecutorError("clob_invalid_json") from exc
        if not isinstance(parsed, dict):
            raise LiveExecutorError("clob_invalid_response_shape")
        return parsed

    def submit(
        self,
        intent: OrderIntent,
        *,
        tick_size: Any,
        min_order_size: Any,
        neg_risk: bool,
        salt: int,
        expiration: int = 0,
    ) -> LiveSubmitResult:
        """Sign and POST a single order. BUY only for now."""
        self._assert_enabled()
        if intent.side != "BUY":
            raise LiveOrderNotSupported(
                "live submit is BUY-only until the signed-order builder supports SELL"
            )
        if not self.address or not self.private_key:
            raise LiveExecutorError("POLY_ADDRESS and POLY_PRIVATE_KEY are required to sign an order")
        try:
            unsigned = build_unsigned_buy_order(
                token_id=intent.token_id, price=intent.price, size=intent.quantity,
                tick_size=tick_size, min_order_size=min_order_size,
                maker=self.address, signer=self.address, salt=salt,
                timestamp=int(time.time() * 1000), expiration=expiration,
                neg_risk=neg_risk,
            )
            signature, _ = sign_order(unsigned, self.private_key)
        except OrderEncodingError as exc:
            raise LiveExecutorError(f"order_encoding_error: {exc}") from exc

        order_wire = build_order_wire(unsigned, signature)
        body = build_send_order(intent, order_wire, owner=self.address)
        response = self._request("POST", ORDER_ENDPOINT, body)
        if not response.get("success"):
            raise LiveExecutorError(f"order_rejected: {response.get('errorMsg') or response.get('error')}")
        return LiveSubmitResult(
            order_id=str(response.get("orderID", "")),
            status=str(response.get("status", "")),
            making_amount=str(response["makingAmount"]) if response.get("makingAmount") is not None else None,
            taking_amount=str(response["takingAmount"]) if response.get("takingAmount") is not None else None,
            transaction_hashes=tuple(response.get("transactionsHashes") or ()),
            trade_ids=tuple(response.get("tradeIDs") or ()),
            error_msg=str(response.get("errorMsg") or ""),
        )

    def cancel(self, order_id: str) -> dict[str, Any]:
        """DELETE a resting order by its order id."""
        return self._request("DELETE", ORDER_ENDPOINT, {"orderID": order_id})

    def query_orders(self, market: str | None = None) -> dict[str, Any]:
        """Query open orders, optionally scoped to one market/condition id."""
        url = DATA_ORDERS_ENDPOINT if market is None else f"{DATA_ORDERS_ENDPOINT}?market={market}"
        return self._request("GET", url)
