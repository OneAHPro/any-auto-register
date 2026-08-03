from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, StrictInt
from sqlmodel import Session, select
from typing import Optional
from copy import deepcopy
from datetime import datetime, timezone
from weakref import WeakKeyDictionary
import math
import os
import uuid
from core.db import (
    AccountModel,
    ChatGPTAttemptBindingModel,
    TaskLog,
    TaskRunModel,
    engine,
)
from core.task_runtime import (
    AttemptOutcome,
    AttemptResult,
    RegisterTaskStore,
    SkipCurrentAttemptRequested,
    StopTaskRequested,
)
from core.sms_pool import SmsPoolExhaustedError, mask_sms_code, sms_pool_service
from core.chatgpt_task_gate import chatgpt_task_gate
from platforms.chatgpt.log_sanitizer import sanitize_chatgpt_log_message
import time, json, asyncio, threading, logging, re

router = APIRouter(prefix="/tasks", tags=["tasks"])
logger = logging.getLogger(__name__)

MAX_FINISHED_TASKS = 200
CLEANUP_THRESHOLD = 250
CHATGPT_BIND_PHONE_FLAG = "chatgpt_existing_account_bind_phone_and_get_rt"
CHATGPT_LEADBEE_CODES_KEY = "chatgpt_existing_account_leadbee_codes"
CHATGPT_USE_SMS_POOL_FLAG = "chatgpt_existing_account_use_sms_pool"
CHATGPT_LEADBEE_BASE_URLS_KEY = "chatgpt_existing_account_leadbee_base_urls"
CHATGPT_SMS_POOL_ITEM_IDS_KEY = "chatgpt_sms_pool_item_ids"
CHATGPT_RETRY_BINDINGS_KEY = "chatgpt_retry_bindings"
CHATGPT_MAIL_PROVIDER_PLAN_KEY = "chatgpt_existing_account_mail_provider_plan"
CHATGPT_RELOGIN_MAX_CONCURRENCY = 10
CHATGPT_PHONE_FINALIZATION_WAIT_SECONDS = 45.0
MAX_PERSISTED_TASK_LOG_ENTRIES = 500
MAX_PERSISTED_TASK_LOG_BYTES = 256 * 1024
TASK_SNAPSHOT_PERSIST_INTERVAL_SECONDS = 1.0
_CHATGPT_LEADBEE_SECRET_KEYS = {
    CHATGPT_LEADBEE_CODES_KEY,
    CHATGPT_LEADBEE_BASE_URLS_KEY,
    CHATGPT_SMS_POOL_ITEM_IDS_KEY,
    CHATGPT_USE_SMS_POOL_FLAG,
    CHATGPT_RETRY_BINDINGS_KEY,
    "_chatgpt_attempt_binding_callback",
    "leadbee_code",
    "chatgpt_leadbee_code",
}
_task_store = RegisterTaskStore(
    max_finished_tasks=MAX_FINISHED_TASKS,
    cleanup_threshold=CLEANUP_THRESHOLD,
)
_chatgpt_task_enqueue_lock = threading.RLock()
_chatgpt_binding_db_lock = threading.RLock()
_chatgpt_binding_table_ready = WeakKeyDictionary()
_task_snapshot_persist_lock = threading.RLock()
_task_snapshot_last_persisted_at: dict[str, float] = {}
_task_snapshot_write_locks: dict[str, threading.Lock] = {}
_automation_stop_watchdog_lock = threading.Lock()
_automation_runner_active_tasks: set[str] = set()
_automation_stop_watchdog_tasks: dict[str, threading.Event] = {}
_sms_pool_quarantine_lock = threading.Lock()
_sms_pool_quarantine_item_ids_by_task: dict[str, set[int]] = {}


def _automation_force_stop_seconds() -> float:
    """Return the grace period before recycling a stuck automation worker."""
    raw_value = os.getenv("CHATGPT_AUTOMATION_FORCE_STOP_SECONDS", "30")
    try:
        seconds = float(raw_value)
    except (TypeError, ValueError):
        seconds = 30.0
    if not math.isfinite(seconds):
        seconds = 30.0
    return min(max(seconds, 0.01), 300.0)


def _is_automatic_chatgpt_task(snapshot: dict) -> bool:
    return (
        str(snapshot.get("platform") or "").strip().lower() == "chatgpt"
        and _is_truthy((snapshot.get("meta") or {}).get("automation"))
    )


def _mark_automation_runner_started(task_id: str) -> None:
    with _automation_stop_watchdog_lock:
        _automation_runner_active_tasks.add(task_id)


def _mark_automation_runner_finished(task_id: str) -> None:
    """Signal completion only after the automation gate has been released."""
    with _automation_stop_watchdog_lock:
        _automation_runner_active_tasks.discard(task_id)
        completion = _automation_stop_watchdog_tasks.get(task_id)
        if completion is not None:
            completion.set()


def _arm_automation_stop_watchdog(task_id: str) -> bool:
    """Recycle the service if a stopped automation runner cannot quiesce."""
    with _automation_stop_watchdog_lock:
        if task_id in _automation_stop_watchdog_tasks:
            # The runner is already protected. Treat this as successfully
            # armed so repeated stop requests remain idempotent even after the
            # task snapshot has reached a terminal state.
            return True
        runner_active = task_id in _automation_runner_active_tasks
        snapshot = _task_store.snapshot_if_present(task_id)
        if snapshot is None and not runner_active:
            return False
        if not runner_active:
            if not _is_automatic_chatgpt_task(snapshot or {}):
                return False
            if str((snapshot or {}).get("status") or "") not in {
                "pending",
                "running",
            }:
                return False
        runner_finished = threading.Event()
        _automation_stop_watchdog_tasks[task_id] = runner_finished

    grace_seconds = _automation_force_stop_seconds()

    def _watch() -> None:
        try:
            if runner_finished.wait(timeout=grace_seconds):
                return
            # Do not take a logging or database lock here. The watchdog must
            # still terminate the process when one of those locks is wedged.
            os._exit(75)
        finally:
            # This also keeps tests deterministic when os._exit is mocked.
            with _automation_stop_watchdog_lock:
                if (
                    _automation_stop_watchdog_tasks.get(task_id)
                    is runner_finished
                ):
                    _automation_stop_watchdog_tasks.pop(task_id, None)

    watchdog = threading.Thread(
        target=_watch,
        name=f"automation-stop-watchdog-{task_id[-12:]}",
        daemon=True,
    )
    try:
        watchdog.start()
    except Exception:
        with _automation_stop_watchdog_lock:
            if (
                _automation_stop_watchdog_tasks.get(task_id)
                is runner_finished
            ):
                _automation_stop_watchdog_tasks.pop(task_id, None)
        # The gate deliberately swallows stop-callback exceptions. If a
        # watchdog thread cannot start, recycle synchronously instead of
        # leaving foreground work blocked forever.
        os._exit(75)
        return False
    return True


class RegisterTaskRequest(BaseModel):
    platform: str
    email: Optional[str] = None
    password: Optional[str] = None
    count: int = 1
    concurrency: int = 1
    register_delay_seconds: float = 0
    proxy: Optional[str] = None
    executor_type: str = "protocol"
    captcha_solver: str = "yescaptcha"
    extra: dict = Field(default_factory=dict)


class ChatGPTReloginTaskRequest(BaseModel):
    account_ids: list[StrictInt] = Field(default_factory=list)
    concurrency: StrictInt = Field(
        default=1,
        ge=1,
        le=CHATGPT_RELOGIN_MAX_CONCURRENCY,
    )


class ChatGPTRetryFailedTaskRequest(BaseModel):
    concurrency: StrictInt = Field(
        default=1,
        ge=1,
        le=CHATGPT_RELOGIN_MAX_CONCURRENCY,
    )


def _normalize_chatgpt_relogin_account_ids(
    value,
    *,
    max_accounts: int | None = 100,
) -> list[int]:
    if not isinstance(value, (list, tuple)):
        raise HTTPException(400, "账号 ID 列表格式无效")

    normalized: list[int] = []
    seen: set[int] = set()
    for raw in value:
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise HTTPException(400, "账号 ID 必须为正整数")
        account_id = raw
        if account_id <= 0:
            raise HTTPException(400, "账号 ID 必须为正整数")
        if account_id in seen:
            continue
        seen.add(account_id)
        normalized.append(account_id)

    if not normalized:
        raise HTTPException(400, "请选择需要重登的 ChatGPT 账号")
    if max_accounts is not None and len(normalized) > max_accounts:
        raise HTTPException(400, "单次最多重登 100 个 ChatGPT 账号")
    return normalized


def _normalize_chatgpt_relogin_concurrency(
    value,
    *,
    account_count: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(400, "并发数必须为整数")
    if value < 1 or value > CHATGPT_RELOGIN_MAX_CONCURRENCY:
        raise HTTPException(
            400,
            f"并发数必须在 1 到 {CHATGPT_RELOGIN_MAX_CONCURRENCY} 之间",
        )
    return min(value, account_count)


def _is_truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _normalize_leadbee_codes(value) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        candidates = value.splitlines()
    elif isinstance(value, (list, tuple)):
        candidates = value
    else:
        raise HTTPException(400, "LeadBee 卡密格式无效，请一行填写一个卡密")
    normalized = [
        str(code or "").strip()
        for code in candidates
        if str(code or "").strip()
    ]
    if len(normalized) != len(set(normalized)):
        raise HTTPException(
            400,
            "LeadBee 卡密不能重复，请为每个账号提供唯一卡密",
        )
    return normalized


def _normalize_leadbee_base_urls(value) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, (list, tuple)):
        raise HTTPException(400, "接码地址格式无效")
    return [str(item or "").strip().rstrip("/") for item in value]


def _normalize_sms_pool_item_ids(value) -> list[int]:
    if value in (None, ""):
        return []
    if not isinstance(value, (list, tuple)):
        raise HTTPException(400, "SMS 接码池卡密绑定格式无效")
    try:
        normalized = [int(item) for item in value]
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "SMS 接码池卡密绑定格式无效") from exc
    if any(item < 1 for item in normalized) or len(normalized) != len(set(normalized)):
        raise HTTPException(400, "SMS 接码池卡密绑定格式无效")
    return normalized


def _normalize_chatgpt_retry_bindings(value) -> list[dict]:
    if value in (None, ""):
        return []
    if not isinstance(value, (list, tuple)):
        raise HTTPException(400, "失败账号绑定格式无效")
    normalized = []
    for raw in value:
        if isinstance(raw, ChatGPTAttemptBindingModel):
            mailbox_context = _json_loads(raw.mailbox_context_json, {})
            source = {
                "id": raw.id,
                "email": raw.email,
                "leadbee_code": raw.leadbee_code,
                "use_sms_pool": (
                    _is_truthy(mailbox_context.get("sms_pool_managed"))
                    if isinstance(mailbox_context, dict)
                    else False
                ),
                "mail_provider": (
                    mailbox_context.get("provider")
                    if isinstance(mailbox_context, dict)
                    else ""
                ),
            }
        elif isinstance(raw, dict):
            source = raw
        else:
            raise HTTPException(400, "失败账号绑定格式无效")
        email = str(source.get("email") or "").strip()
        leadbee_code = str(source.get("leadbee_code") or "").strip()
        try:
            binding_id = int(source.get("id") or source.get("binding_id") or 0)
        except (TypeError, ValueError):
            binding_id = 0
        if not email or not leadbee_code:
            raise HTTPException(400, "失败账号缺少邮箱或对应 LeadBee 卡密")
        item = {
            "id": binding_id,
            "email": email,
            "leadbee_code": leadbee_code,
        }
        if _is_truthy(
            source.get("use_sms_pool") or source.get("sms_pool_managed")
        ):
            item["use_sms_pool"] = True
        mail_provider = str(
            source.get("mail_provider") or source.get("provider") or ""
        ).strip().lower()
        if mail_provider not in {"", "custom_provider", "chatgpt_credentials"}:
            item["mail_provider"] = mail_provider
        normalized.append(item)
    return normalized


def _normalize_chatgpt_mail_provider_plan(value) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, (list, tuple)):
        raise HTTPException(400, "邮箱来源分配格式无效")
    normalized = [str(item or "").strip().lower() for item in value]
    if any(item not in {"microsoft", "applemail"} for item in normalized):
        raise HTTPException(400, "邮箱来源分配包含不支持的类型")
    return normalized


def _build_chatgpt_retry_request(
    bindings,
    concurrency: int = 1,
) -> RegisterTaskRequest:
    """Build a bounded retry task while preserving each mailbox/card binding."""
    from core.config_store import config_store

    normalized = _normalize_chatgpt_retry_bindings(bindings)
    if not normalized:
        raise HTTPException(400, "没有可重试的失败账号")
    normalized_concurrency = _normalize_chatgpt_relogin_concurrency(
        concurrency,
        account_count=len(normalized),
    )
    config = config_store.get_all().copy()
    executor_type = str(config.get("default_executor") or "headless").strip()
    captcha_solver = str(
        config.get("default_captcha_solver") or "yescaptcha"
    ).strip()
    pool_modes = [bool(item.get("use_sms_pool")) for item in normalized]
    if any(pool_modes) and not all(pool_modes):
        raise HTTPException(409, "手填卡密与 SMS 接码池任务不能混合重试")
    use_sms_pool = all(pool_modes)
    extra = {
        "chatgpt_registration_mode": "refresh_token",
        "chatgpt_has_refresh_token_solution": True,
        "chatgpt_existing_account_login_only": True,
        "chatgpt_existing_account_login_stage": "access_token",
        "chatgpt_existing_account_allow_phone_verification": False,
        CHATGPT_BIND_PHONE_FLAG: True,
        CHATGPT_RETRY_BINDINGS_KEY: normalized,
    }
    if use_sms_pool:
        extra[CHATGPT_USE_SMS_POOL_FLAG] = True
    else:
        extra[CHATGPT_LEADBEE_CODES_KEY] = [
            item["leadbee_code"] for item in normalized
        ]
    return RegisterTaskRequest(
        platform="chatgpt",
        count=len(normalized),
        concurrency=normalized_concurrency,
        register_delay_seconds=1,
        executor_type=executor_type,
        captcha_solver=captcha_solver,
        proxy=None,
        extra=extra,
    )


def _chatgpt_bind_phone_enabled(req: RegisterTaskRequest) -> bool:
    return (
        req.platform == "chatgpt"
        and _is_truthy(req.extra.get("chatgpt_existing_account_login_only"))
        and _is_truthy(req.extra.get(CHATGPT_BIND_PHONE_FLAG))
    )


def _redact_task_secret(message, secret: str) -> str:
    text = str(message or "").strip()
    normalized_secret = str(secret or "").strip()
    if normalized_secret:
        text = text.replace(normalized_secret, "[卡密已隐藏]")
    return text


def _redact_chatgpt_relogin_log(message) -> str:
    return sanitize_chatgpt_log_message(message).strip()


_LEADBEE_CARD_UNUSABLE_MARKERS = (
    "LeadBee 兑换码已使用",
    "CARD_ALREADY_USED",
    "任务不可取消",
    "状态不可取消",
    "卡密不可复用",
)

_LEADBEE_POOL_REPLACEMENT_ERROR_CODES = frozenset(
    {"CARD_ALREADY_USED", "CARD_NOT_FOUND"}
)


def _leadbee_card_cannot_be_reused(message) -> bool:
    normalized = str(message or "")
    return any(marker in normalized for marker in _LEADBEE_CARD_UNUSABLE_MARKERS)


def _task_action_terms(req: RegisterTaskRequest) -> tuple[str, str]:
    if req.platform == "chatgpt" and _is_truthy(
        req.extra.get("chatgpt_existing_account_login_only")
    ):
        if _chatgpt_bind_phone_enabled(req):
            return "登录并接码", "登录并接码"
        return "登录", "登录"
    return "注册", "注册"


def _refresh_saved_chatgpt_login(req: RegisterTaskRequest, saved_account) -> str:
    if (
        req.platform != "chatgpt"
        or not _is_truthy(req.extra.get("chatgpt_existing_account_login_only"))
        or not getattr(saved_account, "id", None)
    ):
        return ""
    try:
        from services.chatgpt_account_refresh import refresh_chatgpt_account_by_id

        refresh_chatgpt_account_by_id(int(saved_account.id))
        return "账号状态刷新完成"
    except Exception as exc:
        return f"账号状态刷新失败: {exc}"


