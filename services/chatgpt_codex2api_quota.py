"""Estimate remaining Codex2API quota from its 7d usage and billed cost."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterable, Literal, Mapping


CENT = Decimal("0.01")
HUNDRED = Decimal("100")
NORMAL_REMOTE_STATUSES = {"active", "rate_limited"}
VALID_ESTIMATE_STATES = {"available", "exhausted"}


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


def _has_finite_quota_signal(row: Mapping[str, object]) -> bool:
    """Return whether at least one window has enough data to estimate a cap."""

    for suffix in ("5h", "7d"):
        percent = _decimal(row.get(f"usage_percent_{suffix}"))
        billed = _decimal(row.get(f"billed_{suffix}"))
        if (
            percent is not None
            and billed is not None
            and billed >= 0
            and percent > 0
        ):
            return True
    return False


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
        if not _has_finite_quota_signal(row):
            continue
        healthy_count += 1
        estimate = estimate_window_quota(row, "7d")
        short_estimate = estimate_window_quota(row, "5h")
        # Accounts with a valid 5-hour window use it for the "current" amount.
        # Accounts without that window (for example Pro) use their weekly
        # estimate so they remain represented instead of contributing zero.
        has_5h_window = row.get("has_5h_window")
        if has_5h_window is None:
            has_5h_window = bool(
                row.get("usage_percent_5h") is not None
                or plan_type not in {"", "pro"}
            )
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
        current_fresh=(healthy_count > 0 and current_count == healthy_count),
        total_fresh=(healthy_count > 0 and total_count == healthy_count),
        available=bool(
            healthy_count > 0
            and current_count == healthy_count
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
    "summarize_available_quota",
]
