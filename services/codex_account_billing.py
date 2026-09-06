"""Read and briefly cache all-time Codex2API billing for visible accounts."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, wait
from datetime import datetime, timezone
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
_ERROR_TTL_SECONDS = 15
_FETCH_DEADLINE_SECONDS = 5.0
_CACHE: WeakKeyDictionary[Engine, dict[AccountBillingKey, tuple[float, BillingSummary]]] = WeakKeyDictionary()
_IN_FLIGHT: WeakKeyDictionary[Engine, dict[AccountBillingKey, Future[BillingSummary]]] = WeakKeyDictionary()
# Preserve first cancellation order so repeated forced refreshes reach every row.
_DEFERRED: WeakKeyDictionary[Engine, dict[AccountBillingKey, None]] = WeakKeyDictionary()
# A completed/cancelled future may invoke its callback in the registering thread.
_CACHE_LOCK = RLock()
# A shared executor bounds concurrent remote reads across overlapping page loads.
_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="codex-account-billing")


def _summary(billed_usd: float | None) -> BillingSummary:
    return {
        "scope": "all",
        "billed_usd": billed_usd,
        "source": "codex2api",
        "status": "available" if billed_usd is not None else "error",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def _fetch(client: Codex2APITargetClient, remote_id: int) -> BillingSummary:
    try:
        usage = client.account_usage_all(remote_id)
        return _summary(float(usage["total_account_billed"]))
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
                future = _EXECUTOR.submit(_fetch, client, remote_id)
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