class TaskLogBatchDeleteRequest(BaseModel):
    ids: list[int]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _json_dumps(value, fallback):
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return json.dumps(fallback, ensure_ascii=False)


def _json_loads(raw: str, fallback):
    try:
        return json.loads(raw or "")
    except Exception:
        return fallback


def _bounded_persisted_task_logs(logs: list) -> list[str]:
    """Return a recent log tail that is safe to rewrite into one SQLite row."""
    normalized = [str(item) for item in logs]
    if not normalized:
        return []

    tail = normalized[-MAX_PERSISTED_TASK_LOG_ENTRIES:]

    def _render(items: list[str]) -> list[str]:
        omitted = len(normalized) - len(items)
        if omitted <= 0:
            return list(items)
        return [
            f"[SYSTEM] 较早日志已省略 {omitted} 条，仅保留最近的持久化日志",
            *items,
        ]

    persisted = _render(tail)
    if (
        len(_json_dumps(persisted, []).encode("utf-8"))
        > MAX_PERSISTED_TASK_LOG_BYTES
        and len(tail) > 1
    ):
        low, high = 0, len(tail) - 1
        while low < high:
            middle = (low + high) // 2
            candidate = _render(tail[middle:])
            if (
                len(_json_dumps(candidate, []).encode("utf-8"))
                <= MAX_PERSISTED_TASK_LOG_BYTES
            ):
                high = middle
            else:
                low = middle + 1
        tail = tail[low:]
        persisted = _render(tail)

    if len(_json_dumps(persisted, []).encode("utf-8")) <= MAX_PERSISTED_TASK_LOG_BYTES:
        return persisted

    # A single unusually large log line must not defeat the row-size bound.
    latest = tail[-1]
    suffix = "…[单条日志已截断]"
    low, high = 0, len(latest)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = _render([latest[:middle] + suffix])
        if len(_json_dumps(candidate, []).encode("utf-8")) <= MAX_PERSISTED_TASK_LOG_BYTES:
            low = middle
        else:
            high = middle - 1
    return _render([latest[:low] + suffix])


def _to_epoch_seconds(value) -> float:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).timestamp()
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _to_datetime(value) -> datetime:
    try:
        ts = float(value or 0)
        if ts > 1_000_000_000_000:
            ts /= 1000
        if ts <= 0:
            return _utcnow()
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except Exception:
        return _utcnow()


def _normalize_snapshot(snapshot: dict) -> dict:
    return {
        "id": str(snapshot.get("id") or ""),
        "status": str(snapshot.get("status") or "pending"),
        "platform": str(snapshot.get("platform") or ""),
        "source": str(snapshot.get("source") or "manual"),
        "meta": snapshot.get("meta") if isinstance(snapshot.get("meta"), dict) else {},
        "total": int(snapshot.get("total") or 0),
        "progress": str(snapshot.get("progress") or "0/0"),
        "logs": snapshot.get("logs") if isinstance(snapshot.get("logs"), list) else [],
        "success": int(snapshot.get("success") or 0),
        "registered": int(snapshot.get("registered") or 0),
        "skipped": int(snapshot.get("skipped") or 0),
        "errors": snapshot.get("errors") if isinstance(snapshot.get("errors"), list) else [],
        "control": snapshot.get("control") if isinstance(snapshot.get("control"), dict) else {},
        "cashier_urls": snapshot.get("cashier_urls") if isinstance(snapshot.get("cashier_urls"), list) else [],
        "error": str(snapshot.get("error") or ""),
        "created_at": _to_epoch_seconds(snapshot.get("created_at")),
        "updated_at": _to_epoch_seconds(snapshot.get("updated_at")),
    }


