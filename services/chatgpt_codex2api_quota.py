"""Estimate remaining Codex2API quota from its 7d usage and billed cost."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterable, Literal, Mapping


CENT = Decimal("0.01")
HUNDRED = Decimal("100")
NORMAL_REMOTE_STATUSES = {
    "active",
    "ready",
    "rate_limited",
    "rate_limited_5h",
    "rate_limited_7d",
    "usage_exhausted",
    "usage_limited",
    "quota_paused",
}
VALID_ESTIMATE_STATES = {"available", "exhausted"}
SUMMARY_QUOTA_FIELDS = (
    "usage_percent_5h",
    "billed_5h",
    "usage_percent_7d",
    "billed_7d",
)


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
    current_remaining_usd: Decimal | None = None
    plan_type: str = ""


@dataclass(frozen=True)
class AvailableQuotaReport:
    account_count: int
    estimated_remaining_usd: Decimal
    accounts: tuple[AvailableQuotaAccount, ...]
    remote_account_count: int = 0
    current_remaining_usd: Decimal | None = None
    total_remaining_usd: Decimal | None = None
    eligible_account_count: int = 0
    current_data_count: int = 0
    total_data_count: int = 0
    current_data_complete: bool = False
    total_data_complete: bool = False
    current_fresh: bool = True
    total_fresh: bool = True
    available: bool = True

    def __post_init__(self):
        if self.current_remaining_usd is None:
            object.__setattr__(
                self,
                "current_remaining_usd",
                self.estimated_remaining_usd,
            )
        if self.total_remaining_usd is None:
            object.__setattr__(
                self,
                "total_remaining_usd",
                self.estimated_remaining_usd,
            )


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


def _quota_row_identities(row: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    identities: list[tuple[str, str]] = []
    remote_id = _remote_id(row.get("remote_id") or row.get("id"))
    if remote_id is not None:
        identities.append(("id", str(remote_id)))
    email = str(row.get("email") or row.get("name") or "").strip().lower()
    if email:
        identities.append(("email", email))
    return tuple(identities)


def merge_quota_rows(
    rows: Iterable[Mapping[str, object]],
    fallback_rows: Iterable[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Fill transiently missing summary fields from the same probe snapshot.

    The fallback is deliberately limited to reset-aligned summary fields.  It
    never imports rolling ``usage_*_detail.account_billed`` values or replaces
    a value returned by the newest response.
    """

    fallback_list = [dict(row) for row in fallback_rows if isinstance(row, Mapping)]
    fallback_by_identity: dict[tuple[str, str], dict[str, object]] = {}
    for row in fallback_list:
        if not isinstance(row, Mapping):
            continue
        for identity in _quota_row_identities(row):
            fallback_by_identity.setdefault(identity, dict(row))
    merged_rows: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        merged = dict(row)
        identities = _quota_row_identities(row)
        fallback = next(
            (fallback_by_identity.get(identity) for identity in identities),
            None,
        )
        if fallback is not None:
            for field in SUMMARY_QUOTA_FIELDS:
                timestamp_key = (
                    "quota_5h_updated_at"
                    if field.endswith("_5h")
                    else "quota_7d_updated_at"
                )
                final_timestamp = merged.get(timestamp_key)
                fallback_timestamp = fallback.get(timestamp_key)
                same_snapshot = not (
                    final_timestamp
                    and fallback_timestamp
                    and final_timestamp != fallback_timestamp
                )
                if not final_timestamp and not fallback_timestamp:
                    percent_key = (
                        "usage_percent_5h"
                        if field.endswith("_5h")
                        else "usage_percent_7d"
                    )
                    same_snapshot = (
                        merged.get(percent_key) is not None
                        and merged.get(percent_key) == fallback.get(percent_key)
                    )
                if (
                    same_snapshot
                    and merged.get(field) is None
                    and fallback.get(field) is not None
                ):
                    merged[field] = fallback[field]
                    merged[
                        "_quota_fallback_5h"
                        if field.endswith("_5h")
                        else "_quota_fallback_7d"
                    ] = True
            if "has_5h_window" not in merged and "has_5h_window" in fallback:
                merged["has_5h_window"] = True
        merged_rows.append(merged)
    return merged_rows


def _has_5h_window(row: Mapping[str, object], plan_type: str) -> bool:
    value = row.get("has_5h_window")
    if value is not None:
        return bool(value)
    return bool(
        row.get("usage_percent_5h") is not None
        or plan_type not in {"", "pro"}
    )


def _is_non_finite_row(row: Mapping[str, object], plan_type: str) -> bool:
    """Recognize explicit 0% windows as unlimited/non-finite, not missing."""

    suffixes = ["7d"]
    if _has_5h_window(row, plan_type):
        suffixes.insert(0, "5h")
    for suffix in suffixes:
        percent = _decimal(row.get(f"usage_percent_{suffix}"))
        billed = _decimal(row.get(f"billed_{suffix}"))
        if percent is None or billed is None or billed < 0 or percent != 0:
            return False
    return True


