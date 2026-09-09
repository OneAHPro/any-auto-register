"""Read and briefly cache all-time Codex2API billing for visible accounts."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, wait
from copy import deepcopy
from datetime import datetime, timezone
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import logging
from threading import RLock
from time import monotonic
from typing import Literal, TypedDict
from weakref import WeakKeyDictionary

from sqlalchemy import and_, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from core.operations_models import OperationsBillingSnapshotModel
from services.codex2api_target_client import Codex2APITargetClient, get_target_client


AccountBillingKey = tuple[int, int]


class BillingSummary(TypedDict):
    scope: Literal["all"]
    billed_usd: float | None
    source: str
    status: Literal["available", "error"]
    fetched_at: str


_SUCCESS_TTL_SECONDS = 60
_DETAIL_TTL_SECONDS = 60
_ERROR_TTL_SECONDS = 15
_FETCH_DEADLINE_SECONDS = 5.0
_DATABASE_BATCH_SIZE = 1000
_LOCAL_SNAPSHOT_QUERY_BATCH_SIZE = 500
_CACHE: WeakKeyDictionary[Engine, dict[AccountBillingKey, tuple[float, BillingSummary]]] = WeakKeyDictionary()
_IN_FLIGHT: WeakKeyDictionary[Engine, dict[AccountBillingKey, Future[BillingSummary]]] = WeakKeyDictionary()
# Preserve first cancellation order so repeated forced refreshes reach every row.
_DEFERRED: WeakKeyDictionary[Engine, dict[AccountBillingKey, None]] = WeakKeyDictionary()
_DETAIL_CACHE: WeakKeyDictionary[Engine, dict[AccountBillingKey, tuple[float, dict]]] = WeakKeyDictionary()
# Direct PostgreSQL reads use their own bounded executor.  A slow database
# must not occupy the API worker that is rendering an account page, and a
# concurrent page load should join an existing target batch while it is in
# flight instead of opening another full ``usage_logs`` scan.
_DB_IN_FLIGHT: WeakKeyDictionary[
    Engine,
    dict[tuple[int, bool, tuple[int, ...]], Future[dict[AccountBillingKey, dict]]],
] = WeakKeyDictionary()
_DB_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="codex-account-db")
# A completed/cancelled future may invoke its callback in the registering thread.
_CACHE_LOCK = RLock()
# A shared executor bounds concurrent remote reads across overlapping page loads.
_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="codex-account-billing")
_LOGGER = logging.getLogger(__name__)


def _database_adapter_for_target(target_id: int):
    """Build an optional direct database reader for one target."""
    try:
        from services.codex2api_db import get_codex2api_db_adapter

        return get_codex2api_db_adapter(int(target_id))
    except Exception as exc:
        _LOGGER.debug("初始化 Codex2API PostgreSQL 读取器失败: %s", type(exc).__name__)
        return None


def _timestamp_datetime(value) -> datetime | None:
    """Parse a timestamp and normalize it to an aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    elif isinstance(value, datetime):
        parsed = value
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _timestamp(value) -> str | None:
    """Normalize a persisted/provider timestamp to an ISO-8601 UTC string."""

    parsed = _timestamp_datetime(value)
    return parsed.isoformat() if parsed is not None else None


def _snapshot_amount(micros) -> float | None:
    """Convert a durable integer micro-dollar amount to a finite float."""
    if micros is None or isinstance(micros, bool):
        return None
    try:
        amount = Decimal(int(micros)) / Decimal(1_000_000)
    except (ValueError, TypeError, InvalidOperation, OverflowError):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    result = float(amount)
    return result if result == result and result != float("inf") and result != float("-inf") else None


def _snapshot_amount_text(micros) -> str | None:
    """Convert integer micro-dollars to a stable decimal string."""
    if micros is None or isinstance(micros, bool):
        return None
    try:
        amount = Decimal(int(micros)) / Decimal(1_000_000)
    except (ValueError, TypeError, InvalidOperation, OverflowError):
        return None
    return format(amount, "f") if amount.is_finite() and amount >= 0 else None


def _summary_from_snapshot(row: OperationsBillingSnapshotModel) -> BillingSummary | None:
    amount = _snapshot_amount(row.total_billed_micros)
    if amount is None:
        return None
    return {
        "scope": "all",
        "billed_usd": amount,
        "source": str(row.source or "codex2api")[:80],
        "status": "error" if str(row.error or "").strip() else "available",
        "fetched_at": _timestamp(row.captured_at) or "",
    }


def _snapshot_rows_for_keys(database_engine: Engine, keys: list[AccountBillingKey]):
    """Read rows for explicit target/remote keys, tolerating old schemas."""
    if not keys:
        return []
    try:
        with Session(database_engine) as session:
            result = []
            for start in range(0, len(keys), _LOCAL_SNAPSHOT_QUERY_BATCH_SIZE):
                chunk = keys[start : start + _LOCAL_SNAPSHOT_QUERY_BATCH_SIZE]
                conditions = [
                    (OperationsBillingSnapshotModel.target_id == int(target_id))
                    & (OperationsBillingSnapshotModel.remote_id == int(remote_id))
                    for target_id, remote_id in chunk
                ]
                result.extend(
                    session.exec(
                        select(OperationsBillingSnapshotModel).where(or_(*conditions))
                    ).all()
                )
            return result
    except Exception as exc:
        # A rolling deployment can briefly run against a database created before
        # the operations table existed. A local snapshot is an optimization and
        # must never make the account list fail in that interval.
        _LOGGER.debug("读取本地计费快照失败: %s", type(exc).__name__)
        return []


def _row_is_newer(candidate: OperationsBillingSnapshotModel, current: OperationsBillingSnapshotModel) -> bool:
    candidate_stamp = _timestamp_datetime(candidate.captured_at)
    current_stamp = _timestamp_datetime(current.captured_at)
    if candidate_stamp is None:
        return False
    if current_stamp is None:
        return True
    if candidate_stamp != current_stamp:
        return candidate_stamp > current_stamp
    return int(candidate.id or 0) > int(current.id or 0)


