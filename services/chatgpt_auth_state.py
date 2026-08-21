"""Canonical ChatGPT authentication state and generation-safe MFA recovery."""

from __future__ import annotations

import hashlib
import hmac
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
try:
    from enum import StrEnum
except ImportError:  # Python 3.10 compatibility for the production venv.
    from enum import Enum

    class StrEnum(str, Enum):
        def __str__(self) -> str:
            return self.value
from typing import Any, Iterator, Mapping
from uuid import uuid4

from sqlalchemy import func, insert, inspect, update
from sqlmodel import Session, select

from core import db
from core.db import (
    AccountModel,
    ChatGPTAuthStateModel,
    ChatGPTMfaOperationModel,
    ChatGPTMfaRotationJournalModel,
)


class ChatGPTPrimaryState(StrEnum):
    ABSENT = "absent"
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class ChatGPTMfaState(StrEnum):
    ABSENT = "absent"
    ACTIVE = "active"
    # Kept for reading rows written by the first projection implementation.
    CONFIRMED = "confirmed"
    SUSPECT = "suspect"
    REPAIR_REQUIRED = "repair_required"
    BLOCKED = "blocked"


class ChatGPTMfaOperationStatus(StrEnum):
    STAGED = "staged"
    ACTIVATING = "activating"
    ACTIVATED_REMOTE = "activated_remote"
    COMMITTED = "committed"
    ACTIVATION_UNKNOWN = "activation_unknown"
    ABORTED = "aborted"
    SUPERSEDED = "superseded"
    QUARANTINED = "quarantined"


class ChatGPTRecoveryCodeState(StrEnum):
    AVAILABLE = "available"
    RESERVED = "reserved"
    CONSUMED = "consumed"
    UNKNOWN = "unknown"


class ChatGPTAuthVersionConflict(RuntimeError):
    """The caller attempted to commit against a stale authentication version."""


class ChatGPTMfaOperationConflict(RuntimeError):
    """The MFA operation no longer matches the state required for commit."""


class ChatGPTAuthIdentityConflict(RuntimeError):
    """The local account identity is missing, ambiguous, or mismatched."""


@dataclass(frozen=True)
class ChatGPTAuthState:
    account_id: int
    auth_version: int
    primary_state: ChatGPTPrimaryState
    mfa_state: ChatGPTMfaState
    active_mfa_generation: str
    email_recovery_state: str
    credential_revision: str
    last_success_at: datetime | None
    failure_domain: str
    error_code: str
    created_at: datetime
    updated_at: datetime
    failure_count: int = 0
    next_retry_at: datetime | None = None
    circuit_state: str = "closed"
    last_failure_at: datetime | None = None


@dataclass(frozen=True)
class ChatGPTMfaOperation:
    operation_id: str
    account_id: int
    email: str
    generation: str
    base_auth_version: int
    status: ChatGPTMfaOperationStatus
    totp_secret: str
    recovery_code: str
    recovery_code_state: ChatGPTRecoveryCodeState
    remote_activated_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ChatGPTLoginMfaCandidate:
    operation_id: str
    account_id: int
    email: str
    generation: str
    totp_secret: str
    recovery_code: str
    recovery_code_state: ChatGPTRecoveryCodeState
    remote_activated_at: datetime | None