def _task_run_to_snapshot(row: TaskRunModel) -> dict:
    return _normalize_snapshot(
        {
            "id": row.id,
            "status": row.status,
            "platform": row.platform,
            "source": row.source,
            "meta": _json_loads(row.meta_json, {}),
            "total": row.total,
            "progress": row.progress,
            "logs": _json_loads(row.logs_json, []),
            "success": row.success,
            "registered": row.registered,
            "skipped": row.skipped,
            "errors": _json_loads(row.errors_json, []),
            "control": _json_loads(row.control_json, {}),
            "cashier_urls": _json_loads(row.cashier_urls_json, []),
            "error": row.error,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
    )


def _upsert_task_run(snapshot: dict) -> None:
    normalized = _normalize_snapshot(snapshot)
    if not normalized["id"]:
        return
    persisted_logs = _bounded_persisted_task_logs(normalized["logs"])
    with Session(engine) as s:
        row = s.get(TaskRunModel, normalized["id"])
        if row is None:
            row = TaskRunModel(
                id=normalized["id"],
                platform=normalized["platform"],
                source=normalized["source"],
                status=normalized["status"],
                total=normalized["total"],
                progress=normalized["progress"],
                success=normalized["success"],
                registered=normalized["registered"],
                skipped=normalized["skipped"],
                error=normalized["error"],
                meta_json=_json_dumps(normalized["meta"], {}),
                logs_json=_json_dumps(persisted_logs, []),
                errors_json=_json_dumps(normalized["errors"], []),
                cashier_urls_json=_json_dumps(normalized["cashier_urls"], []),
                control_json=_json_dumps(normalized["control"], {}),
                created_at=_to_datetime(normalized["created_at"]),
                updated_at=_to_datetime(normalized["updated_at"]),
            )
            s.add(row)
        else:
            row.platform = normalized["platform"]
            row.source = normalized["source"]
            row.status = normalized["status"]
            row.total = normalized["total"]
            row.progress = normalized["progress"]
            row.success = normalized["success"]
            row.registered = normalized["registered"]
            row.skipped = normalized["skipped"]
            row.error = normalized["error"]
            row.meta_json = _json_dumps(normalized["meta"], {})
            row.logs_json = _json_dumps(persisted_logs, [])
            row.errors_json = _json_dumps(normalized["errors"], [])
            row.cashier_urls_json = _json_dumps(normalized["cashier_urls"], [])
            row.control_json = _json_dumps(normalized["control"], {})
            if row.created_at is None:
                row.created_at = _to_datetime(normalized["created_at"])
            row.updated_at = _to_datetime(normalized["updated_at"])
            s.add(row)
        s.commit()


def _persist_task_snapshot(task_id: str) -> None:
    with _task_snapshot_persist_lock:
        write_lock = _task_snapshot_write_locks.setdefault(
            task_id,
            threading.Lock(),
        )

    # Serialize only writes for the same task. A busy database write for one
    # task must not hold the global throttle-map lock and stall every logger.
    with write_lock:
        snapshot = _task_store.snapshot_if_present(task_id)
        if snapshot is None:
            return
        _upsert_task_run(snapshot)
    if str(snapshot.get("status") or "") in {"done", "failed", "stopped"}:
        with _task_snapshot_persist_lock:
            _task_snapshot_last_persisted_at.pop(task_id, None)


def _persist_task_snapshot_best_effort(task_id: str) -> bool:
    """Keep persistence failures from changing an already-valid task outcome."""
    try:
        _persist_task_snapshot(task_id)
        return True
    except Exception as exc:
        logger.warning(
            "任务快照持久化失败 task_id=%s error_type=%s",
            task_id,
            type(exc).__name__,
        )
        return False


def _persist_task_snapshot_throttled(task_id: str) -> bool:
    """Coalesce high-volume log updates while final/counter writes stay explicit."""
    snapshot = _task_store.snapshot_if_present(task_id)
    if snapshot is None:
        with _task_snapshot_persist_lock:
            _task_snapshot_last_persisted_at.pop(task_id, None)
        return True

    now = time.monotonic()
    with _task_snapshot_persist_lock:
        last_persisted_at = _task_snapshot_last_persisted_at.get(task_id)
        if (
            last_persisted_at is not None
            and now - last_persisted_at < TASK_SNAPSHOT_PERSIST_INTERVAL_SECONDS
        ):
            return True
        # Reserve the cooldown before releasing the map lock. This bounds
        # retries after a locked/busy write and prevents a concurrent storm.
        _task_snapshot_last_persisted_at[task_id] = now

    return _persist_task_snapshot_best_effort(task_id)


def _get_persisted_task(task_id: str) -> Optional[dict]:
    with Session(engine) as s:
        row = s.get(TaskRunModel, task_id)
        if row is None:
            return None
        return _task_run_to_snapshot(row)


def _list_persisted_tasks() -> list[dict]:
    with Session(engine) as s:
        rows = s.exec(select(TaskRunModel)).all()
    snapshots = [_task_run_to_snapshot(row) for row in rows]
    snapshots.sort(
        key=lambda item: (
            {"running": 0, "pending": 1, "done": 2, "failed": 3, "stopped": 4}.get(
                str(item.get("status") or ""),
                9,
            ),
            -_to_epoch_seconds(item.get("created_at")),
        )
    )
    return snapshots


def _finalize_orphan_tasks() -> set[str]:
    finalized_ids: set[str] = set()
    with Session(engine) as s:
        rows = s.exec(
            select(TaskRunModel).where(TaskRunModel.status.in_(["pending", "running"]))
        ).all()
        if not rows:
            return finalized_ids
        changed = False
        for row in rows:
            if _task_store.exists(row.id):
                continue
            row.status = "stopped"
            row.error = row.error or "任务因服务重启中断"
            logs = _json_loads(row.logs_json, [])
            tip = "[SYSTEM] 任务因服务重启中断，已自动标记为已停止"
            if tip not in logs:
                ts = datetime.now().strftime("%H:%M:%S")
                logs.append(f"[{ts}] {tip}")
            row.logs_json = _json_dumps(logs, [])
            row.updated_at = _utcnow()
            s.add(row)
            finalized_ids.add(str(row.id))
            changed = True
        if changed:
            s.commit()
    return finalized_ids


def _ensure_task_exists(task_id: str) -> None:
    if _task_store.exists(task_id):
        return
    if _get_persisted_task(task_id) is None:
        raise HTTPException(404, "任务不存在")


def _ensure_task_mutable(task_id: str) -> None:
    _ensure_task_exists(task_id)
    snapshot = _task_store.snapshot_if_present(task_id)
    if snapshot is None:
        snapshot = _get_persisted_task(task_id) or {}
    if snapshot.get("status") in {"done", "failed", "stopped"}:
        raise HTTPException(409, "任务已结束，无法再执行控制操作")


def _get_task_snapshot(task_id: str) -> dict:
    memory_snapshot = _task_store.snapshot_if_present(task_id)
    if memory_snapshot is not None:
        snapshot = _normalize_snapshot(memory_snapshot)
        _persist_task_snapshot_throttled(task_id)
        return snapshot
    snapshot = _get_persisted_task(task_id)
    if snapshot is None:
        raise HTTPException(404, "任务不存在")
    return snapshot


def _prepare_register_request(req: RegisterTaskRequest) -> RegisterTaskRequest:
    from core.config_store import config_store
    from core.registry import is_platform_enabled

    req_data = req.model_dump()
    req_data["extra"] = deepcopy(req_data.get("extra") or {})
    prepared = RegisterTaskRequest(**req_data)
    prepared.platform = str(prepared.platform or "").strip().lower()

    if not is_platform_enabled(prepared.platform):
        raise HTTPException(400, f"{prepared.platform} 平台已下线，不再支持注册")

    is_chatgpt_login = (
        prepared.platform == "chatgpt"
        and _is_truthy(
            prepared.extra.get("chatgpt_existing_account_login_only")
        )
    )
    provider_plan = _normalize_chatgpt_mail_provider_plan(
        prepared.extra.get(CHATGPT_MAIL_PROVIDER_PLAN_KEY)
    )
    if provider_plan:
        if not is_chatgpt_login or len(provider_plan) != prepared.count:
            raise HTTPException(400, "邮箱来源分配数量与登录数量不一致")
        prepared.extra[CHATGPT_MAIL_PROVIDER_PLAN_KEY] = provider_plan
    else:
        prepared.extra.pop(CHATGPT_MAIL_PROVIDER_PLAN_KEY, None)

    bind_phone_requested = _is_truthy(
        prepared.extra.get(CHATGPT_BIND_PHONE_FLAG)
    )
    use_sms_pool_requested = _is_truthy(
        prepared.extra.get(CHATGPT_USE_SMS_POOL_FLAG)
    )
    if bind_phone_requested:
        if (
            prepared.platform != "chatgpt"
            or not _is_truthy(
                prepared.extra.get("chatgpt_existing_account_login_only")
            )
        ):
            raise HTTPException(400, "登录并接码仅支持 ChatGPT 已有账号登录任务")
        if use_sms_pool_requested:
            codes = []
            prepared.extra.pop(CHATGPT_LEADBEE_CODES_KEY, None)
            prepared.extra.pop(CHATGPT_LEADBEE_BASE_URLS_KEY, None)
            prepared.extra.pop(CHATGPT_SMS_POOL_ITEM_IDS_KEY, None)
        else:
            codes = _normalize_leadbee_codes(
                prepared.extra.get(CHATGPT_LEADBEE_CODES_KEY)
            )
            if len(codes) != prepared.count:
                raise HTTPException(
                    400,
                    (
                        "卡密数量需与登录数量一致"
                        f"（需要 {prepared.count} 个，当前 {len(codes)} 个）"
                    ),
                )
        retry_bindings = _normalize_chatgpt_retry_bindings(
            prepared.extra.get(CHATGPT_RETRY_BINDINGS_KEY)
        )
        if retry_bindings:
            if len(retry_bindings) != prepared.count:
                raise HTTPException(
                    400,
                    "失败账号绑定数量需与重试数量一致",
                )
            bound_codes = [item["leadbee_code"] for item in retry_bindings]
            if not use_sms_pool_requested and bound_codes != codes:
                raise HTTPException(400, "失败账号与 LeadBee 卡密顺序不一致")
            prepared.extra[CHATGPT_RETRY_BINDINGS_KEY] = retry_bindings
        else:
            prepared.extra.pop(CHATGPT_RETRY_BINDINGS_KEY, None)
        prepared.extra[CHATGPT_BIND_PHONE_FLAG] = True
        prepared.extra[CHATGPT_USE_SMS_POOL_FLAG] = use_sms_pool_requested
        if not use_sms_pool_requested:
            prepared.extra[CHATGPT_LEADBEE_CODES_KEY] = codes
        # 先走已经实测稳定的 AT 登录，再续接同一授权事务完成手机验证。
        prepared.extra["chatgpt_existing_account_login_stage"] = "access_token"
        prepared.extra["chatgpt_existing_account_allow_phone_verification"] = False
    else:
        prepared.extra.pop(CHATGPT_LEADBEE_CODES_KEY, None)
        prepared.extra.pop(CHATGPT_LEADBEE_BASE_URLS_KEY, None)
        prepared.extra.pop(CHATGPT_SMS_POOL_ITEM_IDS_KEY, None)
        prepared.extra.pop(CHATGPT_USE_SMS_POOL_FLAG, None)
        prepared.extra.pop(CHATGPT_RETRY_BINDINGS_KEY, None)
        if (
            prepared.platform == "chatgpt"
            and _is_truthy(
                prepared.extra.get("chatgpt_existing_account_login_only")
            )
        ):
            prepared.extra[CHATGPT_BIND_PHONE_FLAG] = False
        else:
            prepared.extra.pop(CHATGPT_BIND_PHONE_FLAG, None)

    mail_provider = prepared.extra.get("mail_provider") or config_store.get(
        "mail_provider", ""
    )
    if mail_provider == "luckmail":
        platform = prepared.platform
        if platform in ("tavily", "openblocklabs"):
            raise HTTPException(400, f"LuckMail 渠道暂时不支持 {platform} 项目注册")

        mapping = {
            "cursor": "cursor",
            "grok": "grok",
            "kiro": "kiro",
            "chatgpt": "openai",
        }
        prepared.extra["luckmail_project_code"] = mapping.get(platform, platform)

    return prepared


def _attach_sms_pool_reservation(
    task_id: str,
    req: RegisterTaskRequest,
) -> None:
    if not _is_truthy(req.extra.get(CHATGPT_USE_SMS_POOL_FLAG)):
        return
    try:
        items = sms_pool_service.reserve(task_id=task_id, count=req.count)
    except SmsPoolExhaustedError as exc:
        raise HTTPException(409, str(exc)) from exc
    req.extra[CHATGPT_LEADBEE_CODES_KEY] = [item.code for item in items]
    req.extra[CHATGPT_LEADBEE_BASE_URLS_KEY] = [item.base_url for item in items]
    req.extra[CHATGPT_SMS_POOL_ITEM_IDS_KEY] = [int(item.id) for item in items]


def _create_task_record(
    task_id: str, req: RegisterTaskRequest, source: str, meta: dict | None = None
):
    task_meta = dict(meta or {})
    if (
        req.platform == "chatgpt"
        and _is_truthy(req.extra.get("chatgpt_existing_account_login_only"))
    ):
        task_meta.setdefault("mode", "login")
    _task_store.create(
        task_id,
        platform=req.platform,
        total=req.count,
        source=source,
        meta=task_meta,
    )
    _persist_task_snapshot(task_id)


def _enqueue_prepared_register_task(
    prepared: RegisterTaskRequest,
    *,
    background_tasks: BackgroundTasks | None = None,
    source: str = "manual",
    meta: dict | None = None,
) -> str:
    task_id = f"task_{uuid.uuid4().hex}"
    try:
        _attach_sms_pool_reservation(task_id, prepared)
        _create_task_record(task_id, prepared, source, meta)
        if background_tasks is None:
            thread = threading.Thread(
                target=_run_register, args=(task_id, prepared), daemon=True
            )
            thread.start()
        else:
            background_tasks.add_task(_run_register, task_id, prepared)
    except Exception as exc:
        if _is_truthy(prepared.extra.get(CHATGPT_USE_SMS_POOL_FLAG)):
            sms_pool_service.release_task(task_id)
        if prepared.platform == "chatgpt":
            _terminalize_failed_chatgpt_register_enqueue(task_id, exc)
        raise
    return task_id


def enqueue_register_task(
    req: RegisterTaskRequest,
    *,
    background_tasks: BackgroundTasks | None = None,
    source: str = "manual",
    meta: dict | None = None,
) -> str:
    if str(req.platform or "").strip().lower() == "chatgpt":
        with _chatgpt_task_enqueue_lock:
            prepared = _prepare_register_request(req)
            return _enqueue_prepared_register_task(
                prepared,
                background_tasks=background_tasks,
                source=source,
                meta=meta,
            )
    prepared = _prepare_register_request(req)
    return _enqueue_prepared_register_task(
        prepared,
        background_tasks=background_tasks,
        source=source,
        meta=meta,
    )


def has_active_register_task(
    *, platform: str | None = None, source: str | None = None
) -> bool:
    return _task_store.has_active(platform=platform, source=source)


def _log(task_id: str, msg: str):
    """向任务追加一条日志"""
    ts = time.strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    _task_store.append_log(task_id, entry)
    _persist_task_snapshot_throttled(task_id)
    print(entry)


def _append_task_log_best_effort(task_id: str, message: str) -> None:
    try:
        _log(task_id, message)
    except Exception:
        try:
            _task_store.append_log(
                task_id,
                f"[{time.strftime('%H:%M:%S')}] {message}",
            )
        except Exception:
            pass


def _terminalize_stopped_task(task_id: str, message: str) -> None:
    """Best-effort terminal state for a task stopped before business work."""
    snapshot = _task_store.snapshot_if_present(task_id)
    if snapshot is None:
        return
    if str(snapshot.get("status") or "") in {"done", "failed", "stopped"}:
        _persist_task_snapshot_best_effort(task_id)
        _task_store.cleanup()
        return

    try:
        _task_store.control_for(task_id).request_stop()
    except Exception:
        pass
    _append_task_log_best_effort(task_id, message)
    snapshot = _task_store.snapshot_if_present(task_id) or snapshot
    _task_store.finish(
        task_id,
        status="stopped",
        success=int(snapshot.get("success") or 0),
        registered=int(snapshot.get("registered") or 0),
        skipped=int(snapshot.get("skipped") or 0),
        errors=list(snapshot.get("errors") or []),
    )
    _persist_task_snapshot_best_effort(task_id)
    _task_store.cleanup()


def _terminalize_failed_chatgpt_register_enqueue(
    task_id: str,
    exc: Exception,
) -> None:
    """Prevent a created ChatGPT register task from remaining pending."""
    snapshot = _task_store.snapshot_if_present(task_id)
    if snapshot is None or str(snapshot.get("status") or "") in {
        "done",
        "failed",
        "stopped",
    }:
        return

    message = _redact_chatgpt_relogin_log(exc) or type(exc).__name__
    try:
        _task_store.control_for(task_id).request_stop()
    except Exception:
        pass
    _append_task_log_best_effort(task_id, f"[FAIL] ChatGPT 任务入队失败: {message}")
    snapshot = _task_store.snapshot_if_present(task_id) or snapshot
    errors = list(snapshot.get("errors") or [])
    errors.append(f"ChatGPT 任务入队失败: {message}")
    try:
        _task_store.finish(
            task_id,
            status="failed",
            success=int(snapshot.get("success") or 0),
            registered=int(snapshot.get("registered") or 0),
            skipped=int(snapshot.get("skipped") or 0),
            errors=errors,
            error=message,
        )
    finally:
        _persist_task_snapshot_best_effort(task_id)
        _task_store.cleanup()


def _terminalize_failed_chatgpt_register_run(
    task_id: str,
    exc: Exception,
) -> None:
    """Fail a non-terminal ChatGPT run without rewriting a finished outcome."""
    snapshot = _task_store.snapshot_if_present(task_id)
    if snapshot is None:
        return
    if str(snapshot.get("status") or "") in {"done", "failed", "stopped"}:
        _persist_task_snapshot_best_effort(task_id)
        _task_store.cleanup()
        return

    message = _redact_chatgpt_relogin_log(exc) or type(exc).__name__
    try:
        _task_store.control_for(task_id).request_stop()
    except Exception:
        pass
    _append_task_log_best_effort(
        task_id,
        f"[FAIL] ChatGPT 任务异常终止: {message}",
    )
    snapshot = _task_store.snapshot_if_present(task_id) or snapshot
    errors = list(snapshot.get("errors") or [])
    errors.append(f"ChatGPT 任务异常终止: {message}")
    _task_store.finish(
        task_id,
        status="failed",
        success=int(snapshot.get("success") or 0),
        registered=int(snapshot.get("registered") or 0),
        skipped=int(snapshot.get("skipped") or 0),
        errors=errors,
        error=message,
    )
    _persist_task_snapshot_best_effort(task_id)
    _task_store.cleanup()


def _save_task_log(
    platform: str, email: str, status: str, error: str = "", detail: dict = None
):
    """Write a TaskLog record to the database (fire-and-forget, non-blocking)."""
    def _write():
        with Session(engine) as s:
            log = TaskLog(
                platform=platform,
                email=email,
                status=status,
                error=error,
                detail_json=json.dumps(detail or {}, ensure_ascii=False),
            )
            s.add(log)
            s.commit()
    threading.Thread(target=_write, daemon=True).start()


def _create_chatgpt_relogin_task_record(
    task_id: str,
    account_ids: list[int],
    concurrency: int = 1,
    *,
    source: str = "manual_relogin",
    automation: bool = False,
) -> None:
    normalized_ids = _normalize_chatgpt_relogin_account_ids(
        account_ids,
        max_accounts=None if automation else 100,
    )
    effective_concurrency = _normalize_chatgpt_relogin_concurrency(
        concurrency,
        account_count=len(normalized_ids),
    )
    task_meta = {
        "mode": "remote_auth_monitor" if automation else "relogin",
        "automation": automation,
        "account_ids": normalized_ids,
        "concurrency": effective_concurrency,
    }
    if automation:
        task_meta.update(
            {
                "invalid_rt_count": 0,
                "relogin_failed_count": 0,
                "alert_sent": False,
                "alert_reason": "pending",
            }
        )
    _task_store.create(
        task_id,
        platform="chatgpt",
        total=len(normalized_ids),
        source=source,
        meta=task_meta,
    )
    _persist_task_snapshot(task_id)


def _terminalize_failed_chatgpt_relogin_enqueue(
    task_id: str,
    exc: Exception,
) -> None:
    """Best-effort cleanup for failures after a relogin record was created."""
    try:
        snapshot = _task_store.snapshot_if_present(task_id)
    except Exception:
        return
    if snapshot is None or snapshot["status"] in {"done", "failed", "stopped"}:
        return

    message = _redact_chatgpt_relogin_log(exc) or type(exc).__name__
    try:
        _task_store.control_for(task_id).request_stop()
    except Exception:
        pass
    try:
        errors = list(snapshot.get("errors") or [])
        errors.append(f"重登任务入队失败: {message}")
        _task_store.finish(
            task_id,
            status="failed",
            success=int(snapshot.get("success") or 0),
            registered=int(snapshot.get("registered") or 0),
            skipped=int(snapshot.get("skipped") or 0),
            errors=errors,
            error=message,
        )
    except Exception:
        return
    try:
        _persist_task_snapshot_best_effort(task_id)
    except Exception:
        pass
    try:
        _task_store.cleanup()
    except Exception:
        pass


def _enqueue_chatgpt_relogin_task_locked(
    account_ids,
    concurrency,
    source: str = "manual_relogin",
    automation: bool = False,
    background_tasks: BackgroundTasks | None = None,
) -> str:
    task_id = f"task_relogin_{uuid.uuid4().hex}"
    try:
        _create_chatgpt_relogin_task_record(
            task_id,
            account_ids,
            concurrency=concurrency,
            source=source,
            automation=automation,
        )
        task_meta = _task_store.snapshot(task_id)["meta"]
        normalized_ids = list(task_meta["account_ids"])
        effective_concurrency = task_meta["concurrency"]
        runner_args = (task_id, normalized_ids, effective_concurrency)
        if background_tasks is None:
            threading.Thread(
                target=_run_chatgpt_relogin_task,
                args=runner_args,
                daemon=True,
            ).start()
        else:
            background_tasks.add_task(_run_chatgpt_relogin_task, *runner_args)
    except Exception as exc:
        _terminalize_failed_chatgpt_relogin_enqueue(task_id, exc)
        raise
    return task_id


def enqueue_chatgpt_relogin_task(
    account_ids,
    concurrency,
    source: str = "manual_relogin",
    automation: bool = False,
    background_tasks: BackgroundTasks | None = None,
) -> str:
    with _chatgpt_task_enqueue_lock:
        return _enqueue_chatgpt_relogin_task_locked(
            account_ids,
            concurrency,
            source=source,
            automation=automation,
            background_tasks=background_tasks,
        )


def try_enqueue_scheduled_chatgpt_relogin(
    account_ids,
    concurrency,
) -> dict[str, object]:
    """Atomically create one scheduled task only while ChatGPT work is idle."""

    with _chatgpt_task_enqueue_lock:
        _finalize_orphan_tasks()
        gate = dict(chatgpt_task_gate.snapshot())
        if int(gate.get("foreground_active") or 0) or int(
            gate.get("foreground_waiters") or 0
        ):
            return {
                "accepted": False,
                "task_id": None,
                "reason": "foreground_busy",
            }
        if _task_store.has_active(platform="chatgpt") or bool(
            gate.get("automation_active")
        ):
            return {
                "accepted": False,
                "task_id": None,
                "reason": "task_busy",
            }

        task_id = enqueue_chatgpt_relogin_task(
            account_ids,
            concurrency,
            source="schedule",
            automation=True,
            background_tasks=None,
        )
        return {
            "accepted": True,
            "task_id": task_id,
            "reason": "enqueued",
        }


def _as_aware_utc(value) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            parsed = _utcnow()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _chatgpt_task_observation(
    snapshot: dict,
    *,
    memory: bool,
    orphaned: bool = False,
) -> dict[str, object] | None:
    if str(snapshot.get("platform") or "") != "chatgpt":
        return None
    status = str(snapshot.get("status") or "")
    updated_at = _as_aware_utc(snapshot.get("updated_at"))
    meta = snapshot.get("meta") if isinstance(snapshot.get("meta"), dict) else {}
    raw_completed_at = meta.get("completed_at")
    has_completed_at = raw_completed_at is not None and bool(
        str(raw_completed_at).strip()
    )
    completed_at = (
        _as_aware_utc(raw_completed_at)
        if status in {"done", "failed", "stopped"}
        and has_completed_at
        else updated_at
    )
    return {
        "status": status,
        "completed_at": completed_at,
        "updated_at": updated_at,
        "live": memory and status in {"pending", "running"},
        "orphaned": bool(orphaned),
    }


def observe_chatgpt_task(task_id: str) -> dict[str, object] | None:
    """Observe a ChatGPT task without exposing the mutable runtime store."""

    memory_snapshot = _task_store.snapshot_if_present(task_id)
    if memory_snapshot is not None:
        return _chatgpt_task_observation(memory_snapshot, memory=True)

    finalized_ids = _finalize_orphan_tasks() or set()

    # A task can enter memory while orphan reconciliation is reading the DB.
    memory_snapshot = _task_store.snapshot_if_present(task_id)
    if memory_snapshot is not None:
        return _chatgpt_task_observation(memory_snapshot, memory=True)

    persisted = _get_persisted_task(task_id)
    if persisted is None:
        return None
    status = str(persisted.get("status") or "")
    orphaned = str(task_id) in finalized_ids or (
        status == "stopped"
        and "服务重启中断" in str(persisted.get("error") or "")
    )
    return _chatgpt_task_observation(
        persisted,
        memory=False,
        orphaned=orphaned,
    )


def _run_chatgpt_relogin_task_inner(
    task_id: str,
    account_ids: list[int],
    concurrency: int = 1,
) -> None:
    """Relogin selected accounts concurrently and aggregate every sync result."""
    from concurrent.futures import (
        FIRST_COMPLETED,
        CancelledError,
        ThreadPoolExecutor,
        wait,
    )
    from services.chatgpt_relogin import relogin_chatgpt_account

    control = _task_store.control_for(task_id)
    task_snapshot = _task_store.snapshot(task_id)
    automation = _is_truthy((task_snapshot.get("meta") or {}).get("automation"))
    if control.is_stop_requested():
        _terminalize_stopped_task(
            task_id,
            "任务尚未开始账号处理，停止请求已生效",
        )
        return
    remote_health: dict[int, dict[str, object]] = {}
    if automation:
        from services.chatgpt_codex2api_health import (
            confirm_codex2api_auth_failure,
            inspect_codex2api_account_health,
        )

        remote_health = inspect_codex2api_account_health(account_ids)
        if control.is_stop_requested():
            _terminalize_stopped_task(
                task_id,
                "Codex2API 鉴权探针结束后检测到停止请求，"
                "未派发账号处理",
            )
            return
    task_label = "Codex2API 鉴权巡检" if automation else "重登"
    total = len(account_ids)
    max_workers = min(
        max(int(concurrency or 1), 1),
        CHATGPT_RELOGIN_MAX_CONCURRENCY,
        total,
    )
    success = 0
    processed = 0
    skipped = 0
    errors: list[str] = []
    stopped = False
    cycle_counts_lock = threading.Lock()
    invalid_rt_count = 0
    relogin_failed_count = 0

    def _record_confirmed_auth_failure() -> None:
        nonlocal invalid_rt_count
        with cycle_counts_lock:
            invalid_rt_count += 1
            _task_store.update_meta(
                task_id,
                invalid_rt_count=invalid_rt_count,
                relogin_failed_count=relogin_failed_count,
            )

    def _record_automatic_result(result: dict) -> None:
        nonlocal relogin_failed_count
        if not automation:
            return
        full_relogin_failed = (
            str(result.get("mode") or "").strip().lower() == "full_login"
            and not bool(result.get("relogin_ok"))
            and not bool(result.get("account_removed"))
            and str(result.get("stage") or "").strip() != "account_removed"
        )
        with cycle_counts_lock:
            if full_relogin_failed:
                relogin_failed_count += 1
            _task_store.update_meta(
                task_id,
                invalid_rt_count=invalid_rt_count,
                relogin_failed_count=relogin_failed_count,
            )

    _task_store.mark_running(task_id)
    if automation:
        _task_store.update_meta(
            task_id,
            mode="remote_auth_monitor",
            invalid_rt_count=invalid_rt_count,
            relogin_failed_count=0,
        )
    _persist_task_snapshot_best_effort(task_id)
    _log(
        task_id,
        f"开始{task_label}，共 {total} 个账号，并发 {max_workers}",
    )

    def _do_one(index: int, account_id: int) -> AttemptResult:
        attempt_id: int | None = None
        try:
            control.checkpoint()
            attempt_id = control.start_attempt()
            control.checkpoint(attempt_id=attempt_id)
            _log(task_id, f"开始{task_label}第 {index}/{total} 个账号（ID: {account_id}）")
            control.checkpoint(attempt_id=attempt_id)

            def _service_log(message) -> None:
                text = _redact_chatgpt_relogin_log(message)
                if text:
                    _log(task_id, f"  [账号 {account_id}] {text}")

            if automation:
                health = dict(remote_health.get(account_id) or {})
                health_state = str(health.get("state") or "").strip().lower()
                if health_state == "auth_failed":
                    health = confirm_codex2api_auth_failure(health)
                    health_state = str(
                        health.get("state") or ""
                    ).strip().lower()
                email = str(health.get("email") or "").strip()
                if health_state == "healthy":
                    healthy_message = str(
                        health.get("message")
                        or "Codex2API 远端鉴权正常"
                    )
                    if "无需" not in healthy_message:
                        healthy_message += "，无需重登"
                    result = {
                        "ok": True,
                        "relogin_ok": False,
                        "stage": "remote_healthy",
                        "mode": "remote_probe",
                        "account_id": account_id,
                        "email": email,
                        "remote_status": health.get("remote_status"),
                        "message": healthy_message,
                    }
                elif health_state == "auth_failed":
                    _record_confirmed_auth_failure()
                    result = relogin_chatgpt_account(
                        account_id,
                        log_fn=_service_log,
                        task_control=control,
                        attempt_id=attempt_id,
                    )
                    if isinstance(result, dict):
                        result = {
                            **result,
                            "mode": "full_login",
                            "remote_auth_state": "auth_failed",
                            "remote_status": health.get("remote_status"),
                        }
                else:
                    result = {
                        "ok": False,
                        "relogin_ok": False,
                        "stage": "remote_probe_deferred",
                        "mode": "remote_probe",
                        "account_id": account_id,
                        "email": email,
                        "remote_status": health.get("remote_status"),
                        "message": str(
                            health.get("message")
                            or "Codex2API 状态暂不可判定，等待下一轮复查"
                        ),
                    }
            else:
                result = relogin_chatgpt_account(
                    account_id,
                    log_fn=_service_log,
                    task_control=control,
                    attempt_id=attempt_id,
                )
            if not isinstance(result, dict):
                raise RuntimeError("重登服务返回了无效结果")

            email = str(result.get("email") or "").strip()
            account_label = email or f"账号 ID {account_id}"
            message = _redact_chatgpt_relogin_log(result.get("message"))
            stage = str(result.get("stage") or "").strip()
            result_mode = str(result.get("mode") or "").strip() or (
                "remote_probe" if automation else "relogin"
            )
            relogin_ok = bool(result.get("relogin_ok"))
            account_removed = bool(result.get("account_removed")) or stage == "account_removed"
            _record_automatic_result(result)

            if bool(result.get("ok")):
                detail_message = message or "重登并同步成功"
                success_label = (
                    "远端认证正常"
                    if result_mode == "remote_probe"
                    else "完整登录并同步成功"
                    if automation and result_mode == "full_login"
                    else "重登并同步成功"
                )
                _log(task_id, f"[OK] {success_label}: {account_label}（{detail_message}）")
                if not (
                    automation
                    and result_mode == "remote_probe"
                    and stage == "remote_healthy"
                ):
                    _save_task_log(
                        "chatgpt",
                        email,
                        "success",
                        detail={
                            "mode": result_mode,
                            "account_id": account_id,
                            "stage": stage or "completed",
                        },
                    )
                return AttemptResult.success()
            else:
                detail_message = message or "未返回失败详情"
                error_entry = f"{account_label}: {detail_message}"
                if account_removed:
                    _log(
                        task_id,
                        f"[REMOVE] 账号已被删除或停用，本地记录已移除: "
                        f"{account_label}（{detail_message}）",
                    )
                    failure_label = ""
                elif relogin_ok or stage == "codex2api_sync":
                    failure_label = (
                        "认证更新成功，但 Codex2API 覆盖更新失败"
                        if automation
                        else "重登成功，但 Codex2API 覆盖更新失败"
                    )
                elif stage == "remote_probe_deferred":
                    failure_label = "Codex2API 状态暂不可判定，等待下轮复查"
                else:
                    failure_label = f"{task_label}失败"
                if failure_label:
                    _log(
                        task_id,
                        f"[FAIL] {failure_label}: {account_label}（{detail_message}）",
                    )
                _save_task_log(
                    "chatgpt",
                    email,
                    "failed",
                    error=detail_message,
                    detail={
                        "mode": result_mode,
                        "account_id": account_id,
                        "stage": stage or "relogin",
                        "relogin_ok": relogin_ok,
                        "account_removed": account_removed,
                    },
                )
                return AttemptResult.failed(error_entry)
        except SkipCurrentAttemptRequested as exc:
            _log(task_id, f"[SKIP] 已跳过账号 ID {account_id}: {exc}")
            _save_task_log("chatgpt", "", "skipped", error=str(exc))
            return AttemptResult.skipped(str(exc))
        except StopTaskRequested as exc:
            _log(task_id, f"[STOP] {exc}")
            return AttemptResult.stopped(str(exc))
        except Exception as exc:
            message = _redact_chatgpt_relogin_log(exc) or type(exc).__name__
            _log(task_id, f"[FAIL] 重登失败: 账号 ID {account_id}（{message}）")
            _save_task_log("chatgpt", "", "failed", error=message)
            return AttemptResult.failed(f"账号 ID {account_id}: {message}")
        finally:
            control.finish_attempt(attempt_id)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        jobs = iter(enumerate(account_ids, start=1))
        pending = {}

        def _submit_next() -> bool:
            try:
                index, account_id = next(jobs)
            except StopIteration:
                return False
            future = pool.submit(_do_one, index, account_id)
            pending[future] = (index, account_id)
            return True

        try:
            for _ in range(max_workers):
                if not _submit_next():
                    break

            while pending:
                completed, _ = wait(
                    tuple(pending),
                    timeout=0.1,
                    return_when=FIRST_COMPLETED,
                )
                for future in completed:
                    pending.pop(future, None)
                    try:
                        outcome = future.result()
                    except CancelledError:
                        continue
                    except Exception as exc:
                        message = (
                            _redact_chatgpt_relogin_log(exc)
                            or type(exc).__name__
                        )
                        outcome = AttemptResult.failed(message)

                    if outcome.outcome == AttemptOutcome.SUCCESS:
                        success += 1
                        processed += 1
                    elif outcome.outcome == AttemptOutcome.SKIPPED:
                        skipped += 1
                        processed += 1
                    elif outcome.outcome == AttemptOutcome.STOPPED:
                        stopped = True
                    else:
                        errors.append(outcome.message)
                        processed += 1

                    _task_store.set_progress(task_id, f"{processed}/{total}")
                    _task_store.update_counters(
                        task_id,
                        success=success,
                        registered=processed,
                    )
                    _persist_task_snapshot_best_effort(task_id)

                if stopped or control.is_stop_requested():
                    first_stop_observation = not stopped
                    stopped = True
                    for future in tuple(pending):
                        if future.cancel():
                            pending.pop(future, None)
                    if first_stop_observation:
                        _log(
                            task_id,
                            "停止请求已生效，已取消未开始账号，"
                            "正在等待当前步骤安全退出",
                        )
                else:
                    while len(pending) < max_workers and _submit_next():
                        pass
        except Exception:
            control.request_stop()
            for future in pending:
                future.cancel()
            raise

    final_status = "stopped" if stopped or control.is_stop_requested() else "done"
    # Freeze the business outcome before starting optional SMTP work. This is
    # the atomic boundary: every stop accepted before it resolves to stopped
    # and suppresses alerts; after it the task is terminal and cleanup-only
    # stop requests must not rewrite its outcome.
    final_status = _task_store.finish(
        task_id,
        status=final_status,
        success=success,
        registered=processed,
        skipped=skipped,
        errors=errors,
    )
    # Persist the terminal business outcome before optional SMTP/log work.
    # If cleanup later needs to recycle the process, restart recovery must see
    # the already-decided result rather than the previous running snapshot.
    _persist_task_snapshot_best_effort(task_id)
    if final_status == "stopped":
        summary = (
            f"{task_label}任务已停止: 完整成功 {success} 个，"
            f"已处理 {processed} 个，失败 {len(errors)} 个"
        )
    else:
        summary = (
            f"{task_label}任务完成: 完整成功 {success} 个，"
            f"已处理 {processed} 个，失败 {len(errors)} 个"
        )
    if automation:
        summary += (
            f"，Codex2API 鉴权失效 {invalid_rt_count} 个，"
            f"完整重登失败 {relogin_failed_count} 个"
        )
    _log(task_id, summary)

    if automation and final_status == "done":
        try:
            from services.chatgpt_auto_relogin_alerts import (
                send_auto_relogin_alert,
            )

            alert_result = send_auto_relogin_alert(
                task_id=task_id,
                total_accounts=total,
                invalid_rt_count=invalid_rt_count,
                relogin_failed_count=relogin_failed_count,
            )
            if not isinstance(alert_result, dict):
                raise RuntimeError("invalid alert result")
        except Exception as exc:
            alert_result = {
                "sent": False,
                "reason": "send_failed",
                "error_type": type(exc).__name__,
            }

        alert_meta = {
            "alert_sent": bool(alert_result.get("sent")),
            "alert_reason": str(alert_result.get("reason") or "unknown"),
        }
        if alert_result.get("threshold") is not None:
            alert_meta["alert_threshold"] = int(alert_result["threshold"])
        if alert_result.get("error_type"):
            alert_meta["alert_error_type"] = str(alert_result["error_type"])
        _task_store.update_meta(task_id, **alert_meta)

        alert_reason = alert_meta["alert_reason"]
        if alert_meta["alert_sent"]:
            _log(task_id, "[ALERT] 本轮阈值告警邮件已发送")
        elif alert_reason == "below_threshold":
            _log(task_id, "邮件告警未触发：本轮统计低于配置阈值")
        elif alert_reason == "smtp_not_configured":
            _log(task_id, "[ALERT] 已达到告警阈值，但 SMTP 配置不完整")
        else:
            error_type = str(alert_meta.get("alert_error_type") or "UnknownError")
            _log(task_id, f"[ALERT] 告警邮件发送失败（{error_type}）")
    elif automation:
        _task_store.update_meta(
            task_id,
            alert_sent=False,
            alert_reason="task_stopped",
        )

    _persist_task_snapshot_best_effort(task_id)
    _task_store.cleanup()


def _run_chatgpt_relogin_task_body(
    task_id: str,
    account_ids: list[int],
    concurrency: int = 1,
) -> None:
    """Run a relogin task and never leave its in-memory state active on failure."""
    try:
        _run_chatgpt_relogin_task_inner(
            task_id,
            account_ids,
            concurrency=concurrency,
        )
    except Exception as exc:
        if not _task_store.exists(task_id):
            raise
        message = _redact_chatgpt_relogin_log(exc) or type(exc).__name__
        snapshot = _task_store.snapshot(task_id)
        if str(snapshot.get("status") or "") in {"done", "failed", "stopped"}:
            _persist_task_snapshot_best_effort(task_id)
            _task_store.cleanup()
            return
        try:
            _task_store.control_for(task_id).request_stop()
        except Exception:
            pass
        try:
            errors = list(snapshot.get("errors") or [])
            failure = f"重登任务异常终止: {message}"
            errors.append(failure)
            _task_store.append_log(
                task_id,
                f"[{time.strftime('%H:%M:%S')}] [FAIL] {failure}",
            )
            _task_store.finish(
                task_id,
                status="failed",
                success=int(snapshot.get("success") or 0),
                registered=int(snapshot.get("registered") or 0),
                skipped=int(snapshot.get("skipped") or 0),
                errors=errors,
                error=message,
            )
        finally:
            _persist_task_snapshot_best_effort(task_id)
            _task_store.cleanup()


def _run_chatgpt_relogin_task_coordinated(
    task_id: str,
    account_ids: list[int],
    concurrency: int = 1,
) -> None:
    """Coordinate relogin priority before running its failure-safe body."""
    snapshot = _task_store.snapshot(task_id)
    control = _task_store.control_for(task_id)
    automation = _is_truthy((snapshot.get("meta") or {}).get("automation"))

    if automation:
        _mark_automation_runner_started(task_id)

        def _request_automation_stop() -> None:
            state, first_request, _ = _task_store.request_stop_if_active(task_id)
            _arm_automation_stop_watchdog(task_id)
            if state == "active" and first_request:
                _log(
                    task_id,
                    "手工任务优先：正在安全停止自动重登，不再派发新账号",
                )
            if state == "active":
                _persist_task_snapshot_best_effort(task_id)

        lease = None
        try:
            lease = chatgpt_task_gate.try_enter_automation(
                stop_callback=_request_automation_stop,
            )
            if lease is None:
                _terminalize_stopped_task(
                    task_id,
                    "自动重登未启动：已有手工 ChatGPT 任务等待/运行，"
                    "或另一自动重登正在运行",
                )
                return
            _run_chatgpt_relogin_task_body(
                task_id,
                account_ids,
                concurrency=concurrency,
            )
        finally:
            if lease is not None:
                chatgpt_task_gate.leave_automation(lease)
            # Keep this after leave_automation. A stopped task is not fully
            # quiescent while it can still hold the automation gate.
            _mark_automation_runner_finished(task_id)
        return

    lease = chatgpt_task_gate.enter_foreground(
        on_wait=lambda: _log(
            task_id,
            "等待自动重登释放；手工任务优先，自动重登将安全停止",
        ),
        cancelled=control.is_stop_requested,
    )
    if lease is None:
        _terminalize_stopped_task(
            task_id,
            "手工重登任务已停止：等待自动重登释放期间收到停止请求",
        )
        return
    try:
        _run_chatgpt_relogin_task_body(
            task_id,
            account_ids,
            concurrency=concurrency,
        )
    finally:
        chatgpt_task_gate.leave_foreground(lease)


def _run_chatgpt_relogin_task(
    task_id: str,
    account_ids: list[int],
    concurrency: int = 1,
) -> None:
    """Keep the live record until all runner and gate cleanup has completed."""
    _task_store.protect_from_cleanup(task_id)
    try:
        _run_chatgpt_relogin_task_coordinated(
            task_id,
            account_ids,
            concurrency=concurrency,
        )
    finally:
        _task_store.release_cleanup_protection(task_id)


def _ensure_chatgpt_attempt_binding_table() -> None:
    """Create the retry table lazily for hot upgrades and isolated test engines."""
    with _chatgpt_binding_db_lock:
        if _chatgpt_binding_table_ready.get(engine):
            return
        ChatGPTAttemptBindingModel.__table__.create(bind=engine, checkfirst=True)
        _chatgpt_binding_table_ready[engine] = True


def _upsert_chatgpt_attempt_binding(
    *,
    task_id: str,
    attempt_index: int,
    leadbee_code: str,
    email: str = "",
    account_id: int = 0,
    stage: str = "login",
    status: str = "pending",
    error: str = "",
    mailbox_context: dict | None = None,
    parent_binding_id: int = 0,
) -> ChatGPTAttemptBindingModel:
    """Persist retry state synchronously so a process restart cannot lose pairing."""
    with _chatgpt_binding_db_lock:
        _ensure_chatgpt_attempt_binding_table()
        with Session(engine) as s:
            row = s.exec(
                select(ChatGPTAttemptBindingModel)
                .where(ChatGPTAttemptBindingModel.task_id == str(task_id))
                .where(ChatGPTAttemptBindingModel.attempt_index == int(attempt_index))
            ).first()
            if row is None:
                row = ChatGPTAttemptBindingModel(
                    task_id=str(task_id),
                    attempt_index=int(attempt_index),
                    leadbee_code=str(leadbee_code or "").strip(),
                    parent_binding_id=max(0, int(parent_binding_id or 0)),
                )
            if str(leadbee_code or "").strip():
                row.leadbee_code = str(leadbee_code or "").strip()
            if str(email or "").strip():
                row.email = str(email or "").strip()
            if int(account_id or 0) > 0:
                row.account_id = int(account_id)
            if str(stage or "").strip():
                row.stage = str(stage or "").strip()
            if str(status or "").strip():
                row.status = str(status or "").strip()
            row.error = str(error or "")[:2000]
            if isinstance(mailbox_context, dict) and mailbox_context:
                row.mailbox_context_json = _json_dumps(mailbox_context, {})
            row.updated_at = _utcnow()
            s.add(row)
            s.commit()
            s.refresh(row)

            if row.parent_binding_id > 0 and row.status in {"failed", "success"}:
                parent = s.get(ChatGPTAttemptBindingModel, row.parent_binding_id)
                if parent is not None:
                    parent.retry_count = int(parent.retry_count or 0) + 1
                    parent.status = (
                        "resolved" if row.status == "success" else "failed"
                    )
                    parent.error = "" if row.status == "success" else row.error
                    parent.updated_at = _utcnow()
                    s.add(parent)
                    s.commit()
            return row


def _retryable_chatgpt_bindings(task_id: str) -> list[ChatGPTAttemptBindingModel]:
    with _chatgpt_binding_db_lock:
        _ensure_chatgpt_attempt_binding_table()
        with Session(engine) as s:
            rows = s.exec(
                select(ChatGPTAttemptBindingModel)
                .where(ChatGPTAttemptBindingModel.task_id == str(task_id))
                .where(ChatGPTAttemptBindingModel.status == "failed")
                .order_by(ChatGPTAttemptBindingModel.attempt_index)
            ).all()
            retryable = []
            changed = False
            for row in rows:
                if not str(row.email or "").strip() or not str(
                    row.leadbee_code or ""
                ).strip():
                    continue
                account = None
                if int(row.account_id or 0) > 0:
                    account = s.get(AccountModel, int(row.account_id))
                if account is None and row.email:
                    account = s.exec(
                        select(AccountModel)
                        .where(AccountModel.platform == "chatgpt")
                        .where(AccountModel.email == row.email)
                    ).first()
                extra = account.get_extra() if account is not None else {}
                if str(extra.get("refresh_token") or "").strip():
                    row.status = "resolved"
                    row.error = ""
                    row.updated_at = _utcnow()
                    s.add(row)
                    changed = True
                    continue
                retryable.append(row)
            if changed:
                s.commit()
            for row in retryable:
                if changed:
                    s.refresh(row)
                s.expunge(row)
            return retryable


def _chatgpt_binding_public(row: ChatGPTAttemptBindingModel) -> dict:
    code = str(row.leadbee_code or "").strip()
    code_hint = mask_sms_code(code) if code else ""
    return {
        "id": row.id,
        "task_id": row.task_id,
        "attempt_index": row.attempt_index,
        "email": row.email,
        "account_id": row.account_id,
        "stage": row.stage,
        "status": row.status,
        "error": _redact_task_secret(row.error, code),
        "leadbee_code_hint": code_hint,
        "retry_count": row.retry_count,
    }


def _auto_upload_integrations(task_id: str, account):
    """注册成功后自动导入外部系统（后台线程，不阻塞注册流程）。"""
    def _run():
        try:
            from services.external_sync import sync_account

            for result in sync_account(account):
                name = result.get("name", "Auto Upload")
                ok = bool(result.get("ok"))
                msg = result.get("msg", "")
                _log(task_id, f"  [{name}] {'[OK] ' + msg if ok else '[FAIL] ' + msg}")
        except Exception as e:
            _log(task_id, f"  [Auto Upload] 自动导入异常: {e}")
    threading.Thread(target=_run, daemon=True).start()


def _complete_chatgpt_leadbee_verification(
    *,
    task_id: str,
    account_id: int,
    leadbee_code: str,
    leadbee_base_url: str = "",
    on_provider_start=None,
    on_exchange_code_consumed=None,
    on_exchange_code_restored=None,
    control,
    attempt_id: int | None,
) -> dict:
    """Enter one of LeadBee's five provider slots and wait for completion."""
    from services.chatgpt_phone_verification import (
        LEADBEE_PROVIDER_SLOT_WAIT_SECONDS,
        leadbee_phone_flow_lock,
        phone_verification_manager,
    )

    slot_deadline = (
        time.monotonic() + LEADBEE_PROVIDER_SLOT_WAIT_SECONDS
    )
    acquired_provider_slot = leadbee_phone_flow_lock.acquire(blocking=False)
    while not acquired_provider_slot:
        control.checkpoint(attempt_id=attempt_id)
        remaining = slot_deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(
                "LeadBee 服务并发槽位排队超时，兑换码尚未激活"
            )
        acquired_provider_slot = leadbee_phone_flow_lock.acquire(
            timeout=min(0.25, remaining)
        )
    provider_slot_handed_off = False
    provider_slot_released = False
    provider_session_id = ""
    result: dict = {}

    def _tracked_provider_start() -> None:
        if callable(on_provider_start):
            on_provider_start()

    def _mark_provider_slot_handed_off(session_id: str = "") -> None:
        nonlocal provider_slot_handed_off, provider_session_id
        provider_slot_handed_off = True
        provider_session_id = str(session_id or "").strip()

    def _release_reused_provider_slot() -> None:
        nonlocal provider_slot_released
        if acquired_provider_slot and not provider_slot_released:
            leadbee_phone_flow_lock.release()
            provider_slot_released = True

    try:
        # A task may be stopped while this attempt waits behind the active card.
        # Check once more before handing a fresh exchange code to LeadBee.
        control.checkpoint(attempt_id=attempt_id)
        result = _complete_chatgpt_leadbee_verification_serialized(
            task_id=task_id,
            account_id=account_id,
            leadbee_code=leadbee_code,
            leadbee_base_url=leadbee_base_url,
            on_provider_start=(
                _tracked_provider_start if callable(on_provider_start) else None
            ),
            on_exchange_code_consumed=on_exchange_code_consumed,
            on_exchange_code_restored=on_exchange_code_restored,
            on_provider_lock_handoff=_mark_provider_slot_handed_off,
            on_provider_lock_reuse=_release_reused_provider_slot,
            control=control,
            attempt_id=attempt_id,
        )
        return result
    except (StopTaskRequested, SkipCurrentAttemptRequested):
        raise
    except Exception as exc:
        if not provider_slot_handed_off:
            raise
        _append_task_log_best_effort(
            task_id,
            "  [接码] LeadBee 状态读取异常，改为直接等待后台 worker 清理终态"
            f"（{type(exc).__name__}）",
        )
        # Ownership was transferred to the manager worker. Do not let the
        # attempt/final task cleanup release its pool card until that worker
        # has released the permit and published a definitive settlement.
        fallback_finalization_deadline: float | None = None
        while True:
            cleanup = phone_verification_manager.wait_for_provider_cleanup(
                int(account_id),
                provider_session_id,
                timeout=0.25,
            )
            cleanup_status = str(
                cleanup.get("status") or ""
            ).strip().lower()
            if cleanup_status == "persisting":
                now = time.monotonic()
                if fallback_finalization_deadline is None:
                    fallback_finalization_deadline = (
                        now + CHATGPT_PHONE_FINALIZATION_WAIT_SECONDS
                    )
                if now >= fallback_finalization_deadline:
                    return {
                        **dict(cleanup),
                        "status": "failed",
                        "message": (
                            "Refresh Token 本地保存超过等待期限，尚未确认成功；"
                            "任务已停止等待，账号状态将在后续巡检中重新确认"
                        ),
                        "finalization_pending": True,
                    }
            if (
                bool(cleanup.get("provider_cleanup_settled", False))
                and cleanup_status in {"completed", "failed", "expired"}
            ):
                return dict(cleanup)
    finally:
        if (
            acquired_provider_slot
            and not provider_slot_handed_off
            and not provider_slot_released
        ):
            leadbee_phone_flow_lock.release()


def _complete_chatgpt_leadbee_verification_serialized(
    *,
    task_id: str,
    account_id: int,
    leadbee_code: str,
    leadbee_base_url: str = "",
    on_provider_start=None,
    on_exchange_code_consumed=None,
    on_exchange_code_restored=None,
    on_provider_lock_handoff=None,
    on_provider_lock_reuse=None,
    control,
    attempt_id: int | None,
) -> dict:
    """启动自动接码并等待终态，同时把安全日志并入当前登录任务。"""
    from services.chatgpt_phone_verification import phone_verification_manager

    start_kwargs = {
        "leadbee_code": str(leadbee_code or "").strip(),
        "leadbee_base_url": str(leadbee_base_url or "").strip(),
        "provider_lock_already_held": True,
    }
    if callable(on_exchange_code_consumed):
        start_kwargs["on_exchange_code_consumed"] = on_exchange_code_consumed
    if callable(on_provider_start):
        start_kwargs["on_provider_start"] = on_provider_start
    if callable(on_exchange_code_restored):
        start_kwargs["on_exchange_code_restored"] = on_exchange_code_restored
    if callable(on_provider_lock_handoff):
        start_kwargs["on_provider_lock_handoff"] = on_provider_lock_handoff
    snapshot = phone_verification_manager.start(int(account_id), **start_kwargs)
    session_id = str(snapshot.get("session_id") or "").strip()
    if not session_id:
        raise RuntimeError("LeadBee 自动接码会话启动失败")
    if bool(snapshot.get("reused", False)):
        if callable(on_provider_lock_reuse):
            on_provider_lock_reuse()
        lifecycle_callbacks_present = any(
            callable(callback)
            for callback in (
                on_provider_start,
                on_exchange_code_consumed,
                on_exchange_code_restored,
            )
        )
        if lifecycle_callbacks_present:
            # The existing broker owns a different callback lifecycle. It is
            # unsafe to bind a newly reserved pool row to that old worker.
            # Reconcile only the state already published by that broker; never
            # attach callbacks that could fire later in a different task.
            callback_errors: list[str] = []

            def replay_callback(name: str, callback) -> None:
                if not callable(callback):
                    return
                try:
                    callback()
                except Exception as exc:
                    callback_errors.append(f"{name}:{type(exc).__name__}")

            settlement = str(
                snapshot.get("exchange_code_settlement") or ""
            ).strip().lower()
            existing_provider_started = bool(snapshot.get("provider_started"))
            existing_cleanup_settled = bool(
                snapshot.get("provider_cleanup_settled", False)
            )
            known_settlements = {"restored", "consumed", "unusable"}
            safe_without_provider_start = bool(
                not existing_provider_started
                and existing_cleanup_settled
                and not settlement
            )
            must_quarantine = bool(
                settlement == "active_unknown"
                or (
                    settlement not in known_settlements
                    and not safe_without_provider_start
                )
            )

            if existing_provider_started or must_quarantine:
                replay_callback("provider_start", on_provider_start)
            if settlement == "restored":
                replay_callback("restored", on_exchange_code_restored)
            elif settlement in {"consumed", "unusable"}:
                replay_callback("consumed", on_exchange_code_consumed)

            effective_settlement = (
                "active_unknown" if must_quarantine else settlement
            )
            effective_provider_started = bool(
                existing_provider_started or must_quarantine
            )
            return {
                **dict(snapshot),
                "session_id": session_id,
                "status": "failed",
                "message": (
                    "同一 LeadBee 卡密已有独立会话在运行；"
                    + (
                        "当前任务的卡密副本已隔离，未接管旧会话"
                        if must_quarantine
                        else "当前任务已按旧会话的已知卡密终态安全结算"
                    )
                ),
                "provider_started": effective_provider_started,
                "provider_cleanup_settled": True,
                "existing_provider_cleanup_settled": existing_cleanup_settled,
                "exchange_code_settlement": effective_settlement,
                "exchange_code_restoration_confirmed": (
                    settlement == "restored"
                ),
                "exchange_code_consumed": settlement == "consumed",
                "exchange_code_unusable": settlement == "unusable",
                "reused": True,
                "ownership_conflict": True,
                "lifecycle_callback_errors": callback_errors,
                "provider_start_callback_error": next(
                    (
                        error.split(":", 1)[1]
                        for error in callback_errors
                        if error.startswith("provider_start:")
                    ),
                    "",
                ),
                "logs": list(snapshot.get("logs") or []),
            }

    def cancel_phone_session(message: str) -> dict | None:
        try:
            result = phone_verification_manager.cancel(
                int(account_id),
                session_id,
                message=message,
            )
            return dict(result) if isinstance(result, dict) else None
        except ValueError:
            return None

    forwarded_logs = 0
    waiting_for_finalization = False
    control_request_deferred = False
    deadline_cancel_requested = False
    cleanup_wait_logged = False
    finalization_deadline: float | None = None
    expires_in = max(1, int(snapshot.get("expires_in") or 600))
    deadline = time.monotonic() + expires_in + 5
    while True:
        logs = snapshot.get("logs") if isinstance(snapshot.get("logs"), list) else []
        for line in logs[forwarded_logs:]:
            safe_line = _redact_task_secret(line, leadbee_code)
            safe_line = re.sub(r"^\[\d{2}:\d{2}:\d{2}\]\s*", "", safe_line)
            if safe_line:
                _log(task_id, f"  [接码] {safe_line}")
        forwarded_logs = max(forwarded_logs, len(logs))

        status = str(snapshot.get("status") or "").strip().lower()
        terminal = status in {"completed", "failed", "expired"}
        cleanup_settled = bool(
            snapshot.get("provider_cleanup_settled", False)
        )
        if status == "persisting":
            finalization_now = time.monotonic()
            if (
                not deadline_cancel_requested
                and finalization_now >= deadline
            ):
                cancelled = cancel_phone_session(
                    "LeadBee 自动接码等待超时，后台任务已取消"
                )
                deadline_cancel_requested = True
                if cancelled is not None:
                    snapshot = cancelled
            if finalization_deadline is None:
                finalization_deadline = (
                    finalization_now + CHATGPT_PHONE_FINALIZATION_WAIT_SECONDS
                )
                _log(
                    task_id,
                    "  [接码] LeadBee 已结束，正在确认 Refresh Token 本地保存",
                )
            if not control_request_deferred:
                try:
                    control.checkpoint(attempt_id=attempt_id)
                except (StopTaskRequested, SkipCurrentAttemptRequested):
                    _log(
                        task_id,
                        "  [接码] 已收到任务控制请求；Refresh Token 正在提交，"
                        "为避免把已消费卡密误判失败，将等待本次保存确认或达到保存期限",
                    )
                    control_request_deferred = True
            waiting_for_finalization = True
            if finalization_now >= finalization_deadline:
                return {
                    **dict(snapshot),
                    "status": "failed",
                    "message": (
                        "Refresh Token 本地保存超过等待期限，尚未确认成功；"
                        "任务已停止等待，账号状态将在后续巡检中重新确认"
                    ),
                    "provider_cleanup_settled": cleanup_settled,
                    "finalization_pending": True,
                }
        if terminal and cleanup_settled:
            return dict(snapshot)
        if terminal and not cleanup_settled and not cleanup_wait_logged:
            _log(
                task_id,
                "  [接码] 业务结果已结束，正在等待 LeadBee 后台清理确认终态",
            )
            cleanup_wait_logged = True
        if (
            not terminal
            and not waiting_for_finalization
            and not control_request_deferred
            and not deadline_cancel_requested
            and time.monotonic() >= deadline
        ):
            cancelled = cancel_phone_session(
                "LeadBee 自动接码等待超时，后台任务已取消"
            )
            deadline_cancel_requested = True
            cancelled_status = str(
                (cancelled or {}).get("status") or ""
            ).strip().lower()
            if cancelled_status in {"persisting", "completed"}:
                snapshot = cancelled or snapshot
                waiting_for_finalization = cancelled_status == "persisting"
                if waiting_for_finalization:
                    if finalization_deadline is None:
                        finalization_deadline = (
                            time.monotonic()
                            + CHATGPT_PHONE_FINALIZATION_WAIT_SECONDS
                        )
                    _log(
                        task_id,
                        "  [接码] Refresh Token 正在安全保存，等待当前账号完成后再结束任务",
                    )
                continue
            if cancelled is not None:
                snapshot = cancelled
            if (
                str(snapshot.get("status") or "").strip().lower()
                in {"completed", "failed", "expired"}
                and bool(snapshot.get("provider_cleanup_settled", False))
            ):
                return dict(snapshot)
            _log(
                task_id,
                "  [接码] 已请求结束超时会话，继续等待服务端清理确认；"
                "期间不会释放卡密或并发槽位",
            )
            continue

        if (
            not terminal
            and not waiting_for_finalization
            and not control_request_deferred
        ):
            try:
                control.checkpoint(attempt_id=attempt_id)
            except (StopTaskRequested, SkipCurrentAttemptRequested) as exc:
                action = "停止任务" if isinstance(exc, StopTaskRequested) else "跳过账号"
                _log(
                    task_id,
                    f"  [接码] 已收到{action}请求；LeadBee 激活后排队任务可能不可取消，"
                    "为避免烧掉卡密，当前卡继续等待到终态，其余账号不再启动接码",
                )
                control_request_deferred = True
                continue
        time.sleep(0.5)
        try:
            snapshot = phone_verification_manager.status(int(account_id), session_id)
        except ValueError as exc:
            fallback = {
                **dict(snapshot),
                "status": "failed",
                "message": _redact_task_secret(exc, leadbee_code),
                # Automatic sessions are retired only after the worker has
                # released its provider permit and published cleanup. A
                # missing session here therefore settled between snapshots.
                "provider_cleanup_settled": True,
            }
            if (
                bool(snapshot.get("provider_started"))
                and not bool(snapshot.get("exchange_code_consumed"))
                and not bool(snapshot.get("exchange_code_unusable"))
                and not bool(
                    snapshot.get("exchange_code_restoration_confirmed")
                )
                and str(
                    snapshot.get("exchange_code_settlement") or ""
                ).strip().lower()
                not in {"restored", "consumed", "unusable"}
            ):
                fallback["exchange_code_settlement"] = "active_unknown"
                fallback["exchange_code_unusable"] = False
                fallback["message"] = (
                    "LeadBee 会话记录已结束，但服务端卡密终态不可确认；"
                    "卡密保持隔离等待人工核对"
                )
            return fallback


def _reload_saved_account(account_id: int, fallback):
    try:
        from core.db import AccountModel

        with Session(engine) as session:
            account = session.get(AccountModel, int(account_id))
            return account or fallback
    except Exception:
        return fallback


def _requeue_chatgpt_login_mailbox(mailbox, account) -> bool:
    """Return a consumed mailbox after a phone-stage failure for later retry."""
    from core.base_mailbox import MailboxAccount

    requeue = getattr(mailbox, "requeue_account", None)
    extra = (
        dict(getattr(account, "extra", None) or {})
        if account is not None
        else {}
    )
    context = extra.get("mailbox_login_context")
    if not callable(requeue) or not isinstance(context, dict):
        return False
    email = str(context.get("email") or getattr(account, "email", "") or "").strip()
    account_extra = context.get("extra")
    if not email or not isinstance(account_extra, dict):
        return False
    requeue(
        MailboxAccount(
            email=email,
            account_id=str(context.get("account_id") or ""),
            extra=dict(account_extra),
        )
    )
    return True


def _run_register(task_id: str, req: RegisterTaskRequest):
    if req.platform != "chatgpt":
        return _run_register_inner(task_id, req)

    uses_sms_pool = _is_truthy(req.extra.get(CHATGPT_USE_SMS_POOL_FLAG))
    if uses_sms_pool:
        with _sms_pool_quarantine_lock:
            _sms_pool_quarantine_item_ids_by_task[task_id] = set()
    lease: object | None = None
    try:
        control = _task_store.control_for(task_id)
        lease = chatgpt_task_gate.enter_foreground(
            on_wait=lambda: _log(
                task_id,
                "等待自动重登释放；手工任务优先，自动重登将安全停止",
            ),
            cancelled=control.is_stop_requested,
        )
        if lease is None:
            _terminalize_stopped_task(
                task_id,
                "手工 ChatGPT 任务已停止：等待自动重登释放期间收到停止请求",
            )
            return None
        try:
            return _run_register_inner(task_id, req)
        except Exception as exc:
            _terminalize_failed_chatgpt_register_run(task_id, exc)
            return None
    finally:
        try:
            if uses_sms_pool:
                with _sms_pool_quarantine_lock:
                    quarantine_item_ids = (
                        _sms_pool_quarantine_item_ids_by_task.pop(
                            task_id,
                            set(),
                        )
                    )
                if quarantine_item_ids:
                    sms_pool_service.release_task(
                        task_id,
                        quarantine_item_ids=quarantine_item_ids,
                    )
                else:
                    sms_pool_service.release_task(task_id)
        finally:
            if lease is not None:
                chatgpt_task_gate.leave_foreground(lease)


def _run_register_inner(task_id: str, req: RegisterTaskRequest):
    from core.registry import get
    from core.base_platform import RegisterConfig
    from core.db import (
        delete_incomplete_chatgpt_account,
        save_account,
        save_account_with_creation_state,
    )
    from core.base_mailbox import MailboxClaimScope, create_mailbox
    from core.proxy_utils import normalize_proxy_url
    from services.chatgpt_account_state import chatgpt_account_refresh_token

    control = _task_store.control_for(task_id)
    with _sms_pool_quarantine_lock:
        quarantine_item_ids = _sms_pool_quarantine_item_ids_by_task.get(task_id)
    if quarantine_item_ids is None:
        quarantine_item_ids = set()
    quarantine_item_ids_lock = threading.Lock()
    _task_store.mark_running(task_id)
    _persist_task_snapshot(task_id)
    success = 0
    skipped = 0
    errors = []
    start_gate_lock = threading.Lock()
    next_start_time = time.time()
    action_name, success_action_name = _task_action_terms(req)

    def _sleep_with_control(
        wait_seconds: float,
        *,
        attempt_id: int | None = None,
    ) -> None:
        remaining = max(float(wait_seconds or 0), 0.0)
        while remaining > 0:
            control.checkpoint(attempt_id=attempt_id)
            chunk = min(0.25, remaining)
            time.sleep(chunk)
            remaining -= chunk

    try:
        PlatformCls = get(req.platform)

        # 预先计算 merged_extra，所有线程共享只读副本，避免每线程重复调用 config_store
        from core.config_store import config_store as _cs
        _bind_phone_and_get_rt = _chatgpt_bind_phone_enabled(req)
        _leadbee_codes = (
            _normalize_leadbee_codes(req.extra.get(CHATGPT_LEADBEE_CODES_KEY))
            if _bind_phone_and_get_rt
            else []
        )
        _use_sms_pool = _is_truthy(req.extra.get(CHATGPT_USE_SMS_POOL_FLAG))
        _leadbee_base_urls = (
            _normalize_leadbee_base_urls(
                req.extra.get(CHATGPT_LEADBEE_BASE_URLS_KEY)
            )
            if _use_sms_pool
            else []
        )
        _sms_pool_item_ids = (
            _normalize_sms_pool_item_ids(
                req.extra.get(CHATGPT_SMS_POOL_ITEM_IDS_KEY)
            )
            if _use_sms_pool
            else []
        )
        _retry_bindings = (
            _normalize_chatgpt_retry_bindings(
                req.extra.get(CHATGPT_RETRY_BINDINGS_KEY)
            )
            if _bind_phone_and_get_rt
            else []
        )
        _mail_provider_plan = _normalize_chatgpt_mail_provider_plan(
            req.extra.get(CHATGPT_MAIL_PROVIDER_PLAN_KEY)
        )
        if _bind_phone_and_get_rt and len(_leadbee_codes) != req.count:
            raise RuntimeError(
                "卡密数量需与登录数量一致"
                f"（需要 {req.count} 个，当前 {len(_leadbee_codes)} 个）"
            )
        if _use_sms_pool and (
            len(_leadbee_base_urls) != req.count
            or len(_sms_pool_item_ids) != req.count
        ):
            raise RuntimeError("SMS 接码池卡密绑定数量与登录数量不一致")
        if _retry_bindings and len(_retry_bindings) != req.count:
            raise RuntimeError("失败账号绑定数量需与重试数量一致")
        if _mail_provider_plan and len(_mail_provider_plan) != req.count:
            raise RuntimeError("邮箱来源分配数量与登录数量不一致")
        # Failed mailboxes return to the global pool immediately so another
        # task can retry them.  Within this task, all workers share one scope
        # so a later attempt cannot reclaim an address already tried here.
        _mailbox_claim_scope = (
            MailboxClaimScope()
            if req.platform == "chatgpt" and not _retry_bindings
            else None
        )
        _base_extra = _cs.get_all().copy()
        if _bind_phone_and_get_rt:
            for secret_key in _CHATGPT_LEADBEE_SECRET_KEYS:
                _base_extra.pop(secret_key, None)
        _base_extra.update(
            {
                k: v
                for k, v in req.extra.items()
                if k not in _CHATGPT_LEADBEE_SECRET_KEYS
                and v is not None
                and v != ""
            }
        )

        # 批量预取代理（无固定代理时），减少每线程单独查 DB
        from core.proxy_pool import proxy_pool as _proxy_pool
        _prefetched_proxies: list[str] = []
        _prefetch_lock = threading.Lock()
        if not req.proxy and req.count > 1:
            with Session(engine) as _s:
                from core.db import ProxyModel
                from sqlmodel import select as _sel
                _active = _s.exec(
                    _sel(ProxyModel).where(ProxyModel.is_active == True)
                ).all()
                _prefetched_proxies = [p.url for p in _active if p.url]

        def _get_proxy() -> Optional[str]:
            if req.proxy:
                return req.proxy
            if _prefetched_proxies:
                with _prefetch_lock:
                    if _prefetched_proxies:
                        import random
                        return random.choice(_prefetched_proxies)
            return _proxy_pool.get_next()

        def _build_mailbox(
            proxy: Optional[str],
            provider_override: str = "",
        ):
            mailbox = create_mailbox(
                provider=(
                    str(provider_override or "").strip()
                    or _base_extra.get("mail_provider", "luckmail")
                ),
                extra=_base_extra,
                proxy=proxy,
            )
            bind_claim_scope = getattr(mailbox, "bind_claim_scope", None)
            if _mailbox_claim_scope is not None and callable(bind_claim_scope):
                bind_claim_scope(_mailbox_claim_scope)
            return mailbox

        binding_persistence_disabled = threading.Event()

        def _persist_binding(**kwargs):
            if binding_persistence_disabled.is_set():
                return None
            try:
                return _upsert_chatgpt_attempt_binding(**kwargs)
            except Exception as exc:
                if not binding_persistence_disabled.is_set():
                    binding_persistence_disabled.set()
                    _log(
                        task_id,
                        "[WARN] 邮箱与接码卡密绑定暂未写入数据库，"
                        f"本次登录继续执行（{type(exc).__name__}）",
                    )
                return None

        def _do_one(i: int):
            nonlocal next_start_time
            _proxy = None
            _mailbox = None
            account = None
            saved_account = None
            saved_account_created_by_attempt = False
            saved_account_extra_snapshot: str | None = None
            phone_flow_completed = False
            phone_background_ownership_pending = False
            retry_binding = _retry_bindings[i] if _retry_bindings else {}
            bound_email = str(retry_binding.get("email") or "").strip()
            bound_mail_provider = str(
                retry_binding.get("mail_provider") or ""
            ).strip()
            leadbee_code = _leadbee_codes[i] if _bind_phone_and_get_rt else ""
            leadbee_base_url = _leadbee_base_urls[i] if _use_sms_pool else ""
            sms_pool_item_id = _sms_pool_item_ids[i] if _use_sms_pool else 0
            sms_pool_consumed = False
            sms_pool_restoration_confirmed = False
            sms_pool_settlement_pending = False
            parent_binding_id = int(retry_binding.get("id") or 0)
            current_email = bound_email or req.email or ""
            current_stage = "login"
            attempt_id: int | None = None

            def _sms_pool_binding_context(value=None):
                context = dict(value) if isinstance(value, dict) else {}
                if sms_pool_item_id:
                    context["sms_pool_managed"] = True
                    context["sms_pool_item_id"] = sms_pool_item_id
                return context or None

            try:
                control.checkpoint()
                attempt_id = control.start_attempt()
                control.checkpoint(attempt_id=attempt_id)
                if _bind_phone_and_get_rt:
                    _persist_binding(
                        task_id=task_id,
                        attempt_index=i,
                        leadbee_code=leadbee_code,
                        email=current_email,
                        stage=current_stage,
                        status="running",
                        mailbox_context=_sms_pool_binding_context(),
                        parent_binding_id=parent_binding_id,
                    )
                _proxy = normalize_proxy_url(_get_proxy())
                if req.register_delay_seconds > 0:
                    with start_gate_lock:
                        control.checkpoint(attempt_id=attempt_id)
                        now = time.time()
                        wait_seconds = max(0.0, next_start_time - now)
                        if wait_seconds > 0:
                            _log(
                                task_id,
                                f"第 {i + 1} 个账号启动前延迟 {wait_seconds:g} 秒",
                            )
                            _sleep_with_control(
                                wait_seconds,
                                attempt_id=attempt_id,
                            )
                        next_start_time = time.time() + req.register_delay_seconds
                control.checkpoint(attempt_id=attempt_id)

                # 每个 attempt 使用独立字典，避免并发任务之间发生配置串用。
                merged_extra = _base_extra.copy()
                planned_mail_provider = (
                    _mail_provider_plan[i]
                    if i < len(_mail_provider_plan)
                    else ""
                )
                attempt_mail_provider = (
                    bound_mail_provider
                    or planned_mail_provider
                    or str(merged_extra.get("mail_provider") or "").strip()
                )
                if attempt_mail_provider:
                    merged_extra["mail_provider"] = attempt_mail_provider

                if _bind_phone_and_get_rt:
                    def _record_mailbox_binding(email_value, mailbox_account):
                        nonlocal current_email
                        current_email = str(email_value or current_email or "").strip()
                        account_extra = dict(
                            getattr(mailbox_account, "extra", None) or {}
                        )
                        mailbox_context = _sms_pool_binding_context({
                            "provider": str(
                                account_extra.get("provider")
                                or merged_extra.get("mail_provider")
                                or "custom_provider"
                            ).strip(),
                            "email": current_email,
                            "account_id": str(
                                getattr(mailbox_account, "account_id", "") or ""
                            ).strip(),
                            "extra": account_extra,
                        })
                        _persist_binding(
                            task_id=task_id,
                            attempt_index=i,
                            leadbee_code=leadbee_code,
                            email=current_email,
                            stage="login",
                            status="running",
                            mailbox_context=mailbox_context,
                            parent_binding_id=parent_binding_id,
                        )

                    merged_extra["_chatgpt_attempt_binding_callback"] = (
                        _record_mailbox_binding
                    )

                _config = RegisterConfig(
                    executor_type=req.executor_type,
                    captcha_solver=req.captcha_solver,
                    proxy=_proxy,
                    extra=merged_extra,
                )
                _mailbox = _build_mailbox(_proxy, attempt_mail_provider)
                _platform = PlatformCls(config=_config, mailbox=_mailbox)
                _platform._task_attempt_token = attempt_id
                _platform._log_fn = lambda msg: _log(task_id, msg)
                _platform.bind_task_control(control)
                if getattr(_platform, "mailbox", None) is not None:
                    _platform.mailbox._task_attempt_token = attempt_id
                    _platform.mailbox._log_fn = _platform._log_fn
                _task_store.set_progress(task_id, f"{i + 1}/{req.count}")
                _persist_task_snapshot(task_id)
                _log(task_id, f"开始{action_name}第 {i + 1}/{req.count} 个账号")
                if _proxy:
                    _log(task_id, f"使用代理: {_proxy}")
                account = _platform.register(
                    email=bound_email or req.email or None,
                    password=req.password,
                )
                current_email = account.email or current_email
                if str(merged_extra.get("mail_provider", "")).strip() == "cfworker":
                    from core.email_domain_policy import validate_email_domain_policy

                    validate_email_domain_policy(
                        account.email,
                        {
                            "email_domain_rule_enabled": merged_extra.get(
                                "email_domain_rule_enabled", "0"
                            ),
                            "email_domain_level_count": merged_extra.get(
                                "email_domain_level_count", "2"
                            ),
                        },
                    )
                if isinstance(account.extra, dict):
                    for secret_key in _CHATGPT_LEADBEE_SECRET_KEYS:
                        account.extra.pop(secret_key, None)
                    mail_provider = merged_extra.get("mail_provider", "")
                    if mail_provider:
                        account.extra.setdefault("mail_provider", mail_provider)
                    if mail_provider == "luckmail" and req.platform == "chatgpt":
                        mailbox_token = getattr(_mailbox, "_token", "") or ""
                        if mailbox_token:
                            account.extra.setdefault("mailbox_token", mailbox_token)
                        if merged_extra.get("luckmail_project_code"):
                            account.extra.setdefault(
                                "luckmail_project_code",
                                merged_extra.get("luckmail_project_code"),
                            )
                        if merged_extra.get("luckmail_email_type"):
                            account.extra.setdefault(
                                "luckmail_email_type",
                                merged_extra.get("luckmail_email_type"),
                            )
                        if merged_extra.get("luckmail_domain"):
                            account.extra.setdefault(
                                "luckmail_domain", merged_extra.get("luckmail_domain")
                            )
                        if merged_extra.get("luckmail_base_url"):
                            account.extra.setdefault(
                                "luckmail_base_url",
                                merged_extra.get("luckmail_base_url"),
                            )
                if _bind_phone_and_get_rt:
                    (
                        saved_account,
                        saved_account_created_by_attempt,
                    ) = save_account_with_creation_state(account)
                    if saved_account_created_by_attempt:
                        raw_saved_extra = getattr(saved_account, "extra_json", None)
                        if isinstance(raw_saved_extra, str):
                            saved_account_extra_snapshot = raw_saved_extra
                else:
                    saved_account = save_account(account)
                if _proxy:
                    _proxy_pool.report_success(_proxy)

                if _bind_phone_and_get_rt:
                    current_stage = "phone"
                    _persist_binding(
                        task_id=task_id,
                        attempt_index=i,
                        leadbee_code=leadbee_code,
                        email=account.email,
                        account_id=int(getattr(saved_account, "id", 0) or 0),
                        stage=current_stage,
                        status="running",
                        mailbox_context=_sms_pool_binding_context(
                            account.extra.get("mailbox_login_context")
                            if isinstance(getattr(account, "extra", None), dict)
                            else None
                        ),
                        parent_binding_id=parent_binding_id,
                    )
                    account_extra = (
                        dict(account.extra)
                        if isinstance(getattr(account, "extra", None), dict)
                        else {}
                    )
                    resume_context = account_extra.get("oauth_resume_context")
                    phone_oauth_ready = bool(
                        _is_truthy(account_extra.get("phone_oauth_ready"))
                        or (
                            isinstance(resume_context, dict)
                            and bool(resume_context)
                        )
                    )
                    if not phone_oauth_ready:
                        failure = (
                            "邮箱登录成功，Access Token 已保存，但手机授权事务未就绪；"
                            "本次未启动 LeadBee，请重新执行该账号的登录接码"
                        )
                        _log(task_id, f"[FAIL] {failure}: {account.email}")
                        _save_task_log(
                            req.platform,
                            account.email,
                            "failed",
                            error=failure,
                            detail={
                                "partial_success": True,
                                "access_token_saved": True,
                                "phone_status": "not_started",
                                "exchange_code_consumed": False,
                            },
                        )
                        _requeue_chatgpt_login_mailbox(_mailbox, account)
                        _persist_binding(
                            task_id=task_id,
                            attempt_index=i,
                            leadbee_code=leadbee_code,
                            email=account.email,
                            account_id=int(getattr(saved_account, "id", 0) or 0),
                            stage=current_stage,
                            status="failed",
                            error=failure,
                            parent_binding_id=parent_binding_id,
                        )
                        return AttemptResult.failed(failure)

                    account_id = getattr(saved_account, "id", None)
                    if not account_id:
                        failure = "邮箱登录成功，但账号保存后缺少 ID，无法开始接码"
                        _log(task_id, f"[FAIL] {failure}: {account.email}")
                        _save_task_log(
                            req.platform,
                            account.email,
                            "failed",
                            error=failure,
                            detail={
                                "partial_success": True,
                                "access_token_saved": True,
                            },
                        )
                        _requeue_chatgpt_login_mailbox(_mailbox, account)
                        _persist_binding(
                            task_id=task_id,
                            attempt_index=i,
                            leadbee_code=leadbee_code,
                            email=account.email,
                            stage=current_stage,
                            status="failed",
                            error=failure,
                            parent_binding_id=parent_binding_id,
                        )
                        return AttemptResult.failed(failure)

                    _log(
                        task_id,
                        f"邮箱登录成功，Access Token 已保存；开始自动接码: {account.email}",
                    )
                    # Every card initially reserved for this task belongs to a
                    # specific mailbox attempt.  A released card from another
                    # attempt is not a "spare" and must never be stolen here.
                    attempted_sms_pool_item_ids: set[int] = {
                        int(item_id)
                        for item_id in _sms_pool_item_ids
                        if int(item_id or 0) > 0
                    }
                    same_card_no_number_retries = 0
                    phone_result: dict = {}
                    phone_status = ""
                    phone_message = ""
                    exchange_code_consumed = False

                    while True:
                        active_pool_item_id = int(sms_pool_item_id or 0)
                        active_leadbee_code = str(leadbee_code or "").strip()
                        active_leadbee_base_url = str(
                            leadbee_base_url or ""
                        ).strip()
                        if active_pool_item_id:
                            attempted_sms_pool_item_ids.add(active_pool_item_id)

                        def _mark_sms_pool_consumed(
                            _item_id=active_pool_item_id,
                        ) -> None:
                            nonlocal sms_pool_consumed
                            if int(sms_pool_item_id or 0) == int(_item_id or 0):
                                # `active` is already crash-safe: interrupted
                                # tasks conservatively settle it as used.  Keep
                                # the callback provisional until the provider
                                # has had a chance to confirm restoration.
                                sms_pool_consumed = True

                        def _mark_sms_pool_active(
                            _item_id=active_pool_item_id,
                        ) -> None:
                            nonlocal sms_pool_consumed
                            nonlocal sms_pool_restoration_confirmed
                            if _item_id and not sms_pool_service.mark_active(
                                item_id=_item_id,
                                task_id=task_id,
                                account_email=current_email,
                            ):
                                raise RuntimeError(
                                    "SMS 接码池卡密使用中状态保存失败"
                                )
                            # A previously restored card stays reusable while
                            # waiting to retry. It becomes at-risk again only
                            # when the provider actually starts a new attempt.
                            sms_pool_consumed = False
                            sms_pool_restoration_confirmed = False

                        def _mark_sms_pool_restored(
                            _item_id=active_pool_item_id,
                        ) -> None:
                            nonlocal sms_pool_consumed
                            nonlocal sms_pool_restoration_confirmed
                            if int(sms_pool_item_id or 0) == int(_item_id or 0):
                                if _item_id and not sms_pool_service.mark_restored(
                                    item_id=_item_id,
                                    task_id=task_id,
                                ):
                                    raise RuntimeError(
                                        "SMS 接码池卡密恢复状态保存失败"
                                    )
                                sms_pool_consumed = False
                                sms_pool_restoration_confirmed = True

                        try:
                            phone_result = _complete_chatgpt_leadbee_verification(
                                task_id=task_id,
                                account_id=int(account_id),
                                leadbee_code=active_leadbee_code,
                                leadbee_base_url=active_leadbee_base_url,
                                on_provider_start=(
                                    _mark_sms_pool_active
                                    if active_pool_item_id
                                    else None
                                ),
                                on_exchange_code_consumed=(
                                    _mark_sms_pool_consumed
                                    if active_pool_item_id
                                    else None
                                ),
                                on_exchange_code_restored=(
                                    _mark_sms_pool_restored
                                    if active_pool_item_id
                                    else None
                                ),
                                control=control,
                                attempt_id=attempt_id,
                            )
                        except (SkipCurrentAttemptRequested, StopTaskRequested):
                            raise
                        except Exception as exc:
                            phone_result = {
                                "status": "failed",
                                "message": _redact_task_secret(
                                    exc,
                                    active_leadbee_code,
                                ),
                            }

                        phone_status = str(
                            phone_result.get("status") or ""
                        ).lower()
                        phone_message = _redact_task_secret(
                            phone_result.get("message")
                            or "LeadBee 自动接码失败",
                            active_leadbee_code,
                        )
                        sms_pool_restoration_confirmed = bool(
                            sms_pool_restoration_confirmed
                            or phone_result.get(
                                "exchange_code_restoration_confirmed",
                                False,
                            )
                        )
                        exchange_code_settlement = str(
                            phone_result.get("exchange_code_settlement") or ""
                        ).strip().lower()
                        sms_pool_settlement_pending = (
                            exchange_code_settlement == "active_unknown"
                            or (
                                bool(phone_result.get("provider_started"))
                                and exchange_code_settlement
                                not in {"restored", "consumed", "unusable"}
                            )
                        )
                        if sms_pool_settlement_pending and active_pool_item_id:
                            with quarantine_item_ids_lock:
                                quarantine_item_ids.add(active_pool_item_id)
                        phone_background_ownership_pending = bool(
                            phone_result.get("finalization_pending", False)
                            or phone_result.get("ownership_conflict", False)
                        )
                        exchange_code_consumed = bool(
                            phone_result.get("exchange_code_consumed", False)
                            or (
                                (
                                    sms_pool_consumed
                                    or phone_result.get(
                                        "exchange_code_unusable",
                                        False,
                                    )
                                )
                                and not sms_pool_restoration_confirmed
                            )
                        )
                        sms_pool_consumed = exchange_code_consumed
                        provider_error_code = str(
                            phone_result.get("provider_error_code") or ""
                        ).strip().upper()
                        if phone_status != "completed":
                            # The stop/skip request may arrive while the provider
                            # is still polling.  Observe it before any retry,
                            # replacement reservation, or binding rewrite.
                            control.checkpoint(attempt_id=attempt_id)

                        can_retry_same_card = bool(
                            phone_status != "completed"
                            and provider_error_code == "CARD_NOT_IN_SESSION"
                            and sms_pool_restoration_confirmed
                            and not str(phone_result.get("phone") or "").strip()
                            and not bool(phone_result.get("phone_verified", False))
                            and same_card_no_number_retries < 1
                        )
                        if can_retry_same_card:
                            same_card_no_number_retries += 1
                            _log(
                                task_id,
                                "  [接码] LeadBee 本轮暂时无可用号码，"
                                "原卡已释放；2 秒后使用同一张卡重新排队，"
                                "不占用其他邮箱的卡密",
                            )
                            sms_pool_consumed = False
                            _sleep_with_control(2, attempt_id=attempt_id)
                            continue

                        can_switch_pool_card = bool(
                            phone_status != "completed"
                            and _use_sms_pool
                            and active_pool_item_id
                            and provider_error_code
                            in _LEADBEE_POOL_REPLACEMENT_ERROR_CODES
                            and not str(phone_result.get("phone") or "").strip()
                            and not bool(phone_result.get("phone_verified", False))
                        )
                        if not can_switch_pool_card:
                            break

                        try:
                            replacement_rows = sms_pool_service.reserve(
                                task_id=task_id,
                                count=1,
                                exclude_item_ids=set(attempted_sms_pool_item_ids),
                            )
                        except SmsPoolExhaustedError:
                            replacement_rows = []
                        if not replacement_rows:
                            card_state = (
                                "当前卡已释放，稍后仍可重试"
                                if sms_pool_restoration_confirmed
                                else "当前坏卡已隔离"
                            )
                            _log(
                                task_id,
                                f"  [接码] {card_state}；SMS 接码池暂无其他备用卡，"
                                "邮箱将保留供下次继续",
                            )
                            break

                        replacement = replacement_rows[0]
                        if active_pool_item_id:
                            finalized = sms_pool_service.finalize(
                                item_id=active_pool_item_id,
                                task_id=task_id,
                                consumed=sms_pool_consumed,
                                restoration_confirmed=(
                                    sms_pool_restoration_confirmed
                                ),
                                account_email=current_email,
                            )
                            if not finalized:
                                sms_pool_service.finalize(
                                    item_id=int(replacement.id),
                                    task_id=task_id,
                                    consumed=False,
                                )
                                raise RuntimeError(
                                    "SMS 接码池旧卡状态保存失败，已停止自动换卡"
                                )

                        leadbee_code = str(replacement.code or "").strip()
                        leadbee_base_url = str(
                            replacement.base_url or ""
                        ).strip()
                        sms_pool_item_id = int(replacement.id or 0)
                        sms_pool_consumed = False
                        sms_pool_restoration_confirmed = False
                        sms_pool_settlement_pending = False
                        _log(
                            task_id,
                            "  [接码] 当前卡不可用于本轮会话，已为同一邮箱"
                            f"切换备用卡 {mask_sms_code(leadbee_code)}；无需重新登录邮箱",
                        )
                        _persist_binding(
                            task_id=task_id,
                            attempt_index=i,
                            leadbee_code=leadbee_code,
                            email=account.email,
                            account_id=int(account_id),
                            stage=current_stage,
                            status="running",
                            error=phone_message,
                            mailbox_context=_sms_pool_binding_context(
                                account.extra.get("mailbox_login_context")
                                if isinstance(
                                    getattr(account, "extra", None),
                                    dict,
                                )
                                else None
                            ),
                            parent_binding_id=parent_binding_id,
                        )

                    if phone_status != "completed":
                        failure = f"邮箱登录成功，但接码失败: {phone_message}"
                        _log(task_id, f"[FAIL] {failure}: {account.email}")
                        _save_task_log(
                            req.platform,
                            account.email,
                            "failed",
                            error=failure,
                            detail={
                                "partial_success": True,
                                "access_token_saved": True,
                                "phone_status": phone_status or "failed",
                                "exchange_code_consumed": exchange_code_consumed,
                            },
                        )
                        if phone_background_ownership_pending:
                            _log(
                                task_id,
                                "[WARN] 后台手机验证会话仍持有当前账号；"
                                "保留账号与邮箱凭据，不回池、不删除，等待后续巡检确认",
                            )
                        else:
                            _requeue_chatgpt_login_mailbox(_mailbox, account)
                        _persist_binding(
                            task_id=task_id,
                            attempt_index=i,
                            leadbee_code=leadbee_code,
                            email=account.email,
                            account_id=int(account_id),
                            stage=current_stage,
                            status="failed",
                            error=failure,
                            parent_binding_id=parent_binding_id,
                        )
                        return AttemptResult.failed(failure)

                    final_account = _reload_saved_account(
                        int(account_id),
                        saved_account or account,
                    )
                    if not chatgpt_account_refresh_token(final_account):
                        failure = (
                            "手机验证流程已结束，但本地账号未保存 Refresh Token"
                        )
                        _log(task_id, f"[FAIL] {failure}: {account.email}")
                        _save_task_log(
                            req.platform,
                            account.email,
                            "failed",
                            error=failure,
                            detail={
                                "partial_success": True,
                                "access_token_saved": True,
                                "phone_status": "completed_without_refresh_token",
                                "exchange_code_consumed": exchange_code_consumed,
                            },
                        )
                        _requeue_chatgpt_login_mailbox(_mailbox, account)
                        _persist_binding(
                            task_id=task_id,
                            attempt_index=i,
                            leadbee_code=leadbee_code,
                            email=account.email,
                            account_id=int(account_id),
                            stage=current_stage,
                            status="failed",
                            error=failure,
                            parent_binding_id=parent_binding_id,
                        )
                        return AttemptResult.failed(failure)

                    # AppleMail credentials are claimed before the login starts.
                    # In the phone flow the Refresh Token is persisted only after
                    # the verification session completes, so the plugin cannot
                    # finalize that claim during its initial AT-only return.
                    # Once the database confirms the RT, make the pool record
                    # permanently unavailable.  A commit error is best-effort:
                    # the existing `claimed` state is deliberately kept locked
                    # instead of re-queuing an account that already owns an RT.
                    mark_mailbox_used = getattr(
                        _mailbox,
                        "mark_account_used",
                        None,
                    )
                    if callable(mark_mailbox_used):
                        try:
                            marked_mailbox_used = mark_mailbox_used(account)
                            if marked_mailbox_used is False:
                                _log(
                                    task_id,
                                    "[WARN] Refresh Token 已获取，但邮箱池消费状态"
                                    "未确认；该邮箱继续保持锁定，不会再次分配",
                                )
                        except Exception as exc:
                            _log(
                                task_id,
                                "[WARN] Refresh Token 已获取，但邮箱池消费状态"
                                "保存失败；该邮箱继续保持锁定，不会再次分配"
                                f"（{type(exc).__name__}）",
                            )

                    if phone_message:
                        _log(task_id, f"  [接码结果] {phone_message}")
                    phone_flow_completed = True
                    _log(
                        task_id,
                        f"[OK] {success_action_name}成功: {account.email}",
                    )
                    _save_task_log(
                        req.platform,
                        account.email,
                        "success",
                        detail={
                            "phone_verified": bool(
                                phone_result.get("phone_verified", False)
                            ),
                            "exchange_code_consumed": exchange_code_consumed,
                        },
                    )
                    _persist_binding(
                        task_id=task_id,
                        attempt_index=i,
                        leadbee_code=leadbee_code,
                        email=account.email,
                        account_id=int(account_id),
                        stage="completed",
                        status="success",
                        parent_binding_id=parent_binding_id,
                    )
                    _auto_upload_integrations(task_id, final_account)
                    return AttemptResult.success()

                _log(task_id, f"[OK] {success_action_name}成功: {account.email}")
                _save_task_log(req.platform, account.email, "success")
                refresh_message = _refresh_saved_chatgpt_login(
                    req,
                    saved_account or account,
                )
                if refresh_message:
                    _log(task_id, f"  {refresh_message}")
                _auto_upload_integrations(task_id, saved_account or account)
                cashier_url = (account.extra or {}).get("cashier_url", "")
                if cashier_url:
                    _log(task_id, f"  [升级链接] {cashier_url}")
                    _task_store.add_cashier_url(task_id, cashier_url)
                    _persist_task_snapshot(task_id)
                return AttemptResult.success()
            except SkipCurrentAttemptRequested as e:
                _log(task_id, f"[SKIP] 已跳过当前账号: {e}")
                _save_task_log(
                    req.platform,
                    current_email,
                    "skipped",
                    error=str(e),
                )
                if _bind_phone_and_get_rt:
                    if (
                        current_stage == "phone"
                        and account is not None
                        and not phone_background_ownership_pending
                    ):
                        _requeue_chatgpt_login_mailbox(_mailbox, account)
                    _persist_binding(
                        task_id=task_id,
                        attempt_index=i,
                        leadbee_code=leadbee_code,
                        email=current_email,
                        account_id=int(getattr(saved_account, "id", 0) or 0),
                        stage=current_stage,
                        status="failed",
                        error=str(e),
                        parent_binding_id=parent_binding_id,
                    )
                return AttemptResult.skipped(str(e))
            except StopTaskRequested as e:
                _log(task_id, f"[STOP] {e}")
                if _bind_phone_and_get_rt:
                    if (
                        current_stage == "phone"
                        and account is not None
                        and not phone_background_ownership_pending
                    ):
                        _requeue_chatgpt_login_mailbox(_mailbox, account)
                    _persist_binding(
                        task_id=task_id,
                        attempt_index=i,
                        leadbee_code=leadbee_code,
                        email=current_email,
                        account_id=int(getattr(saved_account, "id", 0) or 0),
                        stage=current_stage,
                        status="failed",
                        error=str(e),
                        parent_binding_id=parent_binding_id,
                    )
                return AttemptResult.stopped(str(e))
            except Exception as e:
                if _proxy:
                    _proxy_pool.report_fail(_proxy)
                _log(task_id, f"[FAIL] {action_name}失败: {e}")
                _save_task_log(
                    req.platform,
                    current_email,
                    "failed",
                    error=str(e),
                )
                if _bind_phone_and_get_rt:
                    if (
                        current_stage == "phone"
                        and account is not None
                        and not phone_background_ownership_pending
                    ):
                        _requeue_chatgpt_login_mailbox(_mailbox, account)
                    _persist_binding(
                        task_id=task_id,
                        attempt_index=i,
                        leadbee_code=leadbee_code,
                        email=current_email,
                        account_id=int(getattr(saved_account, "id", 0) or 0),
                        stage=current_stage,
                        status="failed",
                        error=str(e),
                        parent_binding_id=parent_binding_id,
                    )
                return AttemptResult.failed(str(e))
            finally:
                if (
                    _bind_phone_and_get_rt
                    and saved_account_created_by_attempt
                    and not phone_flow_completed
                    and not phone_background_ownership_pending
                    and saved_account is not None
                    and saved_account_extra_snapshot is not None
                ):
                    saved_id = int(getattr(saved_account, "id", 0) or 0)
                    saved_email = str(
                        getattr(saved_account, "email", "") or current_email or ""
                    ).strip()
                    saved_created_at = getattr(saved_account, "created_at", None)
                    if saved_id and saved_email and saved_created_at is not None:
                        try:
                            deleted = delete_incomplete_chatgpt_account(
                                saved_id,
                                expected_email=saved_email,
                                expected_created_at=saved_created_at,
                                expected_extra_json=saved_account_extra_snapshot,
                            )
                            if deleted:
                                _log(
                                    task_id,
                                    "未获取 Refresh Token，已移除本次产生的半成品账号: "
                                    f"{saved_email}",
                                )
                        except Exception as exc:
                            _log(
                                task_id,
                                "[WARN] 未获取 Refresh Token 的半成品账号清理失败"
                                f"（{type(exc).__name__}）",
                            )
                if sms_pool_item_id and not sms_pool_settlement_pending:
                    try:
                        sms_pool_service.finalize(
                            item_id=sms_pool_item_id,
                            task_id=task_id,
                            consumed=sms_pool_consumed,
                            restoration_confirmed=sms_pool_restoration_confirmed,
                            account_email=current_email,
                        )
                    except Exception as exc:
                        _log(
                            task_id,
                            "[WARN] SMS 接码池状态更新失败"
                            f"（{type(exc).__name__}）",
                        )
                elif sms_pool_item_id and sms_pool_settlement_pending:
                    _log(
                        task_id,
                        "[WARN] LeadBee 服务端未确认卡密终态；"
                        "该卡保持隔离，不会重新分配或标记为已使用",
                    )
                control.finish_attempt(attempt_id)

        from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed

        max_workers = min(req.concurrency, req.count)
        stopped = False
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_do_one, i) for i in range(req.count)]
            for f in as_completed(futures):
                try:
                    result = f.result()
                except CancelledError:
                    continue
                except Exception as e:
                    _log(task_id, f"[ERROR] 任务线程异常: {e}")
                    errors.append(str(e))
                    continue
                if result.outcome == AttemptOutcome.SUCCESS:
                    success += 1
                elif result.outcome == AttemptOutcome.SKIPPED:
                    skipped += 1
                elif result.outcome == AttemptOutcome.STOPPED:
                    stopped = True
                else:
                    errors.append(result.message)
                _task_store.update_counters(
                    task_id,
                    success=success,
                    registered=success + skipped + len(errors),
                )
                _persist_task_snapshot(task_id)
                if stopped or control.is_stop_requested():
                    stopped = True
                    for pending in futures:
                        if pending is not f:
                            pending.cancel()
    except Exception as e:
        _log(task_id, f"致命错误: {e}")
        _task_store.finish(
            task_id,
            status="failed",
            success=success,
            registered=success + skipped + len(errors),
            skipped=skipped,
            errors=errors,
            error=str(e),
        )
        _persist_task_snapshot(task_id)
        _task_store.cleanup()
        return

    final_status = "stopped" if control.is_stop_requested() or stopped else "done"
    if final_status == "stopped":
        summary = (
            f"任务已停止: 成功 {success} 个, 跳过 {skipped} 个, 失败 {len(errors)} 个"
        )
    else:
        summary = f"完成: 成功 {success} 个, 跳过 {skipped} 个, 失败 {len(errors)} 个"
    _log(task_id, summary)
    _task_store.finish(
        task_id,
        status=final_status,
        success=success,
        registered=success + skipped + len(errors),
        skipped=skipped,
        errors=errors,
    )
    _persist_task_snapshot(task_id)
    _task_store.cleanup()


