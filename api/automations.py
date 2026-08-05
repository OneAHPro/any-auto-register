from fastapi import APIRouter, HTTPException

from services.chatgpt_auto_relogin import (
    get_chatgpt_auto_relogin_status,
    trigger_chatgpt_auto_relogin_now,
)


router = APIRouter(prefix="/automations", tags=["automations"])


@router.get("/chatgpt-relogin")
def get_chatgpt_relogin_automation_status():
    return get_chatgpt_auto_relogin_status()


_RUN_NOW_ERROR_DETAILS = {
    "disabled_by_config": "自动重登已关闭，请先在设置中开启",
    "no_eligible_accounts": "当前没有可执行自动认证的账号",
    "foreground_busy": "当前有手工 ChatGPT 任务正在等待或运行",
    "task_busy": "当前已有 ChatGPT 自动化任务正在运行",
    "enqueue_failed": "自动化任务启动失败，请稍后重试",
}


@router.post("/chatgpt-relogin/run-now")
def run_chatgpt_relogin_now():
    result = trigger_chatgpt_auto_relogin_now()
    if result.get("accepted"):
        return result

    reason = str(result.get("reason") or "enqueue_failed")
    raise HTTPException(
        status_code=503 if reason == "enqueue_failed" else 409,
        detail=_RUN_NOW_ERROR_DETAILS.get(
            reason,
            "当前暂时不能启动自动化任务，请稍后重试",
        ),
    )
