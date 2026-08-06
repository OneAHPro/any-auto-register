from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse, StreamingResponse
from sqlmodel import Session, select, func
from pydantic import BaseModel
from core.db import AccountModel, get_session
from services.chatgpt_account_state import account_is_visible_in_default_list
from services.chatgpt_account_removal import remove_account
from typing import Optional
from datetime import datetime, timezone
import io, csv, json, logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/accounts", tags=["accounts"])


def _safe_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _sanitize_account_extra_for_api(raw_extra: str) -> str:
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

    extra.pop("cookies", None)
    return json.dumps(extra, ensure_ascii=False)


def _account_for_response(account: AccountModel) -> dict:
    payload = account.model_dump()
    payload["extra_json"] = _sanitize_account_extra_for_api(
        str(payload.get("extra_json") or "{}")
    )
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
    start = (page - 1) * page_size
    items = visible_accounts[start:start + page_size]
    return {
        "total": total,
        "page": page,
        "items": [_account_for_response(item) for item in items],
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
    return _account_for_response(acc)


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
        parts = line.strip().split()
        if len(parts) < 2:
            continue
        email, password = parts[0], parts[1]
        extra = parts[2] if len(parts) > 2 else ""
        if extra:
            try:
                json.loads(extra)
            except (json.JSONDecodeError, ValueError):
                extra = "{}"
        else:
            extra = "{}"
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
    return _account_for_response(acc)


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
    return _account_for_response(acc)


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
