"""Continuous, target-independent quota accounting."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable, Mapping

from sqlmodel import Session, select

from core.db import AccountQuotaSnapshotModel, engine as default_engine


CENT = Decimal("0.01")
HUNDRED = Decimal("100")
DEDUPLICATION_WINDOW = timedelta(minutes=5)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _money(value: Any) -> Decimal | None:
    parsed = _decimal(value)
    if parsed is None:
        return None
    return parsed.quantize(CENT, rounding=ROUND_HALF_UP)


def _coerce_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        result = value
    else:
        try:
            result = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if result.tzinfo is None:
        return result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _digest_observation(
    *,
    window: str,
    usage_percent: Decimal | None,
    billed_usd: Decimal | None,
    reset_at: datetime | None,
    source: str,
) -> str:
    payload = {
        "window": str(window or ""),
        "usage_percent": str(usage_percent) if usage_percent is not None else None,
        "billed_usd": str(billed_usd) if billed_usd is not None else None,
        "reset_at": reset_at.isoformat() if reset_at is not None else None,
        "source": str(source or ""),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class QuotaLedgerResult:
    identity_id: str
    window: str
    continuous_billed_usd: Decimal
    billed_usd: Decimal | None
    usage_percent: Decimal | None
    remaining_usd: Decimal | None
    reset_at: datetime | None
    captured_at: datetime
    continuity_state: str
    fresh: bool
    scheduler_eligible: bool
    snapshot_id: int | None = None


def _result_from_row(row: AccountQuotaSnapshotModel) -> QuotaLedgerResult:
    billed = _money(row.billed_usd)
    continuous = _money(
        row.continuous_billed_usd
        if row.continuous_billed_usd is not None
        else row.billed_usd
    ) or Decimal("0.00")
    percent = _decimal(row.usage_percent)
    remaining = _money(row.remaining_usd)
    reset_at = _coerce_datetime(row.reset_at)
    fresh = bool(row.is_fresh)
    state = str(row.continuity_state or "normal")
    return QuotaLedgerResult(
        identity_id=str(row.identity_id),
        window=str(row.window),
        continuous_billed_usd=continuous,
        billed_usd=billed,
        usage_percent=percent,
        remaining_usd=remaining,
        reset_at=reset_at,
        captured_at=_coerce_datetime(row.captured_at) or _utcnow(),
        continuity_state=state,
        fresh=fresh,
        scheduler_eligible=fresh and state not in {"unknown", "ambiguous", "stale"},
        snapshot_id=int(row.id) if row.id is not None else None,
    )


def _remaining(billed: Decimal | None, percent: Decimal | None) -> Decimal | None:
    if billed is None or percent is None or billed < 0 or percent <= 0:
        return None
    if percent >= HUNDRED:
        return Decimal("0.00")
    return (billed * (HUNDRED - percent) / percent).quantize(
        CENT,
        rounding=ROUND_HALF_UP,
    )


def record_snapshot(
    database_engine,
    *,
    identity_id: str,
    local_account_id: int,
    target_id: int | None,
    window: str,
    billed_usd: Any,
    usage_percent: Any = None,
    reset_at: Any = None,
    source: str = "codex2api",
    captured_at: datetime | None = None,
    is_fresh: bool | None = None,
    raw_digest: str = "",
) -> QuotaLedgerResult:
    """Persist one observation and return its continuous ledger projection."""

    target_engine = database_engine or default_engine
    normalized_identity = str(identity_id or "").strip()
    normalized_window = str(window or "").strip().lower()
    if not normalized_identity or not normalized_window:
        raise ValueError("identity_id and window are required")
    captured = _coerce_datetime(captured_at) or _utcnow()
    reset = _coerce_datetime(reset_at)
    billed = _money(billed_usd)
    percent = _decimal(usage_percent)
    digest = str(raw_digest or "").strip() or _digest_observation(
        window=normalized_window,
        usage_percent=percent,
        billed_usd=billed,
        reset_at=reset,
        source=source,
    )
    fresh = bool(reset is not None) if is_fresh is None else bool(is_fresh)
    if billed is None:
        fresh = False

    with Session(target_engine) as session:
        prior = session.exec(
            select(AccountQuotaSnapshotModel)
            .where(AccountQuotaSnapshotModel.identity_id == normalized_identity)
            .where(AccountQuotaSnapshotModel.window == normalized_window)
            .order_by(AccountQuotaSnapshotModel.captured_at.desc())
        ).first()

        if prior is not None:
            prior_captured = _coerce_datetime(prior.captured_at)
            if (
                prior.target_id == target_id
                and str(prior.raw_digest or "") == digest
                and prior_captured is not None
                and abs(captured - prior_captured) <= DEDUPLICATION_WINDOW
            ):
                return _result_from_row(prior)

        prior_billed = _money(
            prior.billed_usd
            if prior is not None
            else None
        )
        prior_continuous = _money(
            prior.continuous_billed_usd
            if prior is not None and prior.continuous_billed_usd is not None
            else prior_billed
        ) or Decimal("0.00")
        continuity_state = "normal"
        continuous = billed or Decimal("0.00")

        if prior is None:
            continuity_state = "normal" if fresh else "unknown"
        else:
            prior_reset = _coerce_datetime(prior.reset_at)
            if reset is None or prior_reset is None:
                continuous = prior_continuous
                continuity_state = "unknown"
                fresh = False
            elif reset != prior_reset:
                continuous = billed or Decimal("0.00")
                continuity_state = "window_reset"
            elif target_id == prior.target_id:
                if billed is not None and prior_billed is not None and billed >= prior_billed:
                    continuous = max(prior_continuous, billed)
                    continuity_state = "normal"
                else:
                    continuous = prior_continuous
                    continuity_state = "monotonic_hold"
            elif billed is not None and prior_billed is not None and billed < prior_billed:
                # A newly imported credential often starts the target-local
                # counter at zero.  Carry the previous total and add the new
                # target's observed contribution instead of dropping history.
                continuous = prior_continuous + billed
                continuity_state = "node_counter_reset"
            else:
                continuous = max(prior_continuous, billed or Decimal("0.00"))
                continuity_state = "target_counter_continuous"

        if not fresh:
            continuity_state = "unknown" if continuity_state == "normal" else continuity_state

        row = AccountQuotaSnapshotModel(
            identity_id=normalized_identity,
            local_account_id=int(local_account_id),
            target_id=int(target_id) if target_id is not None else None,
            window=normalized_window,
            usage_percent=float(percent) if percent is not None else None,
            billed_usd=float(billed) if billed is not None else None,
            continuous_billed_usd=float(continuous),
            remaining_usd=(
                float(_remaining(billed, percent))
                if _remaining(billed, percent) is not None
                else None
            ),
            reset_at=reset,
            source=str(source or "codex2api"),
            captured_at=captured,
            is_fresh=fresh,
            raw_digest=digest,
            continuity_state=continuity_state,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return _result_from_row(row)


def latest_snapshot(
    database_engine,
    *,
    identity_id: str,
    window: str,
) -> QuotaLedgerResult | None:
    target_engine = database_engine or default_engine
    with Session(target_engine) as session:
        row = session.exec(
            select(AccountQuotaSnapshotModel)
            .where(AccountQuotaSnapshotModel.identity_id == str(identity_id or ""))
            .where(AccountQuotaSnapshotModel.window == str(window or "").strip().lower())
            .order_by(AccountQuotaSnapshotModel.captured_at.desc())
        ).first()
        return _result_from_row(row) if row is not None else None


def history(
    database_engine,
    *,
    identity_id: str,
    window: str | None = None,
    limit: int = 200,
) -> list[QuotaLedgerResult]:
    target_engine = database_engine or default_engine
    with Session(target_engine) as session:
        query = select(AccountQuotaSnapshotModel).where(
            AccountQuotaSnapshotModel.identity_id == str(identity_id or "")
        )
        if window:
            query = query.where(
                AccountQuotaSnapshotModel.window == str(window).strip().lower()
            )
        rows = session.exec(
            query.order_by(AccountQuotaSnapshotModel.captured_at.desc()).limit(
                max(min(int(limit or 200), 1000), 1)
            )
        ).all()
        return [_result_from_row(row) for row in rows]


def scheduler_eligibility(result: QuotaLedgerResult | None) -> bool:
    return bool(result is not None and result.scheduler_eligible)


def merge_remote_rows(
    database_engine,
    *,
    identity_id: str,
    local_account_id: int,
    target_id: int,
    rows: Iterable[Mapping[str, Any]],
    captured_at: datetime | None = None,
) -> dict[str, QuotaLedgerResult]:
    """Record the supported 5h/7d fields from a target account row."""

    result: dict[str, QuotaLedgerResult] = {}
    row = next((dict(item) for item in rows if isinstance(item, Mapping)), {})
    if not row:
        return result
    for window in ("5h", "7d"):
        billed = row.get(f"billed_{window}")
        percent = row.get(f"usage_percent_{window}")
        reset = row.get(f"reset_{window}_at") or row.get(
            "codex_5h_usage_updated_at" if window == "5h" else "codex_usage_updated_at"
        )
        if billed is None and percent is None:
            continue
        result[window] = record_snapshot(
            database_engine,
            identity_id=identity_id,
            local_account_id=local_account_id,
            target_id=target_id,
            window=window,
            billed_usd=billed,
            usage_percent=percent,
            reset_at=reset,
            captured_at=captured_at,
        )
    return result


__all__ = [
    "QuotaLedgerResult",
    "history",
    "latest_snapshot",
    "merge_remote_rows",
    "record_snapshot",
    "scheduler_eligibility",
]
