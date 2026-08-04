import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from core.config_store import config_store
from services.mail_imports import MailImportExecuteRequest, MailImportSnapshotRequest, mail_import_registry

router = APIRouter(prefix="/config", tags=["config"])
logger = logging.getLogger(__name__)

_CHATGPT_AUTO_RELOGIN_CONFIG_KEYS = {
    "chatgpt_auto_relogin_enabled",
    "chatgpt_auto_relogin_interval_minutes",
    "chatgpt_auto_relogin_concurrency",
}
_SMTP_CONFIG_KEYS = {
    "smtp_host",
    "smtp_port",
    "smtp_username",
    "smtp_password",
    "smtp_sender_email",
    "smtp_recipient_email",
    "smtp_use_ssl",
    "smtp_force_auth_login",
}

CONFIG_KEYS = [
    "email_domain_rule_enabled",
    "email_domain_level_count",
    "laoudo_auth",
    "laoudo_email",
    "laoudo_account_id",
    "yescaptcha_key",
    "twocaptcha_key",
    "default_executor",
    "default_captcha_solver",
    "duckmail_api_url",
    "duckmail_provider_url",
    "duckmail_bearer",
    "duckmail_domain",
    "duckmail_api_key",
    "freemail_api_url",
    "freemail_admin_token",
    "freemail_username",
    "freemail_password",
    "freemail_domain",
    "moemail_api_url",
    "moemail_api_key",
    "skymail_api_base",
    "skymail_token",
    "skymail_domain",
    "cloudmail_api_base",
    "cloudmail_admin_email",
    "cloudmail_admin_password",
    "cloudmail_domain",
    "cloudmail_subdomain",
    "cloudmail_timeout",
    "mail_provider",
    "outlook_backend",
    "mailbox_otp_timeout_seconds",
    "maliapi_base_url",
    "maliapi_api_key",
    "maliapi_domain",
    "maliapi_auto_domain_strategy",
    "applemail_base_url",
    "applemail_pool_dir",
    "applemail_pool_file",
    "applemail_mailboxes",
    "gptmail_base_url",
    "gptmail_api_key",
    "gptmail_domain",
    "opentrashmail_api_url",
    "opentrashmail_domain",
    "opentrashmail_password",
    "cfworker_api_url",
    "cfworker_admin_token",
    "cfworker_custom_auth",
    "cfworker_domain",
    "cfworker_domains",
    "cfworker_enabled_domains",
    "cfworker_subdomain",
    "cfworker_random_subdomain",
    "cfworker_random_name_subdomain",
    "cfworker_fingerprint",
    "smstome_cookie",
    "smstome_country_slugs",
    "smstome_phone_attempts",
    "smstome_otp_timeout_seconds",
    "smstome_poll_interval_seconds",
    "smstome_sync_max_pages_per_country",
    "luckmail_base_url",
    "luckmail_api_key",
    "luckmail_email_type",
    "luckmail_domain",
    "cpa_enabled",
    "cpa_api_url",
    "cpa_api_key",
    "cpa_cleanup_enabled",
    "cpa_cleanup_interval_minutes",
    "cpa_cleanup_threshold",
    "cpa_cleanup_concurrency",
    "cpa_cleanup_register_delay_seconds",
    "sub2api_enabled",
    "sub2api_api_url",
    "sub2api_api_key",
    "sub2api_group_ids",
    "codex2api_enabled",
    "codex2api_api_url",
    "codex2api_admin_key",
    "team_manager_url",
    "team_manager_key",
    "codex_proxy_url",
    "codex_proxy_key",
    "codex_proxy_upload_type",
    "chatgpt_auto_relogin_enabled",
    "chatgpt_auto_relogin_interval_minutes",
    "chatgpt_auto_relogin_concurrency",
    "chatgpt_auto_relogin_alert_threshold",
    "smtp_host",
    "smtp_port",
    "smtp_username",
    "smtp_password",
    "smtp_sender_email",
    "smtp_recipient_email",
    "smtp_use_ssl",
    "smtp_force_auth_login",
    "cliproxyapi_base_url",
    "cliproxyapi_management_key",
    "grok2api_url",
    "grok2api_app_key",
    "grok2api_pool",
    "grok2api_quota",
    "kiro_manager_path",
    "kiro_manager_exe",
    "external_apps_update_mode",
    "contribution_enabled",
    "contribution_server_url",
    "contribution_key",
    "contribution_mode",
    "custom_contribution_url",
    "custom_contribution_token",
]


class ConfigUpdate(BaseModel):
    data: dict


class SMTPTestRequest(BaseModel):
    data: dict = Field(default_factory=dict)


class AppleMailImportRequest(BaseModel):
    content: str
    filename: str = ""
    pool_dir: str = ""
    bind_to_config: bool = True


