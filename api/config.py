import logging
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.config_store import config_store
from platforms.chatgpt.leadbee_capacity import parse_leadbee_capacity
from platforms.chatgpt.leadbee_open_api import LeadBeeAPIError, LeadBeeOpenAPIClient
from services.chatgpt_bark_alerts import BarkEndpointError, normalize_bark_endpoint
from services.mail_imports import (
    MailImportExecuteRequest,
    MailImportSnapshotRequest,
    mail_import_registry,
)

router = APIRouter(prefix="/config", tags=["config"])
logger = logging.getLogger(__name__)

QUOTA_ALERT_MIN_USD = Decimal("0.00")
QUOTA_ALERT_MAX_USD = Decimal("10000000.00")
USD_CENT = Decimal("0.01")
BARK_ENDPOINT_VALIDATION_MESSAGE = (
    "Bark 推送地址必须使用 https://api.day.app/ 官方地址"
)

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
_BARK_CONFIG_KEYS = {
    "bark_enabled",
    "bark_endpoint",
}
_LEADBEE_CONFIG_KEYS = {
    "leadbee_api_enabled",
    "leadbee_api_key",
    "leadbee_api_secret",
    "leadbee_api_product_id",
}
_LEADBEE_SECRET_KEYS = {"leadbee_api_key", "leadbee_api_secret"}
_LEADBEE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
_LEADBEE_SENSITIVE_HEADER_NAMES = {
    "authorization",
    "proxyauthorization",
    "apikey",
    "xapikey",
    "apisecret",
    "xapisecret",
    "authtoken",
    "xauthtoken",
    "accesstoken",
    "xaccesstoken",
    "refreshtoken",
    "cookie",
    "setcookie",
    "xsignature",
    "xtimestamp",
    "xnonce",
    "idempotencykey",
    "requestid",
    "xrequestid",
    "correlationid",
    "xcorrelationid",
    "traceparent",
    "tracestate",
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
    "codex2api_delete_on_account_remove_enabled",
    "team_manager_url",
    "team_manager_key",
    "codex_proxy_url",
    "codex_proxy_key",
    "codex_proxy_upload_type",
    "chatgpt_auto_relogin_enabled",
    "chatgpt_auto_relogin_interval_minutes",
    "chatgpt_auto_relogin_concurrency",
    "chatgpt_auto_relogin_alert_threshold",
    "chatgpt_auto_relogin_quota_alert_threshold_usd",
    "smtp_host",
    "smtp_port",
    "smtp_username",
    "smtp_password",
    "smtp_sender_email",
    "smtp_recipient_email",
    "smtp_use_ssl",
    "smtp_force_auth_login",
    "bark_enabled",
    "bark_endpoint",
    "leadbee_api_enabled",
    "leadbee_api_key",
    "leadbee_api_secret",
    "leadbee_api_product_id",
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


class LeadBeeTestRequest(BaseModel):
    data: dict = Field(default_factory=dict)


class AppleMailImportRequest(BaseModel):
    content: str
    filename: str = ""
    pool_dir: str = ""
    bind_to_config: bool = True


def _normalize_quota_alert_threshold(value: object) -> str:
    text = str(value if value is not None else "").strip() or "0"
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError):
        raise HTTPException(
            status_code=400,
            detail="Codex2API 剩余额度告警阈值必须是有效美元金额",
        ) from None
    if not parsed.is_finite():
        raise HTTPException(
            status_code=400,
            detail="Codex2API 剩余额度告警阈值必须是有效美元金额",
        )
    rounded = parsed.quantize(USD_CENT, rounding=ROUND_HALF_UP)
    if parsed != rounded:
        raise HTTPException(
            status_code=400,
            detail="Codex2API 剩余额度告警阈值最多保留两位小数",
        )
    if not QUOTA_ALERT_MIN_USD <= parsed <= QUOTA_ALERT_MAX_USD:
        raise HTTPException(
            status_code=400,
            detail=(
                "Codex2API 剩余额度告警阈值必须在 "
                "0.00 到 10000000.00 美元之间"
            ),
        )
    return f"{rounded:.2f}"


