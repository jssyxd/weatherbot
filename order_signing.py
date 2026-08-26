"""Offline Polymarket CLOB V2 order construction and EIP-712 signing.

This module intentionally has no HTTP client, wallet loader, balance lookup,
or order-submission code. It accepts an explicitly supplied private key only
from the caller/test fixture and returns a signed order payload for inspection.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any

from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import to_checksum_address

from clob_market_data import BookSnapshot

CHAIN_ID = 137
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
ZERO_BYTES32 = "0x" + "00" * 32
STANDARD_EXCHANGE = to_checksum_address("0xE111180000d2663C0091e4f400237545B87B996B")
NEG_RISK_EXCHANGE = to_checksum_address("0xe2222d279d744050d28e00520010520000310F59")

# Polymarket CLOB V2 Order type. expiration remains in the wire body, but is
# intentionally absent from the EIP-712 signed struct.
ORDER_TYPES = {
    "Order": [
        {"name": "salt", "type": "uint256"},
        {"name": "maker", "type": "address"},
        {"name": "signer", "type": "address"},
        {"name": "tokenId", "type": "uint256"},
        {"name": "makerAmount", "type": "uint256"},
        {"name": "takerAmount", "type": "uint256"},
        {"name": "side", "type": "uint8"},
        {"name": "signatureType", "type": "uint8"},
        {"name": "timestamp", "type": "uint256"},
        {"name": "metadata", "type": "bytes32"},
        {"name": "builder", "type": "bytes32"},
    ]
}

TICK_RULES = {
    "0.1": (1, 2, 3), "0.01": (2, 2, 4), "0.005": (3, 2, 5),
    "0.0025": (4, 2, 6), "0.001": (3, 2, 5), "0.0001": (4, 2, 6),
}


class OrderEncodingError(ValueError):
    pass


@dataclass(frozen=True)
class EncodedAmounts:
    price: Decimal
    size: Decimal
    amount: Decimal
    maker_amount: int
    taker_amount: int
    price_decimals: int
    size_decimals: int
    amount_decimals: int


@dataclass(frozen=True)
class UnsignedOrder:
    exchange_address: str
    domain: dict[str, Any]
    message: dict[str, Any]
    wire_fields: dict[str, Any]
    encoded_amounts: EncodedAmounts

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["encoded_amounts"] = {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in asdict(self.encoded_amounts).items()
        }
        return result


def _as_decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise OrderEncodingError(f"invalid_{field}") from exc
    if not result.is_finite() or result <= 0:
        raise OrderEncodingError(f"invalid_{field}")
    return result


def _rule_for_tick(tick_size: Any) -> tuple[Decimal, int, int, int]:
    tick = _as_decimal(tick_size, "tick_size")
    key = format(tick, "f").rstrip("0").rstrip(".")
    if key not in TICK_RULES:
        raise OrderEncodingError(f"unsupported_tick_size:{key}")
    price_decimals, size_decimals, amount_decimals = TICK_RULES[key]
    return tick, price_decimals, size_decimals, amount_decimals


def _scale_to_int(value: Decimal, decimals: int) -> int:
    scaled = value * (Decimal(10) ** decimals)
    if scaled != scaled.to_integral_value():
        raise OrderEncodingError("amount_not_six_decimal_exact")
    return int(scaled)


def encode_buy_amounts(*, price: Any, size: Any, tick_size: Any, min_order_size: Any = "5") -> EncodedAmounts:
    """Apply documented precision rules and encode BUY maker/taker amounts."""
    tick, price_decimals, size_decimals, amount_decimals = _rule_for_tick(tick_size)
    raw_price = _as_decimal(price, "price")
    raw_size = _as_decimal(size, "size")
    minimum = _as_decimal(min_order_size, "min_order_size")
    if raw_price < tick or raw_price >= Decimal("1"):
        raise OrderEncodingError("price_out_of_range")
    normalized_price = (raw_price / tick).to_integral_value(rounding=ROUND_DOWN) * tick
    normalized_price = normalized_price.quantize(Decimal(1).scaleb(-price_decimals), rounding=ROUND_DOWN)
    normalized_size = raw_size.quantize(Decimal(1).scaleb(-size_decimals), rounding=ROUND_DOWN)
    if normalized_size < minimum:
        raise OrderEncodingError("size_below_min_order_size")
    if normalized_size <= 0:
        raise OrderEncodingError("size_is_zero")
    exact_amount = normalized_price * normalized_size
    intermediate = exact_amount.quantize(Decimal(1).scaleb(-(amount_decimals + 4)), rounding=ROUND_UP)
    normalized_amount = intermediate.quantize(Decimal(1).scaleb(-amount_decimals), rounding=ROUND_DOWN)
    if normalized_amount <= 0:
        raise OrderEncodingError("amount_is_zero")
    return EncodedAmounts(
        price=normalized_price, size=normalized_size, amount=normalized_amount,
        maker_amount=_scale_to_int(normalized_amount, 6),
        taker_amount=_scale_to_int(normalized_size, 6),
        price_decimals=price_decimals, size_decimals=size_decimals,
        amount_decimals=amount_decimals,
    )


def exchange_for_neg_risk(neg_risk: bool) -> str:
    return NEG_RISK_EXCHANGE if bool(neg_risk) else STANDARD_EXCHANGE


def _bytes32(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) != 66:
        raise OrderEncodingError(f"{field}_must_be_bytes32")
    try:
        int(value[2:], 16)
    except ValueError as exc:
        raise OrderEncodingError(f"{field}_must_be_bytes32") from exc
    return value.lower()


def build_unsigned_buy_order(*, token_id: Any, price: Any, size: Any, tick_size: Any, min_order_size: Any, maker: str, signer: str, salt: int, timestamp: int, expiration: int = 0, neg_risk: bool, signature_type: int = 0, metadata: str = ZERO_BYTES32, builder: str = ZERO_BYTES32) -> UnsignedOrder:
    if int(token_id) < 0 or salt < 0 or timestamp < 0 or expiration < 0:
        raise OrderEncodingError("negative_order_integer")
    if signature_type not in (0, 1, 2, 3):
        raise OrderEncodingError("invalid_signature_type")
    maker = to_checksum_address(maker)
    signer = to_checksum_address(signer)
    encoded = encode_buy_amounts(price=price, size=size, tick_size=tick_size, min_order_size=min_order_size)
    exchange_address = exchange_for_neg_risk(neg_risk)
    metadata = _bytes32(metadata, "metadata")
    builder = _bytes32(builder, "builder")
    domain = {"name": "Polymarket CTF Exchange", "version": "2", "chainId": CHAIN_ID, "verifyingContract": exchange_address}
    message = {
        "salt": int(salt), "maker": maker, "signer": signer, "tokenId": int(token_id),
        "makerAmount": encoded.maker_amount, "takerAmount": encoded.taker_amount,
        "side": 0, "signatureType": int(signature_type), "timestamp": int(timestamp),
        "metadata": metadata, "builder": builder,
    }
    wire_fields = {
        **message, "side": "BUY", "expiration": str(int(expiration)),
        "timestamp": str(int(timestamp)), "makerAmount": str(encoded.maker_amount),
        "takerAmount": str(encoded.taker_amount), "metadata": metadata, "builder": builder,
    }
    return UnsignedOrder(exchange_address=exchange_address, domain=domain, message=message, wire_fields=wire_fields, encoded_amounts=encoded)


def build_unsigned_buy_order_from_book(*, snapshot: BookSnapshot, price: Any, size: Any, maker: str, signer: str, salt: int, timestamp: int, expiration: int = 0, signature_type: int = 0, metadata: str = ZERO_BYTES32, builder: str = ZERO_BYTES32) -> UnsignedOrder:
    if snapshot.min_order_size is None or snapshot.tick_size is None or snapshot.neg_risk is None:
        raise OrderEncodingError("book_missing_trade_metadata")
    return build_unsigned_buy_order(
        token_id=snapshot.token_id, price=price, size=size,
        tick_size=snapshot.tick_size, min_order_size=snapshot.min_order_size,
        maker=maker, signer=signer, salt=salt, timestamp=timestamp,
        expiration=expiration, neg_risk=snapshot.neg_risk,
        signature_type=signature_type, metadata=metadata, builder=builder,
    )


def sign_order(unsigned: UnsignedOrder, private_key: str) -> tuple[str, str]:
    """Sign an explicit V2 order fixture; returns (signature, recovered address)."""
    typed_data = {
        "types": {"EIP712Domain": [
            {"name": "name", "type": "string"}, {"name": "version", "type": "string"},
            {"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"},
        ], **ORDER_TYPES},
        "primaryType": "Order", "domain": unsigned.domain, "message": unsigned.message,
    }
    encoded = encode_typed_data(full_message=typed_data)
    signed = Account.sign_message(encoded, private_key=private_key)
    recovered = Account.recover_message(encoded, signature=signed.signature)
    return "0x" + signed.signature.hex().removeprefix("0x"), to_checksum_address(recovered)
