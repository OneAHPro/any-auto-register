"""Locks shared by ChatGPT account mutation workflows."""

from __future__ import annotations

from contextlib import contextmanager
import threading
from typing import Iterator


_ACCOUNT_LOCKS_GUARD = threading.Lock()
_ACCOUNT_LOCKS: dict[int | str, threading.Lock] = {}
_CODEX2API_MUTATION_LOCK = threading.RLock()


def _account_lock_key(account_id) -> int | str:
    try:
        return int(account_id)
    except (TypeError, ValueError):
        return str(account_id or "").strip()


@contextmanager
def chatgpt_account_operation_lock(
    account_id,
    *,
    blocking: bool = False,
) -> Iterator[bool]:
    """Acquire one account's mutation lock and expose whether it was acquired."""
    key = _account_lock_key(account_id)
    with _ACCOUNT_LOCKS_GUARD:
        account_lock = _ACCOUNT_LOCKS.setdefault(key, threading.Lock())
    acquired = account_lock.acquire(blocking=blocking)
    try:
        yield acquired
    finally:
        if acquired:
            account_lock.release()


@contextmanager
def codex2api_account_mutation_lock() -> Iterator[None]:
    """Serialize remote credential mutations and allow same-thread nesting."""
    with _CODEX2API_MUTATION_LOCK:
        yield
