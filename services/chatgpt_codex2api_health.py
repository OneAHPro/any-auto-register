"""Read Codex2API's latest wham-only account authentication state."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation
import time
from typing import Iterable, Mapping

from curl_cffi import requests as cffi_requests
from sqlmodel import Session, select

from core.db import AccountModel, engine


AUTH_FAILED_STATUSES = {
    "auth_error",
    "invalid",
    "invalid_token",
    "token_invalidated",
    "unauthorized",
}
HEALTHY_STATUSES = {"active", "rate_limited"}
DEFERRED_STATUSES = {"error"}


class Codex2APIHealthError(RuntimeError):
    """The remote health snapshot cannot be trusted in this cycle."""


def _text(value: object) -> str:
    return str(value or "").strip()


def _to_bool(value: object, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    normalized = _text(value).lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _get_config() -> dict[str, object]:
    from core.config_store import config_store

    return dict(config_store.get_all() or {})


def _safe_detail(value: object, *, secrets: tuple[str, ...]) -> str:
    detail = _text(value)
    for secret in secrets:
        if secret:
            detail = detail.replace(secret, "***")
    return detail[:200]


def _response_detail(response, *, secrets: tuple[str, ...]) -> str:
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        value = (
            payload.get("error")
            or payload.get("message")
            or payload.get("msg")
            or ""
        )
    else:
        value = getattr(response, "text", "")
    return _safe_detail(value, secrets=secrets)


def _get_json(
    base_url: str,
    path: str,
    admin_key: str,
) -> object:
    secrets = (admin_key,)
    try:
        response = cffi_requests.get(
            f"{base_url}{path}",
            headers={
                "X-Admin-Key": admin_key,
                "Accept": "application/json",
            },
            proxies=None,
            verify=True,
            allow_redirects=False,
            timeout=20,
            impersonate="chrome110",
        )
    except Exception as exc:
        raise Codex2APIHealthError(
            f"读取 Codex2API 状态异常（{type(exc).__name__}）"
        ) from None
    if int(getattr(response, "status_code", 0) or 0) not in (200, 201):
        detail = _response_detail(response, secrets=secrets)
        suffix = f"：{detail}" if detail else ""
        raise Codex2APIHealthError(
            f"读取 Codex2API 状态失败（HTTP {response.status_code}）{suffix}"
        )
    try:
        return response.json()
    except Exception:
        raise Codex2APIHealthError("Codex2API 状态响应无法解析") from None


def _post_json(
    base_url: str,
    path: str,
    admin_key: str,
) -> object:
    secrets = (admin_key,)
    try:
        response = cffi_requests.post(
            f"{base_url}{path}",
            headers={
                "X-Admin-Key": admin_key,
                "Accept": "application/json",
            },
            proxies=None,
            verify=True,
            allow_redirects=False,
            timeout=20,
            impersonate="chrome110",
        )
    except Exception as exc:
        raise Codex2APIHealthError(
            f"触发 Codex2API 鉴权探针异常（{type(exc).__name__}）"
        ) from None
    if int(getattr(response, "status_code", 0) or 0) not in (200, 201, 202):
        detail = _response_detail(response, secrets=secrets)
        suffix = f"：{detail}" if detail else ""
        raise Codex2APIHealthError(
            f"触发 Codex2API 鉴权探针失败（HTTP {response.status_code}）{suffix}"
        )
    try:
        return response.json()
    except Exception:
        raise Codex2APIHealthError("Codex2API 鉴权探针响应无法解析") from None


def _load_local_accounts(
    account_ids: Iterable[int],
    *,
    database_engine=engine,
) -> dict[int, str]:
    normalized_ids = sorted({int(account_id) for account_id in account_ids})
    if not normalized_ids:
        return {}
    with Session(database_engine) as session:
        accounts = session.exec(
            select(AccountModel).where(
                AccountModel.id.in_(normalized_ids),
                AccountModel.platform == "chatgpt",
            )
        ).all()
    return {
        int(account.id): _text(account.email)
        for account in accounts
        if account.id is not None and _text(account.email)
    }


def _remote_email(row: Mapping[str, object]) -> str:
    return _text(row.get("email") or row.get("name")).lower()


def _remote_id(row: Mapping[str, object]) -> int | None:
    try:
        parsed = int(row.get("id") or 0)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _prefer_detail_billed(summary: object, detail: object) -> object:
    """Use a positive window detail when the summary is a placeholder zero."""

    if detail is None:
        return summary
    if summary is None:
        return detail
    try:
        summary_value = Decimal(str(summary).strip())
        detail_value = Decimal(str(detail).strip())
    except (InvalidOperation, TypeError, ValueError):
        return summary
    if summary_value == 0 and detail_value > 0:
        return detail
    return summary


def _quota_record(row: Mapping[str, object]) -> dict[str, object]:
    usage_5h_detail = row.get("usage_5h_detail")
    usage_7d_detail = row.get("usage_7d_detail")
    billed_5h = row.get("billed_5h")
    billed_7d = row.get("billed_7d")
    if isinstance(usage_5h_detail, dict):
        billed_5h = _prefer_detail_billed(
            billed_5h,
            usage_5h_detail.get("account_billed"),
        )
    if isinstance(usage_7d_detail, dict):
        billed_7d = _prefer_detail_billed(
            billed_7d,
            usage_7d_detail.get("account_billed"),
        )
    result = {
        "remote_id": _remote_id(row),
        "email": _remote_email(row),
        "remote_status": _text(row.get("status")).lower(),
        "usage_percent_7d": row.get("usage_percent_7d"),
        "billed_7d": billed_7d,
    }
    for key in (
        "plan_type",
        "usage_percent_5h",
        "billed_5h",
    ):
        if key in row and row.get(key) is not None:
            result[key] = row.get(key)
    if billed_5h is not None:
        result["billed_5h"] = billed_5h
    if billed_7d is not None:
        result["billed_7d"] = billed_7d
    if (
        "plan_type" in row
        or row.get("reset_5h_at")
        or row.get("codex_5h_usage_updated_at")
    ):
        result["has_5h_window"] = bool(
            row.get("usage_percent_5h") is not None
            or row.get("reset_5h_at")
            or row.get("codex_5h_usage_updated_at")
        )
    return result


def fetch_codex2api_quota_accounts(
    *,
    config: Mapping[str, object] | None = None,
) -> list[dict[str, object]]:
    """Read the latest Codex2API account quota rows without probing."""

    snapshot = dict(config) if config is not None else _get_config()
    base_url = _text(snapshot.get("codex2api_api_url")).rstrip("/")
    admin_key = _text(snapshot.get("codex2api_admin_key"))
    if not base_url or not admin_key:
        raise Codex2APIHealthError("Codex2API 地址或 Admin Key 未配置")

    payload = _get_json(
        base_url,
        "/api/admin/accounts?channel=codex",
        admin_key,
    )
    rows = payload.get("accounts") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise Codex2APIHealthError("Codex2API 账号清单格式无效")
    return [
        _quota_record(row)
        for row in rows
        if isinstance(row, dict)
    ]


def _record(
    *,
    account_id: int,
    email: str,
    state: str,
    remote_id: int | None = None,
    remote_status: str = "",
    remote_updated_at: str = "",
    probe_mode: str = "",
    usage_percent_7d: object = None,
    billed_7d: object = None,
    message: str,
) -> dict[str, object]:
    result = {
        "account_id": int(account_id),
        "email": email,
        "state": state,
        "remote_id": remote_id,
        "remote_status": remote_status,
        "message": message,
    }
    if remote_updated_at:
        result["remote_updated_at"] = remote_updated_at
    if probe_mode:
        result["probe_mode"] = probe_mode
    if usage_percent_7d is not None:
        result["usage_percent_7d"] = usage_percent_7d
    if billed_7d is not None:
        result["billed_7d"] = billed_7d
    return result


def _confirmation_result(
    health: Mapping[str, object],
    *,
    state: str,
    resolution: str,
    remote_status: str,
    remote_updated_at: str = "",
    message: str,
) -> dict[str, object]:
    result = dict(health)
    result.update(
        {
            "state": state,
            "resolution": resolution,
            "remote_status": remote_status,
            "message": message,
        }
    )
    if remote_updated_at:
        result["remote_updated_at"] = remote_updated_at
    return result


def _post_refresh(
    base_url: str,
    remote_id: int,
    admin_key: str,
):
    try:
        return cffi_requests.post(
            f"{base_url}/api/admin/accounts/{remote_id}/refresh",
            headers={
                "X-Admin-Key": admin_key,
                "Accept": "application/json",
            },
            proxies=None,
            verify=True,
            allow_redirects=False,
            timeout=20,
            impersonate="chrome110",
        )
    except Exception:
        return None


def _rows_from_payload(payload: object) -> list[dict[str, object]]:
    rows = payload.get("accounts") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _explicit_refresh_credential_failure(detail: str) -> bool:
    normalized = _text(detail).lower()
    return any(
        marker in normalized
        for marker in (
            "invalid_grant",
            "token_invalidated",
            "refresh token expired",
            "refresh token has expired",
            "refresh_token expired",
            "refresh_token invalid",
            "refresh_token is invalid",
            "refresh token revoked",
        )
    )


def confirm_codex2api_auth_failure(
    health: Mapping[str, object],
    *,
    config: Mapping[str, object] | None = None,
    poll_attempts: int = 4,
    poll_interval_seconds: float = 1.0,
    sleep_fn=time.sleep,
) -> dict[str, object]:
    """Ask Codex2API to refresh its own RT before requiring an OTP login."""

    snapshot = dict(health)
    if _text(snapshot.get("state")).lower() != "auth_failed":
        return snapshot
    if _text(snapshot.get("auth_failure_source")).lower() == "error_message":
        return _confirmation_result(
            snapshot,
            state="auth_failed",
            resolution="remote_error_confirmed_failure",
            remote_status=_text(snapshot.get("remote_status")),
            remote_updated_at=_text(snapshot.get("remote_updated_at")),
            message=(
                "Codex2API 已明确返回 Refresh Token 失效，"
                "需要本地验证码重登"
            ),
        )
    try:
        remote_id = int(snapshot.get("remote_id") or 0)
    except (TypeError, ValueError):
        remote_id = 0
    if remote_id <= 0:
        return _confirmation_result(
            snapshot,
            state="deferred",
            resolution="remote_refresh_unavailable",
            remote_status=_text(snapshot.get("remote_status")),
            message="Codex2API 账号 ID 无效，等待下一轮复查",
        )

    settings = dict(config) if config is not None else _get_config()
    base_url = _text(settings.get("codex2api_api_url")).rstrip("/")
    admin_key = _text(settings.get("codex2api_admin_key"))
    if not base_url or not admin_key:
        return _confirmation_result(
            snapshot,
            state="deferred",
            resolution="remote_refresh_unavailable",
            remote_status=_text(snapshot.get("remote_status")),
            message="Codex2API 地址或 Admin Key 未配置，等待下一轮复查",
        )

    response = _post_refresh(base_url, remote_id, admin_key)
    if response is None:
        return _confirmation_result(
            snapshot,
            state="deferred",
            resolution="remote_refresh_unavailable",
            remote_status=_text(snapshot.get("remote_status")),
            message="Codex2API 自刷新请求异常，等待下一轮复查",
        )
    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code not in (200, 201, 202):
        detail = _response_detail(response, secrets=(admin_key,))
        fresh_wham_401 = (
            _text(snapshot.get("probe_mode")).lower() == "wham_only"
            and _text(snapshot.get("remote_status")).lower()
            in AUTH_FAILED_STATUSES
        )
        if _explicit_refresh_credential_failure(detail) or (
            status_code == 500 and fresh_wham_401
        ):
            return _confirmation_result(
                snapshot,
                state="auth_failed",
                resolution="remote_refresh_confirmed_failure",
                remote_status=_text(snapshot.get("remote_status")),
                message=(
                    "Codex2API 本轮 wham 401 且使用自身 RT 恢复失败，"
                    "需要本地验证码重登"
                ),
            )
        return _confirmation_result(
            snapshot,
            state="deferred",
            resolution="remote_refresh_unavailable",
            remote_status=_text(snapshot.get("remote_status")),
            message=(
                f"Codex2API 自刷新暂时失败（HTTP {status_code}），"
                "等待下一轮复查"
            ),
        )

    attempts = min(max(int(poll_attempts or 1), 1), 10)
    interval = max(float(poll_interval_seconds or 0), 0)
    previous_updated_at = _text(snapshot.get("remote_updated_at"))
    last_status = _text(snapshot.get("remote_status")).lower()
    last_updated_at = previous_updated_at
    for attempt in range(attempts):
        try:
            payload = _get_json(
                base_url,
                "/api/admin/accounts?channel=codex",
                admin_key,
            )
        except Codex2APIHealthError:
            payload = None
        row = next(
            (
                item
                for item in _rows_from_payload(payload)
                if _remote_id(item) == remote_id
            ),
            None,
        )
        if row is not None:
            last_status = _text(row.get("status")).lower()
            last_updated_at = _text(row.get("updated_at"))
            if last_status in HEALTHY_STATUSES:
                return _confirmation_result(
                    snapshot,
                    state="healthy",
                    resolution="remote_refresh_recovered",
                    remote_status=last_status,
                    remote_updated_at=last_updated_at,
                    message=(
                        "Codex2API 已使用自身 RT 恢复鉴权，"
                        "无需本地重登"
                    ),
                )
            if (
                last_status in AUTH_FAILED_STATUSES
                and last_updated_at
                and last_updated_at != previous_updated_at
            ):
                return _confirmation_result(
                    snapshot,
                    state="auth_failed",
                    resolution="remote_refresh_confirmed_failure",
                    remote_status=last_status,
                    remote_updated_at=last_updated_at,
                    message=(
                        "Codex2API 使用自身 RT 刷新后仍鉴权失败，"
                        "需要本地验证码重登"
                    ),
                )
        if attempt + 1 < attempts and interval:
            sleep_fn(interval)

    return _confirmation_result(
        snapshot,
        state="deferred",
        resolution="remote_refresh_pending",
        remote_status=last_status,
        remote_updated_at=last_updated_at,
        message="Codex2API 自刷新结果尚未更新，等待下一轮复查",
    )


def inspect_codex2api_account_health(
    account_ids: Iterable[int],
    *,
    config: Mapping[str, object] | None = None,
    local_accounts: Mapping[int, str] | None = None,
    database_engine=engine,
    probe_poll_attempts: int = 90,
    probe_poll_interval_seconds: float = 1.0,
    sleep_fn=time.sleep,
    quota_accounts: list[dict[str, object]] | None = None,
) -> dict[int, dict[str, object]]:
    """Return one conservative remote-auth decision for every local account."""

    normalized_ids = sorted({int(account_id) for account_id in account_ids})
    snapshot = dict(config) if config is not None else _get_config()
    base_url = _text(snapshot.get("codex2api_api_url")).rstrip("/")
    admin_key = _text(snapshot.get("codex2api_admin_key"))
    if not base_url or not admin_key:
        raise Codex2APIHealthError("Codex2API 地址或 Admin Key 未配置")

    settings = _get_json(base_url, "/api/admin/settings", admin_key)
    if not isinstance(settings, dict):
        raise Codex2APIHealthError("Codex2API 设置响应格式无效")
    if _to_bool(
        settings.get("usage_probe_responses_fallback_enabled"),
        default=True,
    ):
        raise Codex2APIHealthError(
            "Codex2API 尚未关闭 Responses 探针回退，当前状态可能被旧连接掩盖"
        )

    probe = _post_json(
        base_url,
        "/api/admin/accounts/usage/probe",
        admin_key,
    )
    if (
        not isinstance(probe, dict)
        or _text(probe.get("mode")).lower() != "wham_only"
    ):
        raise Codex2APIHealthError(
            "Codex2API 未确认使用 wham_only 鉴权探针"
        )

    attempts = min(max(int(probe_poll_attempts or 1), 1), 300)
    interval = max(float(probe_poll_interval_seconds or 0), 0)
    for attempt in range(attempts):
        runtime = _get_json(base_url, "/api/admin/runtime-status", admin_key)
        if not isinstance(runtime, dict):
            raise Codex2APIHealthError("Codex2API 运行状态响应格式无效")
        probes = runtime.get("probes")
        running = isinstance(probes, dict) and _to_bool(
            probes.get("usage_probe_running")
        )
        if not running:
            break
        if attempt + 1 < attempts and interval:
            sleep_fn(interval)
    else:
        raise Codex2APIHealthError(
            "Codex2API 鉴权探针执行超时，本轮等待下一次巡检"
        )

    payload = _get_json(
        base_url,
        "/api/admin/accounts?channel=codex",
        admin_key,
    )
    rows = payload.get("accounts") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise Codex2APIHealthError("Codex2API 账号清单格式无效")
    if quota_accounts is not None:
        quota_accounts.extend(
            _quota_record(raw_row)
            for raw_row in rows
            if isinstance(raw_row, dict)
        )

    resolved_local_accounts = (
        {
            int(account_id): _text(email)
            for account_id, email in local_accounts.items()
            if int(account_id) in normalized_ids and _text(email)
        }
        if local_accounts is not None
        else _load_local_accounts(
            normalized_ids,
            database_engine=database_engine,
        )
    )
    remote_by_email: dict[str, list[dict[str, object]]] = defaultdict(list)
    for raw_row in rows:
        if not isinstance(raw_row, dict):
            continue
        email = _remote_email(raw_row)
        if email:
            remote_by_email[email].append(raw_row)

    result: dict[int, dict[str, object]] = {}
    for account_id in normalized_ids:
        email = _text(resolved_local_accounts.get(account_id))
        if not email:
            result[account_id] = _record(
                account_id=account_id,
                email="",
                state="missing",
                message="本地 ChatGPT 账号记录已不存在",
            )
            continue
        matches = remote_by_email.get(email.lower(), [])
        if not matches:
            result[account_id] = _record(
                account_id=account_id,
                email=email,
                state="remote_missing",
                message="Codex2API 未找到同邮箱账号，将执行一次完整登录确认",
            )
            continue
        if len(matches) > 1:
            result[account_id] = _record(
                account_id=account_id,
                email=email,
                state="ambiguous",
                message="Codex2API 存在多个同邮箱账号，未触发自动重登",
            )
            continue

        row = matches[0]
        remote_status = _text(row.get("status")).lower()
        explicit_error_auth_failure = (
            remote_status == "error"
            and _explicit_refresh_credential_failure(
                _text(row.get("error_message"))
            )
        )
        remote_id = _remote_id(row)
        remote_updated_at = _text(row.get("updated_at"))
        quota_fields = {
            "usage_percent_7d": row.get("usage_percent_7d"),
            "billed_7d": row.get("billed_7d"),
        }
        if remote_status in AUTH_FAILED_STATUSES or explicit_error_auth_failure:
            record = _record(
                account_id=account_id,
                email=email,
                state="auth_failed",
                remote_id=remote_id,
                remote_status=remote_status,
                remote_updated_at=remote_updated_at,
                probe_mode="wham_only",
                **quota_fields,
                message=(
                    "Codex2API 明确返回 Refresh Token 已失效"
                    if explicit_error_auth_failure
                    else "Codex2API 本轮 wham 探针明确标记账号鉴权失效"
                ),
            )
            if explicit_error_auth_failure:
                record["auth_failure_source"] = "error_message"
            result[account_id] = record
        elif remote_status not in HEALTHY_STATUSES:
            result[account_id] = _record(
                account_id=account_id,
                email=email,
                state="deferred",
                remote_id=remote_id,
                remote_status=remote_status,
                remote_updated_at=remote_updated_at,
                **quota_fields,
                message=(
                    "Codex2API 账号状态为临时错误，等待下一轮复查"
                    if remote_status in DEFERRED_STATUSES
                    else (
                        "Codex2API 账号状态暂不可识别"
                        f"（{remote_status}），等待下一轮复查"
                    )
                    if remote_status
                    else "Codex2API 未返回账号状态，等待下一轮复查"
                ),
            )
        else:
            result[account_id] = _record(
                account_id=account_id,
                email=email,
                state="healthy",
                remote_id=remote_id,
                remote_status=remote_status,
                remote_updated_at=remote_updated_at,
                **quota_fields,
                message=f"Codex2API 鉴权状态正常（{remote_status}）",
            )

    return result


__all__ = [
    "AUTH_FAILED_STATUSES",
    "HEALTHY_STATUSES",
    "Codex2APIHealthError",
    "confirm_codex2api_auth_failure",
    "fetch_codex2api_quota_accounts",
    "inspect_codex2api_account_health",
]