def _normalize_bark_endpoint(value: object) -> str:
    try:
        return normalize_bark_endpoint(value)
    except BarkEndpointError:
        raise HTTPException(
            status_code=400,
            detail=BARK_ENDPOINT_VALIDATION_MESSAGE,
        ) from None


def _normalize_enabled(value: object) -> str:
    return "1" if str(value or "").strip().lower() in {"1", "true", "yes", "on"} else "0"


def _normalize_leadbee_product_id(value: object) -> str:
    return str(value or "").strip()


def _leadbee_value(snapshot: dict, key: str) -> str:
    return str(snapshot.get(key, "") or "").strip()


def _leadbee_product_ids(
    payload: object, *, sensitive_values: tuple[str, ...] = ()
) -> list[str]:
    """Extract only bounded IDs from common product collection envelopes."""
    found: list[str] = []
    seen: set[str] = set()
    folded_sensitive_values = tuple(
        value.strip().casefold() for value in sensitive_values if value.strip()
    )

    def is_sensitive(candidate: str) -> bool:
        folded = candidate.casefold()
        if any(
            folded == sensitive
            or folded in sensitive
            or sensitive in folded
            for sensitive in folded_sensitive_values
        ):
            return True
        if re.fullmatch(r"\d{4,8}", candidate):
            return True
        digits_only = re.sub(r"[\s().-]", "", candidate)
        if digits_only.isdigit() and 10 <= len(digits_only) <= 15:
            return True
        if re.fullmatch(r"[0-9a-fA-F]{64}", candidate):
            return True
        normalized_candidate = re.sub(r"[^a-z0-9]", "", folded)
        return any(
            header_name in normalized_candidate
            for header_name in _LEADBEE_SENSITIVE_HEADER_NAMES
        )

    def add(value: object) -> bool:
        if not isinstance(value, str):
            return False
        candidate = value.strip()
        if (
            not candidate
            or len(candidate) > 128
            or not _LEADBEE_ID_RE.fullmatch(candidate)
            or is_sensitive(candidate)
        ):
            return False
        if candidate not in seen:
            seen.add(candidate)
            found.append(candidate)
        return True

    def walk(value: object, *, collection: bool = False) -> None:
        if isinstance(value, list):
            for item in value:
                walk(item, collection=True)
            return
        if not isinstance(value, dict):
            if collection:
                add(value)
            return
        if collection:
            for key in ("id", "product_id", "productId", "productID"):
                if key in value:
                    if add(value.get(key)):
                        break
        for key, child in value.items():
            if str(key).casefold() in {"products", "items", "list", "data", "results"}:
                walk(child, collection=True)
            elif isinstance(child, dict):
                walk(child, collection=collection)

    walk(payload)
    return found


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
        all_cfg["chatgpt_auto_relogin_concurrency"] = "3"
    if not str(all_cfg.get("chatgpt_auto_relogin_alert_threshold", "") or "").strip():
        all_cfg["chatgpt_auto_relogin_alert_threshold"] = "20"
    if not str(
        all_cfg.get("chatgpt_auto_relogin_quota_alert_threshold_usd", "")
        or ""
    ).strip():
        all_cfg["chatgpt_auto_relogin_quota_alert_threshold_usd"] = "0.00"
    if not str(
        all_cfg.get("codex2api_delete_on_account_remove_enabled", "") or ""
    ).strip():
        all_cfg["codex2api_delete_on_account_remove_enabled"] = "0"
    if not str(all_cfg.get("smtp_port", "") or "").strip():
        all_cfg["smtp_port"] = "587"
    if not str(all_cfg.get("smtp_use_ssl", "") or "").strip():
        all_cfg["smtp_use_ssl"] = "1"
    if not str(all_cfg.get("smtp_force_auth_login", "") or "").strip():
        all_cfg["smtp_force_auth_login"] = "0"
    if not str(all_cfg.get("bark_enabled", "") or "").strip():
        all_cfg["bark_enabled"] = "0"
    all_cfg["leadbee_api_enabled"] = _normalize_enabled(
        all_cfg.get("leadbee_api_enabled", "")
    )
    # SMTP 凭证只允许写入，不回传到前端或 API 调用方。
    all_cfg["smtp_password"] = ""
    # Bark 推送地址包含设备密钥，只允许写入，不回传。
    all_cfg["bark_endpoint"] = ""
    # LeadBee API credentials are write-only; product selection is public.
    all_cfg["leadbee_api_key"] = ""
    all_cfg["leadbee_api_secret"] = ""
    # 只返回已知 key，未设置的返回空字符串
    return {k: all_cfg.get(k, "") for k in CONFIG_KEYS}


