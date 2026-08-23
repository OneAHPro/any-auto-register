"""Ordered remote-first removal for locally stored accounts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func
from sqlmodel import Session

from core.db import AccountModel, cleanup_chatgpt_account_dependents
from platforms.chatgpt.codex2api_upload import delete_codex2api_credential
from services.chatgpt_account_coordination import (
    chatgpt_account_email_operation_lock,
    chatgpt_account_operation_lock,
)


_TRUTHY = {"1", "true", "yes", "on"}
_REMOTE_SUCCESS = {"deleted", "already_absent"}


@dataclass(frozen=True)
class AccountSnapshot:
    account_id: int
    platform: str
    email: str
    user_id: str
    token: str
    created_at: datetime
    updated_at: datetime
    extra: dict[str, Any]


def _text(value: Any) -> str:
    return str(value or "").strip()


def _bounded_message(value: Any, *, secrets: tuple[str, ...] = ()) -> str:
    message = _text(value)
    for secret in secrets:
        if secret:
            message = message.replace(secret, "***")
    return message[:200]


def _codex_state(
    *,
    enabled: bool,
    status: str,
    remote_id: Any = None,
) -> dict[str, Any]:
    state: dict[str, Any] = {"enabled": bool(enabled), "status": _text(status)}
    try:
        parsed_id = int(remote_id) if remote_id is not None else None
    except (TypeError, ValueError):
        parsed_id = None
    if parsed_id is not None:
        state["remote_id"] = parsed_id
    return state


def _result(
    account_id: int,
    *,
    ok: bool,
    status: str,
    local_deleted: bool,
    codex2api: dict[str, Any],
    error_code: str = "",
    message: str = "",
) -> dict[str, Any]:
    return {
        "ok": bool(ok),
        "account_id": int(account_id),
        "status": _text(status),
        "local_deleted": bool(local_deleted),
        "codex2api": codex2api,
        "error_code": _text(error_code),
        "message": _bounded_message(message),
    }


def _safe_extra(account: AccountModel) -> dict[str, Any]:
    try:
        extra = account.get_extra()
    except Exception:
        return {}
    return dict(extra) if isinstance(extra, dict) else {}


def _load_snapshot(database_engine, account_id: int) -> AccountSnapshot | None:
    with Session(database_engine) as session:
        account = session.get(AccountModel, int(account_id))
        if account is None:
            return None
        return AccountSnapshot(
            account_id=int(account.id),
            platform=_text(account.platform),
            email=_text(account.email),
            user_id=_text(account.user_id),
            token=_text(account.token),
            created_at=account.created_at,
            updated_at=account.updated_at,
            extra=_safe_extra(account),
        )


def _first_text(source: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = _text(source.get(key))
        if value:
            return value
    return ""


def _identity_payload(snapshot: AccountSnapshot) -> dict[str, str]:
    extra = snapshot.extra
    return {
        "workspace_id": _first_text(extra, "workspace_id", "workspaceId"),
        "chatgpt_account_id": _first_text(
            extra,
            "chatgpt_account_id",
            "chatgptAccountId",
        ),
        "account_id": _first_text(extra, "account_id", "accountId"),
        "chatgpt_user_id": _first_text(
            extra,
            "chatgpt_user_id",
            "chatgptUserId",
        ),
        "user_id": _first_text(extra, "user_id", "userId") or snapshot.user_id,
        "id_token": _first_text(extra, "id_token", "idToken"),
        "access_token": (
            _first_text(extra, "access_token", "accessToken") or snapshot.token
        ),
    }


def _secret_values(snapshot: AccountSnapshot) -> tuple[str, ...]:
    secrets = {_text(snapshot.token)}

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for nested in value.values():
                collect(nested)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                collect(nested)
        elif isinstance(value, str) and len(value.strip()) >= 4:
            secrets.add(value.strip())

    collect(snapshot.extra)
    secrets.discard("")
    return tuple(sorted(secrets, key=len, reverse=True))


def _checkpoint(task_control, attempt_id: int | None) -> None:
    if task_control is not None:
        task_control.checkpoint(attempt_id=attempt_id)


def _delete_local_snapshot(database_engine, snapshot: AccountSnapshot) -> str:
    with Session(database_engine) as session:
        statement = (
            delete(AccountModel)
            .where(AccountModel.id == snapshot.account_id)
            .where(AccountModel.platform == snapshot.platform)
            .where(func.lower(AccountModel.email) == snapshot.email.lower())
            .where(AccountModel.created_at == snapshot.created_at)
            .where(AccountModel.updated_at == snapshot.updated_at)
        )
        deleted = session.exec(statement)
        if int(getattr(deleted, "rowcount", 0) or 0) == 1:
            if snapshot.platform.lower() == "chatgpt":
                cleanup_chatgpt_account_dependents(
                    session,
                    snapshot.account_id,
                )
            session.commit()
            return "deleted"
        session.rollback()
        return (
            "already_absent"
            if session.get(AccountModel, snapshot.account_id) is None
            else "conflict"
        )


def _setting_enabled(explicit: bool | None) -> bool:
    if explicit is not None:
        if isinstance(explicit, bool):
            return explicit
        return _text(explicit).lower() in _TRUTHY
    try:
        from core.config_store import config_store

        value = config_store.get(
            "codex2api_delete_on_account_remove_enabled",
            "0",
        )
    except Exception:
        return False
    return _text(value).lower() in _TRUTHY


def _remove_account_locked(
    snapshot: AccountSnapshot,
    *,
    database_engine,
    enabled: bool,
    expected_created_at: datetime | None,
    expected_updated_at: datetime | None,
    task_control,
    attempt_id: int | None,
) -> dict[str, Any]:
    if (
        expected_created_at is not None
        and snapshot.created_at != expected_created_at
    ) or (
        expected_updated_at is not None
        and snapshot.updated_at != expected_updated_at
    ):
        return _result(
            snapshot.account_id,
            ok=False,
            status="local_delete_conflict",
            local_deleted=False,
            codex2api=_codex_state(enabled=False, status="not_attempted"),
            error_code="local_delete_conflict",
            message="账号记录已变化，请刷新后重试",
        )

    is_chatgpt = snapshot.platform.lower() == "chatgpt"
    if not is_chatgpt:
        remote_state = _codex_state(enabled=False, status="not_applicable")
    elif not enabled:
        remote_state = _codex_state(enabled=False, status="skipped_disabled")
    else:
        _checkpoint(task_control, attempt_id)
        try:
            remote_result = delete_codex2api_credential(
                email=snapshot.email,
                identity=_identity_payload(snapshot),
            )
        except Exception as exc:
            remote_result = {
                "status": "failed",
                "remote_id": None,
                "message": f"Codex2API 认证删除异常（{type(exc).__name__}）",
            }
        remote_status = _text(remote_result.get("status")) or "failed"
        remote_state = _codex_state(
            enabled=True,
            status=remote_status,
            remote_id=remote_result.get("remote_id"),
        )
        if remote_status not in _REMOTE_SUCCESS:
            message = _bounded_message(
                remote_result.get("message") or "Codex2API 认证删除未完成",
                secrets=_secret_values(snapshot),
            )
            return _result(
                snapshot.account_id,
                ok=False,
                status="remote_failed",
                local_deleted=False,
                codex2api=remote_state,
                error_code=(
                    "remote_ambiguous"
                    if remote_status == "ambiguous"
                    else "codex2api_delete_failed"
                ),
                message=message,
            )

    _checkpoint(task_control, attempt_id)
    try:
        local_status = _delete_local_snapshot(database_engine, snapshot)
    except Exception as exc:
        return _result(
            snapshot.account_id,
            ok=False,
            status="database_error",
            local_deleted=False,
            codex2api=remote_state,
            error_code="database_error",
            message=f"本地账号删除异常（{type(exc).__name__}）",
        )
    if local_status == "deleted":
        return _result(
            snapshot.account_id,
            ok=True,
            status="deleted",
            local_deleted=True,
            codex2api=remote_state,
            message="账号已删除",
        )
    if local_status == "already_absent":
        return _result(
            snapshot.account_id,
            ok=True,
            status="already_absent",
            local_deleted=False,
            codex2api=remote_state,
            message="账号已不存在",
        )
    return _result(
        snapshot.account_id,
        ok=False,
        status="local_delete_conflict",
        local_deleted=False,
        codex2api=remote_state,
        error_code="local_delete_conflict",
        message="账号记录已变化，本地删除未执行",
    )


def remove_account(
    account_id: int,
    *,
    database_engine=None,
    codex2api_delete_on_account_remove_enabled: bool | None = None,
    expected_created_at: datetime | None = None,
    expected_updated_at: datetime | None = None,
    already_locked: bool = False,
    task_control=None,
    attempt_id: int | None = None,
) -> dict[str, Any]:
    """Delete one account, remotely first when the independent switch is on."""
    from core.db import engine as default_engine

    target_engine = database_engine or default_engine
    try:
        normalized_id = int(account_id)
        snapshot = _load_snapshot(target_engine, normalized_id)
    except Exception as exc:
        try:
            normalized_id = int(account_id)
        except (TypeError, ValueError):
            normalized_id = 0
        return _result(
            normalized_id,
            ok=False,
            status="database_error",
            local_deleted=False,
            codex2api=_codex_state(enabled=False, status="not_attempted"),
            error_code="database_error",
            message=f"读取本地账号异常（{type(exc).__name__}）",
        )
    if snapshot is None:
        return _result(
            normalized_id,
            ok=False,
            status="not_found",
            local_deleted=False,
            codex2api=_codex_state(enabled=False, status="not_applicable"),
            error_code="not_found",
            message="账号不存在",
        )

    kwargs = {
        "database_engine": target_engine,
        "enabled": _setting_enabled(
            codex2api_delete_on_account_remove_enabled
        ),
        "expected_created_at": expected_created_at,
        "expected_updated_at": expected_updated_at,
        "task_control": task_control,
        "attempt_id": attempt_id,
    }

    def remove_locked_fresh_snapshot():
        try:
            fresh_snapshot = _load_snapshot(target_engine, normalized_id)
        except Exception as exc:
            return _result(
                normalized_id,
                ok=False,
                status="database_error",
                local_deleted=False,
                codex2api=_codex_state(
                    enabled=False,
                    status="not_attempted",
                ),
                error_code="database_error",
                message=f"读取锁内账号异常（{type(exc).__name__}）",
            )
        if fresh_snapshot is None:
            return _result(
                normalized_id,
                ok=True,
                status="already_absent",
                local_deleted=False,
                codex2api=_codex_state(
                    enabled=False,
                    status="not_applicable",
                ),
                message="账号已不存在",
            )
        if fresh_snapshot != snapshot:
            return _result(
                normalized_id,
                ok=False,
                status="local_delete_conflict",
                local_deleted=False,
                codex2api=_codex_state(
                    enabled=False,
                    status="not_attempted",
                ),
                error_code="local_delete_conflict",
                message="账号记录已变化，请刷新后重试",
            )
        return _remove_account_locked(fresh_snapshot, **kwargs)

    with chatgpt_account_email_operation_lock(
        snapshot.email,
        blocking=False,
    ) as email_acquired:
        if not email_acquired:
            return _result(
                snapshot.account_id,
                ok=False,
                status="busy",
                local_deleted=False,
                codex2api=_codex_state(enabled=False, status="not_attempted"),
                error_code="account_busy",
                message="该账号正在执行认证维护，请稍后重试",
            )
        if already_locked:
            return remove_locked_fresh_snapshot()

        with chatgpt_account_operation_lock(
            snapshot.account_id,
            blocking=False,
        ) as acquired:
            if not acquired:
                return _result(
                    snapshot.account_id,
                    ok=False,
                    status="busy",
                    local_deleted=False,
                    codex2api=_codex_state(
                        enabled=False,
                        status="not_attempted",
                    ),
                    error_code="account_busy",
                    message="该账号正在执行认证维护，请稍后重试",
                )
            return remove_locked_fresh_snapshot()
