"""Estimate remaining Codex2API quota from its 7d usage and billed cost."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterable, Literal, Mapping


CENT = Decimal("0.01")
HUNDRED = Decimal("100")
NORMAL_REMOTE_STATUSES = {"active", "rate_limited"}


@dataclass(frozen=True)
class QuotaEstimate:
    state: Literal["available", "exhausted", "invalid"]
    usage_percent: Decimal | None = None
    billed_usd: Decimal | None = None
    remaining_usd: Decimal | None = None


@dataclass(frozen=True)
class AvailableQuotaAccount:
    email: str
    remote_id: int | None
    usage_percent: Decimal
    billed_usd: Decimal
    remaining_usd: Decimal


@dataclass(frozen=True)
class AvailableQuotaReport:
    account_count: int
    estimated_remaining_usd: Decimal
    accounts: tuple[AvailableQuotaAccount, ...]


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    return parsed if parsed.is_finite() else None


def _remote_id(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def estimate_account_quota(row: Mapping[str, object]) -> QuotaEstimate:
    """Classify one account and estimate its remaining 7d USD quota."""

    percent = _decimal(row.get("usage_percent_7d"))
    billed = _decimal(row.get("billed_7d"))
    if percent is None or billed is None or billed < 0 or percent <= 0:
        return QuotaEstimate(state="invalid")
    if percent >= HUNDRED:
        return QuotaEstimate(
            state="exhausted",
            usage_percent=percent,
            billed_usd=billed.quantize(CENT, rounding=ROUND_HALF_UP),
            remaining_usd=Decimal("0.00"),
        )

    remaining = (
        billed * (HUNDRED - percent) / percent
    ).quantize(CENT, rounding=ROUND_HALF_UP)
    return QuotaEstimate(
        state="available",
        usage_percent=percent,
        billed_usd=billed.quantize(CENT, rounding=ROUND_HALF_UP),
        remaining_usd=remaining,
    )


def summarize_available_quota(
    rows: Iterable[Mapping[str, object]],
) -> AvailableQuotaReport:
    """Aggregate estimated USD quota for normal, non-exhausted accounts."""

    accounts: list[AvailableQuotaAccount] = []
    for row in rows:
        status = str(
            row.get("remote_status") or row.get("status") or ""
        ).strip().lower()
        if status not in NORMAL_REMOTE_STATUSES:
            continue
        estimate = estimate_account_quota(row)
        if (
            estimate.state != "available"
            or estimate.usage_percent is None
            or estimate.billed_usd is None
            or estimate.remaining_usd is None
        ):
            continue
        email = str(row.get("email") or row.get("name") or "").strip()
        accounts.append(
            AvailableQuotaAccount(
                email=email,
                remote_id=_remote_id(row.get("remote_id") or row.get("id")),
                usage_percent=estimate.usage_percent,
                billed_usd=estimate.billed_usd,
                remaining_usd=estimate.remaining_usd,
            )
        )

    accounts.sort(key=lambda item: (item.email.lower(), item.remote_id or 0))
    total = sum(
        (item.remaining_usd for item in accounts),
        start=Decimal("0.00"),
    ).quantize(CENT, rounding=ROUND_HALF_UP)
    return AvailableQuotaReport(
        account_count=len(accounts),
        estimated_remaining_usd=total,
        accounts=tuple(accounts),
    )


__all__ = [
    "AvailableQuotaAccount",
    "AvailableQuotaReport",
    "QuotaEstimate",
    "estimate_account_quota",
    "summarize_available_quota",
]