def estimate_account_quota(row: Mapping[str, object]) -> QuotaEstimate:
    """Classify one account and estimate its remaining 7d USD quota."""

    return estimate_window_quota(row, "7d")


def _estimate_quota_values(percent_value: object, billed_value: object) -> QuotaEstimate:
    percent = _decimal(percent_value)
    billed = _decimal(billed_value)
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
    remote_account_count = 0
    current_total = Decimal("0.00")
    total_total = Decimal("0.00")
    eligible_count = 0
    current_count = 0
    total_count = 0
    healthy_count = 0
    current_used_fallback = False
    total_used_fallback = False
    for row in rows:
        remote_account_count += 1
        status = str(
            row.get("remote_status") or row.get("status") or ""
        ).strip().lower()
        if status not in NORMAL_REMOTE_STATUSES:
            continue
        email = str(row.get("email") or row.get("name") or "").strip()
        plan_type = str(row.get("plan_type") or "").strip().lower()
        if plan_type == "api":
            continue
        if _is_non_finite_row(row, plan_type):
            continue
        current_used_fallback = current_used_fallback or bool(
            row.get("_quota_fallback_5h")
        )
        total_used_fallback = total_used_fallback or bool(
            row.get("_quota_fallback_7d")
        )
        healthy_count += 1
        estimate = estimate_window_quota(row, "7d")
        short_estimate = estimate_window_quota(row, "5h")
        # Accounts with a valid 5-hour window use it for the "current" amount.
        # Accounts without that window (for example Pro) use their weekly
        # estimate so they remain represented instead of contributing zero.
        has_5h_window = _has_5h_window(row, plan_type)
        if short_estimate.state in {"available", "exhausted"}:
            current_estimate = short_estimate
        elif not has_5h_window:
            current_estimate = estimate
        else:
            current_estimate = QuotaEstimate(state="invalid")
        current_valid = current_estimate.state in VALID_ESTIMATE_STATES
        total_valid = estimate.state in VALID_ESTIMATE_STATES
        if current_valid:
            current_count += 1
        if total_valid:
            total_count += 1
        if current_valid or total_valid:
            eligible_count += 1
        if current_valid and current_estimate.remaining_usd is not None:
            current_total += current_estimate.remaining_usd
        if total_valid and estimate.remaining_usd is not None:
            total_total += estimate.remaining_usd
        if not current_valid and not total_valid:
            continue
        # Keep the account list focused on accounts with a non-zero weekly
        # balance, while exhausted rows still count as valid data above.
        if estimate.state != "available" and current_estimate.state != "available":
            continue
        weekly_usage = (
            estimate.usage_percent
            if estimate.usage_percent is not None
            else current_estimate.usage_percent
        )
        weekly_billed = (
            estimate.billed_usd
            if estimate.billed_usd is not None
            else current_estimate.billed_usd
        )
        weekly_remaining = (
            estimate.remaining_usd
            if estimate.remaining_usd is not None
            else Decimal("0.00")
        )
        accounts.append(
            AvailableQuotaAccount(
                email=email,
                remote_id=_remote_id(row.get("remote_id") or row.get("id")),
                usage_percent=weekly_usage or Decimal("0"),
                billed_usd=weekly_billed or Decimal("0"),
                remaining_usd=weekly_remaining,
                current_remaining_usd=(
                    current_estimate.remaining_usd
                    if current_estimate.state == "available"
                    else None
                ),
                plan_type=str(row.get("plan_type") or "").strip().lower(),
            )
        )

    accounts.sort(key=lambda item: (item.email.lower(), item.remote_id or 0))
    return AvailableQuotaReport(
        remote_account_count=remote_account_count,
        account_count=len(accounts),
        estimated_remaining_usd=total_total,
        accounts=tuple(accounts),
        current_remaining_usd=current_total,
        total_remaining_usd=total_total,
        eligible_account_count=eligible_count,
        current_data_count=current_count,
        total_data_count=total_count,
        current_data_complete=(healthy_count > 0 and current_count == healthy_count),
        total_data_complete=(healthy_count > 0 and total_count == healthy_count),
        current_fresh=(
            healthy_count > 0
            and current_count == healthy_count
            and not current_used_fallback
        ),
        total_fresh=(
            healthy_count > 0
            and total_count == healthy_count
            and not total_used_fallback
        ),
        available=bool(
            healthy_count > 0
            and total_count == healthy_count
        ),
    )


def estimate_window_quota(
    row: Mapping[str, object],
    window: Literal["5h", "7d"],
) -> QuotaEstimate:
    """Estimate one independent quota window from the remote row."""
    suffix = "5h" if window == "5h" else "7d"
    return _estimate_quota_values(
        row.get(f"usage_percent_{suffix}"),
        row.get(f"billed_{suffix}"),
    )


__all__ = [
    "AvailableQuotaAccount",
    "AvailableQuotaReport",
    "QuotaEstimate",
    "estimate_account_quota",
    "estimate_window_quota",
    "merge_quota_rows",
    "summarize_available_quota",
]