@router.post("/register")
def create_register_task(
    req: RegisterTaskRequest,
    background_tasks: BackgroundTasks,
):
    task_id = enqueue_register_task(req, background_tasks=background_tasks)
    return {"task_id": task_id}


@router.post("/chatgpt-relogin")
def create_chatgpt_relogin_task(
    req: ChatGPTReloginTaskRequest,
    background_tasks: BackgroundTasks,
):
    task_id = enqueue_chatgpt_relogin_task(
        req.account_ids,
        req.concurrency,
        background_tasks=background_tasks,
    )
    snapshot = _task_store.snapshot(task_id)
    return {
        "task_id": task_id,
        "count": int(snapshot["total"]),
        "concurrency": int(snapshot["meta"]["concurrency"]),
    }


@router.get("/{task_id}/retryable")
def get_retryable_task_bindings(task_id: str):
    snapshot = _get_task_snapshot(task_id)
    if str(snapshot.get("platform") or "") != "chatgpt":
        return {"task_id": task_id, "count": 0, "items": []}
    rows = _retryable_chatgpt_bindings(task_id)
    return {
        "task_id": task_id,
        "count": len(rows),
        "items": [_chatgpt_binding_public(row) for row in rows],
    }


@router.post("/{task_id}/retry-failed")
def retry_failed_task_bindings(
    task_id: str,
    background_tasks: BackgroundTasks,
    req: ChatGPTRetryFailedTaskRequest | None = None,
):
    snapshot = _get_task_snapshot(task_id)
    if str(snapshot.get("platform") or "") != "chatgpt":
        raise HTTPException(400, "仅 ChatGPT 登录接码任务支持绑定重试")
    if str(snapshot.get("status") or "") not in {"done", "failed", "stopped"}:
        raise HTTPException(409, "当前任务尚未结束，请等待完成后再重试")
    with _chatgpt_binding_db_lock:
        rows = _retryable_chatgpt_bindings(task_id)
        if not rows:
            raise HTTPException(400, "当前任务没有可重试的失败账号")
        request = _build_chatgpt_retry_request(
            rows,
            concurrency=req.concurrency if req is not None else 1,
        )
        row_ids = [int(row.id) for row in rows if row.id is not None]
        with Session(engine) as s:
            for row_id in row_ids:
                row = s.get(ChatGPTAttemptBindingModel, row_id)
                if row is not None:
                    row.status = "retrying"
                    row.updated_at = _utcnow()
                    s.add(row)
            s.commit()
    try:
        retry_task_id = enqueue_register_task(
            request,
            background_tasks=background_tasks,
            source="retry",
            meta={"parent_task_id": task_id},
        )
    except Exception:
        with _chatgpt_binding_db_lock:
            with Session(engine) as s:
                for row_id in row_ids:
                    row = s.get(ChatGPTAttemptBindingModel, row_id)
                    if row is not None and row.status == "retrying":
                        row.status = "failed"
                        row.updated_at = _utcnow()
                        s.add(row)
                s.commit()
        raise
    return {
        "task_id": retry_task_id,
        "parent_task_id": task_id,
        "retry_count": len(rows),
        "concurrency": request.concurrency,
    }


