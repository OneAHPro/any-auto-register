"""Locks shared by ChatGPT account mutation workflows."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import threading
from typing import Iterator


_ACCOUNT_LOCKS_GUARD = threading.Lock()
_ACCOUNT_LOCKS: dict[int | str, threading.Lock] = {}
_ACCOUNT_EMAIL_LOCKS_GUARD = threading.Lock()
_ACCOUNT_EMAIL_LOCKS: dict[str, threading.Lock] = {}
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
def chatgpt_account_email_operation_lock(
    email,
    *,
    blocking: bool = False,
) -> Iterator[bool]:
    """Serialize account lifecycle changes before a stable local id exists."""

    key = str(email or "").strip().lower()
    if not key:
        yield False
        return
    with _ACCOUNT_EMAIL_LOCKS_GUARD:
        email_lock = _ACCOUNT_EMAIL_LOCKS.setdefault(key, threading.Lock())
    acquired = email_lock.acquire(blocking=blocking)
    try:
        yield acquired
    finally:
        if acquired:
            email_lock.release()


@contextmanager
def validated_chatgpt_account_operation_lock(
    account_id,
    *,
    email: str,
    created_at: datetime,
    database_engine=None,
    blocking: bool = True,
) -> Iterator[bool]:
    """Hold the account lock only while the saved identity still matches."""

    try:
        normalized_id = int(account_id)
    except (TypeError, ValueError):
        normalized_id = 0
    normalized_email = str(email or "").strip().lower()
    if normalized_id <= 0 or not normalized_email or created_at is None:
        yield False
        return

    with chatgpt_account_operation_lock(
        normalized_id,
        blocking=blocking,
    ) as acquired:
        if not acquired:
            yield False
            return
        from sqlmodel import Session

        from core.db import AccountModel, engine as default_engine

        with Session(database_engine or default_engine) as session:
            account = session.get(AccountModel, normalized_id)
            matches = bool(
                account is not None
                and str(account.platform or "").strip().lower() == "chatgpt"
                and str(account.email or "").strip().lower() == normalized_email
                and account.created_at == created_at
            )
        yield matches


@contextmanager
def codex2api_account_mutation_lock() -> Iterator[None]:
    """Serialize remote credential mutations and allow same-thread nesting."""
    with _CODEX2API_MUTATION_LOCK:
        yield
