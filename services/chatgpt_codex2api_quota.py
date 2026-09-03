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
    current_data_total_count: int = 0
    current_unestimable_count: int = 0
    current_missing_count: int = 0
    current_status: str = "unavailable"
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
    target_id = _remote_id(row.get("target_id"))
    if remote_id is not None:
        identities.append(
            (
                "target_id" if target_id is not None else "id",
                f"{target_id}:{remote_id}" if target_id is not None else str(remote_id),
            )
        )
    email = str(row.get("email") or row.get("name") or "").strip().lower()
    if email and not row.get("_remote_email_missing"):
        identities.append(
            (
                "target_email" if target_id is not None else "email",
                f"{target_id}:{email}" if target_id is not None else email,
            )
        )
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


def _window_is_complete(row: Mapping[str, object], suffix: str) -> bool:
    percent = _decimal(row.get(f"usage_percent_{suffix}"))
    if percent is None or percent <= 0:
        return False
    if percent >= HUNDRED:
        return True
    billed = _decimal(row.get(f"billed_{suffix}"))
    return billed is not None and billed >= 0


def stabilize_quota_rows(
    rows: Iterable[Mapping[str, object]],
    fallback_rows: Iterable[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Reuse a complete same-window observation during a transient cost gap.

    Unlike :func:`merge_quota_rows`, this helper may restore the complete
    percent/cost pair from an older observation.  It requires the same target,
    account identity, and explicit reset boundary, so values from a previous
    provider window or another Codex2API node are never combined.
    """

    fallback_by_identity: dict[tuple[str, str], dict[str, object]] = {}
    for raw_fallback in fallback_rows:
        if not isinstance(raw_fallback, Mapping):
            continue
        fallback = dict(raw_fallback)
        for identity in _quota_row_identities(fallback):
            fallback_by_identity.setdefault(identity, fallback)

    stabilized: list[dict[str, object]] = []
    for raw_row in rows:
        if not isinstance(raw_row, Mapping):
            continue
        row = dict(raw_row)
        fallback = next(
            (
                fallback_by_identity.get(identity)
                for identity in _quota_row_identities(row)
                if fallback_by_identity.get(identity) is not None
            ),
            None,
        )
        if fallback is not None:
            row_target = _remote_id(row.get("target_id"))
            fallback_target = _remote_id(fallback.get("target_id"))
            same_target = row_target == fallback_target
            if same_target:
                for suffix in ("5h", "7d"):
                    if _window_is_complete(row, suffix):
                        continue
                    if not _window_is_complete(fallback, suffix):
                        continue
                    reset_key = f"reset_{suffix}_at"
                    current_reset = str(row.get(reset_key) or "").strip()
                    fallback_reset = str(fallback.get(reset_key) or "").strip()
                    if not current_reset or current_reset != fallback_reset:
                        continue
                    for key in (
                        f"usage_percent_{suffix}",
                        f"billed_{suffix}",
                        f"quota_{suffix}_updated_at",
                        reset_key,
                    ):
                        if key in fallback:
                            row[key] = fallback[key]
                    row[f"_quota_fallback_{suffix}"] = True
        stabilized.append(row)
    return stabilized


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
    if percent is None or percent <= 0:
        return QuotaEstimate(state="invalid")
    if percent >= HUNDRED:
        return QuotaEstimate(
            state="exhausted",
            usage_percent=percent,
            billed_usd=(
                billed.quantize(CENT, rounding=ROUND_HALF_UP)
                if billed is not None and billed >= 0
                else None
            ),
            remaining_usd=Decimal("0.00"),
        )
    if billed is None or billed < 0:
        return QuotaEstimate(state="invalid")

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
    current_unestimable_count = 0
    current_missing_count = 0
    for row in rows:
        remote_account_count += 1
        status = str(
            row.get("remote_status") or row.get("status") or ""
        ).strip().lower()
        if status not in NORMAL_REMOTE_STATUSES:
            continue
        raw_email = str(row.get("email") or "").strip()
        email = raw_email or str(row.get("name") or "").strip()
        # Codex2API can briefly expose token-import placeholders before the
        # account has been hydrated (for example ``at-import-1``).  They are
        # not real subscription accounts and have no quota window to report;
        # counting them as healthy makes an otherwise usable snapshot look
        # incomplete forever.
        if row.get("quota_placeholder") or (
            not raw_email and email.lower().startswith("at-import-")
        ):
            continue
        plan_type = str(row.get("plan_type") or "").strip().lower()
        if plan_type == "api":
            continue
        if _is_non_finite_row(row, plan_type):
            continue
        fallback_5h = bool(row.get("_quota_fallback_5h"))
        fallback_7d = bool(row.get("_quota_fallback_7d"))
        total_used_fallback = total_used_fallback or fallback_7d
        healthy_count += 1
        estimate = estimate_window_quota(row, "7d")
        short_estimate = estimate_window_quota(row, "5h")
        # Accounts with a valid 5-hour window use it for the "current" amount.
        # Accounts without that window (for example Pro) use their weekly
        # estimate so they remain represented instead of contributing zero.
        has_5h_window = _has_5h_window(row, plan_type)
        current_used_fallback = current_used_fallback or fallback_5h or (
            not has_5h_window and fallback_7d
        )
        if short_estimate.state in {"available", "exhausted"}:
            current_estimate = short_estimate
        elif not has_5h_window:
            current_estimate = estimate
        else:
            current_estimate = QuotaEstimate(state="invalid")
            percent_5h = _decimal(row.get("usage_percent_5h"))
            billed_5h = _decimal(row.get("billed_5h"))
            if (
                percent_5h == 0
                and billed_5h is not None
                and billed_5h >= 0
            ):
                current_unestimable_count += 1
            else:
                current_missing_count += 1
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
        current_data_total_count=healthy_count,
        current_unestimable_count=current_unestimable_count,
        current_missing_count=current_missing_count,
        current_status=(
            "fallback"
            if current_used_fallback
            else "pending"
            if current_missing_count
            else "partial_unestimable"
            if current_unestimable_count
            else "complete"
            if healthy_count > 0 and current_count == healthy_count
            else "unavailable"
        ),
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
    "stabilize_quota_rows",
    "summarize_available_quota",
]
