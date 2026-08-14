"""Sanitized Decimal parsing for LeadBee product and balance responses."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

_CENT = Decimal("0.01")
_PRODUCT_ID_KEYS = ("id", "product_id", "productId", "productID")
_PRICE_KEYS = (
    "price",
    "unit_price",
    "unitPrice",
    "sale_price",
    "salePrice",
    "customer_price",
    "customerPrice",
)
_PREFERRED_AVAILABLE_KEYS = {
    "available",
    "availableamount",
    "availablebalance",
}
_FALLBACK_AVAILABLE_KEYS = {
    "balance",
    "amount",
}
_RESERVED_KEYS = {
    "reserved",
    "reservedamount",
    "reservedbalance",
    "frozen",
    "frozenamount",
    "frozenbalance",
}
_CURRENCY_KEYS = {"currency", "currencycode"}
_CURRENCY = re.compile(r"^[A-Z]{3,8}$")


@dataclass(frozen=True, slots=True)
class LeadBeeCapacitySnapshot:
    configured_product_available: bool
    balance_available: Decimal | None
    balance_reserved: Decimal | None
    unit_price: Decimal | None
    currency: str | None

    @property
    def estimated_order_capacity(self) -> int | None:
        if (
            self.balance_available is None
            or self.unit_price is None
            or self.unit_price <= 0
        ):
            return None
        return int(
            (self.balance_available / self.unit_price).to_integral_value(
                rounding=ROUND_FLOOR
            )
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "configured_product_available": self.configured_product_available,
            "balance_available": _format_amount(self.balance_available),
            "balance_reserved": _format_amount(self.balance_reserved),
            "unit_price": _format_amount(self.unit_price),
            "estimated_order_capacity": self.estimated_order_capacity,
            "currency": self.currency,
        }


def parse_leadbee_capacity(
    products_payload: object,
    balance_payload: object,
    *,
    product_id: str,
) -> LeadBeeCapacitySnapshot:
    normalized_product_id = str(product_id or "").strip()
    matched_product: dict[str, Any] | None = None
    for candidate in _walk_dicts(products_payload):
        candidate_ids = {
            value.strip()
            for key in _PRODUCT_ID_KEYS
            if isinstance((value := candidate.get(key)), str) and value.strip()
        }
        if normalized_product_id in candidate_ids:
            matched_product = candidate
            break

    product_available = False
    unit_price: Decimal | None = None
    product_currency: str | None = None
    if matched_product is not None:
        status = str(matched_product.get("status") or "").strip().upper()
        product_available = status not in {
            "DISABLED",
            "INACTIVE",
            "UNAVAILABLE",
            "SUSPENDED",
        }
        for key in _PRICE_KEYS:
            if key in matched_product:
                unit_price = _decimal_amount(
                    matched_product.get(key),
                    positive=True,
                )
                if unit_price is not None:
                    break
        product_currency = _currency_from_value(matched_product.get("currency"))

    preferred_available_value: object = None
    fallback_available_value: object = None
    reserved_value: object = None
    balance_currency: str | None = None
    for candidate in _walk_dicts(balance_payload):
        for key, value in candidate.items():
            normalized_key = _normalized_key(key)
            if (
                preferred_available_value is None
                and normalized_key in _PREFERRED_AVAILABLE_KEYS
            ):
                if not isinstance(value, (dict, list)):
                    preferred_available_value = value
            if (
                fallback_available_value is None
                and normalized_key in _FALLBACK_AVAILABLE_KEYS
            ):
                if not isinstance(value, (dict, list)):
                    fallback_available_value = value
            if reserved_value is None and normalized_key in _RESERVED_KEYS:
                if not isinstance(value, (dict, list)):
                    reserved_value = value
            if balance_currency is None and normalized_key in _CURRENCY_KEYS:
                balance_currency = _currency_from_value(value)

    return LeadBeeCapacitySnapshot(
        configured_product_available=product_available,
        balance_available=_decimal_amount(
            preferred_available_value
            if preferred_available_value is not None
            else fallback_available_value
        ),
        balance_reserved=_decimal_amount(reserved_value),
        unit_price=unit_price,
        currency=balance_currency or product_currency,
    )


def _walk_dicts(value: object):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _normalized_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _decimal_amount(value: object, *, positive: bool = False) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not amount.is_finite() or amount < 0 or (positive and amount <= 0):
        return None
    return amount


def _format_amount(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.quantize(_CENT, rounding=ROUND_HALF_UP), "f")


def _currency_from_value(value: object) -> str | None:
    if isinstance(value, dict):
        value = value.get("code")
    if not isinstance(value, str):
        return None
    candidate = value.strip().upper()
    return candidate if _CURRENCY.fullmatch(candidate) else None
