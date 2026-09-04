from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse, StreamingResponse
from sqlmodel import Session, select, func
from pydantic import BaseModel
from core.db import (
    AccountAssignmentModel,
    AccountModel,
    AccountPoolModel,
    AccountQuotaSnapshotModel,
    AccountTargetBindingModel,
    Codex2APITargetModel,
    get_session,
)
from core.mail_import_delimiters import split_mail_import_fields
from services.chatgpt_account_state import account_is_visible_in_default_list
from services.chatgpt_account_removal import remove_account
from typing import Optional
from datetime import datetime, timezone
import io, csv, json, logging
from urllib.parse import urlsplit, urlunsplit

_CHATGPT_DIRECT_PASSWORD_DOMAINS = {"icloud.com", "me.com", "mac.com"}

logger = logging.getLogger(__name__)

_ACCOUNT_EXTRA_SECRET_KEYS = {
    "access_token",
    "accesstoken",
    "refresh_token",
    "refreshtoken",
    "id_token",
    "idtoken",
    "session_token",
    "sessiontoken",
    "cookies",
    "password",
    "totp_secret",
    "mfa_secret",
    "recovery_code",
    "mfa_recovery_code",
}


def _is_secret_key(key: object) -> bool:
    normalized = str(key or "").strip().lower().replace("-", "_")
    if normalized in _ACCOUNT_EXTRA_SECRET_KEYS:
        return True
    return any(
        marker in normalized
        for marker in (
            "refresh_token",
            "access_token",
            "session_token",
            "id_token",
            "cookie",
            "password",
            "totp",
            "mfa_secret",
            "recovery_code",
            "private_key",
            "admin_key",
            "api_key",
        )
    )


def _scrub_nested_extra(value: object, key: object = "") -> object:
    """Recursively remove credential-shaped fields from API projections."""

    if isinstance(value, dict):
        cleaned: dict[str, object] = {}
        for child_key, child_value in value.items():
            if _is_secret_key(child_key):
                continue
            cleaned[str(child_key)] = _scrub_nested_extra(child_value, child_key)
        return cleaned
    if isinstance(value, list):
        return [_scrub_nested_extra(item, key) for item in value]
    if isinstance(value, str) and "url" in str(key or "").lower():
        try:
            parsed = urlsplit(value)
            if parsed.query or parsed.fragment:
                # Keep the navigable endpoint/path while dropping query
                # parameters that commonly carry bearer or checkout secrets.
                return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        except ValueError:
            pass
    return value

router = APIRouter(prefix="/accounts", tags=["accounts"])


