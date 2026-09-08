"""Read and briefly cache all-time Codex2API billing for visible accounts."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, wait
from copy import deepcopy
from datetime import datetime, timezone
from datetime import date
from decimal import Decimal, InvalidOperation
from threading import RLock
from time import monotonic
from typing import Literal, TypedDict
from weakref import WeakKeyDictionary

from sqlalchemy.engine import Engine

from services.codex2api_target_client import Codex2APITargetClient, get_target_client


AccountBillingKey = tuple[int, int]


class BillingSummary(TypedDict):
    scope: Literal["all"]
    billed_usd: float | None
    source: Literal["codex2api"]
    status: Literal["available", "error"]
    fetched_at: str


_SUCCESS_TTL_SECONDS = 60
_DETAIL_TTL_SECONDS = 60
_ERROR_TTL_SECONDS = 15
_FETCH_DEADLINE_SECONDS = 5.0
_CACHE: WeakKeyDictionary[Engine, dict[AccountBillingKey, tuple[float, BillingSummary]]] = WeakKeyDictionary()
_IN_FLIGHT: WeakKeyDictionary[Engine, dict[AccountBillingKey, Future[BillingSummary]]] = WeakKeyDictionary()
# Preserve first cancellation order so repeated forced refreshes reach every row.
_DEFERRED: WeakKeyDictionary[Engine, dict[AccountBillingKey, None]] = WeakKeyDictionary()
_DETAIL_CACHE: WeakKeyDictionary[Engine, dict[AccountBillingKey, tuple[float, dict]]] = WeakKeyDictionary()
# A completed/cancelled future may invoke its callback in the registering thread.
_CACHE_LOCK = RLock()
# A shared executor bounds concurrent remote reads across overlapping page loads.
_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="codex-account-billing")


def read_cached_account_billing_summaries(
    database_engine: Engine, keys: list[AccountBillingKey],
) -> dict[AccountBillingKey, BillingSummary]:
    """Return unexpired billing snapshots without resolving a remote target.

    This is intentionally separate from ``fetch_account_billing_summaries`` so
    a fast account-list render can never accidentally block on an upstream API.
    """
    now = monotonic()
    result: dict[AccountBillingKey, BillingSummary] = {}
    with _CACHE_LOCK:
        cached = _CACHE.get(database_engine, {})
        for key, (expires_at, value) in list(cached.items()):
            if expires_at <= now:
                del cached[key]
            elif key in keys:
                result[key] = dict(value)
    return result


def _summary(billed_usd: float | None) -> BillingSummary:
    return {
        "scope": "all",
        "billed_usd": billed_usd,
        "source": "codex2api",
        "status": "available" if billed_usd is not None else "error",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
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
        day = date.fromisoformat(str(row.get('date', ''))).isoformat()
    except ValueError:
        return None
    amount = _safe_usage_amount(row.get('account_billed'))
    requests = row.get('requests')
    return {'date': day, 'account_billed': amount,
            'requests': requests if isinstance(requests, int) and not isinstance(requests, bool) and requests >= 0 else None}


def _fetch(client: Codex2APITargetClient, remote_id: int, database_engine: Engine | None = None, target_id: int | None = None) -> BillingSummary:
    try:
        usage = client.account_usage_all(remote_id)
        summary = _summary(float(usage["total_account_billed"]))
        if database_engine is not None and target_id is not None:
            today = _usage_day(usage.get('today')) or {}
            detail = {'total_billed_usd': _safe_usage_amount(usage.get('total_account_billed')),
                      'today_date': today.get('date'), 'today_billed_usd': today.get('account_billed'),
                      'today_requests': today.get('requests'), 'fetched_at': summary['fetched_at'],
                      'history': [day for row in (usage.get('history') or []) if (day := _usage_day(row)) is not None]}
            with _CACHE_LOCK:
                _DETAIL_CACHE.setdefault(database_engine, {})[(int(target_id), int(remote_id))] = (monotonic(), detail)
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
    deadline = now + _FETCH_DEADLINE_SECONDS
    with _CACHE_LOCK:
        cached = _CACHE.get(database_engine, {})
        for key, (expires_at, _) in list(cached.items()):
            if expires_at <= now:
                del cached[key]
        if not refresh:
            result.update({key: dict(cached[key][1]) for key in requested if key in cached})

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
                result[key] = _summary(None)
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
                result[key] = _completed_summary(future)
            else:
                result[key] = _summary(None)
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
    now = monotonic()
    with _CACHE_LOCK:
        details = _DETAIL_CACHE.get(database_engine, {})
        needed = [key for key in requested if refresh or key not in details
                  or now - details[key][0] >= _DETAIL_TTL_SECONDS]

    # This bypass is scoped to needed keys. It also lets a previously cancelled
    # key re-enter the existing deferred queue without waiting for error-cache
    # expiry; fresh automatic results never consume another worker slot.
    summaries = fetch_account_billing_summaries(database_engine, needed, refresh=True) if needed else {}
    now = monotonic()
    with _CACHE_LOCK:
        details = _DETAIL_CACHE.get(database_engine, {})
        result = {}
        for key in requested:
            cached = details.get(key)
            if cached is None or now - cached[0] >= _DETAIL_TTL_SECONDS:
                result[key] = None
                continue
            # History is mutable nested data; callers must not alter the cache.
            snapshot = deepcopy(cached[1])
            if refresh and summaries.get(key, {}).get('status') != 'available':
                snapshot['refresh_error'] = True
            result[key] = snapshot
        return result
