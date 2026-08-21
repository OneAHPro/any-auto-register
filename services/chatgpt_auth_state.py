"""Canonical ChatGPT authentication state and generation-safe MFA recovery."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Iterator, Mapping
from uuid import uuid4

from sqlalchemy import func, inspect, update
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
        ChatGPTMfaOperationStatus.COMMITTED,
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
            row = ChatGPTAuthStateModel(
                account_id=normalized_id,
                primary_state=(
                    ChatGPTPrimaryState.CONFIRMED.value
                    if primary_confirmed
                    else ChatGPTPrimaryState.ABSENT.value
                ),
            )
            active_session.add(row)
            _finish_write(active_session, owns_session)
            if owns_session:
                active_session.refresh(row)
        elif primary_confirmed and row.primary_state != ChatGPTPrimaryState.CONFIRMED:
            row.primary_state = ChatGPTPrimaryState.CONFIRMED.value
            row.updated_at = _utcnow()
            active_session.add(row)
            _finish_write(active_session, owns_session)
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
        if base_auth_version is None:
            state = ensure_chatgpt_auth_state(
                normalized_id,
                session=active_session,
            )
            resolved_version = state.auth_version
        else:
            try:
                resolved_version = int(base_auth_version)
            except (TypeError, ValueError) as exc:
                raise ValueError("base_auth_version must be an integer") from exc
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
        or state.mfa_state != ChatGPTMfaState.CONFIRMED.value
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
    return ChatGPTLoginMfaCandidate(
        operation_id=str(operation.operation_id),
        account_id=int(operation.account_id),
        email=str(operation.email or ""),
        generation=str(operation.generation or ""),
        totp_secret=str(operation.totp_secret or ""),
        recovery_code=str(operation.recovery_code or ""),
        recovery_code_state=_as_recovery_state(operation.recovery_code_state),
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
        inspector = inspect(active_session.connection())
        required_tables = {
            AccountModel.__tablename__,
            ChatGPTAuthStateModel.__tablename__,
            ChatGPTMfaOperationModel.__tablename__,
        }
        if not all(inspector.has_table(table_name) for table_name in required_tables):
            return None
        account_ids = active_session.exec(
            select(AccountModel.id)
            .where(func.lower(AccountModel.platform) == "chatgpt")
            .where(func.lower(AccountModel.email) == normalized_email)
            .order_by(AccountModel.id)
        ).all()
        candidates = [
            candidate
            for account_id in account_ids
            if account_id is not None
            for candidate in [_candidate_in_session(int(account_id), active_session)]
            if candidate is not None
        ]
        return candidates[0] if len(candidates) == 1 else None


def _credential_revision(
    *,
    auth_version: int,
    has_password: bool,
    has_mfa: bool,
    has_mailbox: bool,
    has_tokens: bool,
) -> str:
    """Return a non-secret canonical change token based on field presence."""

    return (
        f"chatgpt-auth-v{auth_version}"
        f":p{int(has_password)}"
        f":m{int(has_mfa)}"
        f":b{int(has_mailbox)}"
        f":t{int(has_tokens)}"
    )


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
        try:
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
                    next_mfa_state = ChatGPTMfaState.CONFIRMED.value
                    next_generation = str(active_operation.generation or "")

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
                account = active_session.get(AccountModel, normalized_id)
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
                if account is None:
                    has_password = bool(password) or (
                        next_primary_state == ChatGPTPrimaryState.CONFIRMED.value
                    )
                next_version = normalized_version + 1
                revision = _credential_revision(
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
                        .values(**account_values)
                    )
                    if int(getattr(account_result, "rowcount", 0) or 0) != 1:
                        raise ChatGPTAuthVersionConflict(
                            "ChatGPT account projection changed before commit"
                        )

            if owns_session:
                active_session.commit()
            else:
                active_session.flush()
        except Exception:
            if owns_session:
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
