"""Manage ChatGPT TOTP MFA immediately after a fresh interactive login."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import quote

from core.icloud_mail import generate_totp


CHATGPT_BASE = "https://chatgpt.com"
AUTH_BASE = "https://auth.openai.com"


class MfaRotationError(RuntimeError):
    """A redacted MFA management failure safe to expose in task logs."""


@dataclass(frozen=True)
class MfaRotationResult:
    totp_secret: str
    recovery_code: str
    replaced_existing: bool
    mfa_enabled: bool
    rotated_at: str


class ChatGPTMfaManager:
    """Enroll or replace ChatGPT TOTP using a recently authenticated session."""

    MFA_INFO_URL = f"{CHATGPT_BASE}/backend-api/accounts/mfa_info"
    DISABLE_URL = (
        f"{CHATGPT_BASE}/backend-api/accounts/mfa/user/disable_in_house"
    )
    REQUEST_TOKEN_URL = (
        f"{CHATGPT_BASE}/backend-api/accounts/mfa/user/request_mfa_token_in_house"
    )
    ENROLL_URL = f"{AUTH_BASE}/api/mfa/public/enroll"
    ACTIVATE_URL = f"{AUTH_BASE}/api/mfa/public/activate_enrollment"
    IN_HOUSE_ENROLL_URL = f"{CHATGPT_BASE}/backend-api/accounts/mfa/enroll"
    IN_HOUSE_ACTIVATE_URL = (
        f"{CHATGPT_BASE}/backend-api/accounts/mfa/user/activate_enrollment"
    )

    def __init__(
        self,
        *,
        session,
        access_token: str,
        account_id: str = "",
        user_agent: str = "",
        impersonate: str = "",
        log_fn: Callable[[str], None] | None = None,
        can_recover_by_email: bool = False,
        on_secret_enrolled: Callable[[str], None] | None = None,
        on_secret_activated: Callable[[str], None] | None = None,
        on_recovery_code: Callable[[str], None] | None = None,
    ) -> None:
        self.session = session
        self.access_token = str(access_token or "").strip()
        self.account_id = str(account_id or "").strip()
        self.user_agent = str(user_agent or "").strip()
        self.impersonate = str(impersonate or "").strip()
        self.log_fn = log_fn or (lambda _message: None)
        self.can_recover_by_email = bool(can_recover_by_email)
        self.on_secret_enrolled = on_secret_enrolled
        self.on_secret_activated = on_secret_activated
        self.on_recovery_code = on_recovery_code

    def _log(self, message: str) -> None:
        self.log_fn(str(message or ""))

    def _backend_headers(self, *, content_type: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.access_token}",
            "Origin": CHATGPT_BASE,
            "Referer": f"{CHATGPT_BASE}/",
        }
        if self.user_agent:
            headers["User-Agent"] = self.user_agent
        if self.account_id:
            headers["chatgpt-account-id"] = self.account_id
        if content_type:
            headers["Content-Type"] = "application/json"
        return headers

    def _auth_headers(self, *, state_token: str) -> dict[str, str]:
        referer = (
            f"{AUTH_BASE}/totp_enroll?origin_app_name=ChatGPT"
            f"&mfa_token={quote(state_token, safe='')}"
        )
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": AUTH_BASE,
            "Referer": referer,
        }
        if self.user_agent:
            headers["User-Agent"] = self.user_agent
        return headers

    def _request_kwargs(self, **kwargs) -> dict[str, Any]:
        result = dict(kwargs)
        result.setdefault("timeout", 30)
        if self.impersonate:
            result["impersonate"] = self.impersonate
        return result

    @staticmethod
    def _response_payload(response) -> dict[str, Any]:
        try:
            payload = response.json()
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _safe_error_code(payload: dict[str, Any]) -> str:
        error = payload.get("error")
        candidates = []
        if isinstance(error, dict):
            candidates.extend((error.get("code"), error.get("type")))
        candidates.extend((payload.get("code"), payload.get("error_code")))
        for candidate in candidates:
            value = str(candidate or "").strip()
            if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value):
                return value
        return ""

    def _checked_payload(self, response, *, operation: str) -> dict[str, Any]:
        status = int(getattr(response, "status_code", 0) or 0)
        payload = self._response_payload(response)
        if 200 <= status < 300:
            response_status = str(payload.get("status") or "").strip().lower()
            if payload.get("success") is not False and response_status not in {
                "error",
                "failed",
                "failure",
            }:
                return payload
            code = self._safe_error_code(payload)
            suffix = f" ({code})" if code else ""
            raise MfaRotationError(
                f"[stage=mfa_rotate] {operation}失败: HTTP {status}{suffix}"
            )
        code = self._safe_error_code(payload)
        suffix = f" ({code})" if code else ""
        raise MfaRotationError(
            f"[stage=mfa_rotate] {operation}失败: HTTP {status}{suffix}"
        )

    @staticmethod
    def _read_value(payload: dict[str, Any], *keys: str) -> str:
        containers = [payload]
        for container_key in ("data", "result"):
            nested = payload.get(container_key)
            if isinstance(nested, dict):
                containers.append(nested)
        for container in containers:
            for key in keys:
                value = str(container.get(key) or "").strip()
                if value:
                    return value
        return ""

    @staticmethod
    def _factor_id(mfa_info: dict[str, Any]) -> str:
        factors = mfa_info.get("factors")
        candidates: list[dict[str, Any]] = []
        if isinstance(factors, dict):
            totp_factors = factors.get("totp")
            if isinstance(totp_factors, list):
                candidates.extend(
                    factor for factor in totp_factors if isinstance(factor, dict)
                )
            elif isinstance(totp_factors, dict):
                candidates.append(totp_factors)
        elif isinstance(factors, list):
            candidates.extend(
                factor
                for factor in factors
                if isinstance(factor, dict)
                and str(
                    factor.get("type") or factor.get("factor_type") or ""
                ).strip().lower()
                == "totp"
            )
        for factor in candidates:
            if not isinstance(factor, dict):
                continue
            factor_id = str(
                factor.get("id") or factor.get("factor_id") or ""
            ).strip()
            if factor_id:
                return factor_id
        if factors in (None, [], {}):
            return ""
        return ""

    @staticmethod
    def _is_mfa_enabled(mfa_info: dict[str, Any]) -> bool:
        if mfa_info.get("mfa_enabled") is True or mfa_info.get("mfa_enabled_v2") is True:
            return True
        factors = mfa_info.get("factors")
        return bool(
            (isinstance(factors, list) and factors)
            or (
                isinstance(factors, dict)
                and any(bool(value) for value in factors.values())
            )
        )

    def _get_mfa_info(self) -> dict[str, Any]:
        response = self.session.get(
            self.MFA_INFO_URL,
            **self._request_kwargs(headers=self._backend_headers()),
        )
        return self._checked_payload(response, operation="读取 MFA 状态")

    def _disable_factor(self, factor_id: str) -> None:
        response = self.session.post(
            self.DISABLE_URL,
            **self._request_kwargs(
                json={"factor_id": factor_id},
                headers=self._backend_headers(content_type=True),
            ),
        )
        self._checked_payload(response, operation="移除旧 MFA")

    def _request_state_token(self) -> str:
        response = self.session.post(
            self.REQUEST_TOKEN_URL,
            **self._request_kwargs(
                json={},
                headers=self._backend_headers(content_type=True),
            ),
        )
        payload = self._checked_payload(response, operation="创建 MFA 轮换会话")
        token = self._read_value(payload, "state_token", "token")
        if not token:
            raise MfaRotationError(
                "[stage=mfa_rotate] 创建 MFA 轮换会话失败: 响应缺少 state_token"
            )
        return token

    def _enroll_factor(self, state_token: str, factor_type: str) -> str:
        response = self.session.post(
            self.ENROLL_URL,
            **self._request_kwargs(
                json={"token": state_token, "factor_type": factor_type},
                headers=self._auth_headers(state_token=state_token),
            ),
        )
        payload = self._checked_payload(
            response,
            operation=("生成新 MFA" if factor_type == "totp" else "生成恢复码"),
        )
        secret = self._read_value(payload, "secret", "code", "recovery_code")
        if not secret:
            label = "MFA 密钥" if factor_type == "totp" else "恢复码"
            raise MfaRotationError(
                f"[stage=mfa_rotate] 生成{label}失败: 响应缺少凭据"
            )
        return secret

    def _activate_factor(
        self,
        state_token: str,
        *,
        factor_type: str,
        code: str,
    ) -> None:
        response = self.session.post(
            self.ACTIVATE_URL,
            **self._request_kwargs(
                json={
                    "code": code,
                    "token": state_token,
                    "factor_type": factor_type,
                    "origin_app_name": "ChatGPT",
                },
                headers=self._auth_headers(state_token=state_token),
            ),
        )
        self._checked_payload(
            response,
            operation=("激活新 MFA" if factor_type == "totp" else "激活恢复码"),
        )

    def _enroll_totp_in_house(self) -> tuple[str, str]:
        response = self.session.post(
            self.IN_HOUSE_ENROLL_URL,
            **self._request_kwargs(
                json={"factor_type": "totp"},
                headers=self._backend_headers(content_type=True),
            ),
        )
        payload = self._checked_payload(
            response,
            operation="通过 ChatGPT 创建新 MFA",
        )
        secret = self._read_value(payload, "secret")
        session_id = self._read_value(payload, "session_id", "sessionId")
        if not secret or not session_id:
            raise MfaRotationError(
                "[stage=mfa_rotate] 通过 ChatGPT 创建新 MFA 失败: "
                "响应缺少 secret 或 session_id"
            )
        return secret, session_id

    def _activate_totp_in_house(self, *, session_id: str, code: str) -> None:
        response = self.session.post(
            self.IN_HOUSE_ACTIVATE_URL,
            **self._request_kwargs(
                json={
                    "code": code,
                    "factor_type": "totp",
                    "session_id": session_id,
                },
                headers=self._backend_headers(content_type=True),
            ),
        )
        self._checked_payload(response, operation="通过 ChatGPT 激活新 MFA")

    def rotate(self) -> MfaRotationResult:
        if not self.access_token:
            raise MfaRotationError(
                "[stage=mfa_rotate] MFA 轮换失败: 登录会话缺少 Access Token"
            )

        self._log("[MFA] 正在读取当前 MFA 状态")
        current = self._get_mfa_info()
        factor_id = self._factor_id(current)
        had_mfa = bool(factor_id)
        if had_mfa:
            if not self.can_recover_by_email:
                raise MfaRotationError(
                    "[stage=mfa_rotate] 已有 MFA 需要轮换，但缺少可用的邮箱验证码恢复渠道；"
                    "为避免账号锁死，本次未删除旧 MFA"
                )
            self._log("[MFA] 正在移除旧 MFA 因子")
            self._disable_factor(factor_id)

        self._log("[MFA] 正在生成并激活项目托管的 MFA")
        state_token = ""
        protocol = "public"
        try:
            state_token = self._request_state_token()
            totp_secret = self._enroll_factor(state_token, "totp")
        except MfaRotationError:
            self._log(
                "[MFA] Auth enrollment 协议未完成，切换 ChatGPT in-house 协议"
            )
            protocol = "in_house"
            totp_secret, enrollment_session_id = self._enroll_totp_in_house()

        if callable(self.on_secret_enrolled):
            self.on_secret_enrolled(totp_secret)
        totp_code = str(generate_totp(totp_secret) or "").strip()
        if not re.fullmatch(r"\d{6}", totp_code):
            raise MfaRotationError(
                "[stage=mfa_rotate] 激活新 MFA 失败: 本地 TOTP 生成异常"
            )
        if protocol == "public":
            # A public activation failure is not a protocol-discovery failure.
            # The server may have accepted the code even when the response was
            # interrupted, so never enroll a second secret and overwrite the
            # durable journal at this point.
            self._activate_factor(
                state_token,
                factor_type="totp",
                code=totp_code,
            )
        else:
            self._activate_totp_in_house(
                session_id=enrollment_session_id,
                code=totp_code,
            )
        rotated_at = datetime.now(timezone.utc).isoformat()
        if callable(self.on_secret_activated):
            self.on_secret_activated(rotated_at)

        recovery_code = ""
        try:
            if protocol != "public":
                raise MfaRotationError("in-house enrollment 不返回恢复码")
            recovery_code = self._enroll_factor(state_token, "recovery_code")
            self._activate_factor(
                state_token,
                factor_type="recovery_code",
                code=recovery_code,
            )
            if callable(self.on_recovery_code):
                self.on_recovery_code(recovery_code)
        except MfaRotationError:
            self._log("[MFA] 新 MFA 已激活；恢复码暂未生成，不影响登录")
            recovery_code = ""

        verified_enabled = True
        try:
            verified_enabled = self._is_mfa_enabled(self._get_mfa_info())
        except MfaRotationError:
            self._log("[MFA] 新 MFA 已激活；状态复核请求失败，将按激活结果保存")
        if not verified_enabled:
            self._log(
                "[MFA] 新 MFA 已激活；状态复核尚未同步，将优先保存新密钥"
            )

        self._log(
            "[MFA] MFA 轮换完成，新密钥将安全保存；"
            "共享接码地址的邮箱控制权仍需单独处理"
        )
        return MfaRotationResult(
            totp_secret=totp_secret,
            recovery_code=recovery_code,
            replaced_existing=had_mfa,
            mfa_enabled=True,
            rotated_at=rotated_at,
        )