def _safe_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _sanitize_account_extra_for_api(
    raw_extra: str,
    *,
    strip_credentials: bool = False,
) -> str:
    try:
        extra = json.loads(raw_extra or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        extra = {}
    if not isinstance(extra, dict):
        extra = {}

    mailbox_context = extra.get("mailbox_login_context")
    if isinstance(mailbox_context, dict):
        extra["mailbox_login_context"] = {
            "provider": str(mailbox_context.get("provider") or "").strip(),
            "email": str(mailbox_context.get("email") or "").strip(),
            "account_id": str(mailbox_context.get("account_id") or "").strip(),
            "configured": True,
        }

    resume_context = extra.get("oauth_resume_context")
    if isinstance(resume_context, dict):
        flow_state = resume_context.get("flow_state")
        page_type = (
            str(flow_state.get("page_type") or "").strip()
            if isinstance(flow_state, dict)
            else ""
        )
        version = int(resume_context.get("version") or 0)
        ready = bool(
            version == 2
            and str(resume_context.get("code_verifier") or "").strip()
            and str(resume_context.get("oauth_state") or "").strip()
            and page_type
        )
        extra["oauth_resume_context"] = {
            "version": version,
            "created_at": _safe_float(resume_context.get("created_at")),
            "expires_at": _safe_float(resume_context.get("expires_at")),
            "ready": ready,
            "flow_state": {"page_type": page_type},
        }

    browser_context = extra.get("oauth_browser_context")
    if isinstance(browser_context, dict):
        version = int(browser_context.get("version") or 0)
        extra["oauth_browser_context"] = {
            "version": version,
            "created_at": _safe_float(browser_context.get("created_at")),
            "expires_at": _safe_float(browser_context.get("expires_at")),
            "ready": bool(
                version == 1
                and isinstance(browser_context.get("cookies"), list)
                and browser_context.get("cookies")
            ),
        }

    if strip_credentials:
        extra = _scrub_nested_extra(extra)
    return json.dumps(extra, ensure_ascii=False)


def _empty_control_plane_summary(identity_id: str = "") -> dict[str, object]:
    return {
        "identity_id": identity_id,
        "assignment": None,
        "binding": None,
        "quota": {},
    }


def _account_control_plane_summaries(
    accounts: list[AccountModel],
    session: Session,
) -> dict[str, dict[str, object]]:
    """Batch-load control-plane projections for one paginated account list."""

    identity_ids = {
        str(account.identity_id or "").strip()
        for account in accounts
        if str(account.identity_id or "").strip()
    }
    summaries = {
        identity_id: _empty_control_plane_summary(identity_id)
        for identity_id in identity_ids
    }
    if not identity_ids:
        return summaries

    assignments = session.exec(
        select(AccountAssignmentModel)
        .where(AccountAssignmentModel.identity_id.in_(identity_ids))
        .where(AccountAssignmentModel.state.in_(["active", "draining", "standby"]))
        .order_by(AccountAssignmentModel.updated_at.desc())
    ).all()
    assignment_by_identity: dict[str, AccountAssignmentModel] = {}
    for assignment in assignments:
        assignment_by_identity.setdefault(str(assignment.identity_id), assignment)
    target_ids = {int(assignment.target_id) for assignment in assignments}
    pool_ids = {str(assignment.pool_id) for assignment in assignments}
    target_names = {
        int(target.id): target.name
        for target in session.exec(
            select(Codex2APITargetModel).where(Codex2APITargetModel.id.in_(target_ids))
        ).all()
        if target.id is not None
    } if target_ids else {}
    pool_names = {
        str(pool.id): pool.name
        for pool in session.exec(
            select(AccountPoolModel).where(AccountPoolModel.id.in_(pool_ids))
        ).all()
    } if pool_ids else {}

    bindings = session.exec(
        select(AccountTargetBindingModel).where(
            AccountTargetBindingModel.identity_id.in_(identity_ids)
        )
    ).all()
    binding_by_key = {
        (str(binding.identity_id), int(binding.target_id)): binding
        for binding in bindings
    }
    snapshots = session.exec(
        select(AccountQuotaSnapshotModel)
        .where(AccountQuotaSnapshotModel.identity_id.in_(identity_ids))
        .where(AccountQuotaSnapshotModel.window.in_(["5h", "7d", "monthly"]))
        .order_by(AccountQuotaSnapshotModel.captured_at.desc())
    ).all()
    snapshot_by_key: dict[tuple[str, str], AccountQuotaSnapshotModel] = {}
    for snapshot in snapshots:
        key = (str(snapshot.identity_id), str(snapshot.window))
        current = snapshot_by_key.get(key)
        assigned_target = assignment_by_identity.get(str(snapshot.identity_id))
        preferred_target_id = (
            int(assigned_target.target_id)
            if assigned_target is not None
            else None
        )
        if current is None or (
            preferred_target_id is not None
            and int(snapshot.target_id or 0) == preferred_target_id
            and int(current.target_id or 0) != preferred_target_id
        ):
            snapshot_by_key[key] = snapshot

    from services.quota_ledger import evaluate_snapshot

    for identity_id, summary in summaries.items():
        assignment = assignment_by_identity.get(identity_id)
        if assignment is not None:
            summary["assignment"] = {
                "pool_id": assignment.pool_id,
                "pool_name": pool_names.get(str(assignment.pool_id), ""),
                "target_id": int(assignment.target_id),
                "target_name": target_names.get(int(assignment.target_id), ""),
                "state": assignment.state,
                "lease_owner": assignment.lease_owner,
                "lease_reason": assignment.lease_reason,
                "lease_started_at": assignment.lease_started_at.isoformat()
                if assignment.lease_started_at
                else None,
                "lease_expires_at": assignment.lease_expires_at.isoformat()
                if assignment.lease_expires_at
                else None,
                "assignment_version": int(assignment.assignment_version or 0),
            }
            binding = binding_by_key.get((identity_id, int(assignment.target_id)))
            if binding is not None:
                summary["binding"] = {
                    "target_id": int(binding.target_id),
                    "remote_account_id": int(binding.remote_account_id or 0),
                    "sync_status": binding.sync_status,
                    "remote_status": binding.remote_status,
                    "enabled": bool(binding.enabled),
                    "last_sync_at": binding.last_sync_at.isoformat()
                    if binding.last_sync_at
                    else None,
                    "last_error": binding.last_error,
                }
        quota: dict[str, dict[str, object]] = {}
        for window in ("5h", "7d", "monthly"):
            snapshot = snapshot_by_key.get((identity_id, window))
            if snapshot is None:
                continue
            evaluated = evaluate_snapshot(snapshot)
            quota[window] = {
                "usage_percent": snapshot.usage_percent,
                "billed_usd": snapshot.billed_usd,
                "continuous_billed_usd": float(evaluated.continuous_billed_usd),
                "remaining_usd": float(evaluated.remaining_usd)
                if evaluated.remaining_usd is not None
                else None,
                "continuous_remaining_usd": float(evaluated.continuous_remaining_usd)
                if evaluated.continuous_remaining_usd is not None
                else None,
                "remaining_scope": evaluated.remaining_scope,
                "reset_at": evaluated.reset_at.isoformat()
                if evaluated.reset_at
                else None,
                "captured_at": evaluated.captured_at.isoformat(),
                "continuity_state": evaluated.continuity_state,
                "fresh": evaluated.fresh,
                "scheduler_eligible": evaluated.scheduler_eligible,
            }
        summary["quota"] = quota
    return summaries


def _account_control_plane_summary(account: AccountModel, session: Session) -> dict:
    identity_id = str(getattr(account, "identity_id", "") or "").strip()
    if not identity_id:
        return _empty_control_plane_summary()
    return _account_control_plane_summaries([account], session).get(
        identity_id,
        _empty_control_plane_summary(identity_id),
    )


def _account_for_response(
    account: AccountModel,
    session: Session | None = None,
    *,
    include_credentials: bool = True,
    control_plane_summary: dict[str, object] | None = None,
) -> dict:
    payload = account.model_dump()
    if not include_credentials:
        payload.pop("password", None)
        payload.pop("token", None)
        cashier_url = str(payload.get("cashier_url") or "")
        if cashier_url:
            payload["cashier_url"] = _scrub_nested_extra(cashier_url, "cashier_url")
    payload["extra_json"] = _sanitize_account_extra_for_api(
        str(payload.get("extra_json") or "{}"),
        strip_credentials=not include_credentials,
    )
    if control_plane_summary is not None:
        payload.update(control_plane_summary)
    elif session is not None:
        payload.update(_account_control_plane_summary(account, session))
    return payload


class AccountCreate(BaseModel):
    platform: str
    email: str
    password: str
    status: str = "registered"
    token: str = ""
    cashier_url: str = ""


class AccountUpdate(BaseModel):
    status: Optional[str] = None
    token: Optional[str] = None
    cashier_url: Optional[str] = None


class ImportRequest(BaseModel):
    platform: str
    lines: list[str]
    account_type: Optional[str] = None


class BatchDeleteRequest(BaseModel):
    ids: list[int]


@router.get("")
def list_accounts(
    platform: Optional[str] = None,
    status: Optional[str] = None,
    email: Optional[str] = None,
    created_at_start: Optional[datetime] = None,
    created_at_end: Optional[datetime] = None,
    page: int = 1,
    page_size: int = 20,
    include_live: bool = False,
    session: Session = Depends(get_session),
):
    q = select(AccountModel)
    if platform:
        q = q.where(AccountModel.platform == platform)
    if status:
        q = q.where(AccountModel.status == status)
    if email:
        q = q.where(AccountModel.email.contains(email))
    if created_at_start:
        q = q.where(AccountModel.created_at >= created_at_start)
    if created_at_end:
        q = q.where(AccountModel.created_at <= created_at_end)
    visible_accounts = [
        account
        for account in session.exec(q).all()
        if account_is_visible_in_default_list(account)
    ]
    total = len(visible_accounts)
    page_size = max(1, min(int(page_size or 20), 200))
    page = max(1, int(page or 1))
    start = (page - 1) * page_size
    items = visible_accounts[start:start + page_size]
    summaries = _account_control_plane_summaries(items, session)

    live_display: dict[int | None, dict[str, object]] = {}
    if include_live and str(platform or "").strip().lower() == "chatgpt":
        live_rows: list[dict[str, object]] | None = None
        live_error = ""
        try:
            from services.chatgpt_codex2api_health import fetch_codex2api_quota_accounts

            fetched = fetch_codex2api_quota_accounts(
                database_engine=session.get_bind(),
                include_display_fields=True,
            )
            if fetched is not None:
                live_rows = [
                    row for row in fetched if isinstance(row, dict)
                ]
        except Exception as exc:
            # The list endpoint remains usable when a remote target is down;
            # the projection marks quota as unavailable instead of serving a
            # stale local estimate as current data.
            live_error = type(exc).__name__
            logger.warning("读取 ChatGPT 实时展示数据失败: %s", live_error)

        from services.chatgpt_account_display import build_chatgpt_account_display_map

        live_display = build_chatgpt_account_display_map(
            items,
            live_rows or [],
            live_available=live_rows is not None,
            live_error=live_error,
        )

    response_items = []
    for item in items:
        payload = _account_for_response(
            item,
            control_plane_summary=summaries.get(
                str(item.identity_id or "").strip(),
                _empty_control_plane_summary(
                    str(item.identity_id or "").strip()
                ),
            ),
        )
        if include_live and str(platform or "").strip().lower() == "chatgpt":
            payload["chatgpt_display"] = live_display.get(
                item.id,
                {"plan_type": None, "plan_source": "none", "quota": None, "quota_status": "not_configured"},
            )
        response_items.append(payload)
    return {
        "total": total,
        "page": page,
        "items": response_items,
    }


@router.post("")
def create_account(body: AccountCreate, session: Session = Depends(get_session)):
    acc = AccountModel(
        platform=body.platform,
        email=body.email,
        password=body.password,
        status=body.status,
        token=body.token,
        cashier_url=body.cashier_url,
    )
    session.add(acc)
    session.commit()
    session.refresh(acc)
    return _account_for_response(acc, session=session)


@router.get("/stats")
def get_stats(session: Session = Depends(get_session)):
    """统计各平台账号数量和状态分布"""
    accounts = session.exec(select(AccountModel)).all()
    platforms: dict = {}
    statuses: dict = {}
    for acc in accounts:
        platforms[acc.platform] = platforms.get(acc.platform, 0) + 1
        statuses[acc.status] = statuses.get(acc.status, 0) + 1
    return {"total": len(accounts), "by_platform": platforms, "by_status": statuses}


@router.get("/export")
def export_accounts(
    platform: Optional[str] = None,
    status: Optional[str] = None,
    session: Session = Depends(get_session),
):
    q = select(AccountModel)
    if platform:
        q = q.where(AccountModel.platform == platform)
    if status:
        q = q.where(AccountModel.status == status)
    accounts = session.exec(q).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["platform", "email", "password", "user_id", "region",
                     "status", "cashier_url", "created_at"])
    for acc in accounts:
        writer.writerow([acc.platform, acc.email, acc.password, acc.user_id,
                         acc.region, acc.status, acc.cashier_url,
                         acc.created_at.strftime("%Y-%m-%d %H:%M:%S")])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=accounts.csv"}
    )


