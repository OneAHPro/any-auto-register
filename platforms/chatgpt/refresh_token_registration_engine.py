"""
ChatGPT Refresh Token 注册引擎。

主链路采用两段式推进：
1. `ChatGPTClient.register_complete_flow()` 负责把注册状态机推进到 about_you
2. `OAuthClient.login_and_get_tokens()` 承接前序会话继续完成 about_you / workspace / token
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from sqlmodel import Session

from core.task_runtime import TaskInterruption
from core.db import (
    load_chatgpt_mfa_rotation,
    mark_chatgpt_mfa_rotation_activated,
    stage_chatgpt_mfa_rotation,
    update_chatgpt_mfa_rotation_recovery_code,
)
from services.chatgpt_auth_state import (
    ChatGPTAuthIdentityConflict,
    load_login_mfa_candidate_by_email,
    resolve_chatgpt_auth_account_id,
)

from .chatgpt_client import ChatGPTClient
from .auth_outcomes import VerificationCodeResult
from .log_sanitizer import sanitize_chatgpt_log_message
from .mfa_manager import ChatGPTMfaManager, MfaRotationError, MfaRotationResult
from .oauth import OAuthManager
from .oauth_client import OAuthClient
from .oauth_resume_cache import oauth_resume_cache, serialize_oauth_resume_context
from .utils import (
    FlowState,
    generate_random_birthday,
    generate_random_name,
    generate_random_password,
)

logger = logging.getLogger(__name__)


class MissingTotpCredentialsError(ValueError):
    error_code = "missing_totp_credentials"


@dataclass
class RegistrationResult:
    """注册结果。"""

    success: bool
    email: str = ""
    password: str = ""
    account_id: str = ""
    workspace_id: str = ""
    access_token: str = ""
    refresh_token: str = ""
    id_token: str = ""
    session_token: str = ""
    error_message: str = ""
    error_code: str = ""
    logs: list | None = None
    metadata: dict | None = None
    source: str = "register"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "email": self.email,
            "password": self.password,
            "account_id": self.account_id,
            "workspace_id": self.workspace_id,
            "access_token": self.access_token[:20] + "..." if self.access_token else "",
            "refresh_token": self.refresh_token[:20] + "..." if self.refresh_token else "",
            "id_token": self.id_token[:20] + "..." if self.id_token else "",
            "session_token": self.session_token[:20] + "..." if self.session_token else "",
            "error_message": self.error_message,
            "error_code": self.error_code,
            "logs": self.logs or [],
            "metadata": self.metadata or {},
            "source": self.source,
        }


@dataclass
class SignupFormResult:
    """保留旧结构，兼容外部引用。"""

    success: bool
    page_type: str = ""
    is_existing_account: bool = False
    response_data: Dict[str, Any] | None = None
    error_message: str = ""


class EmailServiceAdapter:
    """将现有 email_service 适配给 ChatGPTClient / OAuthClient 状态机。"""

    def __init__(self, email_service, email: str, log_fn: Callable[[str], None]):
        self.email_service = email_service
        self.email = email
        self.log_fn = log_fn
        self._used_codes: set[str] = set()
        self._last_code: str = ""
        self._last_code_at: float = 0.0
        self._last_success_code: str = ""
        self._last_success_code_at: float = 0.0

    @property
    def last_code(self) -> str:
        return self._last_success_code or self._last_code

    def _remember_code(self, code: str, *, successful: bool = False) -> None:
        code = str(code or "").strip()
        if not code:
            return
        now = time.time()
        self._last_code = code
        self._last_code_at = now
        self._used_codes.add(code)
        if successful:
            self._last_success_code = code
            self._last_success_code_at = now

    def remember_successful_code(self, code: str) -> None:
        self._remember_code(code, successful=True)

    def get_recent_code(
        self,
        max_age_seconds: int = 180,
        *,
        prefer_successful: bool = True,
    ) -> str:
        now = time.time()
        if (
            prefer_successful
            and self._last_success_code
            and now - self._last_success_code_at <= max_age_seconds
        ):
            return self._last_success_code
        if self._last_code and now - self._last_code_at <= max_age_seconds:
            return self._last_code
        return ""

    def wait_for_verification_code(
        self,
        email: str,
        timeout: int = 90,
        otp_sent_at: float | None = None,
        exclude_codes=None,
        require_fresh_metadata: bool = False,
    ):
        excluded = set(exclude_codes) if exclude_codes is not None else set(self._used_codes)
        self.log_fn(f"正在等待邮箱 {email} 的验证码 ({timeout}s)...")
        kwargs = {
            "email": email,
            "timeout": timeout,
            "otp_sent_at": otp_sent_at,
            "exclude_codes": excluded,
        }
        if require_fresh_metadata:
            kwargs["require_fresh_metadata"] = True
        try:
            raw_code = self.email_service.get_verification_code(**kwargs)
        except TypeError as exc:
            if not require_fresh_metadata or "require_fresh_metadata" not in str(exc):
                raise
            kwargs.pop("require_fresh_metadata", None)
            raw_code = self.email_service.get_verification_code(**kwargs)
        result = VerificationCodeResult.from_value(raw_code)
        if result is not None:
            self._remember_code(result.code, successful=False)
            self.log_fn("成功获取邮箱验证码")
            # Registration/legacy callers expect the six-digit string.  The
            # structured result is opt-in for strict automatic-relogin email
            # fallback and risk challenges.
            return result if require_fresh_metadata else result.code
        return ""

    def get_totp_code(self) -> str:
        getter = getattr(self.email_service, "get_totp_code", None)
        if not callable(getter):
            raise RuntimeError("当前邮箱服务未提供远程 2FA 验证码")
        return str(getter() or "").strip()

    def supports_totp_code(self) -> bool:
        supports = getattr(self.email_service, "supports_totp_code", None)
        if callable(supports):
            return bool(supports())
        return callable(getattr(self.email_service, "get_totp_code", None))

    def commit_password_reset(self, new_password: str) -> bool:
        commit = getattr(self.email_service, "commit_password_reset", None)
        if not callable(commit):
            raise RuntimeError("当前邮箱服务不支持保存重置后的密码")
        return commit(str(new_password or "")) is not False

    def commit_mfa_rotation(
        self,
        *,
        totp_secret: str,
        recovery_code: str,
        rotated_at: str,
    ) -> bool:
        commit = getattr(self.email_service, "commit_mfa_rotation", None)
        if not callable(commit):
            raise RuntimeError("当前邮箱服务不支持保存轮换后的 MFA 凭据")
        return commit(
            totp_secret=str(totp_secret or ""),
            recovery_code=str(recovery_code or ""),
            rotated_at=str(rotated_at or ""),
        ) is not False

    def supports_email_verification(self) -> bool:
        supports = getattr(
            self.email_service,
            "supports_email_verification",
            None,
        )
        if callable(supports):
            try:
                return bool(supports())
            except Exception:
                return False
        return callable(getattr(self.email_service, "get_verification_code", None))


class RefreshTokenRegistrationEngine:
    """Refresh token 注册引擎。"""

    def __init__(
        self,
        email_service,
        proxy_url: Optional[str] = None,
        callback_logger: Optional[Callable[[str], None]] = None,
        task_uuid: Optional[str] = None,
        browser_mode: str = "protocol",
        max_retries: int = 3,
        extra_config: Optional[dict] = None,
    ):
        self.email_service = email_service
        self.proxy_url = proxy_url
        self.callback_logger = callback_logger or (lambda msg: logger.info(msg))
        self.task_uuid = task_uuid
        self.browser_mode = str(browser_mode or "protocol").strip().lower() or "protocol"
        # 已移除整流程重试能力，保留参数仅兼容调用方
        self.max_retries = 1
        self.extra_config = dict(extra_config or {})

        self.email: Optional[str] = None
        self.password: Optional[str] = None
        self.totp_secret: Optional[str] = None
        self.password_reset_required = False
        self.email_info: Optional[Dict[str, Any]] = None
        self._email_error_message = ""
        self._email_error_code = ""
        self.logs: list[str] = []

    def _log(self, message: str, level: str = "info"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        safe_message = sanitize_chatgpt_log_message(message)
        log_message = f"[{timestamp}] {safe_message}"
        self.logs.append(log_message)

        if self.callback_logger:
            self.callback_logger(log_message)

        if level == "error":
            logger.error(log_message)
        elif level == "warning":
            logger.warning(log_message)
        else:
            logger.info(log_message)

    def _existing_account_login_only(self) -> bool:
        value = self.extra_config.get("chatgpt_existing_account_login_only", False)
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

    def _existing_account_phone_verification_enabled(self) -> bool:
        value = self.extra_config.get(
            "chatgpt_existing_account_allow_phone_verification",
            False,
        )
        if isinstance(value, bool):
            enabled = value
        else:
            enabled = str(value or "").strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
        if enabled:
            return True
        sms_mode = str(
            self.extra_config.get("chatgpt_existing_account_sms_mode") or ""
        ).strip().lower()
        return sms_mode == "pool"

    def _existing_account_rotate_mfa_enabled(self) -> bool:
        value = self.extra_config.get(
            "chatgpt_existing_account_rotate_mfa",
            False,
        )
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

    def _existing_account_should_rotate_mfa(self) -> bool:
        if not self._existing_account_rotate_mfa_enabled():
            return False
        skip_managed = self.extra_config.get(
            "chatgpt_existing_account_skip_managed_mfa_rotation",
            False,
        )
        skip_managed = (
            skip_managed
            if isinstance(skip_managed, bool)
            else str(skip_managed or "").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        already_managed = bool(
            isinstance(self.email_info, dict)
            and self.email_info.get("chatgpt_mfa_managed") is True
            and str(self.email_info.get("totp_secret") or "").strip()
        )
        if skip_managed and already_managed:
            self._log("检测到本任务已完成 MFA 轮换，重试阶段跳过再次轮换")
            return False
        return True

    def _existing_account_login_stage(self) -> str:
        value = str(
            self.extra_config.get(
                "chatgpt_existing_account_login_stage",
                "refresh_token",
            )
            or "refresh_token"
        ).strip().lower().replace("-", "_")
        if value in {"access_token", "access_token_only", "at", "at_only"}:
            return "access_token"
        return "refresh_token"

    def _should_upgrade_passwordless_login_for_mfa(self) -> bool:
        """Use the one-time mailbox session to establish a primary password.

        TOTP is a second factor and cannot replace the primary password/email
        challenge.  During MFA rotation, or when a previously managed MFA is
        found, passwordless mailbox imports should therefore set and persist a
        ChatGPT password while the receiver is still available.
        """
        if self.password or self.password_reset_required:
            return False
        email_info = self.email_info if isinstance(self.email_info, dict) else {}
        # Legacy mailbox snapshots may not contain the explicit managed flag,
        # but mailapi/microsoft records only gain a TOTP after project-side MFA
        # enrollment.  Treat the persisted secret itself as the durable marker.
        managed_mfa = bool(str(email_info.get("totp_secret") or "").strip())
        if not self._existing_account_rotate_mfa_enabled() and not managed_mfa:
            return False
        account_type = str(email_info.get("account_type") or "").strip().lower()
        if account_type not in {"mailapi_url", "microsoft_oauth"}:
            return False
        if not callable(getattr(self.email_service, "commit_password_reset", None)):
            return False
        supports = getattr(self.email_service, "supports_email_verification", None)
        if callable(supports):
            try:
                return bool(supports())
            except Exception:
                return False
        return callable(getattr(self.email_service, "get_verification_code", None))

    def _prepare_passwordless_login_for_mfa(self) -> None:
        replacement = generate_random_password(20)
        self.password = replacement
        self.password_reset_required = True
        if isinstance(self.email_info, dict):
            self.email_info["new_password"] = replacement
            self.email_info["password_reset_required"] = True
        self._log(
            "账号当前仅依赖邮箱验证码作为主登录凭据；"
            "本轮先设置项目托管的 ChatGPT 密码，再使用密码 + MFA，"
            "后续常规重登将优先使用密码和项目 MFA",
            "warning",
        )

    def _create_email(self, *, existing_account_login_only: bool = False) -> bool:
        action = "加载" if existing_account_login_only else "创建"
        self._email_error_message = ""
        self._email_error_code = ""
        try:
            if existing_account_login_only:
                self._log(
                    f"正在加载 {self.email_service.service_type.value} 邮箱凭据..."
                )
            else:
                self._log(f"正在创建 {self.email_service.service_type.value} 邮箱...")
            self.email_info = self.email_service.create_email()

            email_value = str(
                self.email
                or (self.email_info or {}).get("email")
                or ""
            ).strip()
            if not email_value:
                self._email_error_message = (
                    f"{self.email_service.service_type.value} 返回空邮箱地址"
                )
                self._log(
                    f"{action}邮箱失败: {self._email_error_message}",
                    "error",
                )
                return False

            if self.email_info is None:
                self.email_info = {}
            self.email_info["email"] = email_value
            self.email = email_value
            if existing_account_login_only:
                local_account_id = (
                    self.extra_config.get("chatgpt_local_account_id")
                    or self.email_info.get("chatgpt_local_account_id")
                )
                # Relogin may be running against an injected/isolated SQLite
                # engine (tests and maintenance workers do this deliberately).
                # Resolve the identity in that same database when supplied;
                # otherwise retain the canonical Task1 resolver.
                auth_engine = self.extra_config.get("_chatgpt_auth_engine")
                if auth_engine is not None:
                    with Session(auth_engine) as auth_session:
                        resolved_account_id = resolve_chatgpt_auth_account_id(
                            email_value,
                            session=auth_session,
                        )
                        candidate = (
                            load_login_mfa_candidate_by_email(
                                email_value,
                                session=auth_session,
                            )
                            if resolved_account_id is not None
                            else None
                        )
                else:
                    resolved_account_id = resolve_chatgpt_auth_account_id(email_value)
                    candidate = (
                        load_login_mfa_candidate_by_email(email_value)
                        if resolved_account_id is not None
                        else None
                    )
                if local_account_id in (None, ""):
                    normalized_account_id = resolved_account_id
                else:
                    try:
                        normalized_account_id = int(local_account_id)
                    except (TypeError, ValueError) as exc:
                        raise ChatGPTAuthIdentityConflict(
                            "ChatGPT local account identity is invalid"
                        ) from exc
                    if (
                        resolved_account_id is None
                        or normalized_account_id != resolved_account_id
                    ):
                        raise ChatGPTAuthIdentityConflict(
                            "ChatGPT local account identity does not match login email"
                        )
                if auth_engine is not None and normalized_account_id != resolved_account_id:
                    candidate = None
                elif auth_engine is None:
                    candidate = (
                        load_login_mfa_candidate_by_email(email_value)
                        if normalized_account_id is not None
                        else None
                    )
                if candidate is not None and (
                    str(candidate.email or "").strip().lower()
                    != email_value.lower()
                ):
                    raise ChatGPTAuthIdentityConflict(
                        "账号确认 MFA 凭据与当前登录邮箱不匹配"
                    )
                if candidate is not None:
                    self.email_info["totp_secret"] = candidate.totp_secret
                    self.email_info["mfa_recovery_code"] = (
                        candidate.recovery_code
                    )
                    self.email_info["chatgpt_mfa_managed"] = True
                    self.email_info["mfa_rotated_at"] = (
                        candidate.remote_activated_at.isoformat()
                        if candidate.remote_activated_at is not None
                        else ""
                    )
                    self.email_info.pop("totp_url", None)
                    self._log(
                        "已加载账号确认生效的 MFA 凭据继续登录"
                    )
                account_type = str(
                    self.email_info.get("account_type") or ""
                ).strip()
                if account_type == "chatgpt_google_password":
                    password = str(self.email_info.get("password") or "")
                    if not password:
                        raise ValueError("Google 联邦登录记录缺少邮箱密码")
                    self.password = password
                    self.totp_secret = str(
                        self.email_info.get("totp_secret") or ""
                    ).strip()
                    self.password_reset_required = False
                    self._log(
                        "已识别企业域名 Google 联邦登录凭据；"
                        "将从 OpenAI 邮箱入口自动跳转 Google 登录"
                    )
                elif account_type == "chatgpt_password_totp":
                    password = str(self.email_info.get("password") or "")
                    totp_secret = str(self.email_info.get("totp_secret") or "")
                    if not password or not totp_secret:
                        raise MissingTotpCredentialsError(
                            "ChatGPT 密码 + MFA 登录记录缺少密码或 MFA 秘钥"
                        )
                    self.password = password
                    self.totp_secret = totp_secret
                    self.password_reset_required = False
                    if str(self.email_info.get("mail_api_url") or "").strip():
                        self._log(
                            "已识别 ChatGPT 密码 + MFA 登录凭据；"
                            "认证要求邮箱 OTP 时将通过 MailAPI 自动取码"
                        )
                    else:
                        self._log(
                            "已识别 ChatGPT 密码 + MFA 登录凭据；不会访问 Apple 邮箱"
                        )
                elif account_type in {
                    "chatgpt_password_url_otp",
                    "chatgpt_password_remote_totp",
                }:
                    password = str(self.email_info.get("password") or "")
                    if not password:
                        raise ValueError("ChatGPT URL 验证记录缺少登录密码")
                    self.password = password
                    self.totp_secret = str(
                        self.email_info.get("totp_secret") or ""
                    ).strip()
                    self.password_reset_required = False
                    self._log("已识别 ChatGPT 密码 + URL 邮箱/2FA 凭据")
                elif account_type == "chatgpt_password_reset_url_mail":
                    reset_required = bool(
                        self.email_info.get("password_reset_required", True)
                    )
                    password_key = "new_password" if reset_required else "password"
                    password = str(self.email_info.get(password_key) or "")
                    if len(password) < 12:
                        raise ValueError(
                            "ChatGPT 忘记密码记录缺少可用的新密码"
                        )
                    self.password = password
                    self.totp_secret = str(
                        self.email_info.get("totp_secret") or ""
                    ).strip()
                    self.password_reset_required = reset_required
                    if reset_required:
                        self._log(
                            "已识别忘记密码记录；将自动邮箱取码、设置新密码并继续登录"
                        )
                    else:
                        self._log("已加载此前重置并保存的 ChatGPT 登录密码")
                managed_totp_secret = str(
                    self.email_info.get("totp_secret") or ""
                ).strip()
                if managed_totp_secret:
                    self.totp_secret = managed_totp_secret
                if self._should_upgrade_passwordless_login_for_mfa():
                    self._prepare_passwordless_login_for_mfa()
            if existing_account_login_only:
                self._log(f"邮箱凭据加载成功: {self.email}")
            else:
                self._log(f"成功创建邮箱: {self.email}")
            return True
        except Exception as e:
            self._email_error_message = str(e).strip()
            self._email_error_code = str(getattr(e, "error_code", "") or "").strip()
            self._log(f"{action}邮箱失败: {self._email_error_message}", "error")
            return False

    def _commit_password_reset(
        self,
        email_adapter: EmailServiceAdapter,
        new_password: str,
    ) -> bool:
        committed = email_adapter.commit_password_reset(new_password)
        if committed is False:
            return False
        self.password = str(new_password or "")
        self.password_reset_required = False
        if isinstance(self.email_info, dict):
            self.email_info["password"] = self.password
            self.email_info["password_reset_required"] = False
            self.email_info.pop("new_password", None)
        return True

    def _commit_mfa_rotation(
        self,
        email_adapter: EmailServiceAdapter,
        rotation: MfaRotationResult,
    ) -> bool:
        committed = email_adapter.commit_mfa_rotation(
            totp_secret=rotation.totp_secret,
            recovery_code=rotation.recovery_code,
            rotated_at=rotation.rotated_at,
        )
        if committed is False:
            return False
        self.totp_secret = str(rotation.totp_secret or "").strip()
        if isinstance(self.email_info, dict):
            self.email_info["totp_secret"] = self.totp_secret
            self.email_info["mfa_recovery_code"] = str(
                rotation.recovery_code or ""
            ).strip()
            self.email_info["chatgpt_mfa_managed"] = True
            self.email_info["mfa_rotated_at"] = rotation.rotated_at
            self.email_info.pop("totp_url", None)
            self.email_info.pop("mfa_secret", None)
            self.email_info.pop("totp", None)
        return True

    def _consume_mfa_enrollment(
        self,
        *,
        result: RegistrationResult,
        email_adapter: EmailServiceAdapter,
        session_data: dict,
    ) -> tuple[bool, MfaRotationResult | None]:
        """Persist a mandatory MFA enrollment completed during login."""
        enrollment = (
            session_data.get("mfa_enrollment")
            if isinstance(session_data, dict)
            else None
        )
        if not isinstance(enrollment, dict) or not enrollment:
            return False, None

        totp_secret = str(enrollment.get("totp_secret") or "").strip()
        rotated_at = str(enrollment.get("rotated_at") or "").strip()
        recovery_code = str(
            enrollment.get("recovery_code") or ""
        ).strip()
        if not totp_secret and recovery_code:
            if isinstance(self.email_info, dict):
                self.email_info["mfa_recovery_code"] = recovery_code
            self._log(
                "恢复登录已刷新 MFA 恢复码，继续使用新鲜会话执行 TOTP 轮换"
            )
            return False, None
        if not totp_secret or not rotated_at:
            result.error_message = (
                "[stage=mfa_enroll] 新 MFA 已进入绑定流程，"
                "但服务端没有返回完整托管凭据"
            )
            result.error_code = "mfa_rotation_failed"
            self._log(result.error_message, "error")
            return True, None

        rotation = MfaRotationResult(
            totp_secret=totp_secret,
            recovery_code=recovery_code,
            replaced_existing=True,
            mfa_enabled=True,
            rotated_at=rotated_at,
        )
        if not self._commit_mfa_rotation(email_adapter, rotation):
            result.error_message = (
                "[stage=mfa_enroll] 新 MFA 已激活，但本地凭据保存失败"
            )
            result.error_code = "mfa_rotation_failed"
            self._log(result.error_message, "error")
            return True, None
        self._log("恢复登录后要求的新 MFA 已完成绑定并由项目托管")
        return True, rotation

    def _stage_mfa_recovery_code(
        self,
        email: str,
        recovery_code: str,
    ) -> None:
        """Durably save a recovery code even when OpenAI returns it first."""
        normalized_email = str(email or "").strip()
        normalized_code = str(recovery_code or "").strip()
        if not normalized_email or not normalized_code:
            return
        journal = load_chatgpt_mfa_rotation(normalized_email)
        if not str(journal.get("totp_secret") or "").strip():
            current_secret = str(self.totp_secret or "").strip()
            if not current_secret:
                raise RuntimeError("MFA 恢复码写前记录缺少现有 TOTP")
            stage_chatgpt_mfa_rotation(normalized_email, current_secret)
        update_chatgpt_mfa_rotation_recovery_code(
            normalized_email,
            normalized_code,
        )

    def _rotate_mfa_after_login(
        self,
        *,
        result: RegistrationResult,
        email_adapter: EmailServiceAdapter,
        session,
        access_token: str,
        account_id: str = "",
        user_agent: str = "",
        impersonate: str = "",
    ) -> MfaRotationResult | None:
        self._log("3. 登录会话已刷新，开始新增/轮换 ChatGPT MFA...")
        try:
            rotation = ChatGPTMfaManager(
                session=session,
                access_token=access_token,
                account_id=account_id,
                user_agent=user_agent,
                impersonate=impersonate,
                log_fn=self._log,
                can_recover_by_email=email_adapter.supports_email_verification(),
                can_recover_by_existing_totp=bool(
                    str(self.totp_secret or "").strip()
                ),
                allow_unrecoverable_replacement=bool(
                    self._existing_account_rotate_mfa_enabled()
                ),
                on_secret_enrolled=lambda secret: stage_chatgpt_mfa_rotation(
                    self.email or result.email,
                    secret,
                ),
                on_secret_activated=lambda rotated_at: (
                    mark_chatgpt_mfa_rotation_activated(
                        self.email or result.email,
                        rotated_at=rotated_at,
                    )
                ),
                on_recovery_code=lambda recovery_code: (
                    update_chatgpt_mfa_rotation_recovery_code(
                        self.email or result.email,
                        recovery_code,
                    )
                ),
            ).rotate()
            if not self._commit_mfa_rotation(email_adapter, rotation):
                raise MfaRotationError(
                    "[stage=mfa_rotate] 新 MFA 已激活，但本地凭据保存失败"
                )
        except MfaRotationError as exc:
            result.error_message = str(exc)
            result.error_code = "mfa_rotation_failed"
            self._log(result.error_message, "error")
            return None
        except Exception as exc:
            result.error_message = (
                "[stage=mfa_rotate] MFA 轮换异常: "
                f"{type(exc).__name__}"
            )
            result.error_code = "mfa_rotation_failed"
            self._log(result.error_message, "error")
            return None
        self._log(
            "MFA 已由项目托管；若使用共享接码地址，邮箱控制权仍未接管",
            "warning",
        )
        return rotation

    def _read_int_config(
        self,
        primary_key: str,
        *,
        fallback_keys: tuple[str, ...] = (),
        default: int,
        minimum: int,
        maximum: int,
    ) -> int:
        keys = (primary_key, *tuple(fallback_keys or ()))
        for key in keys:
            if key not in self.extra_config:
                continue
            value = self.extra_config.get(key)
            try:
                parsed = int(value)
            except Exception:
                continue
            return max(minimum, min(parsed, maximum))
        return max(minimum, min(int(default), maximum))

    @staticmethod
    def _should_switch_to_login_after_register_failure(message: str) -> bool:
        text = str(message or "").lower()
        markers = (
            "user_already_exists",
            "account already exists",
            "please login instead",
            "add_phone",
            "add-phone",
        )
        return any(marker in text for marker in markers)

    @staticmethod
    def _is_explicit_password_rejection(message: str) -> bool:
        text = str(message or "").strip().lower()
        if "401" not in text:
            return False
        if "invalid_credentials" in text:
            return True
        password_failure_markers = (
            "密码验证失败",
            "password verification failed",
            "invalid password",
        )
        return any(marker in text for marker in password_failure_markers)

    def _can_reset_rejected_url_password(self, result: RegistrationResult) -> bool:
        email_info = self.email_info if isinstance(self.email_info, dict) else {}
        return bool(
            not result.success
            and self._is_explicit_password_rejection(result.error_message)
            and str(email_info.get("account_type") or "").strip() in {
                "chatgpt_password_totp",
                "chatgpt_password_url_otp",
                "chatgpt_password_remote_totp",
            }
            and str(
                email_info.get("mail_api_url")
                or email_info.get("mailapi_url")
                or ""
            ).strip()
            and callable(getattr(self.email_service, "commit_password_reset", None))
        )

    @staticmethod
    def _is_invalid_passwordless_session(message: str) -> bool:
        text = str(message or "").strip().lower()
        return (
            "409" in text
            and (
                "sign-in session is no longer valid" in text
                or "invalid_login_session" in text
                or "invalid_auth_step" in text
            )
        )

    def _can_reset_invalid_passwordless_session(
        self,
        result: RegistrationResult,
    ) -> bool:
        email_info = self.email_info if isinstance(self.email_info, dict) else {}
        account_type = str(email_info.get("account_type") or "").strip()
        return bool(
            not result.success
            and not self.password
            and self._is_invalid_passwordless_session(result.error_message)
            and account_type in {"mailapi_url", "microsoft_oauth"}
            and str(
                email_info.get("mail_api_url")
                or email_info.get("mailapi_url")
                or ""
            ).strip()
            and callable(getattr(self.email_service, "commit_password_reset", None))
        )

    def _prepare_rejected_url_password_reset(
        self,
        result: RegistrationResult,
    ) -> None:
        replacement = generate_random_password(20)
        self.password = replacement
        self.password_reset_required = True
        result.password = replacement
        result.error_message = ""
        if isinstance(self.email_info, dict):
            self.email_info["new_password"] = replacement
            self.email_info["password_reset_required"] = True
        self._log(
            "已保存密码被认证服务明确拒绝，自动改走忘记密码流程",
            "warning",
        )

    def _prepare_invalid_passwordless_session_password(
        self,
        result: RegistrationResult,
    ) -> None:
        replacement = generate_random_password(20)
        self.password = replacement
        self.password_reset_required = True
        result.password = replacement
        result.error_message = ""
        if isinstance(self.email_info, dict):
            self.email_info["account_type"] = "chatgpt_password_reset_url_mail"
            self.email_info["new_password"] = replacement
            self.email_info["password_reset_required"] = True
        self._log(
            "无密码 OTP 会话已失效，自动补充 ChatGPT 密码并重新开始登录",
            "warning",
        )

    def _login_existing_account_with_password_reset_fallback(
        self,
        *,
        result: RegistrationResult,
        email_adapter: EmailServiceAdapter,
        otp_wait_seconds: int,
        otp_resend_wait_seconds: int,
    ) -> RegistrationResult:
        login_method = (
            self._login_existing_account_access_token
            if self._existing_account_login_stage() == "access_token"
            else self._login_existing_account
        )
        first_result = login_method(
            result=result,
            email_adapter=email_adapter,
            otp_wait_seconds=otp_wait_seconds,
            otp_resend_wait_seconds=otp_resend_wait_seconds,
        )
        if self._can_reset_invalid_passwordless_session(first_result):
            self._prepare_invalid_passwordless_session_password(first_result)
            return login_method(
                result=first_result,
                email_adapter=email_adapter,
                otp_wait_seconds=otp_wait_seconds,
                otp_resend_wait_seconds=otp_resend_wait_seconds,
            )
        if not self._can_reset_rejected_url_password(first_result):
            return first_result
        self._prepare_rejected_url_password_reset(first_result)
        return login_method(
            result=first_result,
            email_adapter=email_adapter,
            otp_wait_seconds=otp_wait_seconds,
            otp_resend_wait_seconds=otp_resend_wait_seconds,
        )

    def _build_chatgpt_client(self) -> ChatGPTClient:
        client = ChatGPTClient(
            proxy=self.proxy_url,
            verbose=False,
            browser_mode=self.browser_mode,
        )
        chain_label = "登录链路" if self._existing_account_login_only() else "注册链路"
        client._log = lambda msg: self._log(f"[{chain_label}] {msg}")
        return client

    def _build_oauth_client(self) -> OAuthClient:
        client = OAuthClient(
            self.extra_config,
            proxy=self.proxy_url,
            verbose=False,
            browser_mode=self.browser_mode,
        )
        client._log = lambda msg: self._log(f"[登录链路] {msg}")
        return client

    def _reuse_register_browser_context(
        self,
        register_client: ChatGPTClient,
        oauth_client: OAuthClient,
    ) -> None:
        oauth_client.adopt_browser_context(
            register_client.session,
            device_id=getattr(register_client, "device_id", "") or "",
            user_agent=getattr(register_client, "ua", None),
            sec_ch_ua=getattr(register_client, "sec_ch_ua", None),
            accept_language=(
                getattr(register_client.session, "headers", {}).get("Accept-Language", "")
                if getattr(register_client, "session", None) is not None
                else ""
            ),
        )
        oauth_client.impersonate = str(
            getattr(register_client, "impersonate", "") or ""
        ).strip()
        self._log("已接入前序 session/cookie/fingerprint，继续处理 OAuth 后续步骤")

    def _extract_account_info(self, tokens: dict[str, Any]) -> dict[str, Any]:
        id_token = str((tokens or {}).get("id_token") or "").strip()
        if not id_token:
            return {}
        manager = OAuthManager(proxy_url=self.proxy_url)
        return manager.extract_account_info(id_token)

    @staticmethod
    def _extract_workspace_id(oauth_client: OAuthClient) -> str:
        workspace_id = str(getattr(oauth_client, "last_workspace_id", "") or "").strip()
        if workspace_id:
            return workspace_id

        try:
            session_data = oauth_client._decode_oauth_session_cookie() or {}
        except Exception:
            session_data = {}

        workspaces = session_data.get("workspaces") or []
        if not workspaces:
            return ""
        return str((workspaces[0] or {}).get("id") or "").strip()

    @staticmethod
    def _extract_session_token(oauth_client: OAuthClient) -> str:
        getter = getattr(oauth_client, "_get_cookie_value", None)
        if not callable(getter):
            return ""
        return str(
            getter("__Secure-next-auth.session-token", "chatgpt.com")
            or getter("__Secure-authjs.session-token", "chatgpt.com")
            or ""
        ).strip()

    def _parallel_add_phone_retry(
        self,
        *,
        result,
        register_client,
        email_adapter,
        first_name: str,
        last_name: str,
        birthdate: str,
        register_otp_wait_seconds: int,
        parallel: int = 3,
    ):
        """add_phone 阻断后，并行启动多路全新 OAuth session，第一个成功的获胜。"""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        winning_tokens = None
        winning_client = None

        def _one_attempt(idx):
            client = self._build_oauth_client()
            client.config.setdefault(
                "chatgpt_oauth_otp_wait_seconds", register_otp_wait_seconds
            )
            self._log(f"add_phone 并行重试 #{idx + 1}/{parallel} 启动...")
            t = client.login_and_get_tokens(
                result.email,
                self.password,
                device_id="",
                user_agent=getattr(register_client, "ua", None),
                sec_ch_ua=getattr(register_client, "sec_ch_ua", None),
                impersonate=getattr(register_client, "impersonate", None),
                skymail_client=email_adapter,
                prefer_passwordless_login=True,
                allow_phone_verification=False,
                force_new_browser=True,
                force_chatgpt_entry=False,
                screen_hint="login",
                force_password_login=False,
                complete_about_you_if_needed=True,
                first_name=first_name,
                last_name=last_name,
                birthdate=birthdate,
                login_source=f"add_phone_parallel_{idx}",
            )
            return t, client

        with ThreadPoolExecutor(max_workers=parallel) as executor:
            futures = {executor.submit(_one_attempt, i): i for i in range(parallel)}
            for future in as_completed(futures):
                try:
                    t, client = future.result()
                    if t and not winning_tokens:
                        winning_tokens = t
                        winning_client = client
                        self._log(
                            f"add_phone 并行重试 #{futures[future] + 1} 成功，取消其余..."
                        )
                        # 取消尚未开始的 futures
                        for f in futures:
                            if f is not future:
                                f.cancel()
                        break
                except Exception as exc:
                    self._log(f"add_phone 并行重试异常: {exc}", "warning")

        return winning_tokens, winning_client

    def _populate_result_from_tokens(
        self,
        result: RegistrationResult,
        tokens: dict[str, Any],
        oauth_client: OAuthClient,
        registration_message: str,
        source: str,
        register_client: Any,
    ) -> None:
        account_info = self._extract_account_info(tokens)
        workspace_id = self._extract_workspace_id(oauth_client)
        session_token = self._extract_session_token(oauth_client)

        result.success = True
        result.email = self.email or ""
        result.password = self.password or ""
        result.access_token = str(tokens.get("access_token") or "").strip()
        result.refresh_token = str(tokens.get("refresh_token") or "").strip()
        result.id_token = str(tokens.get("id_token") or "").strip()
        result.account_id = str(
            tokens.get("account_id")
            or account_info.get("account_id")
            or ""
        ).strip()
        result.workspace_id = workspace_id
        result.session_token = session_token
        result.source = source
        result.metadata = {
            "email_service": self.email_service.service_type.value,
            "proxy_used": self.proxy_url,
            "registered_at": datetime.now().isoformat(),
            "registration_message": registration_message,
            "registration_flow": (
                "skipped_existing_account_login"
                if registration_message == "existing_account_login_only"
                else "chatgpt_client.register_complete_flow"
            ),
            "token_flow": "oauth_client.login_and_get_tokens",
            "token_login_mode": (
                "password_totp" if self.totp_secret else "passwordless"
            ),
            "browser_mode": self.browser_mode,
            "device_id": getattr(register_client, "device_id", ""),
            "impersonate": getattr(register_client, "impersonate", ""),
            "user_agent": getattr(register_client, "ua", ""),
            "workspace_id": workspace_id,
            "account_claims_email": account_info.get("email", ""),
        }

    def _login_existing_account(
        self,
        *,
        result: RegistrationResult,
        email_adapter: EmailServiceAdapter,
        otp_wait_seconds: int,
        otp_resend_wait_seconds: int,
    ) -> RegistrationResult:
        self._log("2. 登录已有 ChatGPT 账号并提取 Access Token + Refresh Token...")
        rotation = None
        authenticated_web_client = None
        if self._existing_account_should_rotate_mfa():
            self._log("先建立 ChatGPT Web 会话，确保 MFA 轮换使用新鲜认证状态")
            web_client = self._build_chatgpt_client()
            web_ok, web_session_result = (
                web_client.login_existing_account_and_get_session(
                    result.email,
                    email_adapter,
                    password=self.password or "",
                    totp_secret=self.totp_secret or "",
                    mfa_recovery_code=str(
                        (self.email_info or {}).get("mfa_recovery_code")
                        or ""
                    ),
                    on_mfa_totp_staged=lambda secret: (
                        stage_chatgpt_mfa_rotation(
                            self.email or result.email,
                            secret,
                        )
                    ),
                    on_mfa_totp_activated=lambda rotated_at: (
                        mark_chatgpt_mfa_rotation_activated(
                            self.email or result.email,
                            rotated_at=rotated_at,
                        )
                    ),
                    on_mfa_recovery_code=lambda recovery_code: (
                        self._stage_mfa_recovery_code(
                            self.email or result.email,
                            recovery_code,
                        )
                    ),
                    password_reset_required=self.password_reset_required,
                    on_password_reset=lambda new_password: self._commit_password_reset(
                        email_adapter,
                        new_password,
                    ),
                    otp_wait_timeout=otp_wait_seconds,
                    otp_resend_wait_timeout=otp_resend_wait_seconds,
                    prepare_phone_oauth=False,
                )
            )
            if not web_ok:
                result.error_message = str(
                    web_session_result
                    or "已有 ChatGPT 账号 Web 登录失败，未执行 MFA 轮换"
                )
                return result
            web_session = (
                web_session_result
                if isinstance(web_session_result, dict)
                else {}
            )
            web_access_token = str(
                web_session.get("access_token") or ""
            ).strip()
            if not web_access_token:
                result.error_message = "ChatGPT Web 会话未返回 Access Token"
                return result
            enrollment_present, rotation = self._consume_mfa_enrollment(
                result=result,
                email_adapter=email_adapter,
                session_data=web_session,
            )
            if enrollment_present:
                if rotation is None:
                    return result
            else:
                rotation = self._rotate_mfa_after_login(
                    result=result,
                    email_adapter=email_adapter,
                    session=web_client.session,
                    access_token=web_access_token,
                    account_id=str(
                        web_session.get("account_id")
                        or web_session.get("workspace_id")
                        or ""
                    ).strip(),
                    user_agent=str(getattr(web_client, "ua", "") or ""),
                    impersonate=str(
                        getattr(web_client, "impersonate", "") or ""
                    ),
                )
                if rotation is None:
                    return result
            authenticated_web_client = web_client

        oauth_client = self._build_oauth_client()
        oauth_client.config.setdefault(
            "chatgpt_oauth_otp_wait_seconds",
            otp_wait_seconds,
        )
        oauth_client.config.setdefault(
            "chatgpt_oauth_otp_resend_wait_seconds",
            otp_resend_wait_seconds,
        )
        allow_phone_verification = (
            self._existing_account_phone_verification_enabled()
        )
        password_login = bool(self.password)
        reuse_authenticated_session = authenticated_web_client is not None
        oauth_device_id = ""
        oauth_user_agent = None
        oauth_sec_ch_ua = None
        oauth_impersonate = None
        if authenticated_web_client is not None:
            self._reuse_register_browser_context(
                authenticated_web_client,
                oauth_client,
            )
            oauth_device_id = str(
                getattr(authenticated_web_client, "device_id", "") or ""
            )
            oauth_user_agent = getattr(authenticated_web_client, "ua", None)
            oauth_sec_ch_ua = getattr(
                authenticated_web_client,
                "sec_ch_ua",
                None,
            )
            oauth_impersonate = getattr(
                authenticated_web_client,
                "impersonate",
                None,
            )
            self._log(
                "复用刚完成登录和 MFA 轮换的认证会话获取 Refresh Token，"
                "不再重复登录 Google"
            )
        oauth_login_kwargs = {
            "device_id": oauth_device_id,
            "user_agent": oauth_user_agent,
            "sec_ch_ua": oauth_sec_ch_ua,
            "impersonate": oauth_impersonate,
            "skymail_client": email_adapter,
            "prefer_passwordless_login": not password_login,
            "allow_phone_verification": allow_phone_verification,
            "force_new_browser": not reuse_authenticated_session,
            "resume_authenticated_session": reuse_authenticated_session,
            "force_chatgpt_entry": False,
            "screen_hint": "login",
            "force_password_login": password_login,
            "totp_secret": self.totp_secret or "",
            "mfa_recovery_code": str(
                (self.email_info or {}).get("mfa_recovery_code") or ""
            ),
            "on_mfa_totp_staged": lambda secret: stage_chatgpt_mfa_rotation(
                self.email or result.email,
                secret,
            ),
            "on_mfa_totp_activated": lambda rotated_at: (
                mark_chatgpt_mfa_rotation_activated(
                    self.email or result.email,
                    rotated_at=rotated_at,
                )
            ),
            "on_mfa_recovery_code": lambda recovery_code: (
                self._stage_mfa_recovery_code(
                    self.email or result.email,
                    recovery_code,
                )
            ),
            "password_reset_required": self.password_reset_required,
            "on_password_reset": lambda new_password: self._commit_password_reset(
                email_adapter,
                new_password,
            ),
            "complete_about_you_if_needed": False,
            "login_source": "existing_account_login_only",
        }
        tokens = oauth_client.login_and_get_tokens(
            result.email,
            self.password or "",
            **oauth_login_kwargs,
        )
        if (
            not tokens
            and reuse_authenticated_session
            and "OpenAI 登录会话已失效" in str(oauth_client.last_error or "")
        ):
            self._log(
                "网页认证会话不能直接续接 Codex OAuth，"
                "自动切换完整授权并使用项目保存的新 MFA",
                "warning",
            )
            oauth_login_kwargs["resume_authenticated_session"] = False
            oauth_login_kwargs["force_new_browser"] = False
            tokens = oauth_client.login_and_get_tokens(
                result.email,
                self.password or "",
                **oauth_login_kwargs,
            )
        if not tokens:
            result.error_message = (
                oauth_client.last_error or "已有 ChatGPT 账号 OAuth 登录失败"
            )
            return result

        access_token = str(tokens.get("access_token") or "").strip()
        refresh_token = str(tokens.get("refresh_token") or "").strip()
        missing_tokens = []
        if not access_token:
            missing_tokens.append("Access Token")
        if not refresh_token:
            missing_tokens.append("Refresh Token")
        if missing_tokens:
            result.error_message = (
                "已有账号登录未同时获取 " + " 和 ".join(missing_tokens)
            )
            return result

        raw_oauth_enrollment = getattr(
            oauth_client,
            "last_mfa_enrollment",
            {},
        )
        oauth_enrollment = (
            dict(raw_oauth_enrollment)
            if isinstance(raw_oauth_enrollment, dict)
            else {}
        )
        if oauth_enrollment:
            enrollment_present, oauth_rotation = self._consume_mfa_enrollment(
                result=result,
                email_adapter=email_adapter,
                session_data={"mfa_enrollment": oauth_enrollment},
            )
            if enrollment_present and oauth_rotation is None:
                return result
            if oauth_rotation is not None:
                rotation = oauth_rotation

        self._populate_result_from_tokens(
            result=result,
            tokens=tokens,
            oauth_client=oauth_client,
            registration_message="existing_account_login_only",
            source="login",
            register_client=None,
        )
        if isinstance(result.metadata, dict):
            metadata_getter = getattr(
                self.email_service,
                "get_mailbox_metadata",
                None,
            )
            if callable(metadata_getter):
                try:
                    result.metadata["mailbox_login_context"] = (
                        metadata_getter() or {}
                    )
                except Exception as exc:
                    self._log(f"读取 MFA 托管凭据上下文失败: {exc}", "warning")
            if rotation is not None:
                result.metadata["mfa_rotation"] = {
                    "managed": True,
                    "replaced_existing": rotation.replaced_existing,
                    "recovery_code_saved": bool(rotation.recovery_code),
                    "rotated_at": rotation.rotated_at,
                    "mailbox_control_risk": "shared_receiver",
                }
        self._log("已有账号登录完成，Access Token 与 Refresh Token 均已获取")
        return result

    def _login_existing_account_access_token(
        self,
        *,
        result: RegistrationResult,
        email_adapter: EmailServiceAdapter,
        otp_wait_seconds: int,
        otp_resend_wait_seconds: int,
    ) -> RegistrationResult:
        self._log("2. 登录已有 ChatGPT 账号并提取 Access Token...")
        chatgpt_client = self._build_chatgpt_client()
        ok, session_result = chatgpt_client.login_existing_account_and_get_session(
            result.email,
            email_adapter,
            password=self.password or "",
            totp_secret=self.totp_secret or "",
            mfa_recovery_code=str(
                (self.email_info or {}).get("mfa_recovery_code") or ""
            ),
            on_mfa_totp_staged=lambda secret: stage_chatgpt_mfa_rotation(
                self.email or result.email,
                secret,
            ),
            on_mfa_totp_activated=lambda rotated_at: (
                mark_chatgpt_mfa_rotation_activated(
                    self.email or result.email,
                    rotated_at=rotated_at,
                )
            ),
            on_mfa_recovery_code=lambda recovery_code: (
                self._stage_mfa_recovery_code(
                    self.email or result.email,
                    recovery_code,
                )
            ),
            password_reset_required=self.password_reset_required,
            on_password_reset=lambda new_password: self._commit_password_reset(
                email_adapter,
                new_password,
            ),
            otp_wait_timeout=otp_wait_seconds,
            otp_resend_wait_timeout=otp_resend_wait_seconds,
        )
        if not ok:
            result.error_message = str(
                session_result or "已有 ChatGPT 账号邮箱登录失败"
            )
            return result

        session_data = session_result if isinstance(session_result, dict) else {}
        access_token = str(session_data.get("access_token") or "").strip()
        if not access_token:
            result.error_message = "已有账号邮箱登录未获取 Access Token"
            return result

        rotation = None
        enrollment_present, rotation = self._consume_mfa_enrollment(
            result=result,
            email_adapter=email_adapter,
            session_data=session_data,
        )
        if enrollment_present and rotation is None:
            return result
        if self._existing_account_should_rotate_mfa() and not enrollment_present:
            rotation = self._rotate_mfa_after_login(
                result=result,
                email_adapter=email_adapter,
                session=chatgpt_client.session,
                access_token=access_token,
                account_id=str(
                    session_data.get("account_id")
                    or session_data.get("workspace_id")
                    or ""
                ).strip(),
                user_agent=str(getattr(chatgpt_client, "ua", "") or ""),
                impersonate=str(
                    getattr(chatgpt_client, "impersonate", "") or ""
                ),
            )
            if rotation is None:
                return result
            refreshed_browser_context = serialize_oauth_resume_context(
                chatgpt_client.session,
                device_id=str(getattr(chatgpt_client, "device_id", "") or ""),
                user_agent=str(getattr(chatgpt_client, "ua", "") or ""),
                sec_ch_ua=str(
                    getattr(chatgpt_client, "sec_ch_ua", "") or ""
                ),
                accept_language=str(
                    getattr(chatgpt_client, "accept_language", "") or ""
                ),
                impersonate=str(
                    getattr(chatgpt_client, "impersonate", "") or ""
                ),
                ttl_seconds=1800,
            )
            if isinstance(refreshed_browser_context, dict):
                chatgpt_client.phone_oauth_browser_context = (
                    refreshed_browser_context
                )
            chatgpt_client.phone_oauth_resume_context = None
            chatgpt_client.phone_oauth_resume_error = (
                "MFA 轮换后将从最新认证浏览器快照建立手机验证事务"
            )
            self._log(
                "MFA 轮换后已作废旧手机 OAuth 预建事务，将使用最新会话继续"
            )

        prepared_context = getattr(
            chatgpt_client, "phone_oauth_resume_context", None
        )
        browser_context = getattr(
            chatgpt_client, "phone_oauth_browser_context", {}
        )
        if not isinstance(browser_context, dict):
            browser_context = {}
        prepared_ready = bool(
            prepared_context is not None
            and isinstance(getattr(prepared_context, "code_verifier", None), str)
            and str(getattr(prepared_context, "code_verifier", "") or "").strip()
            and isinstance(getattr(prepared_context, "oauth_state", None), str)
            and str(getattr(prepared_context, "oauth_state", "") or "").strip()
            and isinstance(getattr(prepared_context, "flow_state", None), FlowState)
        )
        oauth_resume_context = {}
        if prepared_ready:
            context_kwargs = {
                "session": prepared_context.session,
                "device_id": prepared_context.device_id,
                "user_agent": prepared_context.user_agent,
                "sec_ch_ua": prepared_context.sec_ch_ua,
                "accept_language": prepared_context.accept_language,
                "impersonate": prepared_context.impersonate,
                "code_verifier": prepared_context.code_verifier,
                "oauth_state": prepared_context.oauth_state,
                "authorize_url": prepared_context.authorize_url,
                "authorize_params": prepared_context.authorize_params,
                "flow_state": prepared_context.flow_state,
                "referer": prepared_context.referer,
            }
            oauth_resume_cache.remember(result.email, **context_kwargs)
            oauth_resume_context = serialize_oauth_resume_context(
                **context_kwargs,
                ttl_seconds=1800,
            )
        else:
            oauth_resume_cache.take(result.email)
            if browser_context:
                self._log(
                    "Access Token 已获取；接码阶段将从已认证浏览器快照恢复新事务",
                    "warning",
                )
            else:
                self._log(
                    "Access Token 已获取，但手机授权事务与认证浏览器快照均未生成",
                    "warning",
                )

        raw_prepare_diagnostic = getattr(
            chatgpt_client, "phone_oauth_prepare_diagnostic", {}
        )
        phone_oauth_prepare_diagnostic = {}
        if isinstance(raw_prepare_diagnostic, dict):
            raw_page_type = str(
                raw_prepare_diagnostic.get("page_type") or "unknown"
            ).strip().lower()
            safe_page_type = "".join(
                character
                for character in raw_page_type
                if character.isalnum() or character in {"_", "-"}
            )[:64] or "unknown"
            phone_oauth_prepare_diagnostic = {
                "stage": "phone_oauth_prepare",
                "attempt": max(0, int(raw_prepare_diagnostic.get("attempt") or 0)),
                "page_type": safe_page_type,
                "http_status": max(
                    0, int(raw_prepare_diagnostic.get("http_status") or 0)
                ),
                "recovery_status": (
                    "recovered"
                    if raw_prepare_diagnostic.get("recovery_status") == "recovered"
                    else "deferred"
                ),
            }

        mailbox_context = {}
        metadata_getter = getattr(self.email_service, "get_mailbox_metadata", None)
        if callable(metadata_getter):
            try:
                mailbox_context = metadata_getter() or {}
            except Exception as exc:
                self._log(f"读取邮箱登录上下文失败: {exc}", "warning")

        result.success = True
        result.access_token = access_token
        result.refresh_token = ""
        result.session_token = str(session_data.get("session_token") or "").strip()
        result.account_id = str(
            session_data.get("account_id")
            or session_data.get("user_id")
            or ""
        ).strip()
        result.workspace_id = str(session_data.get("workspace_id") or "").strip()
        result.source = "existing_account_web_login"
        result.metadata = {
            "email_service": self.email_service.service_type.value,
            "proxy_used": self.proxy_url,
            "registered_at": datetime.now().isoformat(),
            "registration_message": "existing_account_access_token_login",
            "registration_flow": "skipped_existing_account_web_login",
            "token_flow": "chatgpt_client.login_existing_account_and_get_session",
            "token_login_mode": (
                "password_totp" if self.totp_secret else "passwordless"
            ),
            "browser_mode": self.browser_mode,
            "workspace_id": result.workspace_id,
            "phone_verification_required": True,
            "phone_oauth_ready": prepared_ready,
            "phone_oauth_prepare_error": str(
                getattr(chatgpt_client, "phone_oauth_resume_error", "") or ""
            ).strip(),
            "phone_oauth_prepare_diagnostic": phone_oauth_prepare_diagnostic,
            "mailbox_login_context": mailbox_context,
            "oauth_resume_context": oauth_resume_context,
            "oauth_browser_context": browser_context,
        }
        if rotation is not None:
            result.metadata["mfa_rotation"] = {
                "managed": True,
                "replaced_existing": rotation.replaced_existing,
                "recovery_code_saved": bool(rotation.recovery_code),
                "rotated_at": rotation.rotated_at,
                "mailbox_control_risk": "shared_receiver",
            }
        self._log("已有账号邮箱登录完成，Access Token 已获取；Refresh Token 等待手机验证")
        return result

    def run(self) -> RegistrationResult:
        result = RegistrationResult(success=False, logs=self.logs)
        last_error = ""
        fixed_email = str(self.email or "").strip()
        register_otp_wait_seconds = self._read_int_config(
            "chatgpt_register_otp_wait_seconds",
            fallback_keys=("chatgpt_otp_wait_seconds",),
            default=600,
            minimum=30,
            maximum=3600,
        )
        register_otp_resend_wait_seconds = self._read_int_config(
            "chatgpt_register_otp_resend_wait_seconds",
            fallback_keys=("chatgpt_register_otp_wait_seconds", "chatgpt_otp_wait_seconds"),
            default=300,
            minimum=30,
            maximum=3600,
        )

        try:
            existing_account_login_only = self._existing_account_login_only()
            registration_message = ""
            source = "register"

            self._log("=" * 60)
            if existing_account_login_only:
                self._log("ChatGPT 已有账号登录链路启动")
            else:
                self._log("ChatGPT RT 全新主链路启动")
            self._log(f"请求模式: {self.browser_mode}")
            if existing_account_login_only:
                self._log("实现策略: 根据导入凭据选择密码/MFA或邮箱 OTP 登录")
            else:
                self._log("实现策略: 注册状态机 + OAuth 接续流程")
            self._log("=" * 60)

            if not fixed_email:
                self.email = None

            if existing_account_login_only:
                self._log("1. 加载邮箱凭据...")
            else:
                self._log("1. 创建邮箱...")
            if not self._create_email(
                existing_account_login_only=existing_account_login_only
            ):
                generic_error = (
                    "加载邮箱失败"
                    if existing_account_login_only
                    else "创建邮箱失败"
                )
                detail = self._email_error_message
                last_error = f"{generic_error}: {detail}" if detail else generic_error
                result.error_message = last_error
                result.error_code = self._email_error_code
                return result

            result.email = self.email or ""
            if existing_account_login_only:
                self.password = self.password or ""
            else:
                self.password = self.password or generate_random_password(16)
            result.password = self.password

            email_adapter = EmailServiceAdapter(
                self.email_service,
                result.email,
                self._log,
            )
            if existing_account_login_only:
                return self._login_existing_account_with_password_reset_fallback(
                    result=result,
                    email_adapter=email_adapter,
                    otp_wait_seconds=register_otp_wait_seconds,
                    otp_resend_wait_seconds=register_otp_resend_wait_seconds,
                )

            first_name, last_name = generate_random_name()
            birthdate = generate_random_birthday()
            self._log(f"邮箱: {result.email}")
            self._log(f"密码: {self.password}")
            self._log(f"注册信息: {first_name} {last_name}, 生日: {birthdate}")
            self._log("流程策略: 注册阶段推进到 about_you 后切换到 OAuth 流程继续完成后续步骤")
            self._log(
                "验证码等待策略: "
                f"register_wait={register_otp_wait_seconds}s, "
                f"register_resend_wait={register_otp_resend_wait_seconds}s, "
                "oauth_wait=读取 OAuthClient 配置（默认600s）"
            )

            _REG_RETRY_MARKERS = ("访问首页失败", "预授权被拦截")
            registered = False
            registration_message = ""
            for _reg_attempt in range(3):
                if _reg_attempt > 0:
                    self._log(
                        f"注册状态机重试 {_reg_attempt}/2（原因: {registration_message}）..."
                    )
                register_client = self._build_chatgpt_client()
                self._log("2. 执行注册状态机（interrupt 模式：不在注册阶段提交 about_you）...")
                registered, registration_message = register_client.register_complete_flow(
                    result.email,
                    self.password,
                    first_name,
                    last_name,
                    birthdate,
                    email_adapter,
                    stop_before_about_you_submission=True,
                    otp_wait_timeout=register_otp_wait_seconds,
                    otp_resend_wait_timeout=register_otp_resend_wait_seconds,
                )
                if registered:
                    break
                if not any(m in registration_message for m in _REG_RETRY_MARKERS):
                    break

            if not registered:
                if not self._should_switch_to_login_after_register_failure(
                    registration_message
                ):
                    last_error = f"注册状态机失败: {registration_message}"
                    result.error_message = last_error
                    return result

                self._log(
                    "注册阶段命中可继续处理的终态，改走 OAuth 登录流程",
                    "warning",
                )
                self._log(f"切换原因: {registration_message}")
                source = "login"
            else:
                if registration_message == "pending_about_you_submission":
                    self._log("注册状态机已推进至 about_you，符合预期。下一步进入 OAuth 会话补全资料")
                else:
                    self._log(
                        "注册状态机返回成功但未停在 about_you。"
                        "将继续进入 OAuth 会话，按状态机实际返回推进。"
                    )

            oauth_client = self._build_oauth_client()
            oauth_client.config.setdefault(
                "chatgpt_oauth_otp_wait_seconds",
                register_otp_wait_seconds,
            )
            oauth_client.config.setdefault(
                "chatgpt_oauth_otp_resend_wait_seconds",
                register_otp_resend_wait_seconds,
            )

            use_continued_session = registered and (
                registration_message == "pending_about_you_submission"
            )

            if use_continued_session:
                self._reuse_register_browser_context(register_client, oauth_client)
                self._log("3. 承接前序 session，继续走 OAuth passwordless 流程")
                self._log("4. 沿用前序阶段的 cookie / device_id / 浏览器指纹")
                self._log("5. 登录成功后提交 about_you，并继续 workspace/token 流程")
                tokens = oauth_client.login_and_get_tokens(
                    result.email,
                    self.password,
                    device_id=getattr(register_client, "device_id", "") or "",
                    user_agent=getattr(register_client, "ua", None),
                    sec_ch_ua=getattr(register_client, "sec_ch_ua", None),
                    impersonate=getattr(register_client, "impersonate", None),
                    skymail_client=email_adapter,
                    prefer_passwordless_login=True,
                    allow_phone_verification=False,
                    force_new_browser=False,
                    force_chatgpt_entry=False,
                    screen_hint="login",
                    force_password_login=False,
                    complete_about_you_if_needed=True,
                    first_name=first_name,
                    last_name=last_name,
                    birthdate=birthdate,
                    login_source="post_register_workspace_continue",
                )
            else:
                self._log("3. 新开 OAuth session，按 screen_hint=login + passwordless OTP 登录...")
                self._log("4. 若命中 about_you，则在 OAuth 会话内提交姓名+生日，再继续 workspace/token")
                tokens = oauth_client.login_and_get_tokens(
                    result.email,
                    self.password,
                    device_id="",
                    user_agent=getattr(register_client, "ua", None),
                    sec_ch_ua=getattr(register_client, "sec_ch_ua", None),
                    impersonate=getattr(register_client, "impersonate", None),
                    skymail_client=email_adapter,
                    prefer_passwordless_login=True,
                    allow_phone_verification=False,
                    force_new_browser=True,
                    force_chatgpt_entry=False,
                    screen_hint="login",
                    force_password_login=False,
                    complete_about_you_if_needed=True,
                    first_name=first_name,
                    last_name=last_name,
                    birthdate=birthdate,
                    login_source=(
                        "existing_account_continue" if source == "login" else "post_register_workspace_continue"
                    ),
                )

            if not tokens:
                last_error = oauth_client.last_error or "OAuth 登录状态机失败"
                if "add_phone" in last_error:
                    self._log(
                        "OAuth add_phone 阻断，启动并行 OAuth 重试（3 路并发）...",
                        "warning",
                    )
                    tokens, oauth_client = self._parallel_add_phone_retry(
                        result=result,
                        register_client=register_client,
                        email_adapter=email_adapter,
                        first_name=first_name,
                        last_name=last_name,
                        birthdate=birthdate,
                        register_otp_wait_seconds=register_otp_wait_seconds,
                    )
                    if not tokens:
                        last_error = (oauth_client.last_error if oauth_client else None) or last_error
                if not tokens:
                    result.error_message = last_error
                    return result

            self._populate_result_from_tokens(
                result=result,
                tokens=tokens,
                oauth_client=oauth_client,
                registration_message=registration_message,
                source=source,
                register_client=register_client,
            )

            self._log("5. 主链路完成")
            self._log(f"Account ID: {result.account_id}")
            self._log(f"Workspace ID: {result.workspace_id}")
            self._log("=" * 60)
            return result

        except TaskInterruption:
            raise
        except Exception as e:
            self._log(f"RT 注册主链路异常: {e}", "error")
            result.error_message = str(e)
            return result

    def save_to_database(self, result: RegistrationResult) -> bool:
        """保留旧接口，占位返回。"""
        return bool(result and result.success)