@router.get("")
def get_config():
    all_cfg = config_store.get_all()
    if all_cfg.get("mail_provider") == "outlook":
        all_cfg["mail_provider"] = "microsoft"
    if not all_cfg.get("mail_provider"):
        all_cfg["mail_provider"] = "luckmail"
    if not all_cfg.get("applemail_base_url"):
        all_cfg["applemail_base_url"] = "https://www.appleemail.top"
    if not all_cfg.get("applemail_pool_dir"):
        all_cfg["applemail_pool_dir"] = "mail"
    if not all_cfg.get("applemail_mailboxes"):
        all_cfg["applemail_mailboxes"] = "INBOX,Junk"
    if not all_cfg.get("outlook_backend"):
        all_cfg["outlook_backend"] = "graph"
    if not all_cfg.get("gptmail_base_url"):
        all_cfg["gptmail_base_url"] = "https://mail.chatgpt.org.uk"
    if not all_cfg.get("luckmail_base_url"):
        all_cfg["luckmail_base_url"] = "https://mails.luckyous.com/"
    if not str(all_cfg.get("contribution_enabled", "") or "").strip():
        all_cfg["contribution_enabled"] = "0"
    if not all_cfg.get("contribution_server_url"):
        all_cfg["contribution_server_url"] = "http://new.xem8k5.top:7317/"
    if not all_cfg.get("contribution_mode"):
        all_cfg["contribution_mode"] = "codex"
    if not all_cfg.get("custom_contribution_url"):
        all_cfg["custom_contribution_url"] = "http://127.0.0.1:5000"
    if not all_cfg.get("external_apps_update_mode"):
        all_cfg["external_apps_update_mode"] = "tag"
    if not str(all_cfg.get("email_domain_rule_enabled", "") or "").strip():
        all_cfg["email_domain_rule_enabled"] = "0"
    if not str(all_cfg.get("email_domain_level_count", "") or "").strip():
        all_cfg["email_domain_level_count"] = "2"
    if not str(all_cfg.get("chatgpt_auto_relogin_enabled", "") or "").strip():
        all_cfg["chatgpt_auto_relogin_enabled"] = "0"
    if not str(all_cfg.get("chatgpt_auto_relogin_interval_minutes", "") or "").strip():
        all_cfg["chatgpt_auto_relogin_interval_minutes"] = "2"
    if not str(all_cfg.get("chatgpt_auto_relogin_concurrency", "") or "").strip():
        all_cfg["chatgpt_auto_relogin_concurrency"] = "10"
    if not str(all_cfg.get("chatgpt_auto_relogin_alert_threshold", "") or "").strip():
        all_cfg["chatgpt_auto_relogin_alert_threshold"] = "20"
    if not str(all_cfg.get("smtp_port", "") or "").strip():
        all_cfg["smtp_port"] = "587"
    if not str(all_cfg.get("smtp_use_ssl", "") or "").strip():
        all_cfg["smtp_use_ssl"] = "1"
    if not str(all_cfg.get("smtp_force_auth_login", "") or "").strip():
        all_cfg["smtp_force_auth_login"] = "0"
    # SMTP 凭证只允许写入，不回传到前端或 API 调用方。
    all_cfg["smtp_password"] = ""
    # 只返回已知 key，未设置的返回空字符串
    return {k: all_cfg.get(k, "") for k in CONFIG_KEYS}


