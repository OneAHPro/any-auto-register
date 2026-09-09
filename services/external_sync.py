"""外部系统同步（自动导入 / 回填）"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from services.chatgpt_sync import (
    _get_account_extra,
    persist_codex2api_sync_result,
    persist_cpa_sync_result,
    persist_sub2api_sync_result,
    upload_chatgpt_account_to_cpa,
)
from services.chatgpt_account_coordination import (
    codex2api_account_mutation_lock,
    codex2api_credential_lock,
    codex2api_target_lock,
)
from services.codex2api_remote_accounts import remote_account_email, remote_bool


logger = logging.getLogger(__name__)


def _is_config_enabled(value: Any, default: bool = False) -> bool:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return default
    return normalized in {"1", "true", "yes", "on", "enabled"}


def _pick_text(source: Any, *keys: str, default: str = "") -> str:
    if not isinstance(source, dict):
        return default
    for key in keys:
        value = source.get(key)
        if value is None:
            continue
        text = value.strip() if isinstance(value, str) else str(value).strip()
        if text:
            return text
    return default


def _build_chatgpt_upload_account(account):
    class _A:
        pass

    upload_account = _A()
    upload_account.email = account.email
    extra = _get_account_extra(account)
    upload_account.access_token = (
        _pick_text(extra, "access_token", "accessToken") or account.token
    )
    upload_account.refresh_token = _pick_text(
        extra,
        "refresh_token",
        "refreshToken",
    )
    upload_account.id_token = _pick_text(extra, "id_token", "idToken")
    upload_account.session_token = _pick_text(
        extra,
        "session_token",
        "sessionToken",
    )
    upload_account.client_id = _pick_text(
        extra,
        "client_id",
        "clientId",
        default="app_EMoamEEZ73f0CkXaXp7hrann",
    )
    stored_user_id = str(getattr(account, "user_id", "") or "").strip()
    upload_account.workspace_id = _pick_text(
        extra,
        "workspace_id",
        "workspaceId",
        "chatgpt_account_id",
        "chatgptAccountId",
        default=stored_user_id,
    )
    upload_account.account_id = _pick_text(
        extra,
        "account_id",
        "accountId",
        "chatgpt_account_id",
        "chatgptAccountId",
        default=upload_account.workspace_id,
    )
    upload_account.user_id = _pick_text(
        extra,
        "chatgpt_user_id",
        "chatgptUserId",
        "user_id",
        "userId",
        default=stored_user_id,
    )
    return upload_account


def _account_credential_identity_key(account: Any) -> str:
    """Build a stable lock identity without retaining credential plaintext."""

    extra = _get_account_extra(account)
    if not isinstance(extra, dict):
        extra = {}
    for key in (
        "agent_runtime_id",
        "chatgpt_account_id",
        "account_id",
        "refresh_token",
        "session_token",
        "access_token",
    ):
        value = str(
            extra.get(key)
            or (getattr(account, "token", "") if key == "access_token" else "")
            or ""
        ).strip()
        if value:
            return f"{key}:{value}"
    return f"email:{str(getattr(account, 'email', '') or '').strip().casefold()}"


def _positive_remote_id(row: Any) -> int:
    if not isinstance(row, dict):
        return 0
    for key in ("id", "remote_id"):
        value = row.get(key)
        if isinstance(value, bool) or (
            isinstance(value, float) and not value.is_integer()
        ):
            return 0
        try:
            parsed = int(value or 0)
        except (TypeError, ValueError, OverflowError):
            if value not in (None, ""):
                return 0
            continue
        if parsed > 0:
            return parsed
    return 0


def _remote_import_confirmed(response: Any) -> bool:
    """Require an explicit provider acknowledgement before binding locally."""

    if not isinstance(response, dict):
        return False
    try:
        if int(response.get("failed") or 0) > 0:
            return False
    except (TypeError, ValueError):
        return False
    for key in ("success", "updated", "duplicate", "imported"):
        value = response.get(key)
        if value is True:
            return True
        try:
            if int(value or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    if response.get("ok") is True:
        return True
    if _positive_remote_id(response) > 0:
        return True
    if str(response.get("status") or "").strip().lower() in {
        "ok", "success", "imported", "updated", "duplicate"
    }:
        return True
    return bool(
        str(response.get("account_id") or "").strip()
        and str(response.get("email") or response.get("name") or "").strip()
    )


def _identity_aliases(value: Any) -> set[str]:
    if not isinstance(value, dict):
        return set()
    nested = value.get("credentials")
    nested = nested if isinstance(nested, dict) else {}
    aliases: set[str] = set()
    for key in (
        "workspace_id",
        "effective_workspace_id",
        "chatgpt_account_id",
        "account_id",
        "user_id",
    ):
        for source in (value, nested):
            text = str(source.get(key) or "").strip().casefold()
            if text:
                aliases.add(text)
    return aliases


def _local_identity_aliases(account: Any) -> set[str]:
    try:
        extra = _get_account_extra(account)
    except Exception:
        extra = {}
    aliases = _identity_aliases(extra if isinstance(extra, dict) else {})
    user_id = str(getattr(account, "user_id", "") or "").strip().casefold()
    if user_id:
        aliases.add(user_id)
    return aliases


def _remote_row_matches_account(
    row: dict[str, Any],
    account: AccountModel,
    local_aliases: set[str],
) -> bool:
    row_aliases = _identity_aliases(row)
    account_email = str(getattr(account, "email", "") or "").strip().casefold()
    row_email = remote_account_email(row).strip().casefold()
    email_matches = bool(account_email and row_email and account_email == row_email)
    if local_aliases and row_aliases:
        # Stable IDs must agree; an old remote row with a reused numeric ID is
        # not the same credential merely because its email happens to match.
        # A provider may legitimately rotate the display email, so an
        # intersecting stable alias takes precedence over that stale field.
        return bool(local_aliases & row_aliases)
    return email_matches


def _remote_row_status_usable(row: dict[str, Any]) -> bool:
    return str(
        row.get("remote_status") or row.get("status") or ""
    ).strip().lower() not in {
        "error",
        "invalid",
        "unauthorized",
        "token_invalidated",
        "deleted",
        "expired",
    }


def _identity_has_conflicting_alias(session: Any, identity_id: str) -> bool:
    from sqlmodel import select
    from core.db import AccountIdentityAliasModel, AccountIdentityModel

    aliases = session.exec(
        select(AccountIdentityAliasModel).where(
            AccountIdentityAliasModel.identity_id == str(identity_id)
        )
    ).all()
    for alias in aliases:
        if str(alias.alias_type or "") not in {
            "workspace_id", "chatgpt_account_id", "credential_fingerprint"
        }:
            continue
        if session.exec(
            select(AccountIdentityAliasModel)
            .join(
                AccountIdentityModel,
                AccountIdentityModel.id == AccountIdentityAliasModel.identity_id,
            )
            .where(AccountIdentityModel.platform == "chatgpt")
            .where(AccountIdentityAliasModel.alias_type == alias.alias_type)
            .where(AccountIdentityAliasModel.normalized_value == alias.normalized_value)
            .where(AccountIdentityAliasModel.identity_id != str(identity_id))
        ).first() is not None:
            return True
    return False


def _select_remote_binding_row(
    account: AccountModel,
    rows: list[dict[str, Any]],
    *,
    identity_id: str,
    target_id: int,
    database_engine: Any,
) -> dict[str, Any]:
    """Select one remote row using binding/strong identity before email."""
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RuntimeError("目标账号同步后远端账号清单格式无效")
    candidates = [row for row in rows if isinstance(row, dict)]
    if not candidates:
        raise RuntimeError("目标账号同步后未返回远端账号")

    local_aliases = _local_identity_aliases(account)

    existing_remote_id = 0
    try:
        from sqlmodel import Session, select
        from core.db import AccountTargetBindingModel

        with Session(database_engine) as session:
            binding = session.exec(
                select(AccountTargetBindingModel)
                .where(AccountTargetBindingModel.identity_id == str(identity_id))
                .where(AccountTargetBindingModel.target_id == int(target_id))
            ).first()
            existing_remote_id = int(binding.remote_account_id or 0) if binding else 0
    except Exception as exc:
        # An unreadable local binding table is an identity-integrity failure;
        # falling through to an email guess could silently retarget a live
        # account. Callers receive a retryable sync error instead.
        raise RuntimeError("本地目标绑定读取失败") from exc

    if existing_remote_id > 0:
        bound = [
            row
            for row in candidates
            if _positive_remote_id(row) == existing_remote_id
            and _remote_row_matches_account(row, account, local_aliases)
            and _remote_row_status_usable(row)
        ]
        if len(bound) == 1:
            return bound[0]
        if len(bound) > 1:
            raise RuntimeError("目标账号已有绑定对应多个远端账号")

    if local_aliases:
        strong = [
            row
            for row in candidates
            if local_aliases & _identity_aliases(row)
            and _remote_row_status_usable(row)
        ]
        if len(strong) == 1:
            return strong[0]
        if len(strong) > 1:
            raise RuntimeError("目标账号稳定身份对应多个远端账号")

    email = str(getattr(account, "email", "") or "").strip().casefold()
    email_matches = [
        row for row in candidates
        if email
        and remote_account_email(row).strip().casefold() == email
        and _remote_row_matches_account(row, account, local_aliases)
    ]
    usable = [
        row for row in email_matches
        if _remote_row_status_usable(row)
    ]
    if len(usable) == 1:
        return usable[0]
    if len(usable) > 1 or len(email_matches) > 1:
        raise RuntimeError("目标账号同步后身份不唯一")
    if len(email_matches) == 1:
        raise RuntimeError("目标账号同步后仅匹配到不可用远端账号")
    raise RuntimeError("目标账号同步后未找到唯一远端账号")


def _persist_explicit_target_binding(
    account: Any,
    *,
    target_id: int,
    client: Any,
    database_engine: Any,
) -> None:
    """Persist the verified remote row for a successful explicit-target sync."""

    from sqlmodel import Session, select

    from core.db import (
        AccountAssignmentModel,
        AccountIdentityModel,
        AccountModel,
        AccountTargetBindingModel,
        ChatGPTAuthStateModel,
        Codex2APITargetModel,
    )
    from services.account_identity import ensure_identity_for_model
    from services.account_identity import move_assignments_to_standby
    from services.account_identity import supersede_other_target_bindings
    from services.chatgpt_sync import update_account_model_codex2api_sync

    if not isinstance(account, AccountModel) or account.id is None:
        return
    identity_id = str(account.identity_id or "").strip()
    if not identity_id:
        identity_id = ensure_identity_for_model(database_engine, account).identity_id
    rows = client.list_accounts()
    row = _select_remote_binding_row(
        account,
        rows,
        identity_id=identity_id,
        target_id=int(target_id),
        database_engine=database_engine,
    )
    remote_id = _positive_remote_id(row)
    if remote_id <= 0:
        raise RuntimeError("目标账号同步后缺少远端 ID")
    normalized_email = str(account.email or "").strip().lower()
    with Session(database_engine) as session:
        saved = session.get(AccountModel, int(account.id))
        if saved is None:
            raise RuntimeError("本地账号不存在")
        identity_row = session.get(AccountIdentityModel, identity_id)
        if (
            identity_row is None
            or str(identity_row.platform or "").strip().lower() != "chatgpt"
        ):
            resolved_identity = ensure_identity_for_model(database_engine, saved)
            identity_id = resolved_identity.identity_id
            saved.identity_id = identity_id
            session.add(saved)
            identity_row = session.get(AccountIdentityModel, identity_id)
        auth_state = session.exec(
            select(ChatGPTAuthStateModel).where(
                ChatGPTAuthStateModel.account_id == int(account.id)
            )
        ).first()
        credential_revision = str(
            auth_state.credential_revision if auth_state is not None else ""
        )
        binding = session.exec(
            select(AccountTargetBindingModel)
            .where(AccountTargetBindingModel.identity_id == identity_id)
            .where(AccountTargetBindingModel.target_id == int(target_id))
        ).first()
        now = datetime.now(timezone.utc)
        conflicting = session.exec(
            select(AccountTargetBindingModel)
            .where(AccountTargetBindingModel.target_id == int(target_id))
            .where(AccountTargetBindingModel.remote_account_id == int(remote_id))
        ).first()
        if conflicting is not None and (
            binding is None or int(conflicting.id or 0) != int(binding.id or 0)
        ):
            # A prior inventory pass may have materialized the freshly
            # uploaded row under a duplicate local account. Free that unique
            # remote slot before transferring it to the account just relogged.
            conflicting.remote_account_id = 0
            conflicting.remote_email = ""
            conflicting.enabled = False
            conflicting.sync_status = "superseded"
            conflicting.remote_status = "superseded"
            conflicting.last_error = "凭据重登后转移到当前账号"
            conflicting.updated_at = now
            session.add(conflicting)
            # SQLite enforces the composite unique key per statement, so the
            # slot must be flushed free before the current binding claims it.
            session.flush()
        if binding is None:
            binding = AccountTargetBindingModel(
                identity_id=identity_id,
                local_account_id=int(account.id),
                target_id=int(target_id),
                created_at=now,
            )
        binding.remote_account_id = remote_id
        binding.remote_email = str(
            row.get("email") or row.get("name") or normalized_email
        ).strip().lower()
        binding.sync_status = "synced"
        binding.remote_status = str(
            row.get("remote_status") or row.get("status") or ""
        )
        binding.enabled = remote_bool(row.get("enabled"), True) and not remote_bool(
            row.get("locked"), False
        )
        binding.credential_revision = credential_revision
        binding.last_sync_at = now
        binding.last_error = ""
        binding.updated_at = now
        session.add(binding)
        identity_row = session.get(AccountIdentityModel, identity_id)
        if (
            identity_row is not None
            and binding.enabled
            and not _identity_has_conflicting_alias(session, identity_id)
        ):
            identity_row.state = "active"
            identity_row.current_account_id = int(account.id)
            identity_row.updated_at = now
            session.add(identity_row)
        elif identity_row is not None and _identity_has_conflicting_alias(session, identity_id):
            binding.enabled = False
            binding.sync_status = "ambiguous"
            binding.remote_status = "ambiguous"
            binding.last_error = "身份存在歧义，等待人工确认"
        if binding.enabled:
            supersede_other_target_bindings(
                session,
                identity_id=identity_id,
                current_target_id=int(target_id),
                reason="explicit_target_sync",
            )
        # Keep the durable scheduler assignment aligned with the binding that
        # was just verified.  A relogin can explicitly select a new target
        # while an older active assignment still points at the previous node;
        # advance its CAS version before moving it so an in-flight migration
        # cannot write the stale target back.
        assignment_rows = session.exec(
            select(AccountAssignmentModel)
            .where(AccountAssignmentModel.identity_id == identity_id)
            .where(
                AccountAssignmentModel.state.in_(
                    [
                        "active",
                        "draining",
                        "planned",
                        "locking",
                        "uploading",
                        "pending",
                        "verifying",
                        "assignment_committing",
                        "source_cleaning",
                        "target_enabling",
                        "migrating",
                        "target_disabled",
                        "standby",
                    ]
                )
            )
        ).all()
        def _assignment_stamp(item):
            value = item.updated_at
            if value is None:
                return datetime.min.replace(tzinfo=timezone.utc)
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)

        assignment_rows.sort(
            key=lambda item: (_assignment_stamp(item), int(item.id or 0)),
            reverse=True,
        )
        assignment = assignment_rows[0] if assignment_rows else None
        for duplicate in assignment_rows[1:]:
            duplicate.state = "superseded"
            duplicate.assignment_version = max(
                1, int(duplicate.assignment_version or 0)
            ) + 1
            duplicate.lease_owner = ""
            duplicate.lease_expires_at = None
            duplicate.lease_reason = "explicit_target_duplicate_assignment"
            duplicate.updated_at = now
            session.add(duplicate)
        if assignment is None:
            target_model = session.get(Codex2APITargetModel, int(target_id))
            if target_model is not None:
                assignment = AccountAssignmentModel(
                    identity_id=identity_id,
                    local_account_id=int(account.id),
                    pool_id=str(target_model.default_pool_id or "PUBLIC_POOL"),
                    target_id=int(target_id),
                    state="active" if binding.enabled else "standby",
                    lease_reason="explicit_target_sync" if binding.enabled else "explicit_target_remote_disabled",
                    lease_started_at=now,
                    assignment_version=1,
                )
                session.add(assignment)
        elif int(assignment.target_id or 0) != int(target_id) and binding.enabled:
            previous_version = int(assignment.assignment_version or 0)
            move_assignments_to_standby(
                session,
                identity_id=identity_id,
                reason="explicit_target_sync",
            )
            assignment.target_id = int(target_id)
            assignment.state = "active"
            assignment.assignment_version = max(1, previous_version) + 1
            assignment.lease_reason = "explicit_target_sync"
            assignment.updated_at = now
            session.add(assignment)
        elif assignment is not None and binding.enabled and assignment.state == "standby":
            assignment.state = "active"
            assignment.assignment_version = max(
                1, int(assignment.assignment_version or 0)
            ) + 1
            assignment.lease_owner = ""
            assignment.lease_expires_at = None
            assignment.lease_reason = "explicit_target_sync"
            assignment.updated_at = now
            session.add(assignment)
        elif assignment is not None and not binding.enabled and int(assignment.target_id or 0) == int(target_id):
            move_assignments_to_standby(
                session,
                identity_id=identity_id,
                target_id=int(target_id),
                reason="explicit_target_remote_disabled",
            )
        update_account_model_codex2api_sync(
            saved,
            True,
            "目标账号已导入",
            session=session,
            commit=False,
        )
        session.commit()


def _quarantine_explicit_target_sync(
    account: Any,
    *,
    target_id: int,
    database_engine: Any,
    reason: str,
) -> None:
    """Persist a failed explicit sync as non-schedulable local state."""

    from sqlmodel import Session, select
    from core.db import AccountAssignmentModel, AccountModel, AccountTargetBindingModel
    from services.account_identity import move_assignments_to_standby

    if not isinstance(account, AccountModel) or account.id is None:
        return
    identity_id = str(account.identity_id or "").strip()
    if not identity_id:
        return
    try:
        with Session(database_engine) as session:
            bindings = session.exec(
                select(AccountTargetBindingModel)
                .where(AccountTargetBindingModel.identity_id == identity_id)
                .where(AccountTargetBindingModel.target_id == int(target_id))
            ).all()
            now = datetime.now(timezone.utc)
            for binding in bindings:
                binding.enabled = False
                binding.sync_status = "failed"
                binding.remote_status = "sync_failed"
                binding.last_error = str(reason or "explicit target sync failed")[:240]
                binding.updated_at = now
                session.add(binding)
            move_assignments_to_standby(
                session,
                identity_id=identity_id,
                target_id=int(target_id),
                reason="explicit_target_sync_failed",
            )
            session.commit()
    except Exception as exc:
        logger.warning(
            "failed to quarantine explicit Codex2API sync (%s)",
            type(exc).__name__,
        )


def _assigned_codex2api_target(account: Any, database_engine: Any):
    """Resolve the account's active target for legacy relogin call sites."""

    from sqlmodel import Session, select

    from core.db import AccountAssignmentModel, AccountModel, Codex2APITargetModel

    if not isinstance(account, AccountModel) or account.id is None:
        return None
    from core.db import engine as default_engine

    target_engine = database_engine or default_engine
    try:
        with Session(target_engine) as session:
            assignment = session.exec(
                select(AccountAssignmentModel).where(
                    AccountAssignmentModel.local_account_id == int(account.id),
                    AccountAssignmentModel.identity_id == str(account.identity_id or ""),
                    AccountAssignmentModel.state.in_(
                        [
                            "active", "draining", "planned", "locking", "uploading",
                            "pending", "verifying", "assignment_committing",
                            "source_cleaning", "target_enabling", "migrating",
                            "target_disabled", "standby",
                        ]
                    ),
                )
            ).first()
            if assignment is None:
                return None
            return session.get(
                Codex2APITargetModel,
                int(assignment.target_id),
            )
    except Exception:
        # Preserve upgrades/imports created before the additive control-plane
        # tables exist; those rows continue through the legacy adapter.
        return None