@router.post("/{task_id}/skip-current")
def skip_current_account(task_id: str):
    _finalize_orphan_tasks()
    _ensure_task_mutable(task_id)
    if not _task_store.exists(task_id):
        raise HTTPException(409, "任务已结束或服务已重启，无法跳过当前账号")
    control = _task_store.request_skip_current(task_id)
    _log(task_id, "收到手动跳过当前账号请求")
    return {"ok": True, "task_id": task_id, "control": control}


@router.post("/{task_id}/stop")
def stop_task(task_id: str):
    state, first_request, control = _task_store.request_stop_if_active(task_id)
    if state == "missing":
        # Persisted-only records need reconciliation. Live records deliberately
        # skip SQLite so a database stall cannot block emergency stop control.
        _finalize_orphan_tasks()
        _ensure_task_mutable(task_id)
        raise HTTPException(409, "任务已结束或服务已重启，无法停止")
    if state == "terminal":
        # A runner can reach an in-memory terminal snapshot and then wedge
        # while persisting that snapshot or releasing the automation gate.
        # It is still stoppable until the runner-completed Event is set.
        if not _arm_automation_stop_watchdog(task_id):
            raise HTTPException(409, "任务已结束，无法再执行控制操作")
        return {"ok": True, "task_id": task_id, "control": control}
    # Arm before log/SQLite persistence: if either is the wedged resource,
    # the independent watchdog must already be able to recycle the process.
    _arm_automation_stop_watchdog(task_id)
    if first_request:
        _log(task_id, "收到手动停止任务请求")
    _persist_task_snapshot_best_effort(task_id)
    return {"ok": True, "task_id": task_id, "control": control}


