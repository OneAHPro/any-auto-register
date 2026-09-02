"""Stable account identity resolution for the account-control plane."""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable
from uuid import uuid4

from sqlalchemy import func, update
from sqlmodel import Session, select

from core.db import (
    AccountIdentityAliasModel,
    AccountIdentityModel,
    AccountModel,
    engine as default_engine,
)


_IDENTITY_HMAC_KEY = b"any-auto-register-account-identity-v1"
_IDENTITY_ALIAS_TYPES = {"workspace_id", "chatgpt_account_id"}
_STRONG_ALIAS_TYPES = {
    "workspace_id",
    "chatgpt_account_id",
    "credential_fingerprint",
}
_IDENTITY_MUTATION_LOCK = threading.RLock()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_email(value: Any) -> str:
    return str(value or "").strip().lower()


def normalize_alias(value: Any) -> str:
    return str(value or "").strip().lower()


def credential_fingerprint(
    platform: str,
    email: str,
    *,
    refresh_token: str = "",
    access_token: str = "",
    session_token: str = "",
    workspace_id: str = "",
    chatgpt_account_id: str = "",
) -> str:
    """Return a stable non-secret fingerprint for a credential identity."""

    canonical = {
        "platform": normalize_alias(platform),
        "email": normalize_email(email),
        "refresh_token": str(refresh_token or ""),
        "access_token": str(access_token or ""),
        "session_token": str(session_token or ""),
        "workspace_id": normalize_alias(workspace_id),
        "chatgpt_account_id": normalize_alias(chatgpt_account_id),
    }
    payload = json.dumps(canonical, ensure_ascii=True, sort_keys=True).encode("utf-8")
    return hmac.new(_IDENTITY_HMAC_KEY, payload, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class IdentityResolution:
    identity_id: str
    created: bool
    ambiguous: bool
    state: str


def _alias_values(
    *,
    email: str,
    workspace_id: str = "",
    chatgpt_account_id: str = "",
    credential_fingerprint_value: str = "",
) -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []
    for alias_type, value in (
        ("email", email),
        ("workspace_id", workspace_id),
        ("chatgpt_account_id", chatgpt_account_id),
        ("credential_fingerprint", credential_fingerprint_value),
    ):
        normalized = normalize_alias(value)
        if normalized:
            values.append((alias_type, normalized))
    return values


def _identity_ids_for_aliases(
    session: Session,
    *,
    platform: str,
    email: str,
    aliases: Iterable[tuple[str, str]],
) -> dict[tuple[str, str], set[str]]:
    normalized_platform = normalize_alias(platform)
    result: dict[tuple[str, str], set[str]] = {}
    for alias_type, value in aliases:
        rows = session.exec(
            select(AccountIdentityAliasModel)
            .join(
                AccountIdentityModel,
                AccountIdentityModel.id == AccountIdentityAliasModel.identity_id,
            )
            .where(AccountIdentityModel.platform == normalized_platform)
            .where(AccountIdentityModel.canonical_email == normalize_email(email))
            .where(AccountIdentityAliasModel.alias_type == alias_type)
            .where(AccountIdentityAliasModel.normalized_value == value)
        ).all()
        result[(alias_type, value)] = {
            str(row.identity_id) for row in rows if str(row.identity_id or "").strip()
        }
    return result


def _mark_ambiguous(session: Session, identity_ids: Iterable[str]) -> None:
    now = _utcnow()
    for identity_id in set(identity_ids):
        row = session.get(AccountIdentityModel, identity_id)
        if row is not None:
            row.state = "ambiguous"
            row.updated_at = now
            session.add(row)


def _upsert_aliases(
    session: Session,
    *,
    identity_id: str,
    aliases: Iterable[tuple[str, str]],
    source: str,
) -> set[str]:
    now = _utcnow()
    identity = session.get(AccountIdentityModel, identity_id)
    identity_platform = normalize_alias(identity.platform if identity else "")
    conflicts: set[str] = set()
    for alias_type, value in aliases:
        rows = session.exec(
            select(AccountIdentityAliasModel)
            .join(
                AccountIdentityModel,
                AccountIdentityModel.id == AccountIdentityAliasModel.identity_id,
            )
            .where(AccountIdentityModel.platform == identity_platform)
            .where(AccountIdentityAliasModel.alias_type == alias_type)
            .where(AccountIdentityAliasModel.normalized_value == value)
        ).all()
        other_ids = {
            str(row.identity_id)
            for row in rows
            if str(row.identity_id or "") and str(row.identity_id) != identity_id
        }
        if other_ids and alias_type in _STRONG_ALIAS_TYPES:
            conflicts.update(other_ids)
            continue
        own = next(
            (row for row in rows if str(row.identity_id) == identity_id),
            None,
        )
        if own is None:
            session.add(
                AccountIdentityAliasModel(
                    identity_id=identity_id,
                    platform=identity_platform,
                    alias_type=alias_type,
                    normalized_value=value,
                    source=str(source or ""),
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
        else:
            own.last_seen_at = now
            if source:
                own.source = str(source)
            session.add(own)
    return conflicts


def _ensure_identity_impl(
    database_engine,
    *,
    account_id: int,
    platform: str,
    email: str,
    workspace_id: str = "",
    chatgpt_account_id: str = "",
    credential_fingerprint: str = "",
    source: str = "",
) -> IdentityResolution:
    """Resolve or create one identity using strong aliases before email."""

    target_engine = database_engine or default_engine
    normalized_platform = normalize_alias(platform)
    normalized_email = normalize_email(email)
    if not normalized_platform:
        raise ValueError("platform is required for account identity")
    if not normalized_email:
        raise ValueError("email is required for account identity")

    aliases = _alias_values(
        email=normalized_email,
        workspace_id=workspace_id,
        chatgpt_account_id=chatgpt_account_id,
        credential_fingerprint_value=credential_fingerprint,
    )
    identity_aliases = [alias for alias in aliases if alias[0] in _IDENTITY_ALIAS_TYPES]
    fingerprint_aliases = [
        alias for alias in aliases if alias[0] == "credential_fingerprint"
    ]

    with Session(target_engine) as session:
        existing_account = session.get(AccountModel, int(account_id or 0))
        existing_identity_id = str(
            getattr(existing_account, "identity_id", "") or ""
        ).strip()
        existing_identity = (
            session.get(AccountIdentityModel, existing_identity_id)
            if existing_identity_id
            else None
        )
        ids_by_alias = _identity_ids_for_aliases(
            session,
            platform=normalized_platform,
            email=normalized_email,
            aliases=aliases,
        )
        identity_ids = set().union(
            *(ids_by_alias.get(alias, set()) for alias in identity_aliases)
        )
        fingerprint_ids = set().union(
            *(ids_by_alias.get(alias, set()) for alias in fingerprint_aliases)
        )
        email_ids = ids_by_alias.get(("email", normalized_email), set())
        conflict_ids: set[str] = set()
        created = False

        if (
            existing_identity is not None
            and normalize_alias(existing_identity.platform) == normalized_platform
        ):
            # A re-login/upsert of the same local row is authoritative even
            # when every remote credential fingerprint has changed.
            identity_id = str(existing_identity.id)
            if email_ids and not email_ids.issubset({identity_id}):
                conflict_ids.update(email_ids)
                conflict_ids.add(identity_id)
        elif len(identity_ids) == 1:
            identity_id = next(iter(identity_ids))
            if email_ids and not email_ids.issubset({identity_id}):
                # An exact workspace/account alias is stronger than a shared
                # email.  Reuse it, but keep every same-email identity marked
                # ambiguous for operator visibility.
                conflict_ids.update(email_ids)
                conflict_ids.add(identity_id)
        elif len(identity_ids) > 1:
            conflict_ids.update(identity_ids)
            identity_id = ""
        elif len(email_ids) == 1 and not identity_aliases:
            identity_id = next(iter(email_ids))
        elif len(fingerprint_ids) == 1 and not identity_aliases:
            identity_id = next(iter(fingerprint_ids))
        else:
            identity_id = ""
            conflict_ids.update(email_ids)

        if not identity_id:
            identity_id = str(uuid4())
            created = True
            session.add(
                AccountIdentityModel(
                    id=identity_id,
                    platform=normalized_platform,
                    canonical_email=normalized_email,
                    state="ambiguous" if conflict_ids else "active",
                    current_account_id=int(account_id or 0),
                )
            )

        identity = session.get(AccountIdentityModel, identity_id)
        if identity is None:
            # The row can be absent only when a concurrent writer won a race;
            # create it here and let the database commit establish ownership.
            identity = AccountIdentityModel(
                id=identity_id,
                platform=normalized_platform,
                canonical_email=normalized_email,
                state="active",
                current_account_id=int(account_id or 0),
            )
            session.add(identity)
        identity.canonical_email = normalized_email or identity.canonical_email
        identity.current_account_id = int(account_id or 0)
        identity.updated_at = _utcnow()
        session.add(identity)

        alias_conflicts = _upsert_aliases(
            session,
            identity_id=identity_id,
            aliases=aliases,
            source=source,
        )
        conflict_ids.update(alias_conflicts)
        if conflict_ids:
            conflict_ids.add(identity_id)
            _mark_ambiguous(session, conflict_ids)
            identity.state = "ambiguous"
            session.add(identity)

        account = session.get(AccountModel, int(account_id or 0))
        if account is not None:
            if str(account.identity_id or "") != identity_id:
                account.identity_id = identity_id
                account.updated_at = _utcnow()
                session.add(account)
        else:
            # Legacy test databases or an import race may not have the row;
            # the identity remains valid and can be linked during reconcile.
            session.exec(
                update(AccountModel)
                .where(AccountModel.id == int(account_id or 0))
                .values(identity_id=identity_id)
            )
        session.commit()
        session.refresh(identity)
        return IdentityResolution(
            identity_id=identity_id,
            created=created,
            ambiguous=identity.state == "ambiguous",
            state=str(identity.state or "active"),
        )


def ensure_identity(
    database_engine,
    *,
    account_id: int,
    platform: str,
    email: str,
    workspace_id: str = "",
    chatgpt_account_id: str = "",
    credential_fingerprint: str = "",
    source: str = "",
) -> IdentityResolution:
    """Serialize local identity upserts and retry a transient uniqueness race."""

    with _IDENTITY_MUTATION_LOCK:
        return _ensure_identity_impl(
            database_engine,
            account_id=account_id,
            platform=platform,
            email=email,
            workspace_id=workspace_id,
            chatgpt_account_id=chatgpt_account_id,
            credential_fingerprint=credential_fingerprint,
            source=source,
        )


def get_identity(database_engine, identity_id: str) -> AccountIdentityModel | None:
    target_engine = database_engine or default_engine
    with Session(target_engine) as session:
        return session.get(AccountIdentityModel, str(identity_id or ""))


def identity_for_account(database_engine, account_id: int) -> AccountIdentityModel | None:
    target_engine = database_engine or default_engine
    with Session(target_engine) as session:
        account = session.get(AccountModel, int(account_id or 0))
        if account is None or not str(account.identity_id or "").strip():
            return None
        return session.get(AccountIdentityModel, str(account.identity_id))


def ensure_identity_for_model(database_engine, account: AccountModel) -> IdentityResolution:
    """Attach a saved ORM account to its stable identity."""

    values = _account_identity_values(account)
    return ensure_identity(
        database_engine,
        account_id=int(account.id or 0),
        platform=str(account.platform or ""),
        email=str(account.email or ""),
        workspace_id=values["workspace_id"],
        chatgpt_account_id=values["chatgpt_account_id"],
        credential_fingerprint=values["credential_fingerprint"],
        source="account_save",
    )


def _account_identity_values(account: AccountModel) -> dict[str, str]:
    try:
        extra = account.get_extra()
    except (TypeError, ValueError, json.JSONDecodeError):
        extra = {}
    if not isinstance(extra, dict):
        extra = {}

    def first(*keys: str) -> str:
        for key in keys:
            value = str(extra.get(key) or "").strip()
            if value:
                return value
        return ""

    workspace_id = first("workspace_id", "workspaceId")
    account_id = first("chatgpt_account_id", "chatgptAccountId", "account_id", "accountId")
    fingerprint = credential_fingerprint(
        account.platform,
        account.email,
        refresh_token=first("refresh_token", "refreshToken"),
        access_token=first("access_token", "accessToken") or account.token,
        session_token=first("session_token", "sessionToken"),
        workspace_id=workspace_id,
        chatgpt_account_id=account_id,
    )
    return {
        "workspace_id": workspace_id,
        "chatgpt_account_id": account_id,
        "credential_fingerprint": fingerprint,
    }


def reconcile_existing_accounts(database_engine=None) -> int:
    """Backfill identities for existing ChatGPT rows without changing tokens."""

    target_engine = database_engine or default_engine
    from sqlalchemy import inspect

    try:
        table_names = set(inspect(target_engine).get_table_names())
    except Exception:
        return 0
    if "accounts" not in table_names or "account_identities" not in table_names:
        return 0
    with Session(target_engine) as session:
        accounts = session.exec(
            select(AccountModel).where(func.lower(AccountModel.platform) == "chatgpt")
        ).all()
        account_values = [
            (
                int(account.id or 0),
                str(account.platform or ""),
                str(account.email or ""),
                _account_identity_values(account),
            )
            for account in accounts
            if account.id and normalize_email(account.email)
        ]
    reconciled = 0
    for account_id, platform, email, values in account_values:
        ensure_identity(
            target_engine,
            account_id=account_id,
            platform=platform,
            email=email,
            workspace_id=values["workspace_id"],
            chatgpt_account_id=values["chatgpt_account_id"],
            credential_fingerprint=values["credential_fingerprint"],
            source="startup_reconcile",
        )
        reconciled += 1
    return reconciled


__all__ = [
    "IdentityResolution",
    "credential_fingerprint",
    "ensure_identity",
    "ensure_identity_for_model",
    "get_identity",
    "identity_for_account",
    "normalize_alias",
    "normalize_email",
    "reconcile_existing_accounts",
]
