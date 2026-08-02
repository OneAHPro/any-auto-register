"""Upload ChatGPT credentials to the configured Codex2API instance."""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from curl_cffi import CurlMime
from curl_cffi import requests as cffi_requests

logger = logging.getLogger(__name__)
MAX_ERROR_DETAIL_LENGTH = 200
USAGE_LIMIT_VERIFICATION_NOTE = "凭据已同步并通过鉴权，当前用量已达上限"


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


def _sanitized_target_url(value: Any, secrets: list[str]) -> str:
    try:
        parsed = urlsplit(_text(value))
        hostname = parsed.hostname
        if not parsed.scheme or not hostname:
            raise ValueError("invalid target URL")
        host = f"[{hostname}]" if ":" in hostname else hostname
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        target = urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except (TypeError, ValueError):
        target = "已配置地址（格式无效）"
    return _bounded_redact(target, secrets)


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


def _parse_response_payload(response) -> dict[str, Any] | None:
    """Parse either the JSON response or the final event of an SSE response."""
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        return payload

    complete_event: dict[str, Any] | None = None
    for raw_line in _text(getattr(response, "text", "")).splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        event_data = line[5:].strip()
        if not event_data or event_data == "[DONE]":
            continue
        try:
            event = json.loads(event_data)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        if _text(event.get("type")).lower() == "complete":
            complete_event = event
    return complete_event


def _identity_import_payload(
    account,
    *,
    email: str,
    refresh_token: str,
    access_token: str,
) -> dict[str, str]:
    payload = {
        "name": email,
        "email": email,
        "refresh_token": refresh_token,
        "access_token": access_token,
    }
    for key in (
        "id_token",
        "session_token",
        "account_id",
        "workspace_id",
        "user_id",
        "client_id",
    ):
        value = _text(getattr(account, key, ""))
        if value:
            payload[key] = value

    # The selected workspace is the value Codex requests need in
    # Chatgpt-Account-Id, so keep it authoritative for the importer.  After a
    # refresh, Codex2API may expose either this workspace or the token's
    # ``chatgpt_user_id`` in its account list; both are matched below.
    if payload.get("workspace_id"):
        payload["account_id"] = payload["workspace_id"]
    return payload


