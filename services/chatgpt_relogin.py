"""Re-login saved ChatGPT accounts and replace their Codex2API credentials."""

from __future__ import annotations

import math
import threading
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from sqlalchemy import func, update
from sqlmodel import Session, select

from core.applemail_pool import load_applemail_pool_records
from core.base_mailbox import MailboxAccount, create_mailbox
from core.config_store import config_store
from core.db import AccountModel, OutlookAccountModel, engine
from core.task_runtime import TaskInterruption
from platforms.chatgpt.chatgpt_registration_mode_adapter import (
    ChatGPTRegistrationContext,
    build_chatgpt_registration_mode_adapter,
)
from platforms.chatgpt.token_refresh import TokenRefreshManager
from services.chatgpt_account_state import (
    ChatGPTAccountDeactivatedError,
    account_is_visible_in_default_list,
)
from services.chatgpt_account_coordination import (
    chatgpt_account_operation_lock,
    codex2api_account_mutation_lock,
)
from services.chatgpt_account_removal import remove_account
from services.external_sync import sync_codex2api_account


LogFn = Callable[[str], None]


class ChatGPTMailboxOTPTimeoutError(RuntimeError):
    """The email OTP stage exhausted its full budget without any code."""

    def __init__(
        self,
        message: str,
        *,
        wait_seconds: int,
        elapsed_seconds: float,
    ) -> None:
        super().__init__(message)
        self.wait_seconds = int(wait_seconds)
        self.elapsed_seconds = float(elapsed_seconds)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _emit(log_fn: LogFn | None, message: str) -> None:
    if callable(log_fn):
        log_fn(str(message))


def _emit_observer(log_fn: LogFn | None, message: str) -> None:
    """A logging observer must never change a completed business operation."""
    try:
        _emit(log_fn, message)
    except Exception:
        return


def _checkpoint_task(task_control, attempt_id: int | None) -> None:
    if task_control is not None:
        task_control.checkpoint(attempt_id=attempt_id)


def _resolve_mailbox_otp_timeout(config: Mapping[str, Any]) -> int:
    for value in (
        config.get("mailbox_otp_timeout_seconds"),
        config.get("email_otp_timeout_seconds"),
    ):
        if value in (None, ""):
            continue
        try:
            seconds = int(value)
        except (TypeError, ValueError):
            continue
        if seconds > 0:
            return max(30, min(seconds, 3600))
    return 300


def _is_exhausted_mailbox_otp_failure(
    detail: str,
    *,
    wait_seconds: int,
    elapsed_seconds: float,
) -> bool:
    message = _text(detail)
    return (
        int(wait_seconds) >= 180
        and float(elapsed_seconds) >= float(wait_seconds)
        and "[stage=otp]" in message
        and "OAuth 阶段 OTP 验证失败" in message
        and "已尝试 0 个验证码" in message
        and f"等待窗口 {int(wait_seconds)}s" in message
    )


def _bind_mailbox_task_control(
    mailbox,
    task_control,
    attempt_id: int | None,
) -> None:
    if task_control is None:
        return
    setattr(mailbox, "_task_control", task_control)
    setattr(mailbox, "_task_attempt_token", attempt_id)


def _mailbox_context_from_outlook(
    session: Session,
    email: str,
) -> dict[str, Any] | None:
    imported = session.exec(
        select(OutlookAccountModel)
        .where(func.lower(OutlookAccountModel.email) == email.lower())
        .where(OutlookAccountModel.enabled == True)  # noqa: E712
    ).first()
    if imported is None:
        return None
    return {
        "provider": "microsoft",
        "email": _text(imported.email) or email,
        "account_id": _text(imported.id),
        "extra": {
            "provider": "microsoft",
            "password": str(imported.password or ""),
            "client_id": _text(imported.client_id),
            "refresh_token": _text(imported.refresh_token),
            "account_type": _text(imported.account_type) or "microsoft_oauth",
            "mailapi_url": _text(imported.mailapi_url),
        },
    }


def _resolve_saved_mailbox_provider(
    mailbox_context: Mapping[str, Any],
    context_extra: Mapping[str, Any],
) -> tuple[str, str]:
    provider = _text(mailbox_context.get("provider")).lower()
    if provider == "custom_provider":
        provider = _text(context_extra.get("provider")).lower()
    account_type = _text(context_extra.get("account_type")).lower()
    return provider, account_type


_PUBLIC_RECEIVE_PROVIDERS = frozenset(
    {"tempmail_lol", "duckmail", "gptmail"}
)
_SESSION_BOUND_RECEIVE_PROVIDERS = frozenset({"freemail", "moemail"})


def _merged_saved_mailbox_config(
    config: Mapping[str, Any] | None,
    context_extra: Mapping[str, Any],
) -> dict[str, Any] | None:
    if config is None:
        try:
            merged = dict(config_store.get_all() or {})
        except Exception:
            return None
    else:
        merged = dict(config)
    merged.update(context_extra)
    return merged


def _provider_receive_config_is_usable(
    provider: str,
    mailbox_config: Mapping[str, Any],
) -> bool:
    """Validate receive-time configuration without constructing a mailbox."""

    if provider in _SESSION_BOUND_RECEIVE_PROVIDERS:
        return False
    if provider in _PUBLIC_RECEIVE_PROVIDERS:
        return True
    if provider == "skymail":
        return bool(_text(mailbox_config.get("skymail_token")))
    if provider == "cloudmail":
        api_base = _text(
            mailbox_config.get("cloudmail_api_base")
            or mailbox_config.get("base_url")
        )
        admin_password = _text(
            mailbox_config.get("cloudmail_admin_password")
            or mailbox_config.get("admin_password")
            or mailbox_config.get("api_key")
        )
        return bool(api_base and admin_password)
    if provider == "maliapi":
        return bool(_text(mailbox_config.get("maliapi_api_key")))
    if provider == "opentrashmail":
        return bool(_text(mailbox_config.get("opentrashmail_api_url")))
    if provider == "cfworker":
        return bool(_text(mailbox_config.get("cfworker_api_url")))
    if provider == "luckmail":
        return bool(_text(mailbox_config.get("luckmail_api_key")))
    return bool(_text(mailbox_config.get("laoudo_auth")))


def _mailbox_context_has_usable_credentials(
    saved: dict[str, Any],
    mailbox_context: object,
    config: Mapping[str, Any] | None,
) -> bool:
    if not isinstance(mailbox_context, dict) or not mailbox_context:
        return False
    context_extra = mailbox_context.get("extra")
    if not isinstance(context_extra, dict):
        return False
    provider, account_type = _resolve_saved_mailbox_provider(
        mailbox_context,
        context_extra,
    )
    if account_type == "chatgpt_google_password":
        return bool(
            _text(context_extra.get("password") or saved.get("password"))
        )
    if account_type == "chatgpt_password_remote_totp":
        return bool(
            _text(context_extra.get("password") or saved.get("password"))
            and _text(context_extra.get("totp_url"))
        )
    if account_type in {
        "chatgpt_password_url_otp",
        "chatgpt_password_reset_url_mail",
    }:
        recovery_config = dict(config or {})
        if config is None:
            try:
                recovery_config = dict(config_store.get_all() or {})
            except Exception:
                return False
        try:
            credentials = _recover_url_login_credentials(
                saved,
                mailbox_context,
                recovery_config,
            )
        except Exception:
            return False
        return bool(
            credentials.get("mail_api_url")
            and (
                bool(credentials.get("password"))
                or bool(credentials.get("password_reset_required"))
            )
        )
    if provider == "chatgpt_credentials" or account_type == "chatgpt_password_totp":
        recovery_config = dict(config or {})
        if config is None and not _text(
            context_extra.get("totp_secret")
            or context_extra.get("mfa_secret")
            or context_extra.get("totp")
        ) and _text(context_extra.get("pool_file")):
            try:
                recovery_config = dict(config_store.get_all() or {})
            except Exception:
                return False
        try:
            _recover_password_totp_credentials(
                saved,
                mailbox_context,
                recovery_config,
            )
        except Exception:
            return False
        return True
    if provider == "icloud" or account_type == "icloud_web":
        return bool(
            _text(context_extra.get("password"))
            and _text(context_extra.get("mfa_secret"))
        )
    if provider == "applemail":
        return bool(
            _text(context_extra.get("client_id"))
            and _text(context_extra.get("refresh_token"))
        )
    if provider in {"outlook", "microsoft"}:
        return bool(
            _text(context_extra.get("password"))
            or (
                _text(context_extra.get("client_id"))
                and _text(context_extra.get("refresh_token"))
            )
            or _text(context_extra.get("mailapi_url"))
        )
    if not provider:
        return False
    has_persisted_identity = bool(
        _text(mailbox_context.get("email"))
        or _text(mailbox_context.get("account_id"))
    )
    if not has_persisted_identity:
        return False
    mailbox_config = _merged_saved_mailbox_config(config, context_extra)
    return bool(
        mailbox_config is not None
        and _provider_receive_config_is_usable(provider, mailbox_config)
    )


