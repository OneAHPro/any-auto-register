"""Upload ChatGPT credentials to the configured Codex2API instance."""

from __future__ import annotations

import logging
from typing import Any

from curl_cffi import requests as cffi_requests

logger = logging.getLogger(__name__)
MAX_ERROR_DETAIL_LENGTH = 200


def _get_config_value(key: str) -> str:
    try:
        from core.config_store import config_store

        return str(config_store.get(key, "") or "").strip()
    except Exception:
        return ""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _redact(value: Any, secrets: list[str]) -> str:
    text = _text(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def _bounded_redact(value: Any, secrets: list[str]) -> str:
    return _redact(value, secrets)[:MAX_ERROR_DETAIL_LENGTH]


def _response_detail(response, secrets: list[str]) -> str:
    try:
        payload = response.json()
    except Exception:
        return _bounded_redact(getattr(response, "text", ""), secrets)

    if isinstance(payload, dict):
        return _bounded_redact(
            payload.get("message")
            or payload.get("msg")
            or payload.get("error")
            or "",
            secrets,
        )
    return _bounded_redact(payload, secrets)


def _response_count(payload: dict[str, Any], key: str) -> int:
    try:
        return int(payload.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def upload_to_codex2api(account) -> tuple[bool, str]:
    """Upload one account using the official RT endpoint or AT fallback."""
    api_url = _get_config_value("codex2api_api_url").rstrip("/")
    admin_key = _get_config_value("codex2api_admin_key")
    refresh_token = _text(getattr(account, "refresh_token", ""))
    access_token = _text(getattr(account, "access_token", ""))
    email = _text(getattr(account, "email", "")) or "codex-account"

    if not api_url:
        return False, "Codex2API API URL 未配置"
    if not admin_key:
        return False, "Codex2API Admin Key 未配置"

    if refresh_token:
        credential_label = "Refresh Token"
        upload_url = f"{api_url}/api/admin/accounts"
        payload = {
            "name": email,
            "refresh_token": refresh_token,
        }
    elif access_token:
        credential_label = "Access Token"
        upload_url = f"{api_url}/api/admin/accounts/at"
        payload = {
            "name": email,
            "access_token": access_token,
        }
    else:
        return False, "账号缺少 Refresh Token 和 Access Token"

    secrets = [admin_key, refresh_token, access_token]
    try:
        response = cffi_requests.post(
            upload_url,
            headers={
                "X-Admin-Key": admin_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json=payload,
            proxies=None,
            verify=True,
            allow_redirects=False,
            timeout=30,
            impersonate="chrome110",
        )
    except Exception as exc:
        detail = _bounded_redact(exc, secrets)
        logger.error("Codex2API upload failed: %s", detail)
        return False, f"Codex2API 上传异常: {detail}"

    detail = _response_detail(response, secrets)
    if response.status_code not in (200, 201):
        if response.status_code in (401, 403):
            return False, "Codex2API Admin Key 无效或无权限"
        if response.status_code == 404:
            return False, f"Codex2API 管理接口不存在: {api_url}"
        suffix = f": {detail}" if detail else ""
        return False, f"Codex2API 上传失败: HTTP {response.status_code}{suffix}"

    try:
        data = response.json()
    except Exception:
        return False, "Codex2API 返回了无法解析的响应"
    if not isinstance(data, dict):
        return False, "Codex2API 返回了无法识别的响应"

    success = _response_count(data, "success")
    updated = _response_count(data, "updated")
    duplicate = _response_count(data, "duplicate")
    failed = _response_count(data, "failed")

    if success > 0:
        return True, f"上传成功（{credential_label}）"
    if updated > 0:
        return True, f"远端账号已更新（{credential_label}）"
    if duplicate > 0:
        return True, f"远端账号已存在（{credential_label}）"

    detail = _bounded_redact(
        data.get("message") or data.get("msg") or data.get("error") or "",
        secrets,
    )
    if failed > 0 or detail:
        return False, detail or "Codex2API 拒绝了账号"
    return False, "Codex2API 未确认账号已导入"