@router.get("/logs")
def get_logs(platform: str = None, page: int = 1, page_size: int = 50):
    with Session(engine) as s:
        q = select(TaskLog)
        if platform:
            q = q.where(TaskLog.platform == platform)
        q = q.order_by(TaskLog.id.desc())
        total = len(s.exec(q).all())
        items = s.exec(q.offset((page - 1) * page_size).limit(page_size)).all()
    return {"total": total, "items": items}


@router.post("/logs/batch-delete")
def batch_delete_logs(body: TaskLogBatchDeleteRequest):
    if not body.ids:
        raise HTTPException(400, "任务历史 ID 列表不能为空")

    unique_ids = list(dict.fromkeys(body.ids))
    if len(unique_ids) > 1000:
        raise HTTPException(400, "单次最多删除 1000 条任务历史")

    with Session(engine) as s:
        try:
            logs = s.exec(select(TaskLog).where(TaskLog.id.in_(unique_ids))).all()
            found_ids = {log.id for log in logs if log.id is not None}

            for log in logs:
                s.delete(log)

            s.commit()
            deleted_count = len(found_ids)
            not_found_ids = [log_id for log_id in unique_ids if log_id not in found_ids]
            logger.info("批量删除任务历史成功: %s 条", deleted_count)

            return {
                "deleted": deleted_count,
                "not_found": not_found_ids,
                "total_requested": len(unique_ids),
            }
        except Exception as e:
            s.rollback()
            logger.exception("批量删除任务历史失败")
            raise HTTPException(500, f"批量删除任务历史失败: {str(e)}")