def sync_codex2api_account(
    account,
    *,
    force: bool = False,
    replace_existing: bool = False,
    target: Any | None = None,
    client: Any | None = None,
    database_engine: Any | None = None,
) -> dict[str, Any] | None:
    """只同步 Codex2API，并独立记录该目标的结果。"""
    from core.config_store import config_store

    # New multi-target callers pass an explicit client.  Keeping this branch
    # separate preserves the long-tested legacy upload path and its exact
    # response semantics for existing login/relogin flows.
    if client is not None or target is not None:
        if target is not None and not remote_bool(
            getattr(target, "enabled", True), True
        ):
            return {
                "name": "Codex2API",
                "ok": False,
                "msg": "目标节点已停用",
            }
        if client is None:
            from services.codex2api_target_client import Codex2APITargetClient, TargetConfig

            if isinstance(target, TargetConfig):
                client = Codex2APITargetClient(target)
            else:
                raise ValueError("explicit Codex2API target requires a TargetConfig or client")
        upload_account = _build_chatgpt_upload_account(account)
        payload: dict[str, Any] = {
            "name": _pick_text({"value": upload_account.email}, "value"),
            "email": _pick_text({"value": upload_account.email}, "value"),
        }
        for key in (
            "refresh_token",
            "access_token",
            "id_token",
            "session_token",
            "account_id",
            "workspace_id",
            "user_id",
            "client_id",
        ):
            value = _pick_text({"value": getattr(upload_account, key, "")}, "value")
            if value:
                payload[key] = value
        if payload.get("workspace_id"):
            payload["account_id"] = payload["workspace_id"]
        try:
            with (
                codex2api_credential_lock(_account_credential_identity_key(account)),
                codex2api_account_mutation_lock(),
                codex2api_target_lock(getattr(target, "id", 0)),
            ):
                if payload.get("refresh_token") and payload.get("access_token"):
                    response = client.import_full_json(payload)
                elif payload.get("refresh_token"):
                    response = client.import_refresh_token(payload)
                elif payload.get("access_token"):
                    response = client.import_access_token(payload)
                else:
                    return {"name": "Codex2API", "ok": False, "msg": "账号缺少凭证"}
                response = response if isinstance(response, dict) else response
                if not _remote_import_confirmed(response):
                    response_message = (
                        response.get("message")
                        or response.get("msg")
                        or response.get("error")
                        if isinstance(response, dict)
                        else ""
                    )
                    message = str(
                        response_message
                        or "Codex2API 未确认账号已导入"
                    ).strip()[:200]
                    return {"name": "Codex2API", "ok": False, "msg": message}
                if target is not None and getattr(target, "id", None) is not None:
                    from core.db import engine as default_engine

                    try:
                        _persist_explicit_target_binding(
                            account,
                            target_id=int(target.id),
                            client=client,
                            database_engine=database_engine or default_engine,
                        )
                    except Exception as persist_exc:
                        _quarantine_explicit_target_sync(
                            account,
                            target_id=int(target.id),
                            database_engine=database_engine or default_engine,
                            reason=f"{type(persist_exc).__name__}",
                        )
                        raise
                return {"name": "Codex2API", "ok": True, "msg": "目标账号已导入"}
        except Exception as exc:
            logger.error("Codex2API target sync failed (%s)", type(exc).__name__)
            return {"name": "Codex2API", "ok": False, "msg": "目标账号同步异常"}

    codex2api_enabled = _is_config_enabled(
        config_store.get("codex2api_enabled", "0"),
        default=False,
    )
    if not force and not codex2api_enabled:
        return None

    from core.db import engine as default_engine

    target_engine = database_engine or default_engine
    assigned_target = _assigned_codex2api_target(account, target_engine)
    if assigned_target is not None:
        try:
            from services.codex2api_target_client import get_target_client

            assigned_client = get_target_client(
                int(assigned_target.id),
                target_engine,
            )
            return sync_codex2api_account(
                account,
                force=True,
                replace_existing=replace_existing,
                target=assigned_target,
                client=assigned_client,
                database_engine=target_engine,
            )
        except Exception as exc:
            logger.error(
                "Codex2API assigned-target sync failed (%s)",
                type(exc).__name__,
            )
            try:
                if database_engine is None:
                    persist_codex2api_sync_result(
                        account, False, "目标节点同步异常"
                    )
                else:
                    persist_codex2api_sync_result(
                        account,
                        False,
                        "目标节点同步异常",
                        database_engine=target_engine,
                    )
            except Exception:
                pass
            return {
                "name": "Codex2API",
                "ok": False,
                "msg": "目标节点同步异常",
            }

    try:
        from platforms.chatgpt.codex2api_upload import upload_to_codex2api

        upload_account = _build_chatgpt_upload_account(account)
        with codex2api_credential_lock(_account_credential_identity_key(account)), codex2api_account_mutation_lock():
            ok, msg = upload_to_codex2api(
                upload_account,
                replace_existing=replace_existing,
            )
    except Exception as exc:
        ok = False
        msg = "Codex2API 自动同步异常"
        logger.error("%s (%s)", msg, type(exc).__name__)

    try:
        if database_engine is None:
            persist_codex2api_sync_result(account, ok, msg)
        else:
            persist_codex2api_sync_result(
                account, ok, msg, database_engine=target_engine
            )
    except Exception as exc:
        logger.error(
            "Codex2API sync state persistence failed (%s)",
            type(exc).__name__,
        )
        remote_message = str(msg or "").strip()
        msg = (
            f"{remote_message}，但同步状态保存失败"
            if remote_message
            else "Codex2API 同步状态保存失败"
        )
        ok = False
    return {"name": "Codex2API", "ok": ok, "msg": msg}