@router.put("")
def update_config(body: ConfigUpdate):
    # 只允许更新已知 key
    try:
        stored_snapshot = dict(config_store.get_all() or {})
    except Exception:  # a validation-only update must not require an initialized DB
        stored_snapshot = {}
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
    if "leadbee_api_enabled" in safe:
        safe["leadbee_api_enabled"] = _normalize_enabled(safe["leadbee_api_enabled"])
    for credential_key in _LEADBEE_SECRET_KEYS:
        if credential_key in safe:
            value = str(safe.get(credential_key) or "").strip()
            if value:
                safe[credential_key] = value
            else:
                # Blank write-only fields mean "leave the persisted value alone".
                safe.pop(credential_key, None)
    if "leadbee_api_product_id" in safe:
        safe["leadbee_api_product_id"] = _normalize_leadbee_product_id(
            safe["leadbee_api_product_id"]
        )
    for bool_key in (
        "codex2api_delete_on_account_remove_enabled",
        "smtp_use_ssl",
        "smtp_force_auth_login",
        "bark_enabled",
    ):
        if bool_key in safe:
            enabled = str(safe.get(bool_key, "")).strip().lower()
            safe[bool_key] = "1" if enabled in {"1", "true", "yes", "on"} else "0"
    if "smtp_password" in safe and not str(safe.get("smtp_password") or ""):
        # 前端留空表示保留现有凭证，避免读取配置后误清空。
        safe.pop("smtp_password", None)
    if "bark_endpoint" in safe:
        if not str(safe.get("bark_endpoint") or "").strip():
            # 前端留空表示保留现有 Bark 设备密钥。
            safe.pop("bark_endpoint", None)
        else:
            safe["bark_endpoint"] = _normalize_bark_endpoint(
                safe.get("bark_endpoint")
            )
    if "chatgpt_auto_relogin_quota_alert_threshold_usd" in safe:
        safe["chatgpt_auto_relogin_quota_alert_threshold_usd"] = (
            _normalize_quota_alert_threshold(
                safe.get("chatgpt_auto_relogin_quota_alert_threshold_usd")
            )
        )
    for key, minimum, maximum, label in (
        ("chatgpt_auto_relogin_interval_minutes", 2, 1440, "鉴权巡检间隔"),
        ("chatgpt_auto_relogin_concurrency", 1, 3, "自动重登并发数"),
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

    # Validate the effective point-in-time configuration before writing.  A
    # blank credential in this request has already been removed above, so the
    # stored secret remains part of the effective snapshot.
    leadbee_snapshot = dict(stored_snapshot)
    leadbee_snapshot.update(safe)
    if _normalize_enabled(leadbee_snapshot.get("leadbee_api_enabled")) == "1":
        if not all(
            _leadbee_value(leadbee_snapshot, key)
            for key in ("leadbee_api_key", "leadbee_api_secret", "leadbee_api_product_id")
        ):
            raise HTTPException(
                status_code=400,
                detail="LeadBee API 启用时必须完整配置凭证和产品 ID",
            )
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


@router.post("/bark/test")
def test_bark_config(body: SMTPTestRequest):
    snapshot = {
        key: value
        for key, value in dict(config_store.get_all() or {}).items()
        if key in _BARK_CONFIG_KEYS
    }
    overrides = {
        key: value
        for key, value in dict(body.data or {}).items()
        if key in _BARK_CONFIG_KEYS
    }
    if "bark_enabled" in overrides:
        enabled = str(overrides.get("bark_enabled", "")).strip().lower()
        overrides["bark_enabled"] = (
            "1" if enabled in {"1", "true", "yes", "on"} else "0"
        )
    if "bark_endpoint" in overrides:
        if not str(overrides.get("bark_endpoint") or "").strip():
            overrides.pop("bark_endpoint", None)
        else:
            overrides["bark_endpoint"] = _normalize_bark_endpoint(
                overrides.get("bark_endpoint")
            )
    snapshot.update(overrides)

    from services.chatgpt_bark_alerts import send_bark_test_notification

    result = send_bark_test_notification(config=snapshot)
    if bool(result.get("sent")):
        return {"ok": True, "message": "测试 Bark 强提醒已发送"}

    reason = str(result.get("reason") or "send_failed")
    if reason == "bark_disabled":
        raise HTTPException(status_code=400, detail="请先启用 Bark 强提醒")
    if reason == "bark_not_configured":
        raise HTTPException(
            status_code=400,
            detail="请填写 Bark App 提供的完整推送地址",
        )
    if reason == "invalid_bark_endpoint":
        raise HTTPException(
            status_code=400,
            detail=BARK_ENDPOINT_VALIDATION_MESSAGE,
        )
    error_type = str(result.get("error_type") or "BarkError")[:80]
    raise HTTPException(
        status_code=502,
        detail=f"Bark 测试通知发送失败（{error_type}）",
    )


@router.post("/leadbee/test")
def test_leadbee_config(body: LeadBeeTestRequest):
    stored = {
        key: value
        for key, value in dict(config_store.get_all() or {}).items()
        if key in _LEADBEE_CONFIG_KEYS
    }
    overrides = {
        key: value
        for key, value in dict(body.data or {}).items()
        if key in _LEADBEE_CONFIG_KEYS
    }
    for credential_key in _LEADBEE_SECRET_KEYS:
        if credential_key in overrides:
            value = str(overrides.get(credential_key) or "").strip()
            if value:
                overrides[credential_key] = value
            else:
                overrides.pop(credential_key, None)
    if "leadbee_api_product_id" in overrides:
        overrides["leadbee_api_product_id"] = _normalize_leadbee_product_id(
            overrides["leadbee_api_product_id"]
        )
    merged = dict(stored)
    merged.update(overrides)
    api_key = _leadbee_value(merged, "leadbee_api_key")
    api_secret = _leadbee_value(merged, "leadbee_api_secret")
    product_id = _leadbee_value(merged, "leadbee_api_product_id")
    if not api_key or not api_secret or not product_id:
        raise HTTPException(
            status_code=400,
            detail="请完整填写 LeadBee API 凭证和产品 ID",
        )

    # Deliberately omit base_url: the production connection test always uses
    # the official endpoint baked into LeadBeeOpenAPIClient.
    try:
        client = LeadBeeOpenAPIClient(api_key=api_key, api_secret=api_secret)
        products_payload = client.get_products()
        balance_payload = client.get_balance()
        product_ids = _leadbee_product_ids(
            products_payload,
            sensitive_values=(api_key, api_secret),
        )
        capacity = parse_leadbee_capacity(
            products_payload,
            balance_payload,
            product_id=product_id,
        ).public_dict()
        return {
            "ok": True,
            "product_ids": product_ids,
            **capacity,
            "configured_product_available": bool(
                product_id in product_ids
                and capacity["configured_product_available"]
            ),
        }
    except LeadBeeAPIError:
        logger.warning("LeadBee connection test failed (provider_error)")
        raise HTTPException(status_code=502, detail="LeadBee 连接测试失败") from None
    except Exception:
        # Provider exception text may contain credentials or verification data;
        # keep logs deliberately generic as well as the HTTP response.
        logger.warning("LeadBee connection test failed (provider_error)")
        raise HTTPException(status_code=502, detail="LeadBee 连接测试失败") from None


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