@router.get("/{task_id}/logs/stream")
async def stream_logs(task_id: str, since: int = 0):
    """SSE 实时日志流"""
    await asyncio.to_thread(_finalize_orphan_tasks)
    await asyncio.to_thread(_ensure_task_exists, task_id)

    async def event_generator():
        sent = since
        previously_used_memory = False
        last_snapshot: dict = {}
        while True:
            memory_state = _task_store.log_snapshot_if_present(task_id)
            if memory_state is not None:
                logs, status, snapshot = memory_state
                previously_used_memory = True
                last_snapshot = snapshot
                await asyncio.to_thread(
                    _persist_task_snapshot_throttled,
                    task_id,
                )
            else:
                persisted = await asyncio.to_thread(_get_persisted_task, task_id)
                snapshot = persisted or last_snapshot
                if previously_used_memory:
                    # Persisted logs are a bounded tail and do not share the
                    # full in-memory stream index. Do not replay them.
                    logs = []
                else:
                    logs = snapshot.get("logs") or []
                persisted_status = str((persisted or {}).get("status") or "")
                status = (
                    persisted_status
                    if persisted_status in {"done", "failed", "stopped"}
                    else "stopped"
                )
            counters = {
                "success": int(snapshot.get("success") or 0),
                "registered": int(snapshot.get("registered") or 0),
                "total": int(snapshot.get("total") or 0),
            }
            while sent < len(logs):
                yield f"data: {json.dumps({'line': logs[sent], **counters})}\n\n"
                sent += 1
            if status in ("done", "failed", "stopped"):
                yield f"data: {json.dumps({'done': True, 'status': status, **counters})}\n\n"
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{task_id}")
def get_task(task_id: str):
    _finalize_orphan_tasks()
    return _get_task_snapshot(task_id)


@router.get("")
def list_tasks():
    _finalize_orphan_tasks()
    # 以 DB 为主返回，避免进程重启导致列表丢失
    return _list_persisted_tasks()


@router.delete("/{task_id}")
def delete_task(task_id: str):
    _finalize_orphan_tasks()
    snapshot = _get_task_snapshot(task_id)
    status = str(snapshot.get("status") or "")
    if status in {"pending", "running"}:
        raise HTTPException(409, "运行中的任务不允许删除，请先停止任务")
    with Session(engine) as s:
        row = s.get(TaskRunModel, task_id)
        if row is not None:
            s.delete(row)
            s.commit()
    _task_snapshot_last_persisted_at.pop(task_id, None)
    return {"ok": True, "task_id": task_id}
