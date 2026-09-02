"""Small encryption boundary for persisted control-plane secrets."""

from __future__ import annotations

import os
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


MASTER_KEY_ENV = "ACCOUNT_MANAGER_SECRET_KEY"
SEALED_PREFIX = "fernet:v1:"


class SecretStoreError(RuntimeError):
    """A secret cannot be sealed or opened with the installation key."""


def _fernet() -> Fernet:
    key = str(os.getenv(MASTER_KEY_ENV, "") or "").strip().encode("ascii", "ignore")
    if not key:
        raise SecretStoreError(
            f"{MASTER_KEY_ENV} is required for multi-target secrets"
        )
    try:
        return Fernet(key)
    except (TypeError, ValueError):
        raise SecretStoreError(f"{MASTER_KEY_ENV} is not a valid Fernet key") from None


def secret_store_ready() -> bool:
    try:
        _fernet()
        return True
    except SecretStoreError:
        return False


def seal_secret(value: Any) -> str:
    plaintext = str(value or "").strip()
    if not plaintext:
        raise SecretStoreError("secret value is empty")
    token = _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")
    return f"{SEALED_PREFIX}{token}"


def open_secret(value: Any, *, allow_legacy_plaintext: bool = False) -> str:
    stored = str(value or "").strip()
    if not stored:
        return ""
    if not stored.startswith(SEALED_PREFIX):
        if allow_legacy_plaintext:
            return stored
        raise SecretStoreError("secret is not encrypted")
    token = stored[len(SEALED_PREFIX) :].encode("ascii", "ignore")
    try:
        return _fernet().decrypt(token).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError):
        raise SecretStoreError("secret cannot be decrypted with this installation key") from None


__all__ = [
    "MASTER_KEY_ENV",
    "SEALED_PREFIX",
    "SecretStoreError",
    "open_secret",
    "seal_secret",
    "secret_store_ready",
]