@router.post("/import")
def import_accounts(
    body: ImportRequest,
    session: Session = Depends(get_session),
):
    """批量导入，每行格式: email password [extra]"""
    created = 0
    for line in body.lines:
        parts = split_mail_import_fields(str(line or "").strip())
        if len(parts) < 2:
            continue
        email, password = parts[0], parts[1]
        extra = " ".join(parts[2:]) if len(parts) > 2 else ""
        if extra:
            try:
                json.loads(extra)
            except (json.JSONDecodeError, ValueError):
                extra = "{}"
        else:
            account_type = str(body.account_type or "").strip()
            if not account_type and body.platform.strip().lower() == "chatgpt":
                domain = str(email or "").strip().lower().rpartition("@")[2]
                account_type = (
                    "chatgpt_password"
                    if domain in _CHATGPT_DIRECT_PASSWORD_DOMAINS
                    else "chatgpt_google_password"
                )
            extra = (
                json.dumps({"account_type": account_type}, ensure_ascii=False)
                if account_type
                else "{}"
            )
        acc = AccountModel(platform=body.platform, email=email,
                           password=password, extra_json=extra)
        session.add(acc)
        created += 1
    session.commit()
    return {"created": created}


@router.post("/batch-delete")
def batch_delete_accounts(
    body: BatchDeleteRequest,
    session: Session = Depends(get_session)
):
    """批量删除账号"""
    if not body.ids:
        raise HTTPException(400, "账号 ID 列表不能为空")
    
    if len(body.ids) > 1000:
        raise HTTPException(400, "单次最多删除 1000 个账号")
    
    unique_ids = list(dict.fromkeys(body.ids))
    database_engine = session.get_bind()
    items: list[dict] = []
    for account_id in unique_ids:
        try:
            result = remove_account(
                account_id,
                database_engine=database_engine,
            )
        except Exception as exc:
            result = {
                "ok": False,
                "account_id": int(account_id),
                "status": "database_error",
                "local_deleted": False,
                "codex2api": {"enabled": False, "status": "not_attempted"},
                "error_code": "database_error",
                "message": f"账号删除异常（{type(exc).__name__}）"[:200],
            }
        items.append(result)

    successful = [item for item in items if bool(item.get("ok"))]
    not_found_ids = [
        int(item.get("account_id") or 0)
        for item in items
        if item.get("status") == "not_found"
    ]
    failed_count = sum(
        1
        for item in items
        if not bool(item.get("ok")) and item.get("status") != "not_found"
    )

    def _remote_count(*statuses: str) -> int:
        expected = set(statuses)
        return sum(
            1
            for item in successful
            if str((item.get("codex2api") or {}).get("status") or "") in expected
        )

    response = {
        "total_requested": len(body.ids),
        "total_unique": len(unique_ids),
        "deleted": len(successful),
        "failed": failed_count,
        "not_found": not_found_ids,
        "remote_deleted": _remote_count("deleted"),
        "remote_already_absent": _remote_count("already_absent"),
        "remote_skipped": _remote_count("skipped_disabled", "not_applicable"),
        "items": items,
    }
    logger.info(
        "批量删除完成: requested=%s unique=%s deleted=%s failed=%s not_found=%s",
        response["total_requested"],
        response["total_unique"],
        response["deleted"],
        response["failed"],
        len(not_found_ids),
    )
    return response