@router.put("")
def update_config(body: ConfigUpdate):
    # 只允许更新已知 key
    safe = {k: v for k, v in body.data.items() if k in CONFIG_KEYS}
    if safe.get("mail_provider") == "outlook":
        safe["mail_provider"] = "microsoft"
    if "email_domain_rule_enabled" in safe:
        enabled = str(safe.get("email_domain_rule_enabled", "")).strip().lower()
        safe["email_domain_rule_enabled"] = (
            "1" if enabled in {"1", "true", "yes", "on"} else "0"
        )
    if "email_domain_level_count" in safe:
        try:
            level_count = int(str(safe.get("email_domain_level_count", "")).strip())
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="域名级数必须是整数") from exc
        if level_count < 2:
            raise HTTPException(status_code=400, detail="域名级数不能小于 2")
        safe["email_domain_level_count"] = str(level_count)
    if "chatgpt_auto_relogin_enabled" in safe:
        enabled = str(safe.get("chatgpt_auto_relogin_enabled", "")).strip().lower()
        safe["chatgpt_auto_relogin_enabled"] = (
            "1" if enabled in {"1", "true", "yes", "on"} else "0"
        )
    for bool_key in ("smtp_use_ssl", "smtp_force_auth_login"):
        if bool_key in safe:
            enabled = str(safe.get(bool_key, "")).strip().lower()
            safe[bool_key] = "1" if enabled in {"1", "true", "yes", "on"} else "0"
    if "smtp_password" in safe and not str(safe.get("smtp_password") or ""):
        # 前端留空表示保留现有凭证，避免读取配置后误清空。
        safe.pop("smtp_password", None)
    for key, minimum, maximum, label in (
        ("chatgpt_auto_relogin_interval_minutes", 2, 1440, "鉴权巡检间隔"),
        ("chatgpt_auto_relogin_concurrency", 1, 10, "自动重登并发数"),
        ("chatgpt_auto_relogin_alert_threshold", 1, 10000, "重登失败告警阈值"),
        ("smtp_port", 1, 65535, "SMTP 端口"),
    ):
        if key not in safe:
            continue
        try:
            value = int(str(safe.get(key, "")).strip())
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"{label}必须是整数") from exc
        if not minimum <= value <= maximum:
            raise HTTPException(
                status_code=400,
                detail=f"{label}必须在 {minimum} 到 {maximum} 之间",
            )
        safe[key] = str(value)
    config_store.set_many(safe)
    if _CHATGPT_AUTO_RELOGIN_CONFIG_KEYS.intersection(safe):
        try:
            from services.chatgpt_auto_relogin import tick_chatgpt_auto_relogin

            # Apply stop/enable/interval changes immediately instead of waiting
            # for the scheduler's next periodic tick.
            tick_chatgpt_auto_relogin(store=config_store)
        except Exception:
            logger.exception("ChatGPT 自动重登配置即时协调失败，将由调度器重试")
    return {"ok": True, "updated": list(safe.keys())}


@router.post("/smtp/test")
def test_smtp_config(body: SMTPTestRequest):
    snapshot = {
        key: value
        for key, value in dict(config_store.get_all() or {}).items()
        if key in _SMTP_CONFIG_KEYS
    }
    overrides = {
        key: value
        for key, value in dict(body.data or {}).items()
        if key in _SMTP_CONFIG_KEYS
    }
    if not str(overrides.get("smtp_password") or ""):
        overrides.pop("smtp_password", None)
    for bool_key in ("smtp_use_ssl", "smtp_force_auth_login"):
        if bool_key in overrides:
            enabled = str(overrides.get(bool_key, "")).strip().lower()
            overrides[bool_key] = (
                "1" if enabled in {"1", "true", "yes", "on"} else "0"
            )
    if "smtp_port" in overrides:
        try:
            port = int(str(overrides.get("smtp_port", "")).strip())
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="SMTP 端口必须是整数") from exc
        if not 1 <= port <= 65535:
            raise HTTPException(
                status_code=400,
                detail="SMTP 端口必须在 1 到 65535 之间",
            )
        overrides["smtp_port"] = str(port)
    snapshot.update(overrides)

    from services.chatgpt_auto_relogin_alerts import send_smtp_test_email

    result = send_smtp_test_email(config=snapshot)
    if bool(result.get("sent")):
        return {
            "ok": True,
            "message": "测试邮件已发送",
            "recipient_count": int(result.get("recipient_count") or 0),
        }
    if result.get("reason") == "smtp_not_configured":
        raise HTTPException(
            status_code=400,
            detail="请完整填写 SMTP 服务器、发送者和接收邮箱",
        )
    error_type = str(result.get("error_type") or "SMTPError")[:80]
    raise HTTPException(
        status_code=502,
        detail=f"SMTP 测试邮件发送失败（{error_type}）",
    )


@router.post("/applemail/import")
def import_applemail_pool(body: AppleMailImportRequest):
    try:
        strategy = mail_import_registry.get("applemail")
        result = strategy.execute(
            MailImportExecuteRequest(
                type="applemail",
                content=body.content,
                filename=body.filename,
                pool_dir=body.pool_dir,
                bind_to_config=body.bind_to_config,
            )
        )
        snapshot = result.snapshot.model_dump()
        return {
            "filename": snapshot["filename"],
            "path": result.meta.get("path", ""),
            "count": snapshot["count"],
            "pool_dir": snapshot["pool_dir"],
            "bound_to_config": bool(result.meta.get("bound_to_config")),
            "items": snapshot["items"],
            "truncated": snapshot["truncated"],
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/applemail/pool")
def get_applemail_pool_snapshot(
    pool_dir: str = "",
    pool_file: str = "",
):
    try:
        strategy = mail_import_registry.get("applemail")
        snapshot = strategy.get_snapshot(
            MailImportSnapshotRequest(
                type="applemail",
                pool_dir=pool_dir,
                pool_file=pool_file,
            )
        )
        return snapshot.model_dump()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