def sync_account(account) -> list[dict[str, Any]]:
    """根据平台将账号同步到外部系统。"""
    from core.config_store import config_store

    platform = getattr(account, "platform", "")
    results: list[dict[str, Any]] = []

    if platform == "chatgpt":
        upload_account = _build_chatgpt_upload_account(account)

        codex2api_result = sync_codex2api_account(account)
        if codex2api_result is not None:
            results.append(codex2api_result)

        # Codex2API 已按独立配置处理；贡献模式继续覆盖其余旧上传目标，避免重复上报。
        contribution_enabled = _is_config_enabled(config_store.get("contribution_enabled", "0"))
        if contribution_enabled:
            contribution_mode = str(config_store.get("contribution_mode", "codex") or "codex").strip().lower()

            if contribution_mode == "custom":
                # 自定义贡献系统模式
                custom_url = str(config_store.get("custom_contribution_url", "") or "").strip()
                custom_token = str(config_store.get("custom_contribution_token", "") or "").strip()
                if not custom_url:
                    msg = "自定义贡献服务器地址未配置"
                    persist_cpa_sync_result(account, False, msg)
                    results.append({"name": "CustomContribution", "ok": False, "msg": msg})
                    return results
                if not custom_token:
                    msg = "自定义贡献系统 token 未配置（请先绑定邮箱）"
                    persist_cpa_sync_result(account, False, msg)
                    results.append({"name": "CustomContribution", "ok": False, "msg": msg})
                    return results

                try:
                    import requests
                    from platforms.chatgpt.cpa_upload import generate_token_json

                    # 生成完整的 token JSON
                    extra = _get_account_extra(account)
                    token_json = generate_token_json(account)

                    # 如果 token_json 中没有 refresh_token，从 extra 获取
                    if not token_json.get("refresh_token"):
                        refresh_token = _pick_text(extra, "refresh_token", "refreshToken")
                        if refresh_token:
                            token_json["refresh_token"] = refresh_token
                    if not token_json.get("access_token"):
                        access_token = _pick_text(extra, "access_token", "accessToken") or getattr(account, "token", "")
                        if access_token:
                            token_json["access_token"] = access_token
                    if not token_json.get("id_token"):
                        id_token = _pick_text(extra, "id_token", "idToken")
                        if id_token:
                            token_json["id_token"] = id_token
                    if not token_json.get("client_id"):
                        client_id = _pick_text(extra, "client_id", "clientId")
                        if client_id:
                            token_json["client_id"] = client_id

                    refresh_token = str(token_json.get("refresh_token") or "").strip()
                    access_token = str(token_json.get("access_token") or "").strip()

                    # 验证必须有 refresh_token
                    if not refresh_token:
                        msg = "账号缺少 refresh_token"
                        persist_cpa_sync_result(account, False, msg)
                        results.append({"name": "CustomContribution", "ok": False, "msg": msg})
                        return results

                    resp = requests.post(
                        f"{custom_url.rstrip('/')}/api/upload",
                        json={
                            "email": account.email,
                            "refresh_token": refresh_token,
                            "access_token": access_token,
                            "token_json": token_json,
                        },
                        headers={"Authorization": f"Bearer {custom_token}"},
                        timeout=15,
                    )
                    data = resp.json()
                    if resp.status_code >= 400:
                        msg = data.get("error") or data.get("message") or str(data)
                        persist_cpa_sync_result(account, False, msg)
                        results.append({"name": "CustomContribution", "ok": False, "msg": msg})
                        return results

                    msg = f"上传成功: {data.get('message', '')}"
                    persist_cpa_sync_result(account, True, msg)
                    results.append({"name": "CustomContribution", "ok": True, "msg": msg})
                    return results
                except Exception as exc:
                    msg = f"上传到自定义贡献系统失败: {exc}"
                    persist_cpa_sync_result(account, False, msg)
                    results.append({"name": "CustomContribution", "ok": False, "msg": msg})
                    return results
            else:
                # codex2api 模式（原有逻辑）
                contribution_url = str(config_store.get("contribution_server_url", "") or "").strip()
                contribution_key = str(config_store.get("contribution_key", "") or "").strip()
                if not contribution_url:
                    msg = "Contribution 服务器地址未配置"
                    persist_cpa_sync_result(account, False, msg)
                    results.append({"name": "Contribution", "ok": False, "msg": msg})
                    return results

                ok, msg = upload_chatgpt_account_to_cpa(
                    account,
                    api_url=contribution_url,
                    api_key=contribution_key or None,
                )
                persist_cpa_sync_result(account, ok, msg)
                results.append({"name": "Contribution", "ok": ok, "msg": msg})
                return results

        cpa_url = str(config_store.get("cpa_api_url", "") or "").strip()
        cpa_enabled = _is_config_enabled(
            config_store.get("cpa_enabled", ""),
            default=bool(cpa_url),
        )
        if cpa_enabled and cpa_url:
            ok, msg = upload_chatgpt_account_to_cpa(account)
            persist_cpa_sync_result(account, ok, msg)
            results.append({"name": "CPA", "ok": ok, "msg": msg})

        codex_proxy_url = str(config_store.get("codex_proxy_url", "") or "").strip()
        if codex_proxy_url:
            upload_type = str(config_store.get("codex_proxy_upload_type", "at") or "at").strip().lower()
            extra = _get_account_extra(account)

            class _CP:
                pass

            cp = _CP()
            cp.access_token = _pick_text(extra, "access_token", "accessToken") or account.token
            cp.refresh_token = _pick_text(extra, "refresh_token", "refreshToken")

            if upload_type == "rt":
                from platforms.chatgpt.cpa_upload import upload_to_codex_proxy
                ok, msg = upload_to_codex_proxy(cp)
                results.append({"name": "CodexProxy(RT)", "ok": ok, "msg": msg})
            else:
                from platforms.chatgpt.cpa_upload import upload_at_to_codex_proxy
                ok, msg = upload_at_to_codex_proxy(cp)
                results.append({"name": "CodexProxy(AT)", "ok": ok, "msg": msg})

        # 关键逻辑：ChatGPT 现在支持同时回填 CPA 和 Sub2API，互不覆盖、分别上报结果。
        sub2api_url = str(config_store.get("sub2api_api_url", "") or "").strip()
        sub2api_key = str(config_store.get("sub2api_api_key", "") or "").strip()
        sub2api_enabled = _is_config_enabled(
            config_store.get("sub2api_enabled", ""),
            default=bool(sub2api_url and sub2api_key),
        )
        if sub2api_enabled and sub2api_url and sub2api_key:
            from platforms.chatgpt.sub2api_upload import upload_to_sub2api

            ok, msg = upload_to_sub2api(
                upload_account,
                api_url=sub2api_url,
                api_key=sub2api_key,
            )
            persist_sub2api_sync_result(account, ok, msg)
            results.append({"name": "Sub2API", "ok": ok, "msg": msg})

    elif platform == "grok":
        grok2api_url = str(config_store.get("grok2api_url", "") or "").strip()
        if grok2api_url:
            from services.grok2api_runtime import ensure_grok2api_ready
            from platforms.grok.grok2api_upload import upload_to_grok2api

            ready, ready_msg = ensure_grok2api_ready()
            if not ready:
                results.append({"name": "grok2api", "ok": False, "msg": ready_msg})
                return results

            ok, msg = upload_to_grok2api(account)
            results.append({"name": "grok2api", "ok": ok, "msg": msg})

    elif platform == "kiro":
        from platforms.kiro.account_manager_upload import resolve_manager_path, upload_to_kiro_manager

        configured_path = str(config_store.get("kiro_manager_path", "") or "").strip()
        target_path = resolve_manager_path(configured_path or None)
        if configured_path or target_path.parent.exists() or target_path.exists():
            ok, msg = upload_to_kiro_manager(account, path=configured_path or None)
            results.append({"name": "Kiro Manager", "ok": ok, "msg": msg})

    return results