def _outlook_account_has_usable_credentials(account: OutlookAccountModel) -> bool:
    return bool(
        _text(account.password)
        or (_text(account.client_id) and _text(account.refresh_token))
        or _text(account.mailapi_url)
    )


def _is_saved_chatgpt_account_relogin_eligible_in_session(
    session: Session,
    account: AccountModel | None,
    config: Mapping[str, Any] | None,
) -> bool:
    if (
        account is None
        or account.platform != "chatgpt"
        or not _text(account.email)
        or not account_is_visible_in_default_list(account)
    ):
        return False
    try:
        extra = dict(account.get_extra() or {})
    except Exception:
        return False
    mailbox_context = extra.get("mailbox_login_context")
    if isinstance(mailbox_context, dict) and mailbox_context:
        return _mailbox_context_has_usable_credentials(
            {
                "email": _text(account.email),
                "password": str(account.password or ""),
                "extra": extra,
            },
            mailbox_context,
            config,
        )
    imported = session.exec(
        select(OutlookAccountModel)
        .where(func.lower(OutlookAccountModel.email) == _text(account.email).lower())
        .where(OutlookAccountModel.enabled == True)  # noqa: E712
    ).first()
    return bool(
        imported is not None
        and _outlook_account_has_usable_credentials(imported)
    )


def is_saved_chatgpt_account_relogin_eligible(
    account_id: int,
    *,
    session: Session | None = None,
    database_engine=None,
    config: Mapping[str, Any] | None = None,
) -> bool:
    """Return whether a saved account has a usable re-login path.

    The result contains no credential material and performs no writes.
    """

    try:
        normalized_id = int(account_id)
    except (TypeError, ValueError):
        return False
    if session is not None:
        return _is_saved_chatgpt_account_relogin_eligible_in_session(
            session,
            session.get(AccountModel, normalized_id),
            config,
        )
    resolved_engine = engine if database_engine is None else database_engine
    with Session(resolved_engine) as owned_session:
        return _is_saved_chatgpt_account_relogin_eligible_in_session(
            owned_session,
            owned_session.get(AccountModel, normalized_id),
            config,
        )


def list_relogin_eligible_account_ids(
    *,
    session: Session | None = None,
    database_engine=None,
    config: Mapping[str, Any] | None = None,
) -> list[int]:
    """List eligible local ChatGPT account IDs in deterministic ID order."""

    def _list(active_session: Session) -> list[int]:
        accounts = active_session.exec(
            select(AccountModel)
            .where(AccountModel.platform == "chatgpt")
            .order_by(AccountModel.id)
        ).all()
        return [
            int(account.id)
            for account in accounts
            if account.id is not None
            and _is_saved_chatgpt_account_relogin_eligible_in_session(
                active_session,
                account,
                config,
            )
        ]

    if session is not None:
        return _list(session)
    resolved_engine = engine if database_engine is None else database_engine
    with Session(resolved_engine) as owned_session:
        return _list(owned_session)


def list_auto_maintenance_account_ids(
    *,
    session: Session | None = None,
    database_engine=None,
) -> list[int]:
    """List every visible ChatGPT account that has a Refresh Token."""

    def _list(active_session: Session) -> list[int]:
        accounts = active_session.exec(
            select(AccountModel)
            .where(AccountModel.platform == "chatgpt")
            .order_by(AccountModel.id)
        ).all()
        return [
            int(account.id)
            for account in accounts
            if account.id is not None
            and _text(account.email)
            and account_is_visible_in_default_list(account)
        ]

    if session is not None:
        return _list(session)
    resolved_engine = engine if database_engine is None else database_engine
    with Session(resolved_engine) as owned_session:
        return _list(owned_session)


def _load_saved_account(account_id: int) -> dict[str, Any]:
    try:
        normalized_id = int(account_id)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("ChatGPT 账号 ID 无效") from exc

    with Session(engine) as session:
        account = session.get(AccountModel, normalized_id)
        if account is None or account.platform != "chatgpt":
            raise RuntimeError("ChatGPT 账号不存在")
        email = _text(account.email)
        if not email:
            raise RuntimeError("ChatGPT 账号邮箱未填写")
        extra = dict(account.get_extra() or {})
        mailbox_context = extra.get("mailbox_login_context")
        if not isinstance(mailbox_context, dict) or not mailbox_context:
            mailbox_context = _mailbox_context_from_outlook(session, email)
            if mailbox_context:
                extra["mailbox_login_context"] = mailbox_context
                account.set_extra(extra)
                account.updated_at = datetime.now(timezone.utc)
                session.add(account)
                session.commit()
        return {
            "id": normalized_id,
            "email": email,
            "created_at": account.created_at,
            "updated_at": account.updated_at,
            "password": str(account.password or ""),
            "user_id": _text(account.user_id),
            "extra": extra,
            "mailbox_context": mailbox_context,
        }


