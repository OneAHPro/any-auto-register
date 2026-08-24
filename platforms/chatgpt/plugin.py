"""ChatGPT / Codex CLI 平台插件"""

import random
import math
import string
import threading
import time

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, BasePlatform, RegisterConfig
from core.registry import register
from platforms.chatgpt.chatgpt_registration_mode_adapter import (
    ChatGPTRegistrationContext,
    build_chatgpt_registration_mode_adapter,
)


@register
class ChatGPTPlatform(BasePlatform):
    name = "chatgpt"
    display_name = "ChatGPT"
    version = "1.0.0"

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def check_valid(self, account: Account) -> bool:
        try:
            from platforms.chatgpt.payment import check_subscription_status

            class _A:
                pass

            a = _A()
            extra = account.extra or {}
            a.access_token = extra.get("access_token") or account.token
            a.cookies = extra.get("cookies", "")
            status = check_subscription_status(a, proxy=self.config.proxy if self.config else None)
            return status not in ("expired", "invalid", "banned", None)
        except Exception:
            return False

    def register(self, email: str = None, password: str = None) -> Account:
        proxy = self.config.proxy if self.config else None
        browser_mode = (self.config.executor_type if self.config else None) or "protocol"
        extra_config = (
            dict(self.config.extra or {})
            if self.config and getattr(self.config, "extra", None)
            else {}
        )
        login_only = str(
            extra_config.get("chatgpt_existing_account_login_only", "") or ""
        ).strip().lower() in {"1", "true", "yes", "on"}
        if not password:
            password = (
                ""
                if login_only
                else "".join(
                    random.choices(
                        string.ascii_letters + string.digits + "!@#$",
                        k=16,
                    )
                )
            )

        log_fn = getattr(self, "_log_fn", print)
        max_retries = 3
        try:
            max_retries = int(extra_config.get("register_max_retries", 3) or 3)
        except Exception:
            max_retries = 3

        def _resolve_positive_int(*values, default: int) -> int:
            for value in values:
                if value in (None, ""):
                    continue
                try:
                    seconds = int(value)
                except (TypeError, ValueError):
                    continue
                if seconds > 0:
                    return seconds
            return default

        mailbox_total_timeout = _resolve_positive_int(
            extra_config.get("mailbox_otp_timeout_seconds"),
            extra_config.get("email_otp_timeout_seconds"),
            default=180,
        )
        mailbox_total_timeout = max(30, min(mailbox_total_timeout, 3600))
        extra_config["mailbox_otp_timeout_seconds"] = mailbox_total_timeout
        # The mailbox timeout is the single source of truth.  Keeping every
        # outer ChatGPT wait on the same budget prevents 600s + resend stacking.
        for timeout_key in (
            "chatgpt_oauth_otp_wait_seconds",
            "chatgpt_otp_wait_seconds",
            "chatgpt_register_otp_wait_seconds",
            "chatgpt_register_otp_resend_wait_seconds",
        ):
            extra_config[timeout_key] = mailbox_total_timeout

        def _resolve_mailbox_timeout(requested_timeout: int) -> int:
            candidates = (
                extra_config.get("mailbox_otp_timeout_seconds"),
                extra_config.get("email_otp_timeout_seconds"),
                extra_config.get("otp_timeout"),
                requested_timeout,
            )
            for value in candidates:
                if value in (None, ""):
                    continue
                try:
                    seconds = int(value)
                except (TypeError, ValueError):
                    continue
                if seconds > 0:
                    return seconds
            return requested_timeout

        if self.mailbox:
            _mailbox = self.mailbox
            _fixed_email = email

            def _resolve_email(candidate_email: str = "") -> str:
                resolved_email = str(_fixed_email or candidate_email or "").strip()
                if not resolved_email:
                    raise RuntimeError("custom_provider 返回空邮箱地址")
                return resolved_email

            class GenericEmailService:
                service_type = type("ST", (), {"value": "custom_provider"})()

                def __init__(self):
                    self._acct = None
                    self._email = _fixed_email
                    self._before_ids = set()
                    self._baseline_ready = threading.Event()
                    self._baseline_error = ""
                    self._otp_remaining_seconds = float(
                        mailbox_total_timeout
                    )

                def _build_result(self):
                    account_extra = dict(
                        getattr(self._acct, "extra", None) or {}
                    )
                    result = {
                        "email": self._email,
                        "service_id": self._acct.account_id,
                        "token": "",
                    }
                    if (
                        str(account_extra.get("account_type") or "").strip()
                        == "chatgpt_google_password"
                    ):
                        result.update(
                            {
                                "account_type": "chatgpt_google_password",
                                "password": str(
                                    account_extra.get("password") or ""
                                ),
                            }
                        )
                    elif (
                        str(account_extra.get("account_type") or "").strip()
                        == "chatgpt_password_totp"
                    ):
                        result.update(
                            {
                                "account_type": "chatgpt_password_totp",
                                "password": str(
                                    account_extra.get("password") or ""
                                ),
                                "totp_secret": str(
                                    account_extra.get("totp_secret") or ""
                                ),
                            }
                        )
                        mail_api_url = str(
                            account_extra.get("mail_api_url")
                            or account_extra.get("mailapi_url")
                            or ""
                        ).strip()
                        if mail_api_url:
                            result["mail_api_url"] = mail_api_url
                    elif str(account_extra.get("account_type") or "").strip() in {
                        "chatgpt_password_url_otp",
                        "chatgpt_password_reset_url_mail",
                        "chatgpt_password_remote_totp",
                    }:
                        account_type = str(account_extra.get("account_type") or "").strip()
                        result.update(
                            {
                                "account_type": account_type,
                                "password": str(account_extra.get("password") or ""),
                                "mail_api_url": str(account_extra.get("mail_api_url") or ""),
                                "totp_url": str(account_extra.get("totp_url") or ""),
                                "totp_secret": str(
                                    account_extra.get("totp_secret") or ""
                                ),
                                "password_reset_required": bool(
                                    account_extra.get("password_reset_required", False)
                                ),
                                "new_password": str(account_extra.get("new_password") or ""),
                            }
                        )
                    elif str(account_extra.get("account_type") or "").strip() == "mailapi_url":
                        mail_api_url = str(
                            account_extra.get("mail_api_url")
                            or account_extra.get("mailapi_url")
                            or ""
                        ).strip()
                        if mail_api_url:
                            result.update(
                                {
                                    "account_type": "mailapi_url",
                                    "password": str(account_extra.get("password") or ""),
                                    "mail_api_url": mail_api_url,
                                    "mailapi_url": mail_api_url,
                                    "totp_secret": str(
                                        account_extra.get("totp_secret") or ""
                                    ),
                                }
                            )
                    managed_totp = str(
                        account_extra.get("totp_secret") or ""
                    ).strip()
                    if managed_totp:
                        result["totp_secret"] = managed_totp
                    mailapi_token = str(
                        account_extra.get("mailapi_token") or ""
                    ).strip()
                    if mailapi_token:
                        # Keep the bearer credential attached to the internal
                        # mailbox context.  It is only used if the provider
                        # explicitly requests an email factor.
                        result["mailapi_token"] = mailapi_token
                    return result

                def _load_baseline(self, get_current_ids):
                    try:
                        self._before_ids = set(
                            get_current_ids(self._acct) or []
                        )
                    except Exception as exc:
                        self._before_ids = set()
                        self._baseline_error = str(exc).strip()
                        log_fn(
                            "邮箱旧邮件基线读取失败，将继续等待新验证码: "
                            f"{self._baseline_error}"
                        )
                    finally:
                        self._baseline_ready.set()

                def create_email(self, config=None):
                    if self._email and self._acct and (_fixed_email or login_only):
                        return self._build_result()
                    exact_selector = getattr(
                        _mailbox,
                        "get_email_by_address",
                        None,
                    )
                    if _fixed_email and callable(exact_selector):
                        self._acct = exact_selector(_fixed_email)
                    else:
                        self._acct = _mailbox.get_email()
                    account_extra = dict(
                        getattr(self._acct, "extra", None) or {}
                    )
                    is_chatgpt_credentials = (
                        str(account_extra.get("account_type") or "").strip()
                        in {
                            "chatgpt_google_password",
                            "chatgpt_password_totp",
                            "chatgpt_password_remote_totp",
                        }
                    )
                    has_mail_api_url = bool(
                        str(
                            account_extra.get("mail_api_url")
                            or account_extra.get("mailapi_url")
                            or ""
                        ).strip()
                    )
                    generated_email = getattr(self._acct, "email", "")
                    if not self._email:
                        self._email = _resolve_email(generated_email)
                    elif not _fixed_email:
                        self._email = _resolve_email(generated_email)
                    elif str(generated_email or "").strip().lower() != str(
                        _fixed_email or ""
                    ).strip().lower():
                        raise RuntimeError(
                            "重试绑定邮箱与邮箱池实际取出的账号不一致"
                        )
                    binding_callback = extra_config.get(
                        "_chatgpt_attempt_binding_callback"
                    )
                    if callable(binding_callback):
                        try:
                            binding_callback(self._email, self._acct)
                        except Exception as exc:
                            log_fn(f"保存邮箱与接码卡密绑定失败: {exc}")
                    get_current_ids = getattr(_mailbox, "get_current_ids", None)
                    if is_chatgpt_credentials and not has_mail_api_url:
                        self._before_ids = set()
                        self._baseline_ready.set()
                    elif callable(get_current_ids):
                        threading.Thread(
                            target=self._load_baseline,
                            args=(get_current_ids,),
                            name="chatgpt-mailbox-baseline",
                            daemon=True,
                        ).start()
                    else:
                        self._before_ids = set()
                        self._baseline_ready.set()
                    return self._build_result()

                def get_verification_code(
                    self,
                    email=None,
                    email_id=None,
                    timeout=120,
                    pattern=None,
                    otp_sent_at=None,
                    exclude_codes=None,
                ):
                    if not self._acct:
                        raise RuntimeError("邮箱账户尚未创建，无法获取验证码")
                    resolved_timeout = _resolve_mailbox_timeout(timeout)
                    if self._otp_remaining_seconds <= 0:
                        raise TimeoutError(
                            f"等待邮箱新验证码超时 ({resolved_timeout}s)"
                        )
                    baseline_started_at = time.monotonic()
                    baseline_wait = min(
                        max(int(math.ceil(self._otp_remaining_seconds)), 1),
                        30,
                    )
                    if not self._baseline_ready.wait(timeout=baseline_wait):
                        log_fn(
                            "邮箱旧邮件基线仍未返回，先开始轮询新验证码"
                        )
                    baseline_elapsed = max(
                        0.0,
                        time.monotonic() - baseline_started_at,
                    )
                    self._otp_remaining_seconds = max(
                        0.0,
                        self._otp_remaining_seconds - baseline_elapsed,
                    )
                    if self._otp_remaining_seconds <= 0:
                        raise TimeoutError(
                            f"等待邮箱新验证码超时 ({resolved_timeout}s)"
                        )
                    foreground_budget = max(0.0, 20.0 - baseline_elapsed)
                    foreground_timeout = min(
                        int(math.ceil(foreground_budget)),
                        int(math.ceil(self._otp_remaining_seconds)),
                    )

                    def _wait(wait_seconds: int, poll_interval: int):
                        phase_started_at = time.monotonic()
                        timed_out = False
                        code = None
                        try:
                            code = _mailbox.wait_for_code(
                                self._acct,
                                keyword="",
                                timeout=wait_seconds,
                                before_ids=set(self._before_ids),
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
                                phase_elapsed = max(
                                    phase_elapsed,
                                    float(wait_seconds),
                                )
                            self._otp_remaining_seconds = max(
                                0.0,
                                self._otp_remaining_seconds - phase_elapsed,
                            )

                    code = None
                    if foreground_timeout > 0:
                        try:
                            code = _wait(foreground_timeout, 3)
                        except TimeoutError:
                            code = None
                    if code:
                        return code

                    background_timeout = max(
                        0,
                        int(math.ceil(self._otp_remaining_seconds)),
                    )
                    if background_timeout <= 0:
                        raise TimeoutError(
                            f"等待邮箱新验证码超时 ({resolved_timeout}s)"
                        )

                    pause_slot = getattr(
                        _mailbox,
                        "pause_active_slot_for_mailbox_wait",
                        None,
                    )
                    if callable(pause_slot):
                        wait_scope = pause_slot()
                    else:
                        from contextlib import nullcontext

                        wait_scope = nullcontext(False)
                    try:
                        with wait_scope as released:
                            slot_message = (
                                "已释放账号并发槽"
                                if released
                                else "继续低频轮询"
                            )
                            log_fn(
                                f"前台等待 {foreground_timeout}s 未收到新验证码，"
                                f"转入后台等待（剩余 {background_timeout}s），"
                                f"{slot_message}"
                            )
                            code = _wait(background_timeout, 10)
                    except TimeoutError:
                        code = None
                    if code:
                        return code
                    raise TimeoutError(
                        f"等待邮箱新验证码超时 ({resolved_timeout}s)"
                    )

                def get_totp_code(self):
                    if not self._acct:
                        raise RuntimeError("邮箱账户尚未创建，无法获取 2FA 验证码")
                    getter = getattr(_mailbox, "get_totp_code", None)
                    if not callable(getter):
                        raise RuntimeError("当前邮箱后端不支持远程 2FA")
                    return getter(self._acct)

                def supports_totp_code(self):
                    if not self._acct:
                        return False
                    account_extra = dict(
                        getattr(self._acct, "extra", None) or {}
                    )
                    return bool(str(account_extra.get("totp_url") or "").strip())

                def supports_email_verification(self):
                    if not self._acct:
                        return False
                    account_extra = dict(
                        getattr(self._acct, "extra", None) or {}
                    )
                    account_type = str(
                        account_extra.get("account_type") or ""
                    ).strip()
                    has_mail_api = bool(
                        str(
                            account_extra.get("mail_api_url")
                            or account_extra.get("mailapi_url")
                            or ""
                        ).strip()
                    )
                    if account_type in {
                        "chatgpt_google_password",
                        "chatgpt_password_totp",
                        "chatgpt_password_remote_totp",
                    } and not has_mail_api:
                        return False
                    return callable(getattr(_mailbox, "wait_for_code", None))

                def commit_password_reset(self, new_password=""):
                    if not self._acct:
                        raise RuntimeError("邮箱账户尚未创建，无法保存新密码")
                    commit = getattr(_mailbox, "commit_password_reset", None)
                    if not callable(commit):
                        raise RuntimeError("当前邮箱后端不支持保存重置密码")
                    return commit(self._acct, new_password)

                def commit_mfa_rotation(
                    self,
                    *,
                    totp_secret="",
                    recovery_code="",
                    rotated_at="",
                ):
                    if not self._acct:
                        raise RuntimeError("邮箱账户尚未创建，无法保存 MFA 凭据")
                    secret = str(totp_secret or "").strip()
                    if not secret:
                        raise ValueError("轮换后的 MFA 密钥为空")
                    account_extra = dict(
                        getattr(self._acct, "extra", None) or {}
                    )
                    shared_receiver = bool(
                        str(
                            account_extra.get("mail_api_url")
                            or account_extra.get("mailapi_url")
                            or ""
                        ).strip()
                    )
                    account_extra.update(
                        {
                            "totp_secret": secret,
                            "mfa_recovery_code": str(
                                recovery_code or ""
                            ).strip(),
                            "chatgpt_mfa_managed": True,
                            "mfa_rotated_at": str(rotated_at or "").strip(),
                            "mailbox_control_risk": (
                                "shared_receiver"
                                if shared_receiver
                                else "email_control_unverified"
                            ),
                        }
                    )
                    account_extra.pop("totp_url", None)
                    account_extra.pop("mfa_secret", None)
                    account_extra.pop("totp", None)
                    self._acct.extra = account_extra
                    return True

                def get_mailbox_metadata(self):
                    account = self._acct
                    account_extra = dict(getattr(account, "extra", None) or {})
                    provider = str(
                        account_extra.get("provider")
                        or extra_config.get("mail_provider")
                        or "custom_provider"
                    ).strip()
                    account_type = str(account_extra.get("account_type") or "").strip()
                    if account_type in {
                        "chatgpt_google_password",
                        "chatgpt_password_totp",
                        "chatgpt_password_url_otp",
                        "chatgpt_password_reset_url_mail",
                        "chatgpt_password_remote_totp",
                    }:
                        credential_snapshot = {
                            "provider": "chatgpt_credentials",
                            "account_type": account_type,
                            "pool_file": str(
                                account_extra.get("pool_file") or ""
                            ),
                        }
                        if account_type == "chatgpt_google_password":
                            credential_snapshot["password"] = str(
                                account_extra.get("password") or ""
                            )
                        elif account_type == "chatgpt_password_totp":
                            credential_snapshot.update(
                                {
                                    "password": str(
                                        account_extra.get("password") or ""
                                    ),
                                    "totp_secret": str(
                                        account_extra.get("totp_secret") or ""
                                    ),
                                }
                            )
                            mail_api_url = str(
                                account_extra.get("mail_api_url")
                                or account_extra.get("mailapi_url")
                                or ""
                            ).strip()
                            if mail_api_url:
                                credential_snapshot["mail_api_url"] = mail_api_url
                        elif account_type == "chatgpt_password_remote_totp":
                            credential_snapshot.update(
                                {
                                    "password": str(
                                        account_extra.get("password") or ""
                                    ),
                                    "totp_url": str(
                                        account_extra.get("totp_url") or ""
                                    ),
                                    "totp_secret": str(
                                        account_extra.get("totp_secret") or ""
                                    ),
                                }
                            )
                        else:
                            credential_snapshot.update(
                                {
                                    "password": str(
                                        account_extra.get("password") or ""
                                    ),
                                    "mail_api_url": str(
                                        account_extra.get("mail_api_url")
                                        or account_extra.get("mailapi_url")
                                        or ""
                                    ),
                                    "totp_url": str(
                                        account_extra.get("totp_url") or ""
                                    ),
                                    "totp_secret": str(
                                        account_extra.get("totp_secret") or ""
                                    ),
                                    "password_reset_required": bool(
                                        account_extra.get(
                                            "password_reset_required",
                                            False,
                                        )
                                    ),
                                }
                            )
                        for managed_key in (
                            "mfa_recovery_code",
                            "chatgpt_mfa_managed",
                            "mfa_rotated_at",
                            "mailbox_control_risk",
                        ):
                            if managed_key in account_extra:
                                credential_snapshot[managed_key] = account_extra[
                                    managed_key
                                ]
                        # Durable Outlook lease fencing metadata is an
                        # internal pointer, not a credential.  Preserve it in
                        # the account projection so the post-save task can
                        # atomically bind the claimed mailbox to AccountModel.
                        for lease_key in (
                            "_outlook_row_id",
                            "_outlook_lease_owner",
                            "_outlook_lease_version",
                            "_outlook_state",
                            "_outlook_bound_account_id",
                            "_outlook_lease_expires_at",
                            "_outlook_created_at",
                        ):
                            if lease_key in account_extra:
                                credential_snapshot[lease_key] = account_extra[
                                    lease_key
                                ]
                        account_extra = credential_snapshot
                    return {
                        "provider": provider,
                        "email": str(self._email or getattr(account, "email", "") or "").strip(),
                        "account_id": str(getattr(account, "account_id", "") or "").strip(),
                        "extra": account_extra,
                    }

                def update_status(self, success, error=None):
                    pass

                @property
                def status(self):
                    return None

            email_service = GenericEmailService()
        else:
            from core.base_mailbox import TempMailLolMailbox

            _tmail = TempMailLolMailbox(proxy=proxy)
            _tmail._task_control = getattr(self, "_task_control", None)

            class TempMailEmailService:
                service_type = type("ST", (), {"value": "tempmail_lol"})()

                def __init__(self):
                    self._acct = None
                    self._before_ids = set()

                def create_email(self, config=None):
                    acct = _tmail.get_email()
                    self._acct = acct
                    self._before_ids = set(_tmail.get_current_ids(acct) or [])
                    resolved_email = str(getattr(acct, "email", "") or "").strip()
                    if not resolved_email:
                        raise RuntimeError("tempmail_lol 返回空邮箱地址")
                    return {"email": resolved_email, "service_id": acct.account_id, "token": acct.account_id}

                def get_verification_code(
                    self,
                    email=None,
                    email_id=None,
                    timeout=120,
                    pattern=None,
                    otp_sent_at=None,
                    exclude_codes=None,
                ):
                    return _tmail.wait_for_code(
                        self._acct,
                        keyword="",
                        timeout=_resolve_mailbox_timeout(timeout),
                        before_ids=self._before_ids,
                        otp_sent_at=otp_sent_at,
                        exclude_codes=exclude_codes,
                    )

                def update_status(self, success, error=None):
                    pass

                def supports_email_verification(self):
                    return True

                @property
                def status(self):
                    return None

            email_service = TempMailEmailService()

        adapter = build_chatgpt_registration_mode_adapter(extra_config)
        context = ChatGPTRegistrationContext(
            email_service=email_service,
            proxy_url=proxy,
            callback_logger=log_fn,
            email=email,
            password=password,
            browser_mode=browser_mode,
            max_retries=max_retries,
            extra_config=extra_config,
        )
        def _is_permanent_login_credential_error(error, error_code="") -> bool:
            if str(error_code or "").strip() in {
                "missing_totp_credentials",
                "missing_password_credentials",
            }:
                return True
            normalized = str(error or "").strip().lower()
            return any(
                marker in normalized
                for marker in (
                    "缺少 mfa 秘钥",
                    "missing mfa secret",
                    "missing totp secret",
                    "缺少 chatgpt 密码",
                    "missing chatgpt password",
                    "密码已在认证服务重置，但本地凭据保存失败",
                )
            )

        def _requeue_failed_login_mailbox(error="", error_code=""):
            if not login_only:
                return
            if _is_permanent_login_credential_error(error, error_code):
                if str(error_code or "").strip() == "missing_password_credentials":
                    log_fn(
                        "ChatGPT 密码 + MFA 导入记录缺少密码；"
                        "已从自动重试池隔离，请补齐密码后重新导入"
                    )
                else:
                    log_fn(
                        "当前 ChatGPT 账号仅支持 TOTP MFA，但导入记录缺少秘钥；"
                        "已从自动重试池隔离，请补齐秘钥后重新导入"
                    )
                return
            mailbox_account = getattr(email_service, "_acct", None)
            requeue = getattr(self.mailbox, "requeue_account", None)
            if mailbox_account is not None and callable(requeue):
                detail = f"{error_code} {error}".lower()
                uncertain = any(
                    marker in detail
                    for marker in (
                        "timeout",
                        "timed out",
                        "network",
                        "mailbox_backend",
                        "activation_unknown",
                        "mfa_rotation",
                        "session expired",
                        "429",
                        "502",
                        "503",
                    )
                )
                try:
                    requeue(mailbox_account, uncertain=uncertain)
                except TypeError:
                    requeue(mailbox_account)

        try:
            result = adapter.run(context)
        except Exception as exc:
            _requeue_failed_login_mailbox(
                exc,
                getattr(exc, "error_code", ""),
            )
            raise
        if not result or not result.success:
            result_error = result.error_message if result else "注册失败"
            result_error_code = getattr(result, "error_code", "") if result else ""
            if not isinstance(result_error_code, str):
                result_error_code = ""
            _requeue_failed_login_mailbox(result_error, result_error_code)
            raise RuntimeError(result_error)

        account = adapter.build_account(result, password)
        metadata_getter = getattr(email_service, "get_mailbox_metadata", None)
        if callable(metadata_getter) and isinstance(getattr(account, "extra", None), dict):
            mailbox_context = metadata_getter() or {}
            if isinstance(mailbox_context, dict) and mailbox_context:
                account.extra["mailbox_login_context"] = mailbox_context
        account_extra = (
            dict(account.extra)
            if isinstance(getattr(account, "extra", None), dict)
            else {}
        )
        refresh_token = str(
            account_extra.get("refresh_token")
            or account_extra.get("refreshToken")
            or ""
        ).strip()
        mark_mailbox_used = getattr(self.mailbox, "mark_account_used", None)
        mailbox_account = getattr(email_service, "_acct", None)
        if login_only and refresh_token and callable(mark_mailbox_used):
            try:
                marked = mark_mailbox_used(mailbox_account)
            except Exception as exc:
                log_fn(
                    "[WARN] Refresh Token 已获取，但邮箱池消费状态保存失败；"
                    "当前领取保持不可用，避免重复登录"
                    f"（{type(exc).__name__}）"
                )
            else:
                if marked is False:
                    log_fn(
                        "[WARN] Refresh Token 已获取，但邮箱池消费状态保存失败；"
                        "当前领取保持不可用，避免重复登录"
                    )
        return account

    def get_platform_actions(self) -> list:
        return [
            {"id": "probe_local_status", "label": "探测本地状态", "params": []},
            {"id": "sync_cliproxyapi_status", "label": "同步 CLIProxyAPI 状态", "params": []},
            {"id": "refresh_token", "label": "刷新 Token", "params": []},
            {
                "id": "payment_link",
                "label": "生成支付链接",
                "params": [
                    {"key": "country", "label": "地区", "type": "select", "options": ["US", "SG", "TR", "HK", "JP", "GB", "AU", "CA"]},
                    {"key": "plan", "label": "套餐", "type": "select", "options": ["plus", "team"]},
                ],
            },
            {
                "id": "upload_cpa",
                "label": "上传 CPA",
                "params": [
                    {"key": "api_url", "label": "CPA API URL", "type": "text"},
                    {"key": "api_key", "label": "CPA API Key", "type": "text"},
                ],
            },
            {
                "id": "upload_sub2api",
                "label": "上传 Sub2API",
                "params": [
                    {"key": "api_url", "label": "Sub2API API URL", "type": "text"},
                    {"key": "api_key", "label": "Sub2API API Key", "type": "text"},
                ],
            },
            {
                "id": "upload_codex2api",
                "label": "上传 Codex2API",
                "params": [],
            },
            {
                "id": "upload_tm",
                "label": "上传 Team Manager",
                "params": [
                    {"key": "api_url", "label": "TM API URL", "type": "text"},
                    {"key": "api_key", "label": "TM API Key", "type": "text"},
                ],
            },
            {
                "id": "upload_codex_proxy",
                "label": "上传 CodexProxy",
                "params": [
                    {"key": "api_url", "label": "API URL", "type": "text"},
                    {"key": "api_key", "label": "Admin Key", "type": "text"},
                ],
            },
        ]

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        proxy = self.config.proxy if self.config else None
        extra = account.extra or {}

        def _first_nonblank(*values) -> str:
            for value in values:
                text = str(value or "").strip()
                if text:
                    return text
            return ""

        class _A:
            pass

        a = _A()
        a.email = account.email
        a.access_token = _first_nonblank(
            extra.get("access_token"), extra.get("accessToken"), account.token
        )
        a.refresh_token = _first_nonblank(
            extra.get("refresh_token"), extra.get("refreshToken")
        )
        a.id_token = extra.get("id_token", "")
        a.session_token = extra.get("session_token", "")
        a.client_id = extra.get("client_id", "app_EMoamEEZ73f0CkXaXp7hrann")
        a.cookies = extra.get("cookies", "")
        a.user_id = account.user_id

        if action_id == "probe_local_status":
            from platforms.chatgpt.status_probe import probe_local_chatgpt_status

            probe_result = probe_local_chatgpt_status(a, proxy=proxy)
            summary = (
                f"认证={probe_result.get('auth', {}).get('state', 'unknown')}, "
                f"订阅={probe_result.get('subscription', {}).get('plan', 'unknown')}, "
                f"Codex={probe_result.get('codex', {}).get('state', 'unknown')}"
            )
            return {
                "ok": True,
                "data": {
                    "message": f"本地状态探测完成：{summary}",
                    "probe": probe_result,
                },
                "account_extra_patch": {
                    "chatgpt_local": probe_result,
                },
            }

        if action_id == "sync_cliproxyapi_status":
            from services.cliproxyapi_sync import sync_chatgpt_cliproxyapi_status

            sync_result = sync_chatgpt_cliproxyapi_status(a)
            ok = bool(sync_result.get("uploaded")) and sync_result.get("remote_state") not in {"unreachable", "not_found"}
            summary = (
                f"远端状态={sync_result.get('status') or 'not_found'}, "
                f"探测={sync_result.get('remote_state') or 'not_checked'}"
            )
            return {
                "ok": ok,
                "data": {
                    "message": f"CLIProxyAPI 状态同步完成：{summary}",
                    "sync": sync_result,
                },
                "error": sync_result.get("message") if not ok else "",
                "account_extra_patch": {
                    "sync_statuses": {
                        "cliproxyapi": sync_result,
                    },
                },
            }

        if action_id == "refresh_token":
            from platforms.chatgpt.token_refresh import TokenRefreshManager

            manager = TokenRefreshManager(proxy_url=proxy)
            result = manager.refresh_account(a)
            if result.success:
                return {
                    "ok": True,
                    "data": {
                        "access_token": result.access_token,
                        "refresh_token": result.refresh_token,
                    },
                }
            return {"ok": False, "error": result.error_message}

        if action_id == "payment_link":
            from platforms.chatgpt.payment import generate_plus_link, generate_team_link

            plan = params.get("plan", "plus")
            country = params.get("country", "US")
            if plan == "plus":
                url = generate_plus_link(a, proxy=proxy, country=country)
            else:
                url = generate_team_link(
                    a,
                    workspace_name=params.get("workspace_name", "MyTeam"),
                    price_interval=params.get("price_interval", "month"),
                    seat_quantity=int(params.get("seat_quantity", 5) or 5),
                    proxy=proxy,
                    country=country,
                )
            return {"ok": bool(url), "data": {"url": url}}

        if action_id == "upload_cpa":
            from platforms.chatgpt.cpa_upload import generate_token_json, upload_to_cpa

            token_data = generate_token_json(a)
            ok, msg = upload_to_cpa(
                token_data,
                api_url=params.get("api_url"),
                api_key=params.get("api_key"),
            )
            return {"ok": ok, "data": msg}

        if action_id == "upload_sub2api":
            from platforms.chatgpt.sub2api_upload import upload_to_sub2api

            ok, msg = upload_to_sub2api(
                a,
                api_url=params.get("api_url"),
                api_key=params.get("api_key"),
            )
            return {"ok": ok, "data": msg}

        if action_id == "upload_codex2api":
            from platforms.chatgpt.codex2api_upload import upload_to_codex2api

            ok, msg = upload_to_codex2api(a)
            return {"ok": ok, "data": msg}

        if action_id == "upload_tm":
            from platforms.chatgpt.cpa_upload import upload_to_team_manager

            ok, msg = upload_to_team_manager(
                a,
                api_url=params.get("api_url"),
                api_key=params.get("api_key"),
            )
            return {"ok": ok, "data": msg}

        if action_id == "upload_codex_proxy":
            upload_type = str(
                params.get("upload_type")
                or (self.config.extra or {}).get("codex_proxy_upload_type")
                or "at"
            ).strip().lower()

            if upload_type == "rt":
                from platforms.chatgpt.cpa_upload import upload_to_codex_proxy

                ok, msg = upload_to_codex_proxy(
                    a,
                    api_url=params.get("api_url"),
                    api_key=params.get("api_key"),
                )
            else:
                from platforms.chatgpt.cpa_upload import upload_at_to_codex_proxy

                ok, msg = upload_at_to_codex_proxy(
                    a,
                    api_url=params.get("api_url"),
                    api_key=params.get("api_key"),
                )
            return {"ok": ok, "data": msg}

        raise NotImplementedError(f"未知操作: {action_id}")