def _jwt_claims_no_verify(token: Any) -> dict[str, Any]:
    raw = _text(token)
    if raw.count(".") < 2:
        return {}
    encoded = raw.split(".", 2)[1]
    padding = "=" * ((4 - len(encoded) % 4) % 4)
    try:
        decoded = base64.urlsafe_b64decode((encoded + padding).encode("ascii"))
        payload = json.loads(decoded.decode("utf-8"))
    except (TypeError, ValueError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _identity_aliases(payload: dict[str, str]) -> set[str]:
    # These unverified claims are matching aliases only.  A candidate must
    # still match the same email and pass Codex2API's remote credential test.
    aliases: set[str] = set()
    claim_keys = (
        "chatgpt_account_id",
        "chatgpt_user_id",
        "workspace_id",
        "account_id",
        "user_id",
    )

    def add(value: Any) -> None:
        normalized = _text(value).lower()
        if normalized:
            aliases.add(normalized)

    for key in ("workspace_id", "account_id", "user_id"):
        add(payload.get(key))
    for token_key in ("id_token", "access_token"):
        claims = _jwt_claims_no_verify(payload.get(token_key))
        for key in claim_keys:
            add(claims.get(key))
        auth_claims = claims.get("https://api.openai.com/auth")
        if isinstance(auth_claims, dict):
            for key in claim_keys:
                add(auth_claims.get(key))
    return aliases


def _admin_read_kwargs(admin_key: str, *, accept: str, timeout: int) -> dict[str, Any]:
    return {
        "headers": {
            "X-Admin-Key": admin_key,
            "Accept": accept,
        },
        "proxies": None,
        "verify": True,
        "allow_redirects": False,
        "timeout": timeout,
        "impersonate": "chrome110",
    }


def _remote_accounts(
    api_url: str,
    admin_key: str,
    secrets: list[str],
) -> tuple[list[dict[str, Any]] | None, str]:
    try:
        response = cffi_requests.get(
            f"{api_url}/api/admin/accounts?channel=codex",
            **_admin_read_kwargs(
                admin_key,
                accept="application/json",
                timeout=15,
            ),
        )
    except Exception as exc:
        return None, f"读取远端账号清单异常: {_bounded_redact(exc, secrets)}"
    if response.status_code not in (200, 201):
        detail = _response_detail(response, secrets)
        suffix = f": {detail}" if detail else ""
        return None, f"读取远端账号清单失败: HTTP {response.status_code}{suffix}"
    try:
        payload = response.json()
    except Exception:
        return None, "Codex2API 账号清单响应无法解析"
    raw_accounts = payload.get("accounts") if isinstance(payload, dict) else payload
    if not isinstance(raw_accounts, list):
        return None, "Codex2API 账号清单格式无法识别"
    return [row for row in raw_accounts if isinstance(row, dict)], ""


def _remote_row_id(row: dict[str, Any]) -> int:
    try:
        return int(row.get("id") or 0)
    except (TypeError, ValueError):
        return 0


def _remote_row_identity(row: dict[str, Any]) -> str:
    return _text(
        row.get("chatgpt_account_id")
        or row.get("workspace_id")
        or row.get("account_id")
    )


def _remote_row_has_invalid_credentials(row: dict[str, Any]) -> bool:
    detail = " ".join(
        _text(row.get(key)).lower()
        for key in (
            "status",
            "error",
            "error_message",
            "last_error",
            "message",
        )
    )
    return any(
        marker in detail
        for marker in (
            "token_invalidated",
            "token invalidated",
            "invalid token",
            "unauthorized",
            "authentication token has been invalidated",
        )
    )


def _matching_remote_rows(
    rows: list[dict[str, Any]],
    *,
    email: str,
) -> list[dict[str, Any]]:
    normalized_email = email.lower()
    matches = []
    for row in rows:
        row_email = _text(row.get("email")).lower()
        row_name = _text(row.get("name")).lower()
        if normalized_email in {row_email, row_name}:
            matches.append(row)
    return matches


def _pick_remote_target(
    rows: list[dict[str, Any]],
    *,
    email: str,
    identity_aliases: set[str],
    before_ids: set[int],
    prefer_new: bool,
) -> tuple[dict[str, Any] | None, str]:
    candidates = [
        row
        for row in _matching_remote_rows(rows, email=email)
        if _remote_row_id(row) > 0
    ]
    if prefer_new:
        candidates = [
            row for row in candidates if _remote_row_id(row) not in before_ids
        ]
        if not candidates:
            return None, "Codex2API 导入成功，但未找到新导入的远端账号"
    if identity_aliases:
        candidates = [
            row
            for row in candidates
            if _remote_row_identity(row).lower() in identity_aliases
        ]
        if not candidates:
            return None, "Codex2API 导入后未找到匹配账号身份的远端账号"
    if len(candidates) > 1:
        return None, "Codex2API 远端账号身份不唯一，已停止自动覆盖"
    if not candidates:
        return None, "Codex2API 导入后未找到对应的远端账号"
    return max(candidates, key=_remote_row_id), ""


def _is_auth_failure_result(value: Any) -> bool:
    if isinstance(value, dict):
        for key in ("type", "code", "error_code"):
            if _text(value.get(key)).lower() in {
                "token_invalidated",
                "invalid_token",
                "unauthorized",
            }:
                return True
        for key in ("status", "status_code"):
            try:
                if int(value.get(key) or 0) in (401, 403):
                    return True
            except (TypeError, ValueError):
                pass
        return any(_is_auth_failure_result(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_is_auth_failure_result(item) for item in value)
    normalized = _text(value).lower()
    return any(
        marker in normalized
        for marker in (
            "token_invalidated",
            "token invalidated",
            "invalid_token",
            "invalid token",
            "unauthorized",
            "authentication token has been invalidated",
        )
    )


def _is_usage_limit_result(value: Any) -> bool:
    if isinstance(value, dict):
        for key in ("type", "code", "error_code"):
            if _text(value.get(key)).lower() == "usage_limit_reached":
                return True
        return any(_is_usage_limit_result(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_is_usage_limit_result(item) for item in value)
    normalized = _text(value).lower()
    return bool(
        normalized == "usage_limit_reached"
        or normalized == "the usage limit has been reached"
        or normalized.startswith("the usage limit has been reached\n")
    )


def _verify_remote_account(
    api_url: str,
    admin_key: str,
    remote_id: int,
    secrets: list[str],
) -> tuple[bool, str]:
    try:
        response = cffi_requests.get(
            f"{api_url}/api/admin/accounts/{remote_id}/test",
            **_admin_read_kwargs(
                admin_key,
                accept="text/event-stream, application/json",
                timeout=45,
            ),
        )
    except Exception as exc:
        return False, f"远端账号测试异常: {_bounded_redact(exc, secrets)}"
    if response.status_code not in (200, 201):
        detail = _response_detail(response, secrets)
        suffix = f": {detail}" if detail else ""
        return False, f"远端账号测试失败: HTTP {response.status_code}{suffix}"

    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        if payload.get("success") is True:
            return True, ""
        if _is_auth_failure_result(payload):
            detail = _bounded_redact(
                payload.get("error") or payload.get("message") or "",
                secrets,
            )
            return False, detail or "Codex2API 凭据鉴权失败"
        if _is_usage_limit_result(payload):
            return True, USAGE_LIMIT_VERIFICATION_NOTE
        detail = _bounded_redact(
            payload.get("error") or payload.get("message") or "",
            secrets,
        )
        return False, detail or "Codex2API 未确认账号测试成功"

    last_error = ""
    for raw_line in _text(getattr(response, "text", "")).splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        try:
            event = json.loads(line[5:].strip())
        except (TypeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        event_type = _text(event.get("type")).lower()
        if event_type == "test_complete" and event.get("success") is True:
            return True, ""
        if event_type == "error":
            if _is_auth_failure_result(event):
                detail = _bounded_redact(
                    event.get("error") or event.get("message") or "",
                    secrets,
                )
                return False, detail or "Codex2API 凭据鉴权失败"
            if _is_usage_limit_result(event):
                return True, USAGE_LIMIT_VERIFICATION_NOTE
            last_error = _bounded_redact(
                event.get("error") or event.get("message") or "",
                secrets,
            )
    return False, last_error or "Codex2API 未确认账号测试成功"


def _delete_remote_account(
    api_url: str,
    admin_key: str,
    remote_id: int,
    secrets: list[str],
) -> tuple[bool, str]:
    try:
        response = cffi_requests.delete(
            f"{api_url}/api/admin/accounts/{remote_id}",
            **_admin_read_kwargs(
                admin_key,
                accept="application/json",
                timeout=15,
            ),
        )
    except Exception as exc:
        return False, f"清理远端旧账号异常: {_bounded_redact(exc, secrets)}"
    if response.status_code not in (200, 201, 204):
        detail = _response_detail(response, secrets)
        suffix = f": {detail}" if detail else ""
        return False, f"清理远端旧账号失败: HTTP {response.status_code}{suffix}"
    return True, ""


def _confirm_identity_replacement(
    *,
    api_url: str,
    admin_key: str,
    secrets: list[str],
    email: str,
    payload: dict[str, str],
    before_accounts: list[dict[str, Any]],
    imported_new: bool,
    updated: bool,
    duplicate: bool,
    credential_label: str,
) -> tuple[bool, str]:
    after_accounts, list_error = _remote_accounts(api_url, admin_key, secrets)
    if after_accounts is None:
        return False, list_error
    before_ids = {_remote_row_id(row) for row in before_accounts}
    identity_aliases = _identity_aliases(payload)
    target, target_error = _pick_remote_target(
        after_accounts,
        email=email,
        identity_aliases=identity_aliases,
        before_ids=before_ids,
        prefer_new=imported_new,
    )
    if target is None:
        return False, target_error
    target_id = _remote_row_id(target)
    verified, verify_detail = _verify_remote_account(
        api_url,
        admin_key,
        target_id,
        secrets,
    )
    if not verified:
        if target_id not in before_ids:
            deleted, delete_error = _delete_remote_account(
                api_url,
                admin_key,
                target_id,
                secrets,
            )
            if not deleted:
                return (
                    False,
                    f"Codex2API 新凭据测试失败: {verify_detail}；{delete_error}",
                )
        return False, f"Codex2API 新凭据测试失败: {verify_detail}"

    stale_rows = _matching_remote_rows(
        before_accounts,
        email=email,
    )
    removed = 0
    for stale in stale_rows:
        stale_id = _remote_row_id(stale)
        if stale_id <= 0 or stale_id == target_id:
            continue
        # Without a stable workspace identity, an email match is not enough to
        # prove that two Codex rows represent the same account.
        if not identity_aliases:
            continue
        stale_identity = _remote_row_identity(stale).lower()
        if stale_identity:
            if stale_identity not in identity_aliases:
                continue
        elif not _remote_row_has_invalid_credentials(stale):
            continue
        deleted, delete_error = _delete_remote_account(
            api_url,
            admin_key,
            stale_id,
            secrets,
        )
        if not deleted:
            return False, delete_error
        removed += 1

    if verify_detail:
        if updated:
            action = "远端账号已覆盖更新"
        elif duplicate:
            action = "远端账号凭据已一致"
        elif removed:
            action = f"远端账号已替换 {removed} 条旧失效记录"
        else:
            action = "远端账号已新增"
        return True, f"{action}；{verify_detail}（{credential_label}）"
    if updated:
        return True, f"远端账号已覆盖更新并验证通过（{credential_label}）"
    if duplicate:
        return True, f"远端账号凭据已一致并验证通过（{credential_label}）"
    if removed:
        return True, f"远端账号已验证，并替换 {removed} 条旧失效记录（{credential_label}）"
    return True, f"远端账号已新增并验证通过（{credential_label}）"


def upload_to_codex2api(
    account,
    *,
    replace_existing: bool = False,
) -> tuple[bool, str]:
    """Upload one account, optionally replacing credentials by OAuth identity."""
    api_url = _get_config_value("codex2api_api_url").rstrip("/")
    admin_key = _get_config_value("codex2api_admin_key")
    refresh_token = _text(getattr(account, "refresh_token", ""))
    access_token = _text(getattr(account, "access_token", ""))
    email = _text(getattr(account, "email", "")) or "codex-account"

    if not api_url:
        return False, "Codex2API API URL 未配置"
    if not admin_key:
        return False, "Codex2API Admin Key 未配置"

    if replace_existing:
        if not refresh_token or not access_token:
            return False, "覆盖更新需要新的 Refresh Token 和 Access Token"
        credential_label = "Refresh Token + Access Token"
        upload_url = f"{api_url}/api/admin/accounts/import"
        payload = _identity_import_payload(
            account,
            email=email,
            refresh_token=refresh_token,
            access_token=access_token,
        )
    elif refresh_token:
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

    secrets = [
        admin_key,
        refresh_token,
        access_token,
        *(
            _text(getattr(account, key, ""))
            for key in (
                "id_token",
                "session_token",
                "account_id",
                "workspace_id",
                "user_id",
            )
        ),
    ]
    before_accounts: list[dict[str, Any]] = []
    if replace_existing:
        remote_accounts, list_error = _remote_accounts(
            api_url,
            admin_key,
            secrets,
        )
        if remote_accounts is None:
            return False, list_error
        before_accounts = remote_accounts
    request_kwargs: dict[str, Any] = {
        "headers": {
            "X-Admin-Key": admin_key,
            "Accept": (
                "text/event-stream, application/json"
                if replace_existing
                else "application/json"
            ),
        },
        "proxies": None,
        "verify": True,
        "allow_redirects": False,
        "timeout": 30,
        "impersonate": "chrome110",
    }
    multipart_payload: bytes | None = None
    if replace_existing:
        multipart_payload = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    else:
        request_kwargs["headers"]["Content-Type"] = "application/json"
        request_kwargs["json"] = payload
    max_tls_attempts = 3
    for attempt in range(max_tls_attempts):
        mime: CurlMime | None = None
        try:
            attempt_kwargs = dict(request_kwargs)
            if multipart_payload is not None:
                mime = CurlMime()
                mime.addpart(name="format", data=b"json")
                mime.addpart(
                    name="file",
                    data=multipart_payload,
                    filename="chatgpt-account.json",
                    content_type="application/json",
                )
                attempt_kwargs["multipart"] = mime
            response = cffi_requests.post(upload_url, **attempt_kwargs)
            break
        except Exception as exc:
            retryable_tls_error = bool(
                isinstance(exc, cffi_requests.exceptions.SSLError)
                and type(exc).__module__.startswith("curl_cffi.")
                and getattr(exc, "code", None) == 35
            )
            if retryable_tls_error and attempt < max_tls_attempts - 1:
                logger.warning(
                    "Codex2API TLS handshake failed; retrying upload (%d/%d)",
                    attempt + 2,
                    max_tls_attempts,
                )
                time.sleep(0.25 * (attempt + 1))
                continue
            detail = _bounded_redact(exc, secrets)
            logger.error("Codex2API upload failed: %s", detail)
            return False, f"Codex2API 上传异常: {detail}"
        finally:
            if mime is not None:
                mime.close()

    detail = _response_detail(response, secrets)
    if response.status_code not in (200, 201):
        if response.status_code in (401, 403):
            return False, "Codex2API Admin Key 无效或无权限"
        if response.status_code == 404:
            safe_url = _sanitized_target_url(api_url, secrets)
            return False, f"Codex2API 管理接口不存在: {safe_url}"
        suffix = f": {detail}" if detail else ""
        return False, f"Codex2API 上传失败: HTTP {response.status_code}{suffix}"

    data = _parse_response_payload(response)
    if data is None:
        return False, "Codex2API 返回了无法解析的响应"

    success = _response_count(data, "success")
    updated = _response_count(data, "updated")
    duplicate = _response_count(data, "duplicate")
    failed = _response_count(data, "failed")

    detail = _bounded_redact(
        data.get("message") or data.get("msg") or data.get("error") or "",
        secrets,
    )
    if failed > 0:
        return False, detail or "Codex2API 拒绝了账号"
    if replace_existing and (success > 0 or updated > 0 or duplicate > 0):
        return _confirm_identity_replacement(
            api_url=api_url,
            admin_key=admin_key,
            secrets=secrets,
            email=email,
            payload=payload,
            before_accounts=before_accounts,
            imported_new=success > 0,
            updated=updated > 0,
            duplicate=duplicate > 0,
            credential_label=credential_label,
        )
    if success > 0:
        return True, f"上传成功（{credential_label}）"
    if updated > 0:
        return True, f"远端账号已更新（{credential_label}）"
    if duplicate > 0:
        return True, f"远端账号已存在（{credential_label}）"
    if detail:
        return False, detail
    return False, "Codex2API 未确认账号已导入"