def _recover_password_totp_credentials(
    saved: dict[str, Any],
    mailbox_context: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    email = _text(mailbox_context.get("email")) or saved["email"]
    context_extra = dict(mailbox_context.get("extra") or {})
    password = str(context_extra.get("password") or saved.get("password") or "")
    totp_secret = _text(
        context_extra.get("totp_secret")
        or context_extra.get("mfa_secret")
        or context_extra.get("totp")
    )
    pool_file = _text(context_extra.get("pool_file"))
    mail_api_url = _text(
        context_extra.get("mail_api_url")
        or context_extra.get("mailapi_url")
    )
    record: dict[str, Any] | None = None

    if not totp_secret or (pool_file and not mail_api_url):
        if not pool_file:
            if not totp_secret:
                raise RuntimeError(
                    f"账号 {email} 缺少邮箱登录凭据：未保存 MFA 秘钥来源"
                )
        else:
            try:
                _path, records = load_applemail_pool_records(
                    pool_file=pool_file,
                    pool_dir=_text(config.get("applemail_pool_dir")) or "mail",
                )
            except Exception as exc:
                if not totp_secret:
                    raise RuntimeError(
                        f"账号 {email} 的邮箱登录凭据读取失败: {exc}"
                    ) from exc
                records = []
            email_records = [
                item
                for item in records
                if _text(item.get("email")).lower() == email.lower()
            ]
            if not email_records and not totp_secret:
                raise RuntimeError(
                    f"账号 {email} 在邮箱凭据池 {pool_file} 中不存在"
                )
            record = next(
                (
                    item
                    for item in email_records
                    if _text(item.get("account_type")).lower()
                    == "chatgpt_password_totp"
                ),
                None,
            )
            if record is None and not totp_secret:
                raise RuntimeError(
                    f"账号 {email} 的凭据类型不是 ChatGPT 密码 + MFA"
                )

    if record is not None:
        password = str(record.get("password") or password)
        totp_secret = _text(
            record.get("totp_secret")
            or record.get("mfa_secret")
            or record.get("totp")
        ) or totp_secret
        mail_api_url = _text(
            mail_api_url
            or record.get("mail_api_url")
            or record.get("mailapi_url")
        )

    if not password or not totp_secret:
        raise RuntimeError(f"账号 {email} 缺少邮箱登录凭据：密码或 MFA 秘钥为空")
    credentials: dict[str, Any] = {
        "email": email,
        "password": password,
        "totp_secret": totp_secret,
    }
    if mail_api_url:
        credentials["mail_api_url"] = mail_api_url
    if pool_file:
        credentials["pool_file"] = pool_file
    return credentials


def _recover_url_login_credentials(
    saved: dict[str, Any],
    mailbox_context: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    email = _text(mailbox_context.get("email")) or saved["email"]
    context_extra = dict(mailbox_context.get("extra") or {})
    pool_file = _text(context_extra.get("pool_file"))
    record: dict[str, Any] = {}
    pool_error: Exception | None = None
    if pool_file:
        try:
            _path, records = load_applemail_pool_records(
                pool_file=pool_file,
                pool_dir=_text(config.get("applemail_pool_dir")) or "mail",
            )
            record = next((
                item
                for item in records
                if _text(item.get("email")).lower() == email.lower()
                and _text(item.get("account_type")).lower() in {
                    "chatgpt_password_url_otp",
                    "chatgpt_password_reset_url_mail",
                }
            ), {})
        except Exception as exc:
            pool_error = exc

    account_type = _text(
        context_extra.get("account_type") or record.get("account_type")
    ).lower()
    password = str(
        context_extra.get("password")
        or record.get("password")
        or saved.get("password")
        or ""
    )
    mail_api_url = _text(
        context_extra.get("mail_api_url")
        or context_extra.get("mailapi_url")
        or record.get("mail_api_url")
        or record.get("mailapi_url")
    )
    totp_url = _text(
        context_extra.get("totp_url") or record.get("totp_url")
    )
    totp_secret = _text(
        context_extra.get("totp_secret")
        or context_extra.get("mfa_secret")
        or context_extra.get("totp")
        or record.get("totp_secret")
        or record.get("mfa_secret")
        or record.get("totp")
    )
    reset_required = bool(
        context_extra.get(
            "password_reset_required",
            record.get("password_reset_required", False),
        )
    )
    if password:
        reset_required = False

    if not account_type:
        account_type = "chatgpt_password_reset_url_mail"
    if not mail_api_url:
        if pool_error is not None:
            raise RuntimeError(
                f"账号 {email} 的 URL 邮箱凭据读取失败: {pool_error}"
            ) from pool_error
        if pool_file and not record:
            raise RuntimeError(f"账号 {email} 在 URL 邮箱凭据池中不存在")
        raise RuntimeError(f"账号 {email} 缺少 URL 邮箱收件地址")
    if account_type == "chatgpt_password_url_otp" and not password:
        raise RuntimeError(f"账号 {email} 的 URL 登录密码为空")
    return {
        "email": email,
        "password": password,
        "mail_api_url": mail_api_url,
        "totp_url": totp_url,
        "totp_secret": totp_secret,
        "account_type": account_type,
        "password_reset_required": reset_required,
        "pool_file": pool_file,
    }


class _PasswordTotpEmailService:
    service_type = type("ServiceType", (), {"value": "chatgpt_credentials"})()

    def __init__(
        self,
        credentials: dict[str, str],
        mailbox_context: dict[str, Any],
    ) -> None:
        self._credentials = credentials
        self._mailbox_context = mailbox_context

    def create_email(self, config=None):
        del config
        context_extra = dict(self._mailbox_context.get("extra") or {})
        return {
            "email": self._credentials["email"],
            "service_id": self._credentials["email"],
            "token": "",
            "account_type": "chatgpt_password_totp",
            "password": self._credentials["password"],
            "totp_secret": self._credentials["totp_secret"],
            "mfa_recovery_code": _text(
                context_extra.get("mfa_recovery_code")
            ),
        }

    def get_verification_code(self, **kwargs):
        del kwargs
        raise RuntimeError("当前账号使用密码 + MFA 重登，不读取邮箱验证码")

    def get_mailbox_metadata(self):
        return dict(self._mailbox_context)

    def supports_email_verification(self) -> bool:
        return False

    def commit_mfa_rotation(
        self,
        *,
        totp_secret="",
        recovery_code="",
        rotated_at="",
    ):
        secret = _text(totp_secret)
        if not secret:
            raise ValueError("轮换后的 MFA 密钥为空")
        self._credentials["totp_secret"] = secret
        context_extra = dict(self._mailbox_context.get("extra") or {})
        context_extra.update({
            "totp_secret": secret,
            "mfa_recovery_code": _text(recovery_code),
            "chatgpt_mfa_managed": True,
            "mfa_rotated_at": _text(rotated_at),
            "mailbox_control_risk": "no_email_recovery",
        })
        context_extra.pop("totp_url", None)
        context_extra.pop("mfa_secret", None)
        context_extra.pop("totp", None)
        self._mailbox_context["extra"] = context_extra
        return True


class _GoogleFederatedEmailService:
    service_type = type("ServiceType", (), {"value": "chatgpt_credentials"})()

    def __init__(
        self,
        *,
        email: str,
        password: str,
        mailbox_context: dict[str, Any],
    ) -> None:
        self._email = str(email or "").strip()
        self._password = str(password or "")
        self._mailbox_context = mailbox_context

    def create_email(self, config=None):
        del config
        return {
            "email": self._email,
            "service_id": self._email,
            "token": "",
            "account_type": "chatgpt_google_password",
            "password": self._password,
        }

    def get_verification_code(self, **kwargs):
        del kwargs
        raise RuntimeError("当前账号使用 Google 联邦密码重登，不读取邮箱验证码")

    def supports_email_verification(self):
        return False

    def get_mailbox_metadata(self):
        return dict(self._mailbox_context)


class _PersistedEmailService:
    def __init__(
        self,
        *,
        mailbox,
        mailbox_account: MailboxAccount,
        mailbox_context: dict[str, Any],
        provider: str,
        log_fn: LogFn | None,
        otp_timeout_seconds: int = 300,
    ) -> None:
        self.service_type = type("ServiceType", (), {"value": provider})()
        self._mailbox = mailbox
        self._account = mailbox_account
        self._mailbox_context = mailbox_context
        self._log_fn = log_fn
        self._before_ids: set[Any] = set()
        self._baseline_ready = threading.Event()
        self._baseline_started = False
        self._otp_remaining_seconds = float(
            max(30, min(int(otp_timeout_seconds or 300), 3600))
        )
        self._foreground_remaining_seconds = 20.0

    def _load_baseline(self) -> None:
        try:
            self._before_ids = set(
                self._mailbox.get_current_ids(self._account) or []
            )
        except Exception as exc:
            self._before_ids = set()
            _emit(
                self._log_fn,
                f"邮箱旧邮件基线读取失败，将继续等待新验证码: {exc}",
            )
        finally:
            self._baseline_ready.set()

    def create_email(self, config=None):
        del config
        account_type = _text((self._account.extra or {}).get("account_type"))
        if account_type == "chatgpt_password_remote_totp":
            self._baseline_started = True
            self._before_ids = set()
            self._baseline_ready.set()
        elif not self._baseline_started:
            self._baseline_started = True
            threading.Thread(
                target=self._load_baseline,
                name="chatgpt-relogin-mailbox-baseline",
                daemon=True,
            ).start()
        result = {
            "email": self._account.email,
            "service_id": self._account.account_id,
            "token": "",
        }
        account_extra = dict(self._account.extra or {})
        account_type = _text(account_extra.get("account_type"))
        if account_type in {
            "chatgpt_password_totp",
            "chatgpt_password_remote_totp",
        }:
            result.update({
                "account_type": account_type,
                "password": str(account_extra.get("password") or ""),
            })
            if account_type == "chatgpt_password_totp":
                result["totp_secret"] = str(account_extra.get("totp_secret") or "")
            else:
                result["totp_url"] = _text(account_extra.get("totp_url"))
            mail_api_url = _text(
                account_extra.get("mail_api_url")
                or account_extra.get("mailapi_url")
            )
            if mail_api_url:
                result["mail_api_url"] = mail_api_url
        elif account_type in {
            "chatgpt_password_url_otp",
            "chatgpt_password_reset_url_mail",
        }:
            result.update({
                "account_type": account_type,
                "password": str(account_extra.get("password") or ""),
                "mail_api_url": _text(account_extra.get("mail_api_url")),
                "totp_url": _text(account_extra.get("totp_url")),
                "totp_secret": _text(account_extra.get("totp_secret")),
                "password_reset_required": bool(
                    account_extra.get("password_reset_required", False)
                ),
                "new_password": str(account_extra.get("new_password") or ""),
            })
        elif account_type == "mailapi_url":
            mail_api_url = _text(
                account_extra.get("mail_api_url")
                or account_extra.get("mailapi_url")
            )
            if mail_api_url:
                result.update({
                    "account_type": "mailapi_url",
                    "password": str(account_extra.get("password") or ""),
                    "mail_api_url": mail_api_url,
                    "mailapi_url": mail_api_url,
                    "totp_secret": _text(account_extra.get("totp_secret")),
                })
        managed_totp = _text(account_extra.get("totp_secret"))
        if managed_totp:
            result["totp_secret"] = managed_totp
        context_extra = dict(self._mailbox_context.get("extra") or {})
        recovery_code = _text(
            account_extra.get("mfa_recovery_code")
            or context_extra.get("mfa_recovery_code")
        )
        if recovery_code:
            result["mfa_recovery_code"] = recovery_code
        return result

    def commit_password_reset(self, new_password=""):
        password = str(new_password or "")
        if len(password) < 12:
            raise ValueError("新密码至少需要 12 个字符")
        commit = getattr(self._mailbox, "commit_password_reset", None)
        if not callable(commit):
            raise RuntimeError("当前邮箱后端不支持保存重置后的密码")
        committed = commit(self._account, password)
        if committed is False:
            return False
        if not isinstance(self._account.extra, dict):
            self._account.extra = {}
        self._account.extra["password"] = password
        self._account.extra["password_reset_required"] = False
        self._account.extra.pop("new_password", None)
        context_extra = dict(self._mailbox_context.get("extra") or {})
        context_extra.update(
            {
                "password": password,
                "password_reset_required": False,
            }
        )
        context_extra.pop("new_password", None)
        self._mailbox_context["extra"] = context_extra
        return True

    def commit_mfa_rotation(
        self,
        *,
        totp_secret="",
        recovery_code="",
        rotated_at="",
    ):
        secret = _text(totp_secret)
        if not secret:
            raise ValueError("轮换后的 MFA 密钥为空")
        account_extra = dict(self._account.extra or {})
        shared_receiver = bool(
            _text(
                account_extra.get("mail_api_url")
                or account_extra.get("mailapi_url")
            )
        )
        account_extra.update({
            "totp_secret": secret,
            "mfa_recovery_code": _text(recovery_code),
            "chatgpt_mfa_managed": True,
            "mfa_rotated_at": _text(rotated_at),
            "mailbox_control_risk": (
                "shared_receiver"
                if shared_receiver
                else "email_control_unverified"
            ),
        })
        account_extra.pop("totp_url", None)
        account_extra.pop("mfa_secret", None)
        account_extra.pop("totp", None)
        self._account.extra = account_extra
        context_extra = dict(self._mailbox_context.get("extra") or {})
        context_extra.update(account_extra)
        context_extra.pop("totp_url", None)
        context_extra.pop("mfa_secret", None)
        context_extra.pop("totp", None)
        self._mailbox_context["extra"] = context_extra
        return True

    def get_verification_code(
        self,
        email=None,
        email_id=None,
        timeout=120,
        pattern=None,
        otp_sent_at=None,
        exclude_codes=None,
    ):
        del email, email_id
        requested_timeout = max(int(timeout or 120), 1)
        if self._otp_remaining_seconds <= 0:
            raise TimeoutError(
                f"等待邮箱新验证码超时 ({requested_timeout}s)"
            )

        call_remaining_seconds = min(
            float(requested_timeout),
            self._otp_remaining_seconds,
        )
        baseline_started_at = time.monotonic()
        baseline_wait = min(
            max(int(math.ceil(call_remaining_seconds)), 1),
            30,
        )
        if not self._baseline_ready.wait(timeout=baseline_wait):
            _emit(
                self._log_fn,
                "邮箱旧邮件基线仍未返回，先开始轮询新验证码",
            )
        baseline_elapsed = max(0.0, time.monotonic() - baseline_started_at)
        self._otp_remaining_seconds = max(
            0.0,
            self._otp_remaining_seconds - baseline_elapsed,
        )
        call_remaining_seconds = max(
            0.0,
            call_remaining_seconds - baseline_elapsed,
        )
        if self._otp_remaining_seconds <= 0 or call_remaining_seconds <= 0:
            raise TimeoutError(
                f"等待邮箱新验证码超时 ({requested_timeout}s)"
            )

        foreground_timeout = min(
            int(math.ceil(self._foreground_remaining_seconds)),
            int(math.ceil(call_remaining_seconds)),
            int(math.ceil(self._otp_remaining_seconds)),
        )

        def _wait(
            wait_seconds: int,
            poll_interval: int,
            *,
            foreground: bool = False,
        ):
            nonlocal call_remaining_seconds
            phase_started_at = time.monotonic()
            timed_out = False
            code = None
            try:
                code = self._mailbox.wait_for_code(
                    self._account,
                    keyword="",
                    timeout=wait_seconds,
                    before_ids=set(self._before_ids),
                    code_pattern=pattern,
                    otp_sent_at=otp_sent_at,
                    exclude_codes=exclude_codes,
                    poll_interval=poll_interval,
                )
                return code
            except TimeoutError:
                timed_out = True
                raise
            finally:
                phase_elapsed = max(
                    0.0,
                    time.monotonic() - phase_started_at,
                )
                if timed_out or not code:
                    phase_elapsed = max(phase_elapsed, float(wait_seconds))
                self._otp_remaining_seconds = max(
                    0.0,
                    self._otp_remaining_seconds - phase_elapsed,
                )
                call_remaining_seconds = max(
                    0.0,
                    call_remaining_seconds - phase_elapsed,
                )
                if foreground:
                    self._foreground_remaining_seconds = max(
                        0.0,
                        self._foreground_remaining_seconds - phase_elapsed,
                    )

        code = None
        if foreground_timeout > 0:
            try:
                code = _wait(foreground_timeout, 3, foreground=True)
            except TimeoutError:
                code = None
        if code:
            return code

        background_timeout = max(
            0,
            min(
                int(math.ceil(call_remaining_seconds)),
                int(math.ceil(self._otp_remaining_seconds)),
            ),
        )
        if background_timeout <= 0:
            raise TimeoutError(
                f"等待邮箱新验证码超时 ({requested_timeout}s)"
            )

        pause_slot = getattr(
            self._mailbox,
            "pause_active_slot_for_mailbox_wait",
            None,
        )
        wait_scope = pause_slot() if callable(pause_slot) else nullcontext(False)
        try:
            with wait_scope as released:
                slot_message = (
                    "已释放账号并发槽" if released else "继续低频轮询"
                )
                _emit(
                    self._log_fn,
                    f"前台等待 {foreground_timeout}s 未收到新验证码，"
                    f"转入后台等待（剩余 {background_timeout}s），"
                    f"{slot_message}",
                )
                code = _wait(background_timeout, 10)
        except TimeoutError:
            code = None
        if code:
            return code
        raise TimeoutError(
            f"等待邮箱新验证码超时 ({requested_timeout}s)"
        )

    def get_mailbox_metadata(self):
        context = dict(self._mailbox_context)
        context["email"] = self._account.email
        context["account_id"] = self._account.account_id
        context_extra = dict(self._account.extra or {})
        context_extra.pop("_oauth_token_cache", None)
        context_extra.pop("_pool_claim_id", None)
        context_extra.pop("_pool_state", None)
        context_extra.pop("new_password", None)
        context["extra"] = context_extra
        return context

    def get_totp_code(self):
        getter = getattr(self._mailbox, "get_totp_code", None)
        if not callable(getter):
            raise RuntimeError("当前邮箱后端不支持远程 2FA")
        return getter(self._account)

    def supports_totp_code(self) -> bool:
        account_extra = dict(self._account.extra or {})
        return bool(_text(account_extra.get("totp_url")))

    def supports_email_verification(self) -> bool:
        account_extra = dict(self._account.extra or {})
        has_mail_api = bool(
            _text(
                account_extra.get("mail_api_url")
                or account_extra.get("mailapi_url")
            )
        )
        return has_mail_api and callable(
            getattr(self._mailbox, "wait_for_code", None)
        )


def _build_email_service(
    saved: dict[str, Any],
    config: dict[str, Any],
    *,
    log_fn: LogFn | None,
    task_control=None,
    attempt_id: int | None = None,
    force_password_reset: bool = False,
):
    mailbox_context = saved.get("mailbox_context")
    if not isinstance(mailbox_context, dict) or not mailbox_context:
        raise RuntimeError(
            f"账号 {saved['email']} 缺少邮箱登录凭据，请重新导入后再重登"
        )
    context_extra = dict(mailbox_context.get("extra") or {})
    provider, account_type = _resolve_saved_mailbox_provider(
        mailbox_context,
        context_extra,
    )
    if account_type == "chatgpt_google_password":
        password = str(
            context_extra.get("password") or saved.get("password") or ""
        )
        if not password:
            raise RuntimeError(
                f"账号 {saved['email']} 的 Google 联邦登录凭据缺少密码"
            )
        return _GoogleFederatedEmailService(
            email=_text(mailbox_context.get("email")) or saved["email"],
            password=password,
            mailbox_context=mailbox_context,
        )
    if account_type == "chatgpt_password_remote_totp":
        password = _text(
            context_extra.get("password") or saved.get("password")
        )
        totp_url = _text(context_extra.get("totp_url"))
        if not password or not totp_url:
            raise RuntimeError(
                f"账号 {saved['email']} 的远程 MFA 凭据缺少密码或 2FA 地址"
            )
        mailbox_config = dict(config)
        pool_file = _text(context_extra.get("pool_file"))
        if pool_file:
            mailbox_config["applemail_pool_file"] = pool_file
        mailbox_config["applemail_pool_dir"] = (
            _text(config.get("applemail_pool_dir")) or "mail"
        )
        proxy = _text(saved.get("extra", {}).get("proxy_used")) or None
        mailbox = create_mailbox(
            "applemail",
            extra=mailbox_config,
            proxy=proxy,
        )
        setattr(mailbox, "_log_fn", log_fn)
        _bind_mailbox_task_control(mailbox, task_control, attempt_id)
        if pool_file:
            mailbox_account = mailbox.get_email_by_address(saved["email"])
        else:
            mailbox_account = MailboxAccount(
                email=saved["email"],
                account_id=saved["email"],
                extra={},
            )
        account_extra = dict(mailbox_account.extra or {})
        account_extra.update({
            "provider": "chatgpt_credentials",
            "account_type": account_type,
            "password": password,
            "totp_url": totp_url,
        })
        if pool_file:
            account_extra["pool_file"] = pool_file
        mailbox_account.extra = account_extra
        return _PersistedEmailService(
            mailbox=mailbox,
            mailbox_account=mailbox_account,
            mailbox_context=mailbox_context,
            provider="chatgpt_credentials",
            log_fn=log_fn,
            otp_timeout_seconds=_resolve_mailbox_otp_timeout(config),
        )
    if account_type in {
        "chatgpt_password_url_otp",
        "chatgpt_password_reset_url_mail",
    }:
        credentials = _recover_url_login_credentials(
            saved,
            mailbox_context,
            config,
        )
        if force_password_reset:
            if (
                credentials["account_type"]
                != "chatgpt_password_reset_url_mail"
            ):
                raise RuntimeError(
                    f"账号 {credentials['email']} 的邮箱凭据不支持忘记密码流程"
                )
            credentials["password"] = ""
            credentials["password_reset_required"] = True
        mailbox_config = dict(config)
        if credentials["pool_file"]:
            mailbox_config["applemail_pool_file"] = credentials["pool_file"]
        mailbox_config["applemail_pool_dir"] = (
            _text(config.get("applemail_pool_dir")) or "mail"
        )
        proxy = _text(saved["extra"].get("proxy_used")) or None
        mailbox = create_mailbox("applemail", extra=mailbox_config, proxy=proxy)
        setattr(mailbox, "_log_fn", log_fn)
        _bind_mailbox_task_control(mailbox, task_control, attempt_id)
        if credentials["pool_file"]:
            mailbox_account = mailbox.get_email_by_address(credentials["email"])
        else:
            mailbox_account = MailboxAccount(
                email=credentials["email"],
                account_id=credentials["email"],
                extra={},
            )
        account_extra = dict(mailbox_account.extra or {})
        account_extra.update(
            {
                "provider": "chatgpt_credentials",
                "account_type": credentials["account_type"],
                "password": credentials["password"],
                "mail_api_url": credentials["mail_api_url"],
                "mailapi_url": credentials["mail_api_url"],
                "password_reset_required": credentials[
                    "password_reset_required"
                ],
                "totp_secret": credentials["totp_secret"],
            }
        )
        if credentials["pool_file"]:
            account_extra["pool_file"] = credentials["pool_file"]
        if credentials["totp_url"]:
            account_extra["totp_url"] = credentials["totp_url"]
        if force_password_reset:
            account_extra.pop("new_password", None)
        if (
            credentials["password_reset_required"]
            and not _text(account_extra.get("new_password"))
        ):
            generator = getattr(mailbox, "_generate_password_reset_password", None)
            if not callable(generator):
                raise RuntimeError(
                    f"账号 {credentials['email']} 无法生成密码重置凭据"
                )
            account_extra["new_password"] = str(generator())
        mailbox_account.extra = account_extra
        return _PersistedEmailService(
            mailbox=mailbox,
            mailbox_account=mailbox_account,
            mailbox_context=mailbox_context,
            provider="chatgpt_credentials",
            log_fn=log_fn,
            otp_timeout_seconds=_resolve_mailbox_otp_timeout(config),
        )
    if provider == "chatgpt_credentials" or account_type == "chatgpt_password_totp":
        credentials = _recover_password_totp_credentials(
            saved,
            mailbox_context,
            config,
        )
        if credentials.get("mail_api_url"):
            mailbox_config = dict(config)
            if credentials.get("pool_file"):
                mailbox_config["applemail_pool_file"] = credentials["pool_file"]
            mailbox_config["applemail_pool_dir"] = (
                _text(config.get("applemail_pool_dir")) or "mail"
            )
            proxy = _text(saved["extra"].get("proxy_used")) or None
            mailbox = create_mailbox(
                "applemail",
                extra=mailbox_config,
                proxy=proxy,
            )
            setattr(mailbox, "_log_fn", log_fn)
            _bind_mailbox_task_control(mailbox, task_control, attempt_id)
            if credentials.get("pool_file"):
                mailbox_account = mailbox.get_email_by_address(
                    credentials["email"]
                )
            else:
                mailbox_account = MailboxAccount(
                    email=credentials["email"],
                    account_id=credentials["email"],
                    extra={},
                )
            account_extra = dict(mailbox_account.extra or {})
            account_extra.update(
                {
                    "provider": "chatgpt_credentials",
                    "account_type": "chatgpt_password_totp",
                    "password": credentials["password"],
                    "totp_secret": credentials["totp_secret"],
                    "mail_api_url": credentials["mail_api_url"],
                    "mailapi_url": credentials["mail_api_url"],
                }
            )
            if credentials.get("pool_file"):
                account_extra["pool_file"] = credentials["pool_file"]
            mailbox_account.extra = account_extra
            return _PersistedEmailService(
                mailbox=mailbox,
                mailbox_account=mailbox_account,
                mailbox_context=mailbox_context,
                provider="chatgpt_credentials",
                log_fn=log_fn,
                otp_timeout_seconds=_resolve_mailbox_otp_timeout(config),
            )
        return _PasswordTotpEmailService(credentials, mailbox_context)

    if provider in {"outlook", "microsoft"}:
        provider = "microsoft"
        context_extra.pop("_oauth_token_cache", None)
    elif provider == "icloud":
        provider = "applemail"
        context_extra.setdefault("account_type", "icloud_web")
    if not provider:
        raise RuntimeError(f"账号 {saved['email']} 的邮箱登录凭据来源为空")

    proxy = _text(saved["extra"].get("proxy_used")) or None
    mailbox_config = dict(config)
    mailbox_config.update(context_extra)
    mailbox = create_mailbox(provider, extra=mailbox_config, proxy=proxy)
    setattr(mailbox, "_log_fn", log_fn)
    _bind_mailbox_task_control(mailbox, task_control, attempt_id)
    mailbox_account = MailboxAccount(
        email=_text(mailbox_context.get("email")) or saved["email"],
        account_id=_text(mailbox_context.get("account_id")),
        extra=context_extra,
    )
    return _PersistedEmailService(
        mailbox=mailbox,
        mailbox_account=mailbox_account,
        mailbox_context=mailbox_context,
        provider=provider,
        log_fn=log_fn,
        otp_timeout_seconds=_resolve_mailbox_otp_timeout(config),
    )


def _saved_account_supports_password_reset(saved: Mapping[str, Any]) -> bool:
    mailbox_context = saved.get("mailbox_context")
    if not isinstance(mailbox_context, Mapping):
        return False
    context_extra = mailbox_context.get("extra")
    if not isinstance(context_extra, Mapping):
        return False
    return (
        _text(context_extra.get("account_type")).lower()
        == "chatgpt_password_reset_url_mail"
    )


def _is_explicit_saved_password_rejection(detail: str) -> bool:
    normalized = _text(detail).lower()
    if "invalid_credentials" in normalized:
        return True
    password_failure_markers = (
        "密码验证失败",
        "password verification failed",
        "invalid password",
    )
    return "401" in normalized and any(
        marker in normalized for marker in password_failure_markers
    )


def _login_with_saved_credentials(
    saved: dict[str, Any],
    *,
    log_fn: LogFn | None = None,
    task_control=None,
    attempt_id: int | None = None,
    rotate_mfa: bool = False,
) -> dict[str, Any]:
    mailbox_context = saved.get("mailbox_context")
    if not isinstance(mailbox_context, dict) or not mailbox_context:
        raise RuntimeError(
            f"账号 {saved['email']} 缺少邮箱登录凭据，请重新导入后再重登"
        )
    config = dict(config_store.get_all() or {})
    mailbox_timeout = _resolve_mailbox_otp_timeout(config)
    config["mailbox_otp_timeout_seconds"] = mailbox_timeout
    for timeout_key in (
        "chatgpt_oauth_otp_wait_seconds",
        "chatgpt_otp_wait_seconds",
        "chatgpt_register_otp_wait_seconds",
        "chatgpt_register_otp_resend_wait_seconds",
    ):
        config[timeout_key] = mailbox_timeout
    email_service = _build_email_service(
        saved,
        config,
        log_fn=log_fn,
        task_control=task_control,
        attempt_id=attempt_id,
    )
    extra_config = dict(config)
    extra_config.update(
        {
            "chatgpt_registration_mode": "refresh_token",
            "chatgpt_has_refresh_token_solution": True,
            "chatgpt_existing_account_login_only": True,
            "chatgpt_existing_account_login_stage": "refresh_token",
            "chatgpt_existing_account_allow_phone_verification": False,
            "chatgpt_existing_account_rotate_mfa": bool(rotate_mfa),
            "chatgpt_existing_account_skip_managed_mfa_rotation": False,
        }
    )
    try:
        max_retries = max(int(config.get("register_max_retries") or 3), 1)
    except (TypeError, ValueError):
        max_retries = 3

    adapter = build_chatgpt_registration_mode_adapter(extra_config)

    def run_login(service, *, password: str):
        return adapter.run(ChatGPTRegistrationContext(
            email_service=service,
            proxy_url=_text(saved["extra"].get("proxy_used")) or None,
            callback_logger=log_fn or (lambda _message: None),
            email=saved["email"],
            password=password,
            browser_mode=_text(config.get("default_executor")) or "headless",
            max_retries=max_retries,
            extra_config=extra_config,
        ))

    login_started_at = time.monotonic()
    result = run_login(
        email_service,
        password=str(saved.get("password") or ""),
    )
    if not bool(getattr(result, "success", False)):
        detail = _text(getattr(result, "error_message", "")) or "认证服务未返回成功状态"
        if (
            _saved_account_supports_password_reset(saved)
            and _is_explicit_saved_password_rejection(detail)
        ):
            _checkpoint_task(task_control, attempt_id)
            _emit(
                log_fn,
                "已保存密码被认证服务明确拒绝，自动改走忘记密码流程",
            )
            email_service = _build_email_service(
                saved,
                config,
                log_fn=log_fn,
                task_control=task_control,
                attempt_id=attempt_id,
                force_password_reset=True,
            )
            result = run_login(email_service, password="")
        if not bool(getattr(result, "success", False)):
            detail = (
                _text(getattr(result, "error_message", ""))
                or "认证服务未返回成功状态"
            )
            elapsed_seconds = max(
                0.0,
                time.monotonic() - login_started_at,
            )
            if _is_exhausted_mailbox_otp_failure(
                detail,
                wait_seconds=mailbox_timeout,
                elapsed_seconds=elapsed_seconds,
            ):
                raise ChatGPTMailboxOTPTimeoutError(
                    detail,
                    wait_seconds=mailbox_timeout,
                    elapsed_seconds=elapsed_seconds,
                )
            raise RuntimeError(detail)

    tokens = {
        "access_token": _text(getattr(result, "access_token", "")),
        "refresh_token": _text(getattr(result, "refresh_token", "")),
        "id_token": _text(getattr(result, "id_token", "")),
        "session_token": _text(getattr(result, "session_token", "")),
        "workspace_id": _text(getattr(result, "workspace_id", "")),
        "account_id": _text(getattr(result, "account_id", "")),
        "source": _text(getattr(result, "source", "")) or "existing_account_web_login",
    }
    if not tokens["access_token"] or not tokens["refresh_token"]:
        raise RuntimeError("重登完成，但认证服务未返回完整的 Access Token 和 Refresh Token")
    result_metadata = getattr(result, "metadata", None)
    metadata = dict(result_metadata) if isinstance(result_metadata, dict) else {}
    metadata_getter = getattr(email_service, "get_mailbox_metadata", None)
    if callable(metadata_getter):
        try:
            mailbox_context = metadata_getter() or {}
            if isinstance(mailbox_context, dict) and mailbox_context:
                metadata["mailbox_login_context"] = mailbox_context
                context_extra = mailbox_context.get("extra")
                if isinstance(context_extra, dict) and _text(
                    context_extra.get("account_type")
                ).lower() in {
                    "chatgpt_google_password",
                    "chatgpt_password_totp",
                    "chatgpt_password_url_otp",
                    "chatgpt_password_reset_url_mail",
                }:
                    updated_password = str(context_extra.get("password") or "")
                    if updated_password:
                        tokens["password"] = updated_password
        except Exception as exc:
            _emit(log_fn, f"保存最新邮箱登录凭据失败，将保留原凭据: {exc}")
    if metadata:
        tokens["metadata"] = metadata
    return tokens


def _persist_fresh_tokens(
    account_id: int,
    tokens: dict[str, Any],
    *,
    expected_email: str,
    expected_created_at: datetime,
) -> AccountModel:
    with Session(engine) as session:
        account = session.get(AccountModel, int(account_id))
        if account is None or account.platform != "chatgpt":
            raise RuntimeError("ChatGPT 账号不存在")
        if (
            _text(account.email).lower() != _text(expected_email).lower()
            or account.created_at != expected_created_at
        ):
            raise RuntimeError("本地账号记录已发生变化，已停止保存新令牌")

        # Build a detached snapshot. Mutating the attached ORM object here would
        # let an autoflush issue an ID-only UPDATE before the identity guard below.
        snapshot = AccountModel(**account.model_dump())
        extra = dict(account.get_extra() or {})
        for stale_key in (
            "access_token",
            "accessToken",
            "refresh_token",
            "refreshToken",
            "id_token",
            "idToken",
            "session_token",
            "sessionToken",
            "workspace_id",
            "workspaceId",
            "account_id",
            "accountId",
            "chatgpt_account_id",
            "chatgptAccountId",
            "chatgpt_user_id",
            "chatgptUserId",
            "user_id",
            "userId",
        ):
            extra.pop(stale_key, None)
        for key in (
            "access_token",
            "refresh_token",
            "id_token",
            "session_token",
            "workspace_id",
            "account_id",
        ):
            value = _text(tokens.get(key))
            if value:
                extra[key] = value
        extra["chatgpt_registration_mode"] = "refresh_token"
        extra["chatgpt_has_refresh_token_solution"] = True
        extra["chatgpt_token_source"] = _text(tokens.get("source")) or "relogin"
        extra.pop("chatgpt_local", None)
        metadata = tokens.get("metadata")
        if isinstance(metadata, dict):
            mailbox_context = metadata.get("mailbox_login_context")
            if isinstance(mailbox_context, dict) and mailbox_context:
                extra["mailbox_login_context"] = mailbox_context

        snapshot.token = _text(tokens.get("access_token"))
        snapshot.user_id = _text(tokens.get("account_id"))
        if str(tokens.get("password") or ""):
            snapshot.password = str(tokens["password"])
        snapshot.status = "registered"
        snapshot.updated_at = datetime.now(timezone.utc)
        snapshot.set_extra(extra)

        try:
            result = session.exec(
                update(AccountModel)
                .where(AccountModel.id == int(account_id))
                .where(AccountModel.platform == "chatgpt")
                .where(func.lower(AccountModel.email) == _text(expected_email).lower())
                .where(AccountModel.created_at == expected_created_at)
                .values(
                    password=snapshot.password,
                    token=snapshot.token,
                    user_id=snapshot.user_id,
                    status=snapshot.status,
                    updated_at=snapshot.updated_at,
                    extra_json=snapshot.extra_json,
                )
            )
            updated_count = int(getattr(result, "rowcount", 0) or 0)
            if updated_count != 1:
                session.rollback()
                raise RuntimeError("本地账号记录已发生变化，已停止保存新令牌")
            session.commit()
        except Exception:
            session.rollback()
            raise
        return snapshot


def _remove_local_account_after_terminal_login_failure(
    account_id: int,
    *,
    email: str,
    created_at: datetime,
    updated_at: datetime,
    failure_context: str,
    removed_message: str,
    absent_message: str,
    removal_reason: str,
    log_fn: LogFn | None,
    task_control=None,
    attempt_id: int | None = None,
    codex2api_delete_on_account_remove_enabled: bool | None = None,
) -> dict[str, Any]:
    try:
        removal = remove_account(
            account_id,
            database_engine=engine,
            already_locked=True,
            expected_created_at=created_at,
            expected_updated_at=updated_at,
            task_control=task_control,
            attempt_id=attempt_id,
            codex2api_delete_on_account_remove_enabled=(
                codex2api_delete_on_account_remove_enabled
            ),
        )
    except TaskInterruption:
        raise
    except Exception as exc:
        removal = {
            "status": "remove_exception",
            "error_code": "account_remove_failed",
            "message": f"账号删除联动异常（{type(exc).__name__}）",
        }

    removal_status = _text(removal.get("status"))
    if removal_status not in {"deleted", "already_absent"}:
        error_code = _text(removal.get("error_code")) or "account_remove_failed"
        detail = _text(removal.get("message")) or "账号删除联动未完成"
        if error_code == "local_delete_conflict":
            detail = "本地账号记录已发生变化，已停止自动删除"
        failure_label = (
            "本地记录删除失败"
            if error_code in {"database_error", "local_delete_conflict"}
            else "账号删除联动失败"
        )
        message = f"{failure_context}，但{failure_label}: {detail}"
        _emit_observer(log_fn, message)
        return {
            "ok": False,
            "relogin_ok": False,
            "account_removed": False,
            "removal_reason": removal_reason,
            "stage": "account_remove_failed",
            "error_code": error_code,
            "account_id": int(account_id),
            "email": email,
            "message": message,
        }

    message = removed_message if removal_status == "deleted" else absent_message
    _emit_observer(log_fn, message)
    return {
        "ok": False,
        "relogin_ok": False,
        "account_removed": True,
        "removal_reason": removal_reason,
        "stage": "account_removed",
        "account_id": int(account_id),
        "email": email,
        "message": message,
    }


def _remove_deactivated_local_account(
    account_id: int,
    *,
    email: str,
    created_at: datetime,
    updated_at: datetime,
    log_fn: LogFn | None,
    task_control=None,
    attempt_id: int | None = None,
    codex2api_delete_on_account_remove_enabled: bool | None = None,
) -> dict[str, Any]:
    return _remove_local_account_after_terminal_login_failure(
        account_id,
        email=email,
        created_at=created_at,
        updated_at=updated_at,
        failure_context="检测到账号已被删除或停用",
        removed_message="账号已被删除或停用，本地记录已自动删除",
        absent_message="账号已被删除或停用，本地记录已不存在",
        removal_reason="account_deactivated",
        log_fn=log_fn,
        task_control=task_control,
        attempt_id=attempt_id,
        codex2api_delete_on_account_remove_enabled=(
            codex2api_delete_on_account_remove_enabled
        ),
    )


def _remove_mailbox_otp_timed_out_account(
    account_id: int,
    *,
    email: str,
    created_at: datetime,
    updated_at: datetime,
    wait_seconds: int,
    log_fn: LogFn | None,
    task_control=None,
    attempt_id: int | None = None,
    codex2api_delete_on_account_remove_enabled: bool | None = None,
) -> dict[str, Any]:
    wait_seconds = max(int(wait_seconds), 180)
    return _remove_local_account_after_terminal_login_failure(
        account_id,
        email=email,
        created_at=created_at,
        updated_at=updated_at,
        failure_context=(
            f"邮箱 OTP 等待已达到 {wait_seconds} 秒且未取得验证码"
        ),
        removed_message=(
            f"邮箱 OTP 等待满 {wait_seconds} 秒仍未收到，"
            "本地账号记录已自动移除"
        ),
        absent_message=(
            f"邮箱 OTP 等待满 {wait_seconds} 秒仍未收到，"
            "本地账号记录已不存在"
        ),
        removal_reason="mailbox_otp_timeout",
        log_fn=log_fn,
        task_control=task_control,
        attempt_id=attempt_id,
        codex2api_delete_on_account_remove_enabled=(
            codex2api_delete_on_account_remove_enabled
        ),
    )


def _relogin_chatgpt_account_locked(
    account_id: int,
    *,
    log_fn: LogFn | None = None,
    task_control=None,
    attempt_id: int | None = None,
    codex2api_delete_on_account_remove_enabled: bool | None = None,
    remove_on_mailbox_otp_timeout: bool = False,
    rotate_mfa: bool = False,
) -> dict[str, Any]:
    """Perform a real credential login, persist fresh tokens, then replace Codex2API."""
    email = ""
    saved: dict[str, Any] | None = None
    try:
        saved = _load_saved_account(account_id)
        email = saved["email"]
        _emit_observer(log_fn, f"开始使用已保存的邮箱凭据重新登录 {email}")

        def _login_log(message: str) -> None:
            _emit_observer(log_fn, message)

        tokens = _login_with_saved_credentials(
            saved,
            log_fn=_login_log,
            task_control=task_control,
            attempt_id=attempt_id,
            rotate_mfa=rotate_mfa,
        )
        if rotate_mfa:
            metadata = tokens.get("metadata")
            rotation_metadata = (
                metadata.get("mfa_rotation")
                if isinstance(metadata, dict)
                else None
            )
            if not (
                isinstance(rotation_metadata, dict)
                and rotation_metadata.get("managed") is True
                and _text(rotation_metadata.get("rotated_at"))
            ):
                raise RuntimeError(
                    "[stage=mfa_rotate] 登录流程未确认新 MFA 已激活，"
                    "已停止覆盖本地凭据"
                )
        _checkpoint_task(task_control, attempt_id)
        account = _persist_fresh_tokens(
            saved["id"],
            tokens,
            expected_email=saved["email"],
            expected_created_at=saved["created_at"],
        )
        if rotate_mfa:
            try:
                from core.db import finalize_chatgpt_mfa_rotation

                finalize_chatgpt_mfa_rotation(saved["email"])
            except Exception as exc:
                _emit_observer(
                    log_fn,
                    "MFA 已保存到账号，但写前记录清理失败，将在后续自动恢复"
                    f"（{type(exc).__name__}）",
                )
        _emit_observer(log_fn, "已获取并保存全新的 Access Token / Refresh Token")
    except ChatGPTAccountDeactivatedError:
        if saved is None:
            raise RuntimeError("停用信号缺少对应的本地账号快照")
        return _remove_deactivated_local_account(
            saved["id"],
            email=email,
            created_at=saved["created_at"],
            updated_at=saved["updated_at"],
            log_fn=log_fn,
            task_control=task_control,
            attempt_id=attempt_id,
            codex2api_delete_on_account_remove_enabled=(
                codex2api_delete_on_account_remove_enabled
            ),
        )
    except ChatGPTMailboxOTPTimeoutError as exc:
        if remove_on_mailbox_otp_timeout:
            if saved is None:
                raise RuntimeError("邮箱 OTP 超时信号缺少对应的本地账号快照")
            return _remove_mailbox_otp_timed_out_account(
                saved["id"],
                email=email,
                created_at=saved["created_at"],
                updated_at=saved["updated_at"],
                wait_seconds=exc.wait_seconds,
                log_fn=log_fn,
                task_control=task_control,
                attempt_id=attempt_id,
                codex2api_delete_on_account_remove_enabled=(
                    codex2api_delete_on_account_remove_enabled
                ),
            )
        message = _text(exc) or type(exc).__name__
        return {
            "ok": False,
            "relogin_ok": False,
            "stage": "relogin",
            "account_id": int(account_id) if str(account_id).isdigit() else account_id,
            "email": email,
            "message": f"重登失败: {message}",
        }
    except TaskInterruption:
        raise
    except Exception as exc:
        message = _text(exc) or type(exc).__name__
        return {
            "ok": False,
            "relogin_ok": False,
            "stage": "relogin",
            "account_id": int(account_id) if str(account_id).isdigit() else account_id,
            "email": email,
            "message": f"重登失败: {message}",
        }

    _emit_observer(log_fn, "正在按账号身份覆盖 Codex2API 旧凭据")
    try:
        with codex2api_account_mutation_lock():
            sync_result = sync_codex2api_account(
                account,
                force=True,
                replace_existing=True,
            )
    except TaskInterruption:
        raise
    except Exception as exc:
        sync_result = {
            "name": "Codex2API",
            "ok": False,
            "msg": _text(exc)[:300] or "Codex2API 同步异常",
        }
    if not sync_result or not bool(sync_result.get("ok")):
        detail = _text((sync_result or {}).get("msg")) or "Codex2API 未确认更新"
        return {
            "ok": False,
            "relogin_ok": True,
            "stage": "codex2api_sync",
            "account_id": int(account.id or account_id),
            "email": _text(account.email) or email,
            "message": f"重登成功，但 Codex2API 覆盖更新失败: {detail}",
            "sync": sync_result,
            "mfa_rotated": bool(rotate_mfa),
        }

    return {
        "ok": True,
        "relogin_ok": True,
        "stage": "completed",
        "account_id": int(account.id or account_id),
        "email": _text(account.email) or email,
        "message": "重登并同步 Codex2API 成功",
        "sync": sync_result,
        "mfa_rotated": bool(rotate_mfa),
    }


def _tokens_from_refresh_result(
    saved: dict[str, Any],
    refresh_result,
    *,
    previous_refresh_token: str,
) -> dict[str, Any]:
    extra = dict(saved.get("extra") or {})

    def _saved_value(*keys: str) -> str:
        for key in keys:
            value = _text(extra.get(key))
            if value:
                return value
        return ""

    return {
        "access_token": _text(getattr(refresh_result, "access_token", "")),
        "refresh_token": (
            _text(getattr(refresh_result, "refresh_token", ""))
            or previous_refresh_token
        ),
        "id_token": _saved_value("id_token", "idToken"),
        "session_token": _saved_value("session_token", "sessionToken"),
        "workspace_id": _saved_value("workspace_id", "workspaceId"),
        "account_id": (
            _saved_value(
                "account_id",
                "accountId",
                "chatgpt_account_id",
                "chatgptAccountId",
                "chatgpt_user_id",
                "chatgptUserId",
                "user_id",
                "userId",
            )
            or _text(saved.get("user_id"))
        ),
        "source": "oauth_refresh_token",
    }


def _refresh_or_relogin_chatgpt_account_locked(
    account_id: int,
    *,
    log_fn: LogFn | None = None,
    task_control=None,
    attempt_id: int | None = None,
    codex2api_delete_on_account_remove_enabled: bool | None = None,
) -> dict[str, Any]:
    email = ""
    try:
        saved = _load_saved_account(account_id)
        email = saved["email"]
        extra = dict(saved.get("extra") or {})
        refresh_token = _text(
            extra.get("refresh_token") or extra.get("refreshToken")
        )
    except TaskInterruption:
        raise
    except Exception as exc:
        message = _text(exc) or type(exc).__name__
        return {
            "ok": False,
            "relogin_ok": False,
            "refresh_ok": False,
            "refresh_state": "transient_error",
            "mode": "refresh_token",
            "stage": "refresh_load",
            "account_id": account_id,
            "email": email,
            "message": f"RT 检测准备失败: {message}",
        }

    if not refresh_token:
        _emit_observer(log_fn, "账号缺少 Refresh Token，开始完整登录")
        result = dict(
            _relogin_chatgpt_account_locked(
                account_id,
                log_fn=log_fn,
                task_control=task_control,
                attempt_id=attempt_id,
                codex2api_delete_on_account_remove_enabled=(
                    codex2api_delete_on_account_remove_enabled
                ),
            )
        )
        result.update(
            {
                "mode": "full_login",
                "refresh_ok": False,
                "refresh_state": "invalid",
                "refresh_error_code": "missing_refresh_token",
            }
        )
        return result

    _checkpoint_task(task_control, attempt_id)
    _emit_observer(log_fn, f"正在检测并刷新 {email} 的 Refresh Token")
    try:
        manager = TokenRefreshManager(
            proxy_url=_text(extra.get("proxy_used")) or None
        )
        refresh_result = manager.refresh_by_oauth_token(refresh_token)
    except TaskInterruption:
        raise
    except Exception as exc:
        return {
            "ok": False,
            "relogin_ok": False,
            "refresh_ok": False,
            "refresh_state": "transient_error",
            "refresh_error_code": "refresh_exception",
            "mode": "refresh_token",
            "stage": "refresh_deferred",
            "account_id": account_id,
            "email": email,
            "message": (
                "RT 检测暂时失败，将在下一个自动周期重试: "
                f"{type(exc).__name__}"
            ),
        }

    refresh_state = _text(getattr(refresh_result, "state", ""))
    error_code = _text(getattr(refresh_result, "error_code", ""))
    http_status = int(getattr(refresh_result, "http_status", 0) or 0)
    if (
        bool(getattr(refresh_result, "success", False))
        and refresh_state == "valid"
        and _text(getattr(refresh_result, "access_token", ""))
    ):
        tokens = _tokens_from_refresh_result(
            saved,
            refresh_result,
            previous_refresh_token=refresh_token,
        )
        try:
            _checkpoint_task(task_control, attempt_id)
            account = _persist_fresh_tokens(
                saved["id"],
                tokens,
                expected_email=saved["email"],
                expected_created_at=saved["created_at"],
            )
        except TaskInterruption:
            raise
        except Exception as exc:
            message = _text(exc) or type(exc).__name__
            return {
                "ok": False,
                "relogin_ok": False,
                "refresh_ok": False,
                "refresh_state": "valid",
                "mode": "refresh_token",
                "stage": "refresh_persist",
                "account_id": account_id,
                "email": email,
                "message": f"RT 刷新成功，但本地令牌保存失败: {message}",
            }

        _emit_observer(log_fn, "RT 刷新成功并已保存，正在覆盖 Codex2API 凭据")
        try:
            with codex2api_account_mutation_lock():
                sync_result = sync_codex2api_account(
                    account,
                    force=True,
                    replace_existing=True,
                )
        except TaskInterruption:
            raise
        except Exception as exc:
            sync_result = {
                "name": "Codex2API",
                "ok": False,
                "msg": _text(exc)[:300] or "Codex2API 同步异常",
            }
        if not sync_result or not bool(sync_result.get("ok")):
            detail = _text((sync_result or {}).get("msg")) or "Codex2API 未确认更新"
            return {
                "ok": False,
                "relogin_ok": False,
                "refresh_ok": True,
                "refresh_state": "valid",
                "mode": "refresh_token",
                "stage": "codex2api_sync",
                "account_id": int(account.id or account_id),
                "email": _text(account.email) or email,
                "message": f"RT 刷新成功，但 Codex2API 覆盖更新失败: {detail}",
                "sync": sync_result,
            }
        return {
            "ok": True,
            "relogin_ok": False,
            "refresh_ok": True,
            "refresh_state": "valid",
            "mode": "refresh_token",
            "stage": "completed",
            "account_id": int(account.id or account_id),
            "email": _text(account.email) or email,
            "message": "RT 刷新并同步 Codex2API 成功",
            "sync": sync_result,
        }

    if refresh_state == "invalid":
        _emit_observer(log_fn, "RT 已被服务端明确判定失效，开始完整登录")
        result = dict(
            _relogin_chatgpt_account_locked(
                account_id,
                log_fn=log_fn,
                task_control=task_control,
                attempt_id=attempt_id,
                codex2api_delete_on_account_remove_enabled=(
                    codex2api_delete_on_account_remove_enabled
                ),
            )
        )
        result.update(
            {
                "mode": "full_login",
                "refresh_ok": False,
                "refresh_state": "invalid",
                "refresh_error_code": error_code,
                "refresh_http_status": http_status,
            }
        )
        return result

    detail = error_code or (
        f"HTTP {http_status}" if http_status else "network_or_unknown_error"
    )
    _emit_observer(log_fn, "RT 检测暂时失败，不触发验证码登录")
    return {
        "ok": False,
        "relogin_ok": False,
        "refresh_ok": False,
        "refresh_state": "transient_error",
        "refresh_error_code": error_code,
        "refresh_http_status": http_status,
        "mode": "refresh_token",
        "stage": "refresh_deferred",
        "account_id": account_id,
        "email": email,
        "message": f"RT 检测暂时失败，将在下一个自动周期重试: {detail}",
    }


def refresh_or_relogin_chatgpt_account(
    account_id: int,
    *,
    log_fn: LogFn | None = None,
    task_control=None,
    attempt_id: int | None = None,
    codex2api_delete_on_account_remove_enabled: bool | None = None,
) -> dict[str, Any]:
    """Refresh first; run a full credential login only for explicit RT invalidation."""
    with chatgpt_account_operation_lock(account_id, blocking=False) as acquired:
        if not acquired:
            return {
                "ok": False,
                "relogin_ok": False,
                "refresh_ok": False,
                "refresh_state": "transient_error",
                "mode": "refresh_token",
                "stage": "refresh_deferred",
                "account_id": account_id,
                "email": "",
                "message": "认证维护失败: 该账号正在重登或刷新，请等待当前任务完成",
            }
        return _refresh_or_relogin_chatgpt_account_locked(
            account_id,
            log_fn=log_fn,
            task_control=task_control,
            attempt_id=attempt_id,
            codex2api_delete_on_account_remove_enabled=(
                codex2api_delete_on_account_remove_enabled
            ),
        )


def relogin_chatgpt_account(
    account_id: int,
    *,
    log_fn: LogFn | None = None,
    task_control=None,
    attempt_id: int | None = None,
    codex2api_delete_on_account_remove_enabled: bool | None = None,
    remove_on_mailbox_otp_timeout: bool = False,
    rotate_mfa: bool = False,
) -> dict[str, Any]:
    """Run one account at a time so local and remote credentials cannot cross."""
    with chatgpt_account_operation_lock(account_id, blocking=False) as acquired:
        if not acquired:
            return {
                "ok": False,
                "relogin_ok": False,
                "stage": "relogin",
                "account_id": account_id,
                "email": "",
                "message": "重登失败: 该账号正在重登并同步，请等待当前任务完成",
            }
        return _relogin_chatgpt_account_locked(
            account_id,
            log_fn=log_fn,
            task_control=task_control,
            attempt_id=attempt_id,
            codex2api_delete_on_account_remove_enabled=(
                codex2api_delete_on_account_remove_enabled
            ),
            remove_on_mailbox_otp_timeout=remove_on_mailbox_otp_timeout,
            rotate_mfa=rotate_mfa,
        )