@router.post("/check-all")
def check_all_accounts(platform: Optional[str] = None,
                       background_tasks: BackgroundTasks = None):
    from core.scheduler import scheduler
    background_tasks.add_task(scheduler.check_accounts_valid, platform)
    return {"message": "批量检测任务已启动"}


@router.get("/{account_id}")
def get_account(account_id: int, session: Session = Depends(get_session)):
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    return _account_for_response(acc, session=session)


@router.patch("/{account_id}")
def update_account(account_id: int, body: AccountUpdate,
                   session: Session = Depends(get_session)):
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    if body.status is not None:
        acc.status = body.status
    if body.token is not None:
        acc.token = body.token
    if body.cashier_url is not None:
        acc.cashier_url = body.cashier_url
    acc.updated_at = datetime.now(timezone.utc)
    session.add(acc)
    session.commit()
    session.refresh(acc)
    return _account_for_response(acc, session=session)


@router.delete("/{account_id}")
def delete_account(account_id: int, session: Session = Depends(get_session)):
    try:
        result = remove_account(
            account_id,
            database_engine=session.get_bind(),
        )
    except Exception as exc:
        result = {
            "ok": False,
            "account_id": int(account_id),
            "status": "database_error",
            "local_deleted": False,
            "codex2api": {"enabled": False, "status": "not_attempted"},
            "error_code": "database_error",
            "message": f"账号删除异常（{type(exc).__name__}）"[:200],
        }
    if bool(result.get("ok")):
        return result
    status = str(result.get("status") or "")
    status_code = (
        404
        if status == "not_found"
        else 409
        if status in {"busy", "local_delete_conflict"}
        else 502
        if status == "remote_failed"
        else 500
    )
    return JSONResponse(
        status_code=status_code,
        content={
            **result,
            "detail": str(result.get("message") or "删除失败")[:200],
        },
    )


@router.post("/{account_id}/check")
def check_account(account_id: int, background_tasks: BackgroundTasks,
                  session: Session = Depends(get_session)):
    acc = session.get(AccountModel, account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    background_tasks.add_task(_do_check, account_id)
    return {"message": "检测任务已启动"}


def _do_check(account_id: int):
    from core.db import engine
    from sqlmodel import Session
    with Session(engine) as s:
        acc = s.get(AccountModel, account_id)
    if acc:
        from core.base_platform import Account, RegisterConfig
        from core.registry import get
        try:
            PlatformCls = get(acc.platform)
            plugin = PlatformCls(config=RegisterConfig())
            obj = Account(platform=acc.platform, email=acc.email,
                         password=acc.password, user_id=acc.user_id,
                         region=acc.region, token=acc.token,
                         extra=json.loads(acc.extra_json or "{}"))
            valid = plugin.check_valid(obj)
            with Session(engine) as s:
                a = s.get(AccountModel, account_id)
                if a:
                    if a.platform != "chatgpt":
                        a.status = a.status if valid else "invalid"
                    a.updated_at = datetime.now(timezone.utc)
                    s.add(a)
                    s.commit()
        except Exception:
            logger.exception("检测账号 %s 时出错", account_id)
