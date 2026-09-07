"""Bounded, shared inventory refresh for global operating figures."""
from concurrent.futures import ThreadPoolExecutor, wait
from threading import RLock
from time import monotonic
from weakref import WeakKeyDictionary

from services.codex_inventory import sync_inventory

_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix='operations-inventory')
_IN_FLIGHT = WeakKeyDictionary()
_CACHE = WeakKeyDictionary()
_DEFERRED = WeakKeyDictionary()
_LOCK = RLock()

def _refresh(engine, target_id):
    try:
        result = sync_inventory(engine, target_id=target_id, refresh=False)
        return result.get('targets', 0) == 1 and result.get('errors', 0) == 0
    except Exception:
        return False

def _completed(engine, target_id, future):
    try: ok = False if future.cancelled() else bool(future.result())
    except Exception: ok = False
    with _LOCK:
        if _IN_FLIGHT.get(engine, {}).get(target_id) is future:
            if future.cancelled():
                _DEFERRED.setdefault(engine, {}).setdefault(target_id, None)
                _CACHE.setdefault(engine, {}).pop(target_id, None)
            else:
                _DEFERRED.setdefault(engine, {}).pop(target_id, None)
                _CACHE.setdefault(engine, {})[target_id] = (monotonic(), ok)
            del _IN_FLIGHT[engine][target_id]

def refresh_operations_inventory(engine, target_ids, refresh=False):
    result = {}
    futures = {}
    now = monotonic()
    with _LOCK:
        cached = _CACHE.setdefault(engine, {})
        in_flight = _IN_FLIGHT.setdefault(engine, {})
        priority = {key: index for index, key in enumerate(_DEFERRED.get(engine, {}))}
        for target_id in sorted(target_ids, key=lambda key: priority.get(key, len(priority))):
            entry = cached.get(target_id)
            if not refresh and entry and now - entry[0] < 15:
                result[target_id] = entry[1]
                continue
            future = in_flight.get(target_id)
            if future is None:
                future = _EXECUTOR.submit(_refresh, engine, target_id)
                in_flight[target_id] = future
                future.add_done_callback(lambda completed, target_id=target_id: _completed(engine, target_id, completed))
            futures[future] = target_id
    if futures: wait(futures, timeout=4)
    for future, target_id in futures.items():
        if future.done() and not future.cancelled():
            try: result[target_id] = bool(future.result())
            except Exception: result[target_id] = False
        else:
            result[target_id] = False
            future.cancel()
    return result