def read_persisted_account_billing_summaries(
    database_engine: Engine, keys: list[AccountBillingKey],
) -> dict[AccountBillingKey, BillingSummary]:
    """Read durable all-time billing snapshots keyed by ``(target, remote)``.

    Rows are deliberately returned even when older than the in-process cache
    TTL. They are the last known value and let a restarted service render cards
    while a later reconciliation refreshes the upstream value.
    """
    requested = list(dict.fromkeys(keys))
    if not requested:
        return {}
    rows = _snapshot_rows_for_keys(database_engine, requested)
    by_key: dict[AccountBillingKey, OperationsBillingSnapshotModel] = {}
    requested_set = set(requested)
    for row in rows:
        key = (int(row.target_id), int(row.remote_id))
        if key not in requested_set:
            continue
        previous = by_key.get(key)
        if previous is None or _row_is_newer(row, previous):
            by_key[key] = row
    result: dict[AccountBillingKey, BillingSummary] = {}
    for key, row in by_key.items():
        summary = _summary_from_snapshot(row)
        if summary is not None:
            result[key] = summary
    return result


def _detail_from_snapshot(row: OperationsBillingSnapshotModel) -> dict | None:
    summary = _summary_from_snapshot(row)
    if summary is None:
        return None
    history: list[dict] = []
    try:
        raw_history = json.loads(row.history_json or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        raw_history = []
    if isinstance(raw_history, list):
        for item in raw_history:
            normalized = _usage_day(item)
            if normalized is not None:
                history.append(normalized)
    result = {
        "total_billed_usd": _snapshot_amount_text(row.total_billed_micros),
        "today_date": str(row.today_date or "") or None,
        "today_billed_usd": (
            _snapshot_amount_text(row.today_billed_micros)
            if row.today_billed_micros is not None else None
        ),
        "today_requests": (
            int(row.today_requests)
            if isinstance(row.today_requests, int)
            and not isinstance(row.today_requests, bool)
            and row.today_requests >= 0 else None
        ),
        "fetched_at": summary["fetched_at"],
        "history": history,
    }
    if str(row.error or "").strip():
        result["refresh_error"] = True
    return result


def read_persisted_account_usage_details(
    database_engine: Engine, keys: list[AccountBillingKey],
) -> dict[AccountBillingKey, dict]:
    """Read sanitized daily/all-time details from durable billing snapshots."""
    requested = list(dict.fromkeys(keys))
    if not requested:
        return {}
    rows = _snapshot_rows_for_keys(database_engine, requested)
    by_key: dict[AccountBillingKey, OperationsBillingSnapshotModel] = {}
    requested_set = set(requested)
    for row in rows:
        key = (int(row.target_id), int(row.remote_id))
        if key not in requested_set:
            continue
        previous = by_key.get(key)
        if previous is None or _row_is_newer(row, previous):
            by_key[key] = row
    result: dict[AccountBillingKey, dict] = {}
    for key, row in by_key.items():
        detail = _detail_from_snapshot(row)
        if detail is not None:
            result[key] = detail
    return result


def _persisted_detail_is_complete(detail: dict) -> bool:
    """Whether a durable row has enough history for trend/detail consumers."""
    if detail.get("refresh_error"):
        return False
    history = detail.get("history")
    if not isinstance(history, list):
        return False
    if history:
        return True
    # An empty history is definitive for an account whose all-time counter is
    # exactly zero. A non-zero total with no history usually came from a
    # summary-only refresh and must be expanded before building trends.
    try:
        return Decimal(str(detail.get("total_billed_usd") or "0")) == 0
    except (InvalidOperation, ValueError, TypeError):
        return False


def read_cached_account_billing_summaries(
    database_engine: Engine, keys: list[AccountBillingKey],
) -> dict[AccountBillingKey, BillingSummary]:
    """Return cached billing, falling back to durable local snapshots.

    This is intentionally separate from ``fetch_account_billing_summaries`` so
    a fast account-list render can never accidentally block on an upstream API.
    Durable rows are not subject to the short in-process TTL: they represent
    the last known value and are safe to show while the background refresh runs.
    """
    requested = list(dict.fromkeys(keys))
    if not requested:
        return {}
    now = monotonic()
    result: dict[AccountBillingKey, BillingSummary] = {}
    with _CACHE_LOCK:
        cached = _CACHE.get(database_engine, {})
        for key, (expires_at, value) in list(cached.items()):
            if expires_at <= now:
                del cached[key]
            elif key in requested:
                result[key] = dict(value)
    # Query the durable table even when an in-process value exists. Another
    # worker/process may have captured a newer total since this process cached
    # its value; choosing the newest timestamp avoids regressing the card.
    persisted = read_persisted_account_billing_summaries(database_engine, requested)
    for key, value in persisted.items():
        current = result.get(key)
        if current is None or (
            value.get("status") == "available"
            and current.get("status") != "available"
        ) or (
            value.get("status") == current.get("status") == "available"
            and (_timestamp(value.get("fetched_at")) or "")
            >= (_timestamp(current.get("fetched_at")) or "")
        ) or (
            value.get("status") != "available"
            and current.get("status") != "available"
            and (_timestamp(value.get("fetched_at")) or "")
            >= (_timestamp(current.get("fetched_at")) or "")
        ):
            result[key] = dict(value)
    return result


def _summary(
    billed_usd: float | None,
    *,
    fetched_at: str | None = None,
    source: str = "codex2api",
) -> BillingSummary:
    return {
        "scope": "all",
        "billed_usd": billed_usd,
        "source": str(source or "codex2api")[:80],
        "status": "available" if billed_usd is not None else "error",
        "fetched_at": _timestamp(fetched_at)
        or datetime.now(timezone.utc).isoformat(),
    }


def _safe_usage_amount(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return str(number) if number.is_finite() and number >= 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _usage_day(row):
    if not isinstance(row, dict):
        return None
    try:
        day = date.fromisoformat(
            str(row.get('date') or row.get('day') or '')
        ).isoformat()
    except ValueError:
        return None
    raw_amount = row.get("account_billed")
    if raw_amount is None:
        raw_amount = row.get("billed_usd")
    amount = _safe_usage_amount(raw_amount)
    requests = row.get('requests')
    return {'date': day, 'account_billed': amount,
            'requests': requests if isinstance(requests, int) and not isinstance(requests, bool) and requests >= 0 else None}


def _detail_micros(value) -> int | None:
    amount = _safe_usage_amount(value)
    if amount is None:
        return None
    try:
        micros = int((Decimal(amount) * Decimal(1_000_000)).to_integral_value(rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        return None
    return micros if micros >= 0 else None


def _snapshot_write_values(
    detail: dict,
    *,
    include_history: bool,
    total_requests: int | None,
    last_request_at: str | None,
    source: str,
    history_complete: bool | None,
) -> tuple[int | None, dict, datetime | None]:
    """Normalize one detail into durable columns without touching the DB."""

    total_micros = _detail_micros(detail.get("total_billed_usd"))
    if total_micros is None:
        return None, {}, None
    values: dict[str, object] = {
        "total_billed_micros": total_micros,
        "source": str(source or "codex2api_api")[:80],
        "error": "",
    }
    if (
        isinstance(total_requests, int)
        and not isinstance(total_requests, bool)
        and total_requests >= 0
    ):
        values["total_requests"] = int(total_requests)
    raw_today_date = str(detail.get("today_date") or "").strip()
    try:
        today_date = date.fromisoformat(raw_today_date).isoformat()
    except (TypeError, ValueError):
        today_date = ""
    today_values_present = bool(today_date) and any(
        detail.get(key) not in (None, "")
        for key in ("today_date", "today_billed_usd", "today_requests")
    )
    if today_values_present:
        values["today_date"] = today_date
        if detail.get("today_billed_usd") is not None:
            values["today_billed_micros"] = _detail_micros(
                detail.get("today_billed_usd")
            )
        requests_value = detail.get("today_requests")
        if (
            isinstance(requests_value, int)
            and not isinstance(requests_value, bool)
            and requests_value >= 0
        ):
            values["today_requests"] = int(requests_value)
    captured_at_value = None
    captured = _timestamp(detail.get("fetched_at"))
    if captured is not None:
        try:
            captured_at_value = datetime.fromisoformat(captured)
            values["captured_at"] = captured_at_value
        except ValueError:
            pass
    parsed_last_request = _timestamp(last_request_at)
    if parsed_last_request is not None:
        try:
            values["last_request_at"] = datetime.fromisoformat(parsed_last_request)
        except ValueError:
            pass
    history = detail.get("history")
    if (
        include_history
        and history_complete is not False
        and isinstance(history, list)
    ):
        normalized_history = [
            normalized
            for item in history[:10_000]
            if (normalized := _usage_day(item)) is not None
        ]
        values["history_json"] = json.dumps(
            normalized_history, ensure_ascii=False, separators=(",", ":")
        )
    return total_micros, values, captured_at_value


def _merge_snapshot_values_with_existing(
    existing: OperationsBillingSnapshotModel,
    values: dict,
) -> dict:
    """Keep cumulative counters and the newest business-day window."""

    merged = dict(values)
    incoming_requests = merged.get("total_requests")
    if (
        incoming_requests is not None
        and existing.total_requests is not None
    ):
        try:
            merged["total_requests"] = max(
                int(existing.total_requests), int(incoming_requests)
            )
        except (TypeError, ValueError):
            pass

    if "history_json" in merged:
        try:
            incoming_history = json.loads(merged.get("history_json") or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            incoming_history = []
        try:
            existing_history = json.loads(existing.history_json or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            existing_history = []
        if isinstance(existing_history, list) and existing_history:
            by_day: dict[str, dict] = {}
            for item in existing_history + (
                incoming_history if isinstance(incoming_history, list) else []
            ):
                if not isinstance(item, dict):
                    continue
                day = str(item.get("date") or item.get("day") or "").strip()
                if not day:
                    continue
                current = by_day.setdefault(day, {})
                for field, value in item.items():
                    if value in (None, "", [], {}):
                        continue
                    if field in {"account_billed", "billed_usd", "requests", "tokens"}:
                        try:
                            if field in current and current[field] is not None:
                                current[field] = max(
                                    Decimal(str(current[field])), Decimal(str(value))
                                )
                                if field in {"requests", "tokens"}:
                                    current[field] = int(current[field])
                                else:
                                    current[field] = str(current[field])
                                continue
                        except (InvalidOperation, TypeError, ValueError):
                            pass
                    current[field] = value
            if by_day:
                merged["history_json"] = json.dumps(
                    [by_day[day] for day in sorted(by_day)],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            else:
                merged.pop("history_json", None)

    incoming_last = _timestamp_datetime(merged.get("last_request_at"))
    current_last = _timestamp_datetime(existing.last_request_at)
    if incoming_last is not None and current_last is not None:
        if incoming_last < current_last:
            merged.pop("last_request_at", None)
    elif incoming_last is None and current_last is not None:
        merged.pop("last_request_at", None)

    incoming_day = str(merged.get("today_date") or "").strip()
    current_day = str(existing.today_date or "").strip()
    if incoming_day and current_day:
        if incoming_day < current_day:
            for field in (
                "today_date",
                "today_billed_micros",
                "today_requests",
            ):
                merged.pop(field, None)
        elif incoming_day == current_day:
            for field in ("today_billed_micros", "today_requests"):
                incoming = merged.get(field)
                current = getattr(existing, field, None)
                if incoming is None or current is None:
                    continue
                try:
                    merged[field] = max(int(current), int(incoming))
                except (TypeError, ValueError):
                    pass
        else:
            # A new business day supersedes the prior day's window.  Explicit
            # nulls clear stale values when the new response omits a metric.
            merged.setdefault("today_billed_micros", None)
            merged.setdefault("today_requests", None)
    return merged


def _persist_usage_snapshot(
    database_engine: Engine,
    key: AccountBillingKey,
    detail: dict,
    *,
    include_history: bool = True,
    total_requests: int | None = None,
    last_request_at: str | None = None,
    source: str = "codex2api_api",
    history_complete: bool | None = None,
) -> bool:
    """Best-effort upsert of one sanitized usage snapshot.

    The remote read remains authoritative for the current response, so a
    temporary local database lock or an older release without the table must
    never turn a successful upstream read into an error. All-time counters are
    monotonic; a delayed response with a smaller total updates the daily fields
    only when its total is not accepted, and leaves the durable row untouched.
    """
    total_micros, values, captured_at_value = _snapshot_write_values(
        detail,
        include_history=include_history,
        total_requests=total_requests,
        last_request_at=last_request_at,
        source=source,
        history_complete=history_complete,
    )
    if total_micros is None:
        return False
    target_id, remote_id = key
    if int(target_id) <= 0 or int(remote_id) <= 0:
        return False
    total_column = OperationsBillingSnapshotModel.total_billed_micros
    if captured_at_value is None:
        # Without a capture timestamp an equal-total response has no ordering
        # proof; preserve the existing daily/history fields instead of letting
        # an older or sparse read overwrite them.
        total_guard = or_(total_column.is_(None), total_column < total_micros)
    else:
        total_guard = or_(
            total_column.is_(None),
            total_column < total_micros,
            and_(
                total_column == total_micros,
                or_(
                    OperationsBillingSnapshotModel.captured_at.is_(None),
                    OperationsBillingSnapshotModel.captured_at <= captured_at_value,
                ),
            ),
        )

    # Use a conditional UPDATE so an older concurrent response can never
    # overwrite a newer all-time counter. If two processes race to create the
    # row, the loser retries the conditional update after its insert conflict.
    for attempt in range(2):
        try:
            with Session(database_engine) as session:
                existing = session.exec(
                    select(OperationsBillingSnapshotModel)
                    .where(
                        OperationsBillingSnapshotModel.target_id == int(target_id),
                        OperationsBillingSnapshotModel.remote_id == int(remote_id),
                    )
                    .order_by(
                        OperationsBillingSnapshotModel.captured_at.desc().nulls_last(),
                        OperationsBillingSnapshotModel.id.desc(),
                    )
                ).first()
                if existing is not None:
                    current_total = existing.total_billed_micros
                    if current_total is None:
                        accepts = True
                    elif total_micros > int(current_total):
                        accepts = True
                    elif total_micros < int(current_total):
                        accepts = False
                    elif captured_at_value is None:
                        accepts = False
                    else:
                        current_capture = _timestamp_datetime(existing.captured_at)
                        accepts = (
                            current_capture is None
                            or captured_at_value >= current_capture
                        )
                    if not accepts:
                        session.rollback()
                        return False
                    effective_values = _merge_snapshot_values_with_existing(
                        existing,
                        values,
                    )
                    updated = session.exec(
                        update(OperationsBillingSnapshotModel)
                        .where(OperationsBillingSnapshotModel.id == existing.id)
                        .where(total_guard)
                        .values(**effective_values)
                    )
                    if int(getattr(updated, "rowcount", 0) or 0) != 1:
                        session.rollback()
                        if attempt == 1:
                            return False
                        continue
                    session.commit()
                    return True
                session.add(
                    OperationsBillingSnapshotModel(
                        target_id=int(target_id),
                        remote_id=int(remote_id),
                        **values,
                    )
                )
                session.commit()
                return True
        except IntegrityError:
            if attempt == 1:
                return False
            continue
        except Exception as exc:
            # Persistence is deliberately best effort. The account card can
            # still display the just-fetched response when SQLite is busy or
            # an older release has not created the table yet.
            _LOGGER.debug("写入本地计费快照失败: %s", type(exc).__name__)
            return False


def persist_account_usage_snapshot(
    database_engine: Engine,
    key: AccountBillingKey,
    detail: dict,
    *,
    include_history: bool = True,
    total_requests: int | None = None,
    last_request_at: str | None = None,
    source: str = "codex2api",
    history_complete: bool | None = None,
) -> bool:
    """Persist one sanitized usage detail through the monotonic CAS writer.

    This small public wrapper keeps overview/reporting code from reaching into
    the model table directly.  All writers therefore share the same guards for
    decreasing totals, equal-timestamp races, and sparse history responses.
    """

    return bool(_persist_usage_snapshot(
        database_engine,
        key,
        detail,
        include_history=include_history,
        total_requests=total_requests,
        last_request_at=last_request_at,
        source=source,
        history_complete=history_complete,
    ))


def _persist_usage_snapshots_batch(
    database_engine: Engine,
    entries: list[tuple[AccountBillingKey, dict, int | None, str | None]],
    *,
    include_history: bool,
    source: str,
) -> set[AccountBillingKey]:
    """Persist a target batch in one local transaction.

    Direct PostgreSQL reads can return thousands of accounts.  Opening and
    committing one SQLite transaction per account turns that fast remote batch
    into a long write queue, so the normal path resolves existing rows once and
    applies all monotonic guards in a single transaction.  A concurrent unique
    race falls back to the proven per-row CAS writer.
    """

    normalized: list[tuple[AccountBillingKey, int, dict, datetime | None]] = []
    for key, detail, total_requests, last_request_at in entries:
        try:
            target_id, remote_id = int(key[0]), int(key[1])
        except (TypeError, ValueError, IndexError):
            continue
        total_micros, values, captured_at = _snapshot_write_values(
            detail,
            include_history=include_history,
            total_requests=total_requests,
            last_request_at=last_request_at,
            source=source,
            history_complete=True if include_history else False,
        )
        if total_micros is None or target_id <= 0 or remote_id <= 0:
            continue
        normalized.append(((target_id, remote_id), total_micros, values, captured_at))
    if not normalized:
        return set()

    accepted: set[AccountBillingKey] = set()

    try:
        with Session(database_engine) as session:
            existing_rows = []
            for start in range(0, len(normalized), _LOCAL_SNAPSHOT_QUERY_BATCH_SIZE):
                chunk = normalized[start : start + _LOCAL_SNAPSHOT_QUERY_BATCH_SIZE]
                conditions = [
                    (OperationsBillingSnapshotModel.target_id == key[0])
                    & (OperationsBillingSnapshotModel.remote_id == key[1])
                    for key, _total, _values, _captured in chunk
                ]
                existing_rows.extend(
                    session.exec(
                        select(OperationsBillingSnapshotModel).where(or_(*conditions))
                    ).all()
                )
            existing_by_key = {
                (int(row.target_id), int(row.remote_id)): row for row in existing_rows
            }
            for key, total_micros, values, captured_at in normalized:
                row = existing_by_key.get(key)
                if row is not None:
                    current_total = row.total_billed_micros
                    current_captured = row.captured_at
                    accept = current_total is None or int(current_total) < total_micros
                    if current_total is not None and int(current_total) == total_micros:
                        if captured_at is None:
                            accept = False
                        else:
                            current_stamp = _timestamp(current_captured) or ""
                            incoming_stamp = _timestamp(captured_at) or ""
                            accept = not current_stamp or incoming_stamp >= current_stamp
                    if not accept:
                        continue
                    effective_values = _merge_snapshot_values_with_existing(
                        row,
                        values,
                    )
                    for field, value in effective_values.items():
                        setattr(row, field, value)
                    session.add(row)
                    accepted.add(key)
                else:
                    row = OperationsBillingSnapshotModel(
                        target_id=key[0],
                        remote_id=key[1],
                        **values,
                    )
                    session.add(row)
                    existing_by_key[key] = row
                    accepted.add(key)
            session.commit()
            return accepted
    except IntegrityError:
        # Another process may have inserted one of the natural keys after the
        # batch read.  Preserve correctness by retrying through the per-row
        # conditional writer rather than dropping the entire batch.
        for key, detail, total_requests, last_request_at in entries:
            if _persist_usage_snapshot(
                database_engine,
                key,
                detail,
                include_history=include_history,
                total_requests=total_requests,
                last_request_at=last_request_at,
                source=source,
                history_complete=True if include_history else False,
            ):
                accepted.add(key)
        return accepted
    except Exception as exc:
        _LOGGER.debug("批量写入本地计费快照失败: %s", type(exc).__name__)
        # Keep the remote read useful even when a legacy local schema is being
        # upgraded; individual best-effort retries retain any rows that can be
        # written without making the page wait.
        for key, detail, total_requests, last_request_at in entries:
            if _persist_usage_snapshot(
                database_engine,
                key,
                detail,
                include_history=include_history,
                total_requests=total_requests,
                last_request_at=last_request_at,
                source=source,
                history_complete=True if include_history else False,
            ):
                accepted.add(key)
        return accepted


def _detail_from_database_row(row: dict) -> dict | None:
    """Translate the direct-reader shape into the existing detail contract."""
    if not isinstance(row, dict):
        return None
    total = row.get("total") if isinstance(row.get("total"), dict) else {}
    today = row.get("today") if isinstance(row.get("today"), dict) else {}
    total_amount = _safe_usage_amount(
        total.get("billed_usd", row.get("total_account_billed"))
    )
    if total_amount is None:
        return None
    today_amount = _safe_usage_amount(
        today.get("billed_usd", row.get("today_account_billed"))
    )
    today_requests = today.get("requests", row.get("today_requests"))
    if not isinstance(today_requests, int) or isinstance(today_requests, bool) or today_requests < 0:
        today_requests = None
    history: list[dict] = []
    raw_history = row.get("history")
    if isinstance(raw_history, list):
        for item in raw_history:
            if not isinstance(item, dict):
                continue
            day = item.get("date", item.get("day"))
            try:
                day_value = date.fromisoformat(str(day)).isoformat()
            except (TypeError, ValueError):
                continue
            requests = item.get("requests")
            if not isinstance(requests, int) or isinstance(requests, bool) or requests < 0:
                requests = None
            amount = _safe_usage_amount(
                item.get("account_billed", item.get("billed_usd"))
            )
            history.append({"date": day_value, "account_billed": amount, "requests": requests})
    return {
        "total_billed_usd": total_amount,
        "today_date": str(today.get("date") or row.get("today_date") or "") or None,
        "today_billed_usd": today_amount,
        "today_requests": today_requests,
        "fetched_at": _timestamp(
            row.get("fetched_at") or row.get("updated_at") or row.get("last_request_at")
        ) or datetime.now(timezone.utc).isoformat(),
        "history": history,
    }


def _merge_canonical_detail(canonical: dict, incoming: dict) -> dict:
    """Retain fields present only in a just-read sparse detail."""

    merged = deepcopy(canonical)
    for field in ("today_date", "today_billed_usd", "today_requests"):
        if merged.get(field) in (None, "") and incoming.get(field) not in (None, ""):
            merged[field] = incoming[field]
    if (
        not isinstance(merged.get("history"), list)
        or not merged.get("history")
    ) and isinstance(incoming.get("history"), list) and incoming.get("history"):
        merged["history"] = deepcopy(incoming["history"])
    return merged


def _fetch_database_details_sync(
    database_engine: Engine,
    keys: list[AccountBillingKey],
    *,
    include_history: bool = True,
) -> dict[AccountBillingKey, dict]:
    """Read configured targets in batches and warm both local caches."""
    grouped: dict[int, list[AccountBillingKey]] = {}
    for key in keys:
        try:
            target_id, remote_id = int(key[0]), int(key[1])
        except (TypeError, ValueError, IndexError):
            continue
        if target_id <= 0 or remote_id <= 0:
            continue
        grouped.setdefault(target_id, []).append((target_id, remote_id))
    result: dict[AccountBillingKey, dict] = {}
    for target_id, target_keys in grouped.items():
        adapter = _database_adapter_for_target(target_id)
        if adapter is None:
            continue
        rows: dict[Any, Any] = {}
        for start in range(0, len(target_keys), _DATABASE_BATCH_SIZE):
            chunk = target_keys[start : start + _DATABASE_BATCH_SIZE]
            try:
                try:
                    chunk_rows = adapter.fetch_account_snapshots(
                        chunk,
                        include_history=include_history,
                    )
                except TypeError:
                    # Keep compatibility with injected/older readers that
                    # expose the original two-argument method.
                    chunk_rows = adapter.fetch_account_snapshots(chunk)
            except Exception as exc:
                _LOGGER.debug(
                    "读取 Codex2API PostgreSQL 快照失败: %s", type(exc).__name__
                )
                continue
            if isinstance(chunk_rows, dict):
                rows.update(chunk_rows)
        if not rows:
            continue
        requested_keys = set(target_keys)
        pending_writes: list[
            tuple[AccountBillingKey, dict, int | None, str | None]
        ] = []
        for raw_key, row in rows.items():
            try:
                key = (int(raw_key[0]), int(raw_key[1]))
            except (TypeError, ValueError, IndexError, KeyError):
                continue
            if key not in requested_keys:
                continue
            if isinstance(row, dict):
                try:
                    row_target = int(row.get("target_id") or key[0])
                    row_remote = int(row.get("remote_id") or key[1])
                except (TypeError, ValueError):
                    continue
                if (row_target, row_remote) != key:
                    continue
            detail = _detail_from_database_row(row)
            if detail is None:
                continue
            # ``fetched_at`` describes when this control plane observed the
            # database result.  The provider's account ``updated_at`` and the
            # latest usage-log timestamp are separate business fields and
            # must not make a freshly read snapshot look arbitrarily old.
            detail["fetched_at"] = datetime.now(timezone.utc).isoformat()
            pending_writes.append(
                (
                    key,
                    detail,
                    (
                        row.get("total", {}).get("requests")
                        if isinstance(row.get("total"), dict)
                        else row.get("total_requests")
                    ),
                    row.get("last_request_at"),
                )
            )
            result[key] = detail
        accepted_keys = _persist_usage_snapshots_batch(
            database_engine,
            pending_writes,
            include_history=include_history,
            source="codex2api_postgresql",
        )
        # A concurrent API/overview writer may have won the monotonic CAS with
        # a newer total.  Never warm this process's cache from the rejected
        # older response; prefer the canonical durable row when it exists.
        canonical_details = read_persisted_account_usage_details(
            database_engine,
            list(result),
        )
        for key, detail in list(result.items()):
            canonical = canonical_details.get(key)
            selected = detail
            if canonical is not None:
                try:
                    incoming_total = Decimal(str(detail.get("total_billed_usd")))
                    canonical_total = Decimal(str(canonical.get("total_billed_usd")))
                except (InvalidOperation, TypeError, ValueError):
                    incoming_total = canonical_total = None
                if canonical_total is not None and (
                    incoming_total is None or canonical_total >= incoming_total
                ):
                    selected = _merge_canonical_detail(canonical, detail)
            result[key] = selected
            # If the durable read was rejected and no canonical row is
            # available (for example during a rolling schema upgrade), the
            # response remains useful but is deliberately left uncached.
            if key not in accepted_keys and canonical is None:
                continue
            summary = _summary(
                float(selected["total_billed_usd"]),
                fetched_at=selected.get("fetched_at"),
                source="codex2api_postgresql",
            )
            with _CACHE_LOCK:
                if include_history:
                    _DETAIL_CACHE.setdefault(database_engine, {})[key] = (
                        monotonic(),
                        deepcopy(selected),
                    )
                _CACHE.setdefault(database_engine, {})[key] = (
                    monotonic() + _SUCCESS_TTL_SECONDS,
                    summary,
                )
    return result


def _complete_database_batch(
    database_engine: Engine,
    batch_key: tuple[int, bool, tuple[int, ...]],
    future: Future[dict[AccountBillingKey, dict]],
) -> None:
    """Drop a completed target batch while leaving its warmed caches intact."""

    with _CACHE_LOCK:
        batches = _DB_IN_FLIGHT.get(database_engine, {})
        if batches.get(batch_key) is future:
            del batches[batch_key]


def _ephemeral_sqlite_engine(database_engine: Engine) -> bool:
    """Return whether an engine's in-memory DB is connection-local.

    The application uses a file-backed SQLite engine in normal operation, but
    lightweight callers and tests often pass ``sqlite://``.  A worker thread
    opening a new connection to that URL sees a different empty database, so
    those engines must keep the batch on the caller's connection.
    """

    try:
        return (
            str(database_engine.dialect.name).lower() == "sqlite"
            and str(database_engine.url.database or "") in {"", ":memory:"}
        )
    except Exception:
        return False


def _fetch_database_details(
    database_engine: Engine,
    keys: list[AccountBillingKey],
    *,
    include_history: bool = True,
    deadline: float | None = None,
) -> dict[AccountBillingKey, dict]:
    """Read direct database batches without blocking past the page deadline."""

    if not keys:
        return {}
    grouped: dict[int, list[AccountBillingKey]] = {}
    for key in keys:
        try:
            target_id, remote_id = int(key[0]), int(key[1])
        except (TypeError, ValueError, IndexError):
            continue
        if target_id > 0 and remote_id > 0:
            grouped.setdefault(target_id, []).append((target_id, remote_id))
    if not grouped:
        return {}
    if _ephemeral_sqlite_engine(database_engine):
        # Preserve the connection-local in-memory database contract.  This is
        # only a test/embedded-runtime path; PostgreSQL and file-backed SQLite
        # remain bounded by the executor below.
        return _fetch_database_details_sync(
            database_engine,
            keys,
            include_history=include_history,
        )
    effective_deadline = (
        deadline if deadline is not None else monotonic() + _FETCH_DEADLINE_SECONDS
    )
    # Leave a small portion of the page budget for the per-account HTTP
    # fallback when the direct reader is slow or unavailable.  The overall
    # caller deadline is still enforced; this only prevents a blocked database
    # socket from consuming every millisecond before the fallback can start.
    fallback_reserve = min(0.25, max(0.0, _FETCH_DEADLINE_SECONDS * 0.25))
    database_deadline = max(monotonic(), effective_deadline - fallback_reserve)
    futures: list[Future[dict[AccountBillingKey, dict]]] = []
    for target_id, target_keys in grouped.items():
        unique_keys = list(dict.fromkeys(target_keys))
        batch_key = (
            target_id,
            bool(include_history),
            tuple(sorted(key[1] for key in unique_keys)),
        )
        with _CACHE_LOCK:
            batches = _DB_IN_FLIGHT.setdefault(database_engine, {})
            future = batches.get(batch_key)
            if future is None:
                # De-duplicate keys before handing them to the adapter; this
                # also bounds the size of the SQL ``IN`` list for overlapping
                # callers.
                future = _DB_EXECUTOR.submit(
                    _fetch_database_details_sync,
                    database_engine,
                    unique_keys,
                    include_history=include_history,
                )
                batches[batch_key] = future
                future.add_done_callback(
                    lambda completed, engine=database_engine, key=batch_key:
                    _complete_database_batch(engine, key, completed)
                )
            futures.append(future)
    if futures:
        wait(futures, timeout=max(0.0, database_deadline - monotonic()))
    result: dict[AccountBillingKey, dict] = {}
    for future in futures:
        if not future.done():
            continue
        try:
            value = future.result()
        except Exception as exc:
            _LOGGER.debug("读取 Codex2API PostgreSQL 批次失败: %s", type(exc).__name__)
            continue
        if isinstance(value, dict):
            result.update(value)
    return result


def _fetch(client: Codex2APITargetClient, remote_id: int, database_engine: Engine | None = None, target_id: int | None = None) -> BillingSummary:
    try:
        usage = client.account_usage_all(remote_id)
        total = _safe_usage_amount(usage.get("total_account_billed"))
        if total is None:
            return _summary(None)
        summary = _summary(float(total))
        if database_engine is not None and target_id is not None:
            account_key = (int(target_id), int(remote_id))
            detail = {
                'total_billed_usd': _safe_usage_amount(usage.get('total_account_billed')),
                'fetched_at': summary['fetched_at'],
            }
            today_raw = usage.get('today')
            today = _usage_day(today_raw) if isinstance(today_raw, dict) else None
            if today is not None:
                detail.update({
                    'today_date': today.get('date'),
                    'today_billed_usd': today.get('account_billed'),
                    'today_requests': today.get('requests'),
                })
            history_complete = isinstance(usage.get('history'), list)
            if history_complete:
                detail['history'] = [
                    day for row in usage.get('history', [])
                    if (day := _usage_day(row)) is not None
                ]
            _persist_usage_snapshot(
                database_engine,
                account_key,
                detail,
                total_requests=usage.get("total_requests"),
                last_request_at=usage.get("last_request_at"),
                source="codex2api_api",
                history_complete=history_complete,
            )
            canonical = read_persisted_account_usage_details(
                database_engine,
                [account_key],
            ).get(account_key)
            if canonical is not None:
                try:
                    canonical_total = Decimal(str(canonical.get("total_billed_usd")))
                    current_total = Decimal(str(detail.get("total_billed_usd")))
                except (InvalidOperation, TypeError, ValueError):
                    canonical_total = current_total = None
                if canonical_total is not None and (
                    current_total is None or canonical_total >= current_total
                ):
                    detail = _merge_canonical_detail(canonical, detail)
                    summary = _summary(
                        float(canonical_total),
                        fetched_at=canonical.get("fetched_at"),
                        source="codex2api_postgresql"
                        if str(canonical.get("source") or "").startswith("codex2api_postgresql")
                        else "codex2api",
                    )
            with _CACHE_LOCK:
                existing_detail = _DETAIL_CACHE.get(database_engine, {}).get(account_key)
                existing_has_history = bool(
                    existing_detail
                    and isinstance(existing_detail[1], dict)
                    and isinstance(existing_detail[1].get("history"), list)
                    and existing_detail[1].get("history")
                )
                if history_complete or canonical is not None or not existing_has_history:
                    # A sparse response is useful when no complete history is
                    # available yet, but it must not replace a complete detail
                    # already held in the process cache.
                    _DETAIL_CACHE.setdefault(database_engine, {})[account_key] = (
                        monotonic(),
                        deepcopy(detail),
                    )
        return summary
    except Exception:
        # Keep per-account failures isolated and upstream details out of the API.
        return _summary(None)


def _remember(database_engine: Engine, key: AccountBillingKey, billing: BillingSummary) -> None:
    ttl = _SUCCESS_TTL_SECONDS if billing["status"] == "available" else _ERROR_TTL_SECONDS
    with _CACHE_LOCK:
        _CACHE.setdefault(database_engine, {})[key] = (monotonic() + ttl, dict(billing))


def _completed_summary(future: Future[BillingSummary]) -> BillingSummary:
    try:
        return dict(future.result())
    except Exception:
        return _summary(None)


def _complete(
    database_engine: Engine, key: AccountBillingKey, future: Future[BillingSummary],
) -> None:
    billing = _completed_summary(future)
    with _CACHE_LOCK:
        in_flight = _IN_FLIGHT.get(database_engine, {})
        if in_flight.get(key) is future:
            deferred = _DEFERRED.setdefault(database_engine, {})
            if future.cancelled():
                deferred.setdefault(key, None)
            else:
                deferred.pop(key, None)
            _remember(database_engine, key, billing)
            del in_flight[key]


def fetch_account_billing_summaries(
    database_engine: Engine,
    keys: list[AccountBillingKey],
    refresh: bool = False,
    *,
    _deadline: float | None = None,
    _skip_database: bool = False,
) -> dict[AccountBillingKey, BillingSummary]:
    """Fetch only the supplied target/account pairs, without inventory or probes.

    Successes live in process memory for 60 seconds; errors live for 15 seconds.
    Refreshes reuse any in-flight read for the same account. The page waits at
    most five seconds for remote reads and cancels work that has not started.
    Running requests still update the cache after the page has returned.
    Return values contain only display billing fields, never target secrets.
    """

    requested = list(dict.fromkeys(keys))
    if not requested:
        return {}
    result: dict[AccountBillingKey, BillingSummary] = {}
    now = monotonic()
    deadline = _deadline if _deadline is not None else now + _FETCH_DEADLINE_SECONDS
    memory_keys: set[AccountBillingKey] = set()
    with _CACHE_LOCK:
        cached = _CACHE.get(database_engine, {})
        for key, (expires_at, _) in list(cached.items()):
            if expires_at <= now:
                del cached[key]
            elif key in requested:
                memory_keys.add(key)
        if not refresh:
            result.update({key: dict(cached[key][1]) for key in requested if key in cached})

    # Keep the durable value as a fallback for a restarted process or a cache
    # that has expired. We still try the upstream read for a normal/forced fetch
    # so a healthy target can replace an old snapshot immediately.
    persisted = read_persisted_account_billing_summaries(database_engine, requested)
    if not refresh:
        for key, value in persisted.items():
            current = result.get(key)
            if current is None or (
                value.get("status") == "available"
                and current.get("status") != "available"
            ) or (
                value.get("status") == current.get("status") == "available"
                and (_timestamp(value.get("fetched_at")) or "")
                >= (_timestamp(current.get("fetched_at")) or "")
            ):
                # A different worker/process may have captured a newer durable
                # total since this process populated its short memory cache.
                result[key] = dict(value)

    # When a target has a configured PostgreSQL reader, one grouped query is
    # preferred to one HTTP request per account. The API remains the fallback
    # for targets without a reader or for rows the database cannot return.
    database_needed = requested if refresh else [key for key in requested if key not in result]
    if database_needed and not _skip_database:
        database_details = _fetch_database_details(
            database_engine,
            database_needed,
            include_history=False,
            deadline=deadline,
        )
        for key, detail in database_details.items():
            try:
                result[key] = _summary(
                    float(detail["total_billed_usd"]),
                    fetched_at=detail.get("fetched_at"),
                    source="codex2api_postgresql",
                )
            except (KeyError, TypeError, ValueError):
                continue

    def fallback_for(key: AccountBillingKey, candidate: BillingSummary | None = None) -> BillingSummary:
        if candidate is not None:
            if candidate.get("status") == "available":
                return candidate
            # Preserve the exact timestamp generated by the completed remote
            # future. This keeps the existing short-error-cache contract stable
            # for callers comparing successive responses.
            if key not in persisted or key in memory_keys:
                return candidate
            return dict(persisted[key])
        # Preserve the historical behavior for an in-process forced refresh:
        # callers that already had a memory value continue to see an explicit
        # error when that refresh fails. A durable-only value (typical after a
        # restart) remains visible until a later refresh succeeds.
        if key not in memory_keys and key in persisted:
            return dict(persisted[key])
        return _summary(None)

    missing = [key for key in requested if key not in result]
    with _CACHE_LOCK:
        deferred_order = {
            key: index for index, key in enumerate(_DEFERRED.get(database_engine, {}))
        }
    missing.sort(key=lambda key: deferred_order.get(key, len(deferred_order)))
    clients: dict[int, Codex2APITargetClient | None] = {}
    for target_id, _ in missing:
        if target_id not in clients:
            try:
                clients[target_id] = get_target_client(target_id, database_engine=database_engine)
            except Exception:
                clients[target_id] = None

    futures: dict[Future[BillingSummary], AccountBillingKey] = {}
    for key in missing:
        target_id, remote_id = key
        client = clients[target_id]
        with _CACHE_LOCK:
            in_flight = _IN_FLIGHT.setdefault(database_engine, {})
            future = in_flight.get(key)
            if future is not None:
                futures[future] = key
                continue
            # Another page may have completed this account while clients resolved.
            cached = _CACHE.get(database_engine, {}).get(key)
            if not refresh and cached is not None and cached[0] > monotonic():
                result[key] = dict(cached[1])
            elif client is None or monotonic() >= deadline:
                result[key] = fallback_for(key)
                _remember(database_engine, key, result[key])
            else:
                future = _EXECUTOR.submit(_fetch, client, remote_id, database_engine, target_id)
                in_flight[key] = future
                futures[future] = key
                future.add_done_callback(
                    lambda completed, key=key: _complete(database_engine, key, completed)
                )
    if futures:
        wait(futures, timeout=max(0.0, deadline - monotonic()))
    for future, key in futures.items():
        with _CACHE_LOCK:
            if future.done():
                result[key] = fallback_for(key, _completed_summary(future))
            else:
                result[key] = fallback_for(key)
                _remember(database_engine, key, result[key])
                # Running requests keep their callback and overwrite this short
                # error cache when they finish. Queued requests release their slot.
                future.cancel()
    return result


def fetch_account_usage_details(database_engine: Engine, keys: list[AccountBillingKey], refresh: bool = False) -> dict[AccountBillingKey, dict | None]:
    """Merge independently cached account details with bounded shared reads.

    Automatic polling only fills missing or expired details. Manual refreshes
    still attempt every requested account; when a read fails, a successful
    snapshot younger than sixty seconds is returned with ``refresh_error``.
    Its original ``fetched_at`` remains intact so consumers can show its age.
    Expired or never-observed accounts remain unknown rather than becoming zero.
    """
    requested = list(dict.fromkeys(keys))
    if not requested:
        return {}
    persisted = read_persisted_account_usage_details(database_engine, requested)
    deadline = monotonic() + _FETCH_DEADLINE_SECONDS
    now = monotonic()
    cached_fresh: set[AccountBillingKey] = set()
    needed: list[AccountBillingKey] = []
    with _CACHE_LOCK:
        details = _DETAIL_CACHE.get(database_engine, {})
        for key in requested:
            cached = details.get(key)
            if not refresh and cached is not None and now - cached[0] < _DETAIL_TTL_SECONDS:
                cached_fresh.add(key)
            elif not refresh and key in persisted and _persisted_detail_is_complete(persisted[key]):
                # A durable row is sufficient for the fast overview path. A
                # later explicit refresh will still attempt the upstream read.
                continue
            else:
                needed.append(key)

    # Prefer one database batch for full details. This bypass is scoped to
    # needed keys; API reads below remain the fallback for targets without a
    # configured reader or rows the reader could not return.
    database_details = _fetch_database_details(
        database_engine,
        needed,
        include_history=True,
        deadline=deadline,
    ) if needed else {}
    remaining = [key for key in needed if key not in database_details]
    # This API path also lets a previously cancelled key re-enter the deferred
    # queue without waiting for error-cache expiry.
    summaries = fetch_account_billing_summaries(
        database_engine,
        remaining,
        refresh=True,
        _deadline=deadline,
        _skip_database=True,
    ) if remaining else {}
    now = monotonic()
    with _CACHE_LOCK:
        details = _DETAIL_CACHE.get(database_engine, {})
        result = {}
        for key in requested:
            cached = details.get(key)
            snapshot = None
            if cached is not None and (
                key in cached_fresh
                or key in needed and now - cached[0] < _DETAIL_TTL_SECONDS
            ):
                # History is mutable nested data; callers must not alter the
                # process cache or the SQLModel object that produced it.
                snapshot = deepcopy(cached[1])
            elif key in persisted:
                snapshot = deepcopy(persisted[key])
            if snapshot is None:
                result[key] = None
                continue
            if refresh and summaries.get(key, {}).get('status') != 'available' and key in needed:
                snapshot['refresh_error'] = True
            result[key] = snapshot
        return result