_ALLOWED_TRANSITIONS = {
    ChatGPTMfaOperationStatus.STAGED: {
        ChatGPTMfaOperationStatus.ACTIVATING,
        ChatGPTMfaOperationStatus.ACTIVATED_REMOTE,
        ChatGPTMfaOperationStatus.ABORTED,
        ChatGPTMfaOperationStatus.SUPERSEDED,
        ChatGPTMfaOperationStatus.QUARANTINED,
    },
    ChatGPTMfaOperationStatus.ACTIVATING: {
        ChatGPTMfaOperationStatus.ACTIVATED_REMOTE,
        ChatGPTMfaOperationStatus.ACTIVATION_UNKNOWN,
        ChatGPTMfaOperationStatus.ABORTED,
        ChatGPTMfaOperationStatus.QUARANTINED,
    },
    ChatGPTMfaOperationStatus.ACTIVATED_REMOTE: {
        ChatGPTMfaOperationStatus.ACTIVATION_UNKNOWN,
        ChatGPTMfaOperationStatus.QUARANTINED,
    },
    ChatGPTMfaOperationStatus.ACTIVATION_UNKNOWN: {
        ChatGPTMfaOperationStatus.ACTIVATED_REMOTE,
        ChatGPTMfaOperationStatus.ABORTED,
        ChatGPTMfaOperationStatus.QUARANTINED,
    },
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_account_id(account_id: int) -> int:
    try:
        normalized = int(account_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("ChatGPT account_id must be an integer") from exc
    if normalized <= 0:
        raise ValueError("ChatGPT account_id must be positive")
    return normalized


def _as_primary_state(value: str) -> ChatGPTPrimaryState:
    try:
        return ChatGPTPrimaryState(str(value or ""))
    except ValueError as exc:
        raise ValueError("Unknown ChatGPT primary auth state") from exc


def _as_mfa_state(value: str) -> ChatGPTMfaState:
    try:
        return ChatGPTMfaState(str(value or ""))
    except ValueError as exc:
        raise ValueError("Unknown ChatGPT MFA auth state") from exc


def _as_operation_status(value: str) -> ChatGPTMfaOperationStatus:
    try:
        return ChatGPTMfaOperationStatus(str(value or ""))
    except ValueError as exc:
        raise ValueError("Unknown ChatGPT MFA operation state") from exc


def _as_recovery_state(value: str) -> ChatGPTRecoveryCodeState:
    try:
        return ChatGPTRecoveryCodeState(str(value or ""))
    except ValueError as exc:
        raise ValueError("Unknown ChatGPT recovery-code state") from exc


def _coerce_datetime(value: datetime | str | None) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    normalized = str(value).strip()
    if not normalized:
        return None
    try:
        return datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("rotated_at must be an ISO-8601 datetime") from exc


def _auth_snapshot(row: ChatGPTAuthStateModel) -> ChatGPTAuthState:
    return ChatGPTAuthState(
        account_id=int(row.account_id),
        auth_version=int(row.auth_version),
        primary_state=_as_primary_state(row.primary_state),
        mfa_state=_as_mfa_state(row.mfa_state),
        active_mfa_generation=str(row.active_mfa_generation or ""),
        email_recovery_state=str(row.email_recovery_state or ""),
        credential_revision=str(row.credential_revision or ""),
        last_success_at=row.last_success_at,
        failure_domain=str(row.failure_domain or ""),
        error_code=str(row.error_code or ""),
        created_at=row.created_at,
        updated_at=row.updated_at,
        failure_count=int(getattr(row, "failure_count", 0) or 0),
        next_retry_at=getattr(row, "next_retry_at", None),
        circuit_state=str(getattr(row, "circuit_state", "closed") or "closed"),
        last_failure_at=getattr(row, "last_failure_at", None),
    )


def _operation_snapshot(row: ChatGPTMfaOperationModel) -> ChatGPTMfaOperation:
    return ChatGPTMfaOperation(
        operation_id=str(row.operation_id),
        account_id=int(row.account_id),
        email=str(row.email or ""),
        generation=str(row.generation or ""),
        base_auth_version=int(row.base_auth_version),
        status=_as_operation_status(row.status),
        totp_secret=str(row.totp_secret or ""),
        recovery_code=str(row.recovery_code or ""),
        recovery_code_state=_as_recovery_state(row.recovery_code_state),
        remote_activated_at=row.remote_activated_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@contextmanager
def _session_scope(session: Session | None) -> Iterator[tuple[Session, bool]]:
    if session is not None:
        yield session, False
        return
    with Session(db.engine) as owned_session:
        yield owned_session, True


def _finish_write(session: Session, owns_session: bool) -> None:
    session.flush()
    if owns_session:
        session.commit()


def ensure_chatgpt_auth_state(
    account_id: int,
    *,
    primary_confirmed: bool = False,
    session: Session | None = None,
) -> ChatGPTAuthState:
    normalized_id = _normalize_account_id(account_id)
    with _session_scope(session) as (active_session, owns_session):
        row = active_session.exec(
            select(ChatGPTAuthStateModel).where(
                ChatGPTAuthStateModel.account_id == normalized_id
            )
        ).first()
        if row is None:
            initial = ChatGPTAuthStateModel(
                account_id=normalized_id,
                primary_state=(
                    ChatGPTPrimaryState.CONFIRMED.value
                    if primary_confirmed
                    else ChatGPTPrimaryState.ABSENT.value
                ),
            )
            active_session.exec(
                insert(ChatGPTAuthStateModel)
                .values(**initial.model_dump(exclude={"id"}))
                .prefix_with("OR IGNORE")
            )
            _finish_write(active_session, owns_session)
            row = active_session.exec(
                select(ChatGPTAuthStateModel).where(
                    ChatGPTAuthStateModel.account_id == normalized_id
                )
            ).one()
        if primary_confirmed and row.primary_state != ChatGPTPrimaryState.CONFIRMED:
            expected_version = int(row.auth_version)
            next_version = expected_version + 1
            result = active_session.exec(
                update(ChatGPTAuthStateModel)
                .where(ChatGPTAuthStateModel.account_id == normalized_id)
                .where(ChatGPTAuthStateModel.auth_version == expected_version)
                .where(
                    ChatGPTAuthStateModel.primary_state
                    != ChatGPTPrimaryState.CONFIRMED.value
                )
                .values(
                    auth_version=next_version,
                    primary_state=ChatGPTPrimaryState.CONFIRMED.value,
                    credential_revision=_credential_revision(
                        account_id=normalized_id,
                        auth_version=next_version,
                        has_password=True,
                        has_mfa=bool(row.active_mfa_generation),
                        has_mailbox=False,
                        has_tokens=False,
                    ),
                    updated_at=_utcnow(),
                )
            )
            if int(getattr(result, "rowcount", 0) or 0) == 1:
                _finish_write(active_session, owns_session)
            row = active_session.exec(
                select(ChatGPTAuthStateModel).where(
                    ChatGPTAuthStateModel.account_id == normalized_id
                )
            ).one()
        return _auth_snapshot(row)


def stage_mfa_operation(
    account_id: int,
    email: str,
    totp_secret: str,
    *,
    base_auth_version: int | None = None,
    session: Session | None = None,
) -> ChatGPTMfaOperation:
    normalized_id = _normalize_account_id(account_id)
    normalized_email = str(email or "").strip().lower()
    normalized_secret = str(totp_secret or "").strip()
    if not normalized_email or not normalized_secret:
        raise ValueError("MFA operation requires email and TOTP credentials")

    with _session_scope(session) as (active_session, owns_session):
        state = ensure_chatgpt_auth_state(
            normalized_id,
            session=active_session,
        )
        if base_auth_version is None:
            resolved_version = state.auth_version
        else:
            try:
                resolved_version = int(base_auth_version)
            except (TypeError, ValueError) as exc:
                raise ValueError("base_auth_version must be an integer") from exc
            if resolved_version <= 0 or resolved_version != state.auth_version:
                raise ChatGPTAuthVersionConflict(
                    "ChatGPT authentication state changed before MFA staging"
                )
        now = _utcnow()
        row = ChatGPTMfaOperationModel(
            operation_id=uuid4().hex,
            account_id=normalized_id,
            email=normalized_email,
            generation=uuid4().hex,
            base_auth_version=resolved_version,
            status=ChatGPTMfaOperationStatus.STAGED.value,
            totp_secret=normalized_secret,
            created_at=now,
            updated_at=now,
        )
        active_session.add(row)
        _finish_write(active_session, owns_session)
        return _operation_snapshot(row)


def transition_mfa_operation(
    operation_id: str,
    *,
    expected_state: str,
    new_state: str,
    expected_generation: str = "",
    recovery_code: str | None = None,
    rotated_at: datetime | str | None = None,
    session: Session | None = None,
) -> bool:
    normalized_operation_id = str(operation_id or "").strip()
    normalized_generation = str(expected_generation or "").strip()
    if not normalized_operation_id or not normalized_generation:
        return False
    expected = _as_operation_status(expected_state)
    target = _as_operation_status(new_state)
    if target is ChatGPTMfaOperationStatus.COMMITTED:
        # Promotion is deliberately reserved for the auth-version CAS in
        # commit_auth_projection; retain the historical boolean rejection
        # shape for callback callers.
        return False
    if target not in _ALLOWED_TRANSITIONS.get(expected, set()):
        raise ValueError("Invalid ChatGPT MFA operation transition")

    values: dict[str, Any] = {
        "status": target.value,
        "updated_at": _utcnow(),
    }
    if recovery_code is not None:
        values["recovery_code"] = str(recovery_code).strip()
        values["recovery_code_state"] = ChatGPTRecoveryCodeState.AVAILABLE.value
    activated_at = _coerce_datetime(rotated_at)
    if target is ChatGPTMfaOperationStatus.ACTIVATED_REMOTE:
        values["remote_activated_at"] = activated_at or _utcnow()
    elif activated_at is not None:
        values["remote_activated_at"] = activated_at

    with _session_scope(session) as (active_session, owns_session):
        result = active_session.exec(
            update(ChatGPTMfaOperationModel)
            .where(ChatGPTMfaOperationModel.operation_id == normalized_operation_id)
            .where(ChatGPTMfaOperationModel.status == expected.value)
            .where(ChatGPTMfaOperationModel.generation == normalized_generation)
            .values(**values)
        )
        changed = int(getattr(result, "rowcount", 0) or 0) == 1
        if changed:
            _finish_write(active_session, owns_session)
        elif owns_session:
            active_session.rollback()
        return changed


def _candidate_in_session(
    account_id: int,
    session: Session,
) -> ChatGPTLoginMfaCandidate | None:
    state = session.exec(
        select(ChatGPTAuthStateModel).where(
            ChatGPTAuthStateModel.account_id == account_id
        )
    ).first()
    if (
        state is None
        or state.mfa_state
        not in {
            ChatGPTMfaState.ACTIVE.value,
            ChatGPTMfaState.CONFIRMED.value,
        }
        or not str(state.active_mfa_generation or "").strip()
    ):
        return None
    operation = session.exec(
        select(ChatGPTMfaOperationModel)
        .where(ChatGPTMfaOperationModel.account_id == account_id)
        .where(
            ChatGPTMfaOperationModel.generation
            == state.active_mfa_generation
        )
        .where(
            ChatGPTMfaOperationModel.status
            == ChatGPTMfaOperationStatus.COMMITTED.value
        )
    ).first()
    if operation is None or not str(operation.totp_secret or "").strip():
        return None
    recovery_state = _as_recovery_state(operation.recovery_code_state)
    return ChatGPTLoginMfaCandidate(
        operation_id=str(operation.operation_id),
        account_id=int(operation.account_id),
        email=str(operation.email or ""),
        generation=str(operation.generation or ""),
        totp_secret=str(operation.totp_secret or ""),
        recovery_code=(
            str(operation.recovery_code or "")
            if recovery_state is ChatGPTRecoveryCodeState.AVAILABLE
            else ""
        ),
        recovery_code_state=recovery_state,
        remote_activated_at=operation.remote_activated_at,
    )


def load_login_mfa_candidate(
    account_id: int,
    *,
    session: Session | None = None,
) -> ChatGPTLoginMfaCandidate | None:
    normalized_id = _normalize_account_id(account_id)
    with _session_scope(session) as (active_session, _owns_session):
        return _candidate_in_session(normalized_id, active_session)


def load_login_mfa_candidate_by_email(
    email: str,
    *,
    session: Session | None = None,
) -> ChatGPTLoginMfaCandidate | None:
    """Resolve a unique local account and return only its committed MFA."""

    normalized_email = str(email or "").strip().lower()
    if not normalized_email:
        return None
    with _session_scope(session) as (active_session, _owns_session):
        account_id = resolve_chatgpt_auth_account_id(
            normalized_email,
            session=active_session,
        )
        if account_id is None:
            return None
        inspector = inspect(active_session.connection())
        if not all(
            inspector.has_table(table_name)
            for table_name in (
                ChatGPTAuthStateModel.__tablename__,
                ChatGPTMfaOperationModel.__tablename__,
            )
        ):
            return None
        return _candidate_in_session(account_id, active_session)


def resolve_chatgpt_auth_account_id(
    email: str,
    *,
    session: Session | None = None,
) -> int | None:
    """Resolve exactly one ChatGPT account for an email.

    ``None`` means no account (and is retained for legacy fixtures); more
    than one matching row is an identity conflict and must fail closed.
    """

    normalized_email = str(email or "").strip().lower()
    if not normalized_email:
        return None
    with _session_scope(session) as (active_session, _owns_session):
        inspector = inspect(active_session.connection())
        if not inspector.has_table(AccountModel.__tablename__):
            return None
        account_ids = active_session.exec(
            select(AccountModel.id)
            .where(func.lower(AccountModel.platform) == "chatgpt")
            .where(func.lower(AccountModel.email) == normalized_email)
            .order_by(AccountModel.id)
        ).all()
        ids = [int(value) for value in account_ids if value is not None]
        if len(ids) > 1:
            raise ChatGPTAuthIdentityConflict(
                "Multiple ChatGPT accounts match the login email"
            )
        return ids[0] if ids else None


def _credential_revision(
    *,
    account_id: int,
    auth_version: int,
    has_password: bool,
    has_mfa: bool,
    has_mailbox: bool,
    has_tokens: bool,
) -> str:
    """Return a non-secret canonical change token based on field presence."""

    canonical = (
        f"v1|account_id={int(account_id)}|auth_version={int(auth_version)}"
        f"|password={int(bool(has_password))}|mfa={int(bool(has_mfa))}"
        f"|mailbox={int(bool(has_mailbox))}|tokens={int(bool(has_tokens))}"
    ).encode("utf-8")
    return hmac.new(
        b"chatgpt-auth-state-credential-revision-v1",
        canonical,
        hashlib.sha256,
    ).hexdigest()


def _project_account_values(
    account: AccountModel | None,
    *,
    password: str | None,
    mailbox_context: Mapping[str, Any] | None,
    tokens: Mapping[str, Any] | None,
    active_operation: ChatGPTMfaOperationModel | None,
    now: datetime,
) -> tuple[dict[str, Any], bool, bool, bool]:
    if account is None:
        return {}, bool(password), bool(mailbox_context), bool(tokens)

    extra = dict(account.get_extra() or {})
    projected_mailbox = (
        dict(mailbox_context)
        if mailbox_context is not None
        else dict(extra.get("mailbox_login_context") or {})
    )
    projected_password = str(account.password or "")
    if password is not None:
        projected_password = str(password)

    if active_operation is not None:
        mailbox_extra = dict(projected_mailbox.get("extra") or {})
        mailbox_extra.update(
            {
                "totp_secret": str(active_operation.totp_secret or ""),
                "mfa_recovery_code": str(active_operation.recovery_code or ""),
                "chatgpt_mfa_managed": True,
                "mfa_rotated_at": (
                    active_operation.remote_activated_at.isoformat()
                    if active_operation.remote_activated_at is not None
                    else ""
                ),
            }
        )
        if projected_password:
            mailbox_extra["password"] = projected_password
        mailbox_extra.pop("totp_url", None)
        mailbox_extra.pop("mfa_secret", None)
        mailbox_extra.pop("totp", None)
        projected_mailbox["extra"] = mailbox_extra
        projected_mailbox.setdefault("email", str(account.email or ""))

    if projected_mailbox:
        extra["mailbox_login_context"] = projected_mailbox

    projected_token = str(account.token or "")
    projected_user_id = str(account.user_id or "")
    has_tokens = bool(projected_token)
    if tokens is not None:
        for stale_key in (
            "access_token",
            "accessToken",
            "refresh_token",
            "refreshToken",
            "id_token",
            "idToken",
            "session_token",
            "sessionToken",
            "workspace_id",
            "workspaceId",
            "account_id",
            "accountId",
        ):
            extra.pop(stale_key, None)
        for key in (
            "access_token",
            "refresh_token",
            "id_token",
            "session_token",
            "workspace_id",
            "account_id",
        ):
            value = str(tokens.get(key) or "").strip()
            if value:
                extra[key] = value
        projected_token = str(tokens.get("access_token") or "").strip()
        projected_user_id = str(tokens.get("account_id") or "").strip()
        has_tokens = any(
            bool(str(tokens.get(key) or "").strip())
            for key in ("access_token", "refresh_token", "id_token", "session_token")
        )

    snapshot = AccountModel(**account.model_dump())
    snapshot.password = projected_password
    snapshot.token = projected_token
    snapshot.user_id = projected_user_id
    snapshot.updated_at = now
    snapshot.set_extra(extra)
    values = {
        "password": snapshot.password,
        "token": snapshot.token,
        "user_id": snapshot.user_id,
        "extra_json": snapshot.extra_json,
        "updated_at": snapshot.updated_at,
    }
    return values, bool(projected_password), bool(projected_mailbox), has_tokens


def _begin_clean_sqlite_transaction(session: Session) -> bool:
    """Start a real DBAPI transaction only for a clean caller session.

    SQLAlchemy's nested transaction can otherwise release its first savepoint
    without an actual SQLite outer ``BEGIN``.  Returning whether we started it
    lets the caller decide whether rollback belongs to this function or to the
    owner of an already-active transaction.
    """

    bind = session.get_bind()
    if getattr(getattr(bind, "dialect", None), "name", "") != "sqlite":
        return False
    connection = session.connection()
    raw_connection = getattr(connection, "connection", None)
    if raw_connection is None:
        return False
    in_transaction = getattr(raw_connection, "in_transaction", True)
    if not in_transaction:
        connection.exec_driver_sql("BEGIN")
        return True
    return False


def commit_auth_projection(
    account_id: int,
    *,
    expected_version: int,
    password: str | None = None,
    mailbox_context: Mapping[str, Any] | None = None,
    active_operation_id: str | None = None,
    tokens: Mapping[str, Any] | None = None,
    session: Session | None = None,
) -> ChatGPTAuthState:
    normalized_id = _normalize_account_id(account_id)
    try:
        normalized_version = int(expected_version)
    except (TypeError, ValueError) as exc:
        raise ValueError("expected_version must be an integer") from exc
    normalized_operation_id = str(active_operation_id or "").strip()

    with _session_scope(session) as (active_session, owns_session):
        started_outer = False
        try:
            started_outer = _begin_clean_sqlite_transaction(active_session)
            with active_session.begin_nested():
                current = active_session.exec(
                    select(ChatGPTAuthStateModel).where(
                        ChatGPTAuthStateModel.account_id == normalized_id
                    )
                ).first()
                if current is None or int(current.auth_version) != normalized_version:
                    raise ChatGPTAuthVersionConflict(
                        "ChatGPT authentication state changed before commit"
                    )

                account = active_session.get(AccountModel, normalized_id)
                if account is None or str(account.platform or "").strip().lower() != "chatgpt":
                    raise ChatGPTAuthIdentityConflict(
                        "ChatGPT account identity is missing or not ChatGPT"
                    )
                account_email = str(account.email or "").strip().lower()
                if not account_email:
                    raise ChatGPTAuthIdentityConflict(
                        "ChatGPT account email identity is missing"
                    )

                active_operation = None
                next_mfa_state = str(current.mfa_state or ChatGPTMfaState.ABSENT)
                next_generation = str(current.active_mfa_generation or "")
                if normalized_operation_id:
                    active_operation = active_session.exec(
                        select(ChatGPTMfaOperationModel)
                        .where(
                            ChatGPTMfaOperationModel.operation_id
                            == normalized_operation_id
                        )
                        .where(ChatGPTMfaOperationModel.account_id == normalized_id)
                        .where(
                            ChatGPTMfaOperationModel.base_auth_version
                            == normalized_version
                        )
                    ).first()
                    if (
                        active_operation is None
                        or active_operation.status
                        != ChatGPTMfaOperationStatus.ACTIVATED_REMOTE.value
                    ):
                        raise ChatGPTMfaOperationConflict(
                            "ChatGPT MFA operation is not eligible for commit"
                        )
                    if str(active_operation.email or "").strip().lower() != account_email:
                        raise ChatGPTAuthIdentityConflict(
                            "ChatGPT MFA operation email does not match account"
                        )
                    next_mfa_state = ChatGPTMfaState.ACTIVE.value
                    next_generation = str(active_operation.generation or "")
                elif str(current.mfa_state or "") == ChatGPTMfaState.CONFIRMED.value:
                    # Normalize legacy rows on the next projection write.
                    next_mfa_state = ChatGPTMfaState.ACTIVE.value

                next_primary_state = str(
                    current.primary_state or ChatGPTPrimaryState.ABSENT
                )
                if password is not None:
                    next_primary_state = (
                        ChatGPTPrimaryState.CONFIRMED.value
                        if str(password)
                        else ChatGPTPrimaryState.ABSENT.value
                    )

                now = _utcnow()
                account_values, has_password, has_mailbox, has_tokens = (
                    _project_account_values(
                        account,
                        password=password,
                        mailbox_context=mailbox_context,
                        tokens=tokens,
                        active_operation=active_operation,
                        now=now,
                    )
                )
                next_version = normalized_version + 1
                revision = _credential_revision(
                    account_id=normalized_id,
                    auth_version=next_version,
                    has_password=has_password,
                    has_mfa=bool(next_generation),
                    has_mailbox=has_mailbox,
                    has_tokens=has_tokens,
                )

                if active_operation is not None:
                    operation_result = active_session.exec(
                        update(ChatGPTMfaOperationModel)
                        .where(
                            ChatGPTMfaOperationModel.operation_id
                            == normalized_operation_id
                        )
                        .where(
                            ChatGPTMfaOperationModel.status
                            == ChatGPTMfaOperationStatus.ACTIVATED_REMOTE.value
                        )
                        .where(
                            ChatGPTMfaOperationModel.generation
                            == active_operation.generation
                        )
                        .where(
                            ChatGPTMfaOperationModel.base_auth_version
                            == normalized_version
                        )
                        .values(
                            status=ChatGPTMfaOperationStatus.COMMITTED.value,
                            updated_at=now,
                        )
                    )
                    if int(getattr(operation_result, "rowcount", 0) or 0) != 1:
                        raise ChatGPTMfaOperationConflict(
                            "ChatGPT MFA operation changed before commit"
                        )

                state_result = active_session.exec(
                    update(ChatGPTAuthStateModel)
                    .where(ChatGPTAuthStateModel.account_id == normalized_id)
                    .where(
                        ChatGPTAuthStateModel.auth_version == normalized_version
                    )
                    .values(
                        auth_version=next_version,
                        primary_state=next_primary_state,
                        mfa_state=next_mfa_state,
                        active_mfa_generation=next_generation,
                        credential_revision=revision,
                        last_success_at=now,
                        failure_domain="",
                        error_code="",
                        updated_at=now,
                    )
                )
                if int(getattr(state_result, "rowcount", 0) or 0) != 1:
                    raise ChatGPTAuthVersionConflict(
                        "ChatGPT authentication state changed before commit"
                    )

                if account is not None and account_values:
                    account_result = active_session.exec(
                        update(AccountModel)
                        .where(AccountModel.id == normalized_id)
                        .where(
                            func.lower(AccountModel.platform) == "chatgpt"
                        )
                        .where(func.lower(AccountModel.email) == account_email)
                        .values(**account_values)
                    )
                    if int(getattr(account_result, "rowcount", 0) or 0) != 1:
                        raise ChatGPTAuthVersionConflict(
                            "ChatGPT account projection changed before commit"
                        )

            if owns_session:
                active_session.commit()
            else:
                # The caller owns an existing transaction.  A clean SQLite
                # session gets a real outer BEGIN above, but remains caller-
                # controlled so rollback can undo this projection.
                active_session.flush()
        except Exception:
            if owns_session or started_outer:
                active_session.rollback()
            raise

        row = active_session.exec(
            select(ChatGPTAuthStateModel).where(
                ChatGPTAuthStateModel.account_id == normalized_id
            )
        ).one()
        return _auth_snapshot(row)


def quarantine_legacy_staged_journals(
    *,
    session: Session | None = None,
) -> int:
    """Fence legacy staged rows without exposing or consuming their secrets."""

    with _session_scope(session) as (active_session, owns_session):
        result = active_session.exec(
            update(ChatGPTMfaRotationJournalModel)
            .where(ChatGPTMfaRotationJournalModel.status == "staged")
            .values(status="quarantined", updated_at=_utcnow())
        )
        changed = int(getattr(result, "rowcount", 0) or 0)
        if changed:
            _finish_write(active_session, owns_session)
        elif owns_session:
            active_session.rollback()
        return changed


def load_chatgpt_auth_state(
    account_id: int,
    *,
    session: Session | None = None,
) -> ChatGPTAuthState | None:
    """Read the canonical state without creating a row."""
    normalized_id = _normalize_account_id(account_id)
    with _session_scope(session) as (active_session, _owns_session):
        row = active_session.exec(
            select(ChatGPTAuthStateModel).where(
                ChatGPTAuthStateModel.account_id == normalized_id
            )
        ).first()
        return _auth_snapshot(row) if row is not None else None


def chatgpt_auth_retry_allowed(
    account_id: int,
    *,
    now: datetime | None = None,
    session: Session | None = None,
) -> bool:
    """Return whether an automatic maintenance attempt may start now."""
    state = load_chatgpt_auth_state(account_id, session=session)
    if state is None:
        return True
    if state.circuit_state in {"blocked", "open_permanent"}:
        return False
    current = now or _utcnow()
    retry_at = state.next_retry_at
    if retry_at is None:
        return True
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    return retry_at <= current


def record_chatgpt_auth_failure(
    account_id: int,
    *,
    failure_domain: str = "unknown",
    error_code: str = "auth_failed",
    retryable: bool = True,
    session: Session | None = None,
) -> ChatGPTAuthState:
    """Persist account-level failure state with bounded exponential backoff.

    The record intentionally stores only a domain and code.  Credential and
    mailbox secrets never enter the maintenance state or scheduler metadata.
    """
    normalized_id = _normalize_account_id(account_id)
    with _session_scope(session) as (active_session, owns_session):
        state = ensure_chatgpt_auth_state(normalized_id, session=active_session)
        now = _utcnow()
        count = max(int(state.failure_count), 0) + 1
        if retryable:
            # 2m, 4m, 8m ... capped at 6h.  A bad account therefore leaves
            # the next scheduler cohort quickly instead of blocking it.
            delay_seconds = min(6 * 60 * 60, 120 * (2 ** min(count - 1, 8)))
            next_retry = now + timedelta(seconds=delay_seconds)
            circuit = "open"
        else:
            # Permanent credential/state failures stay isolated until an
            # explicit successful login clears them.
            next_retry = None
            circuit = "blocked"
        result = active_session.exec(
            update(ChatGPTAuthStateModel)
            .where(ChatGPTAuthStateModel.account_id == normalized_id)
            .where(ChatGPTAuthStateModel.auth_version == state.auth_version)
            .values(
                failure_count=count,
                next_retry_at=next_retry,
                circuit_state=circuit,
                failure_domain=str(failure_domain or "unknown")[:80],
                error_code=str(error_code or "auth_failed")[:120],
                last_failure_at=now,
                updated_at=now,
            )
        )
        if int(getattr(result, "rowcount", 0) or 0) != 1:
            raise ChatGPTAuthVersionConflict(
                "ChatGPT authentication state changed while recording failure"
            )
        _finish_write(active_session, owns_session)
        row = active_session.exec(
            select(ChatGPTAuthStateModel).where(
                ChatGPTAuthStateModel.account_id == normalized_id
            )
        ).one()
        return _auth_snapshot(row)


def clear_chatgpt_auth_failure(
    account_id: int,
    *,
    session: Session | None = None,
) -> ChatGPTAuthState:
    """Close the account circuit after a verified login/refresh succeeds."""
    normalized_id = _normalize_account_id(account_id)
    with _session_scope(session) as (active_session, owns_session):
        state = ensure_chatgpt_auth_state(normalized_id, session=active_session)
        now = _utcnow()
        result = active_session.exec(
            update(ChatGPTAuthStateModel)
            .where(ChatGPTAuthStateModel.account_id == normalized_id)
            .where(ChatGPTAuthStateModel.auth_version == state.auth_version)
            .values(
                failure_count=0,
                next_retry_at=None,
                circuit_state="closed",
                failure_domain="",
                error_code="",
                last_failure_at=None,
                last_success_at=now,
                updated_at=now,
            )
        )
        if int(getattr(result, "rowcount", 0) or 0) != 1:
            raise ChatGPTAuthVersionConflict(
                "ChatGPT authentication state changed while clearing failure"
            )
        _finish_write(active_session, owns_session)
        row = active_session.exec(
            select(ChatGPTAuthStateModel).where(
                ChatGPTAuthStateModel.account_id == normalized_id
            )
        ).one()
        return _auth_snapshot(row)


def promote_successful_chatgpt_account_auth(
    account_id: int,
    *,
    session: Session | None = None,
) -> ChatGPTAuthState:
    """Promote credentials from a freshly successful login into canonical state.

    This is the compatibility bridge for first-login flows that still build an
    ``AccountModel`` projection before a local account id exists.  It runs only
    after that authenticated result is saved, so imported/staged TOTP material
    is never promoted merely because it is present in the mailbox pool.
    """
    normalized_id = _normalize_account_id(account_id)
    with _session_scope(session) as (active_session, owns_session):
        account = active_session.get(AccountModel, normalized_id)
        if account is None or str(account.platform or "").strip().lower() != "chatgpt":
            raise ChatGPTAuthIdentityConflict("ChatGPT account identity is missing")
        extra = dict(account.get_extra() or {})
        mailbox_context = extra.get("mailbox_login_context")
        context = dict(mailbox_context) if isinstance(mailbox_context, Mapping) else {}
        context_extra = dict(context.get("extra") or {})
        password = str(account.password or context_extra.get("password") or "")
        totp_secret = str(
            context_extra.get("totp_secret")
            or context_extra.get("mfa_secret")
            or context_extra.get("totp")
            or ""
        ).strip()
        recovery_code = str(context_extra.get("mfa_recovery_code") or "").strip()

        state = ensure_chatgpt_auth_state(
            normalized_id,
            primary_confirmed=bool(password),
            session=active_session,
        )
        current_candidate = _candidate_in_session(normalized_id, active_session)
        # Once a generation is canonical and committed, an older mailbox
        # projection is only a compatibility view.  Never let that stale copy
        # replace the active factor after a duplicate/imported login.
        if not totp_secret or current_candidate is not None:
            if owns_session:
                active_session.commit()
            return state

        operation = stage_mfa_operation(
            normalized_id,
            str(account.email or ""),
            totp_secret,
            base_auth_version=state.auth_version,
            session=active_session,
        )
        if not transition_mfa_operation(
            operation.operation_id,
            expected_state=ChatGPTMfaOperationStatus.STAGED,
            new_state=ChatGPTMfaOperationStatus.ACTIVATED_REMOTE,
            expected_generation=operation.generation,
            recovery_code=recovery_code,
            session=active_session,
        ):
            raise ChatGPTMfaOperationConflict(
                "ChatGPT MFA proof changed before canonical promotion"
            )
        promoted = commit_auth_projection(
            normalized_id,
            expected_version=state.auth_version,
            password=password,
            mailbox_context=context or None,
            active_operation_id=operation.operation_id,
            session=active_session,
        )
        if owns_session:
            active_session.commit()
        return promoted
