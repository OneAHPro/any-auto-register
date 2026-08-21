"""
OAuth 客户端模块 - 处理 Codex OAuth 登录流程
"""

import html
import re
import time
import secrets
import uuid
import json
import random
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs
from core.base_mailbox import MailboxAuthenticationError
from core.icloud_mail import generate_totp
from core.proxy_utils import build_requests_proxy_config
from core.task_runtime import TaskInterruption
from services.chatgpt_account_state import (
    ChatGPTAccountDeactivatedError,
    is_account_deactivated_message,
)

try:
    from curl_cffi import requests as curl_requests
except ImportError:
    import requests as curl_requests

from .phone_service import create_phone_service
from .log_sanitizer import sanitize_chatgpt_log_message
from .utils import (
    FlowState,
    build_browser_headers,
    describe_flow_state,
    extract_flow_state,
    generate_datadog_trace,
    generate_pkce,
    normalize_flow_url,
    random_delay,
    seed_oai_device_cookie,
)
from .sentinel_token import build_sentinel_token
from .sentinel_browser import (
    complete_google_federated_login_via_browser,
    get_sentinel_token_via_browser,
)


def _is_password_verify_deactivation_response(
    status_code,
    error_code,
    error_message,
):
    """Accept only the structured 403 signals that may delete a local account."""
    try:
        normalized_status = int(status_code or 0)
    except (TypeError, ValueError):
        normalized_status = 0
    if normalized_status != 403:
        return False

    normalized_code = str(error_code or "").strip().lower()
    if normalized_code in {"account_deactivated", "account_deleted"}:
        return True

    normalized_message = " ".join(
        str(error_message or "").strip().lower().split()
    )
    canonical_messages = (
        "you do not have an account because it has been deleted or deactivated",
        "your account was deleted or deactivated",
        "account has been deleted or deactivated",
        "你没有账号，因为它已被删除或停用",
        "您没有账号，因为它已被删除或停用",
        "账号已被删除或停用",
        "账号已被停用或删除",
        "帐号已被删除或停用",
        "帐号已被停用或删除",
        "账户已被删除或停用",
        "账户已被停用或删除",
        "帳號已被刪除或停用",
        "帳戶已被刪除或停用",
    )
    if normalized_message.rstrip(".!。！") in canonical_messages:
        return True

    canonical_remediation_messages = (
        "you do not have an account because it has been deleted or deactivated. "
        "if you believe this was an error, please contact us through our help center",
        "你没有账号，因为它已被删除或停用。如果您认为这是错误，请通过我们的帮助中心联系我们",
        "您没有账号，因为它已被删除或停用。如果您认为这是错误，请通过我们的帮助中心联系我们",
        "你沒有帳號，因為它已被刪除或停用。如果您認為這是錯誤，請透過我們的幫助中心聯絡我們",
    )
    normalized_without_terminal_punctuation = normalized_message.rstrip(".!。！")
    if normalized_without_terminal_punctuation in canonical_remediation_messages:
        return True

    help_center_url = r"(?:https?://)?help\.openai\.com(?:/[^\s，。！!]*)?"
    for remediation in canonical_remediation_messages:
        if re.fullmatch(
            rf"{re.escape(remediation)}"
            rf"(?:[.,，:：]?\s*(?:at\s+|地址(?:为|是)\s*){help_center_url})",
            normalized_without_terminal_punctuation,
        ):
            return True
    return False


class OAuthClient:
    """OAuth 客户端 - 用于获取 Access Token 和 Refresh Token"""

    def __init__(self, config, proxy=None, verbose=True, browser_mode="protocol"):
        """
        初始化 OAuth 客户端

        Args:
            config: 配置字典
            proxy: 代理地址
            verbose: 是否输出详细日志
            browser_mode: protocol | headless | headed
        """
        self.config = dict(config or {})
        self.oauth_issuer = self.config.get("oauth_issuer", "https://auth.openai.com")
        self.oauth_client_id = self.config.get(
            "oauth_client_id", "app_EMoamEEZ73f0CkXaXp7hrann"
        )
        self.oauth_redirect_uri = self.config.get(
            "oauth_redirect_uri", "http://localhost:1455/auth/callback"
        )
        self.proxy = proxy
        self.verbose = verbose
        self.browser_mode = browser_mode or "protocol"
        self.last_error = ""
        self.last_workspace_id = ""
        self.last_state = FlowState()
        self.last_stage = ""
        self.last_http_status = 0
        self.device_id = ""
        self.ua = ""
        self.sec_ch_ua = ""
        self.impersonate = ""
        self.last_prepared_oauth_context = None
        self.last_phone_send_diagnostic = {}
        self.last_phone_validate_diagnostic = {}
        self.last_mfa_enrollment = {}

        # 创建 session
        self.session = curl_requests.Session()
        if self.proxy:
            self.session.proxies = build_requests_proxy_config(self.proxy)

    def adopt_browser_context(
        self,
        session,
        *,
        device_id: str = "",
        user_agent: str | None = None,
        sec_ch_ua: str | None = None,
        accept_language: str | None = None,
    ):
        """承接前序浏览器上下文，延续已建立的 cookie / session。"""
        if session is not None:
            self.session = session

        if self.proxy:
            try:
                if not getattr(self.session, "proxies", None):
                    self.session.proxies = build_requests_proxy_config(self.proxy)
            except Exception:
                pass

        header_updates = {}
        if user_agent:
            header_updates["User-Agent"] = user_agent
        if sec_ch_ua:
            header_updates["sec-ch-ua"] = sec_ch_ua
        if accept_language:
            header_updates["Accept-Language"] = accept_language

        if header_updates:
            try:
                self.session.headers.update(header_updates)
            except Exception:
                pass

        if device_id:
            self.device_id = str(device_id or "").strip()
            seed_oai_device_cookie(self.session, device_id)
            self._log(f"已接入前序浏览器上下文: device_id={device_id}")
        if user_agent:
            self.ua = str(user_agent or "").strip()
        if sec_ch_ua:
            self.sec_ch_ua = str(sec_ch_ua or "").strip()

    def _log(self, msg):
        """输出日志"""
        if self.verbose:
            print(f"  [OAuth] {sanitize_chatgpt_log_message(msg)}")

    def _enter_stage(self, stage: str, detail: str = ""):
        self.last_stage = str(stage or "").strip()
        if self.last_stage:
            message = f"[stage={self.last_stage}]"
            if detail:
                message += f" {detail}"
            self._log(message)

    def _set_error(self, message):
        raw_message = str(message or "").strip()
        if self.last_stage and raw_message and f"[stage={self.last_stage}]" not in raw_message:
            self.last_error = f"[stage={self.last_stage}] {raw_message}"
        else:
            self.last_error = raw_message
        if self.last_error:
            self._log(self.last_error)

    def _browser_pause(self, low=0.15, high=0.4):
        """在 headed 模式下注入轻微延迟，模拟真实浏览器操作节奏。"""
        if self.browser_mode == "headed":
            random_delay(low, high)

    def _record_phone_provider_diagnostic(
        self,
        *,
        failure_stage,
        safe_error_code="",
        http_status=0,
        retry_count=0,
        recovery_status="failed",
    ):
        try:
            normalized_status = int(http_status or 0)
        except (TypeError, ValueError):
            normalized_status = 0
        if not 0 <= normalized_status <= 599:
            normalized_status = 0
        try:
            normalized_retry_count = int(retry_count or 0)
        except (TypeError, ValueError):
            normalized_retry_count = 0
        normalized_retry_count = min(max(0, normalized_retry_count), 100)
        normalized_code = str(safe_error_code or "").strip().upper()
        if normalized_code and not re.fullmatch(r"[A-Z0-9_]{1,64}", normalized_code):
            normalized_code = "OPENAI_PROVIDER_ERROR"
        payload = {
            "failure_stage": str(failure_stage or "").strip().lower(),
            "safe_error_code": normalized_code,
            "http_status": normalized_status,
            "provider_retry_count": normalized_retry_count,
            "recovery_status": str(recovery_status or "").strip().lower(),
        }
        if payload["failure_stage"] == "openai_send":
            self.last_phone_send_diagnostic = dict(payload)
        elif payload["failure_stage"] == "openai_validate":
            self.last_phone_validate_diagnostic = dict(payload)
        broker = self.config.get("chatgpt_phone_progress_broker")
        marker = getattr(broker, "mark_provider_diagnostic", None)
        if callable(marker):
            try:
                marker(**payload)
            except Exception:
                pass
        return payload

    @staticmethod
    def _random_chrome_fingerprint():
        profiles = [
            {
                "major": 131,
                "impersonate": "chrome131",
                "build": 6778,
                "patch_range": (69, 205),
                "sec_ch_ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
            },
            {
                "major": 133,
                "impersonate": "chrome133a",
                "build": 6943,
                "patch_range": (33, 153),
                "sec_ch_ua": '"Not(A:Brand";v="99", "Google Chrome";v="133", "Chromium";v="133"',
            },
            {
                "major": 136,
                "impersonate": "chrome136",
                "build": 7103,
                "patch_range": (48, 175),
                "sec_ch_ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
            },
        ]
        profile = random.choice(profiles)
        major = profile["major"]
        build = profile["build"]
        patch = random.randint(*profile["patch_range"])
        full_ver = f"{major}.0.{build}.{patch}"
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{full_ver} Safari/537.36"
        )
        return ua, profile["sec_ch_ua"], profile["impersonate"]

    def _ensure_oauth_fingerprint(self, user_agent, sec_ch_ua, impersonate):
        if user_agent and sec_ch_ua and impersonate:
            return user_agent, sec_ch_ua, impersonate

        ua, ch_ua, imp = self._random_chrome_fingerprint()
        user_agent = user_agent or ua
        sec_ch_ua = sec_ch_ua or ch_ua
        impersonate = impersonate or imp
        self.ua = str(user_agent or "").strip()
        self.sec_ch_ua = str(sec_ch_ua or "").strip()
        self.impersonate = str(impersonate or "").strip()

        try:
            self.session.headers.update(
                {
                    "User-Agent": user_agent,
                    "Accept-Language": random.choice(
                        [
                            "en-US,en;q=0.9",
                            "en-US,en;q=0.9,zh-CN;q=0.8",
                            "en,en-US;q=0.9",
                            "en-US,en;q=0.8",
                        ]
                    ),
                    "sec-ch-ua": sec_ch_ua,
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                    "sec-ch-ua-arch": '"x86"',
                    "sec-ch-ua-bitness": '"64"',
                }
            )
        except Exception:
            pass

        self._log(
            f"OAuth 指纹: ua={user_agent.split('Chrome/')[-1][:24]}..., sec-ch-ua={sec_ch_ua}, impersonate={impersonate}"
        )
        return user_agent, sec_ch_ua, impersonate


    @staticmethod
    def _iter_text_fragments(value):
        if isinstance(value, str):
            text = value.strip()
            if text:
                yield text
            return
        if isinstance(value, dict):
            for item in value.values():
                yield from OAuthClient._iter_text_fragments(item)
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                yield from OAuthClient._iter_text_fragments(item)

    @classmethod
    def _should_blacklist_phone_failure(cls, detail="", state: FlowState | None = None):
        fragments = [str(detail or "").strip()]
        if state is not None:
            fragments.extend(
                cls._iter_text_fragments(
                    {
                        "page_type": state.page_type,
                        "continue_url": state.continue_url,
                        "current_url": state.current_url,
                        "payload": state.payload,
                        "raw": state.raw,
                    }
                )
            )

        combined = " | ".join(fragment for fragment in fragments if fragment).lower()
        if not combined:
            return False

        blacklist_markers = (
            "phone number is invalid",
            "invalid phone number",
            "invalid phone",
            "phone number invalid",
            "sms verification failed",
            "send sms verification failed",
            "unable to send sms",
            "not a valid mobile number",
            "unsupported phone number",
            "phone number not supported",
            "carrier not supported",
            "phone number already in use",
            "phone numbers similar to yours",
            "电话号码无效",
            "手机号无效",
            "发送短信验证失败",
            "号码无效",
            "号码不支持",
            "手机号不支持",
            "openai_phone_invalid",
            "openai_phone_unsupported",
            "openai_phone_already_used",
            "openai_phone_similar_rejected",
        )
        if any(marker in combined for marker in blacklist_markers):
            return True

        non_blacklist_markers = (
            "whatsapp",
            "未收到短信验证码",
            "手机号验证码错误",
            "phone-otp/resend",
            "phone-otp/validate 异常",
            "phone-otp/validate 响应不是 json",
            "phone-otp/validate 失败",
            "timeout",
            "timed out",
            "network",
            "connection",
            "proxy",
            "ssl",
            "tls",
            "captcha",
            "too many phone",
            "too many phone numbers",
            "too many verification requests",
            "验证请求过多",
            "接受短信次数过多",
            "session limit",
            "rate limit",
        )
        if any(marker in combined for marker in non_blacklist_markers):
            return False
        return False

    @staticmethod
    def _is_transient_oauth_entry_error(detail=""):
        text = str(detail or "").strip().lower()
        return any(
            marker in text
            for marker in (
                "403",
                "<!doctype",
                "just a moment",
                "please wait",
                "请稍等",
                "sign-in session is no longer valid",
                "invalid_auth_step",
            )
        )

    def _blacklist_phone_if_needed(
        self, phone_service, entry, detail="", state: FlowState | None = None
    ):
        if not entry or not self._should_blacklist_phone_failure(detail, state):
            return False
        if not bool(getattr(phone_service, "supports_blacklist", True)):
            return False
        try:
            phone_service.mark_blacklisted(entry.phone)
            provider_name = str(
                getattr(phone_service, "provider_name", "手机号服务") or "手机号服务"
            )
            phone_hint = self._phone_log_hint(phone_service, entry.phone)
            self._log(f"{provider_name} 已处理被拒绝手机号: {phone_hint}")
            return True
        except Exception as e:
            self._log(f"写入手机号黑名单失败: {e}")
            return False

    @staticmethod
    def _phone_log_hint(phone_service, phone):
        formatter = getattr(type(phone_service), "log_phone_hint", None)
        if not callable(formatter):
            return str(phone or "").strip()
        try:
            hint = str(formatter(phone_service, phone) or "").strip()
        except Exception:
            return "[手机号已脱敏]"
        return hint or "[手机号已脱敏]"

    def _headers(
        self,
        url,
        *,
        user_agent=None,
        sec_ch_ua=None,
        accept,
        referer=None,
        origin=None,
        content_type=None,
        navigation=False,
        fetch_mode=None,
        fetch_dest=None,
        fetch_site=None,
        extra_headers=None,
    ):
        accept_language = None
        try:
            accept_language = self.session.headers.get("Accept-Language")
        except Exception:
            accept_language = None

        return build_browser_headers(
            url=url,
            user_agent=user_agent or "Mozilla/5.0",
            sec_ch_ua=sec_ch_ua,
            accept=accept,
            accept_language=accept_language or "en-US,en;q=0.9",
            referer=referer,
            origin=origin,
            content_type=content_type,
            navigation=navigation,
            fetch_mode=fetch_mode,
            fetch_dest=fetch_dest,
            fetch_site=fetch_site,
            headed=self.browser_mode == "headed",
            extra_headers=extra_headers,
        )

    def _state_from_url(self, url, method="GET"):
        state = extract_flow_state(
            current_url=normalize_flow_url(url, auth_base=self.oauth_issuer),
            auth_base=self.oauth_issuer,
            default_method=method,
        )
        if method:
            state.method = str(method).upper()
        return state

    def _state_from_payload(self, data, current_url=""):
        return extract_flow_state(
            data=data,
            current_url=current_url,
            auth_base=self.oauth_issuer,
        )

    def _get_cookie_value(self, name, domain_hint=None):
        """读取当前会话中的 Cookie。"""
        cookies = getattr(self.session, "cookies", None)
        if cookies is None:
            return ""
        if not domain_hint:
            getter = getattr(cookies, "get", None)
            if callable(getter):
                try:
                    value = getter(name)
                    if value:
                        return str(value)
                except Exception:
                    pass
        try:
            for cookie in cookies:
                cookie_name = cookie.name if hasattr(cookie, "name") else str(cookie)
                if cookie_name != name:
                    continue
                cookie_domain = cookie.domain if hasattr(cookie, "domain") else ""
                if domain_hint and domain_hint not in (cookie_domain or ""):
                    continue
                return cookie.value if hasattr(cookie, "value") else ""
        except Exception:
            pass
        return ""

    def _state_signature(self, state: FlowState):
        return (
            state.page_type or "",
            state.method or "",
            state.continue_url or "",
            state.current_url or "",
        )

    def _extract_code_from_state(self, state: FlowState):
        for candidate in (
            state.continue_url,
            state.current_url,
            (state.payload or {}).get("url", ""),
        ):
            code = self._extract_code_from_url(candidate)
            if code:
                return code
        return None

    def _state_is_login_password(self, state: FlowState):
        return state.page_type == "login_password"

    @staticmethod
    def _state_is_google_federated(state: FlowState) -> bool:
        target = str(state.continue_url or state.current_url or "").strip()
        try:
            return (
                str(urlparse(target).hostname or "").lower()
                == "accounts.google.com"
            )
        except ValueError:
            return False

    def _complete_google_federated_login(
        self,
        state: FlowState,
        *,
        email: str,
        password: str,
        user_agent: str,
    ) -> FlowState | None:
        self._enter_stage("google_federated_login")
        self._log("检测到企业域名 Google 联邦登录，自动提交邮箱和密码")
        try:
            final_url = complete_google_federated_login_via_browser(
                session=self.session,
                start_url=str(state.continue_url or state.current_url or ""),
                email=email,
                password=password,
                proxy=self.proxy,
                user_agent=user_agent,
                headless=self.browser_mode != "headed",
                timeout_ms=90_000,
                log_fn=lambda message: self._log(
                    f"Google 联邦登录: {message}"
                ),
            )
        except TaskInterruption:
            raise
        except Exception as exc:
            detail = str(exc).strip() or type(exc).__name__
            normalized_password = str(password or "")
            if normalized_password:
                detail = detail.replace(
                    normalized_password,
                    "[密码已隐藏]",
                )
            self._set_error(
                "Google 联邦登录失败: "
                f"{detail}"
            )
            return None
        if not final_url:
            self._set_error("Google 联邦登录完成后未返回 OpenAI 授权地址")
            return None
        return self._state_from_url(final_url)

    def _state_is_password_reset_new_password(self, state: FlowState):
        target = f"{state.continue_url} {state.current_url}".lower()
        return (
            state.page_type == "reset_password_new_password"
            or "reset-password/new-password" in target
        )

    def _state_is_password_reset_success(self, state: FlowState):
        target = f"{state.continue_url} {state.current_url}".lower()
        return (
            state.page_type == "reset_password_success"
            or "reset-password/success" in target
        )

    def _state_is_create_account_password(self, state: FlowState):
        target = f"{state.continue_url} {state.current_url}".lower()
        return state.page_type == "create_account_password" or "create-account/password" in target

    def _state_is_email_otp(self, state: FlowState):
        target = f"{state.continue_url} {state.current_url}".lower()
        return (
            state.page_type == "email_otp_verification"
            or "email-verification" in target
            or "email-otp" in target
        )

    def _state_is_mfa_challenge(self, state: FlowState):
        target = f"{state.continue_url} {state.current_url}".lower()
        page_type = str(state.page_type or "").strip().lower()
        return (
            page_type == "mfa_challenge"
            or page_type.startswith("mfa_challenge_")
            or "mfa-challenge" in target
        )

    def _state_is_mfa_enroll(self, state: FlowState):
        target = f"{state.continue_url} {state.current_url}".lower()
        page_type = str(state.page_type or "").strip().lower()
        return (
            page_type == "mfa_enroll"
            or page_type.startswith("mfa_enroll_")
            or "mfa-enroll" in target
        )

    @staticmethod
    def _extract_mfa_factors(state: FlowState) -> list[dict[str, str]]:
        factors: list[dict[str, str]] = []
        known_types = {
            "totp",
            "email",
            "email_otp",
            "email_code",
            "sms",
            "phone",
            "webauthn",
            "passkey",
            "recovery_code",
        }

        def visit(value):
            if isinstance(value, dict):
                factor_type = str(
                    value.get("factor_type") or value.get("type") or ""
                ).strip().lower()
                factor_id = str(
                    value.get("id") or value.get("factor_id") or ""
                ).strip()
                if factor_id and factor_type in known_types:
                    factors.append({"id": factor_id, "type": factor_type})
                for nested in value.values():
                    visit(nested)
            elif isinstance(value, (list, tuple)):
                for nested in value:
                    visit(nested)

        visit(state.payload or {})
        visit(state.raw or {})
        deduplicated: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for factor in factors:
            signature = (factor["id"], factor["type"])
            if signature in seen:
                continue
            seen.add(signature)
            deduplicated.append(factor)
        return deduplicated

    @staticmethod
    def _extract_mfa_enrollment_factors(state: FlowState) -> list[dict[str, str]]:
        """Extract mandatory-enrollment factors without exposing their secrets."""
        factors: list[dict[str, str]] = []

        def visit(value):
            if isinstance(value, dict):
                factor_type = str(
                    value.get("factor_type") or value.get("type") or ""
                ).strip().lower()
                factor_id = str(
                    value.get("id") or value.get("factor_id") or ""
                ).strip()
                metadata = value.get("metadata")
                secret = ""
                if isinstance(metadata, dict):
                    secret = str(metadata.get("secret") or "").strip()
                if not secret:
                    secret = str(value.get("secret") or "").strip()
                if factor_id and factor_type in {"totp", "recovery_code"} and secret:
                    factors.append(
                        {
                            "id": factor_id,
                            "type": factor_type,
                            "secret": secret,
                        }
                    )
                for nested in value.values():
                    visit(nested)
            elif isinstance(value, (list, tuple)):
                for nested in value:
                    visit(nested)

        visit(state.payload or {})
        visit(state.raw or {})
        deduplicated: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for factor in factors:
            signature = (factor["id"], factor["type"])
            if signature in seen:
                continue
            seen.add(signature)
            deduplicated.append(factor)
        return deduplicated

    @staticmethod
    def _extract_mfa_enrollment_factor_id(state: FlowState) -> str:
        found = ""

        def visit(value):
            nonlocal found
            if found:
                return
            if isinstance(value, dict):
                candidate = str(value.get("factor_id") or "").strip()
                if candidate:
                    found = candidate
                    return
                for nested in value.values():
                    visit(nested)
            elif isinstance(value, (list, tuple)):
                for nested in value:
                    visit(nested)

        visit(state.payload or {})
        visit(state.raw or {})
        if found:
            return found

        target = str(state.continue_url or state.current_url or "").strip()
        parts = [part for part in urlparse(target).path.split("/") if part]
        for index, part in enumerate(parts):
            if part.lower() == "mfa-enroll" and index + 1 < len(parts):
                return str(parts[index + 1] or "").strip()
        return ""

    def _submit_mfa_enrollment(
        self,
        state: FlowState,
        *,
        device_id: str,
        user_agent=None,
        sec_ch_ua=None,
        impersonate=None,
        on_totp_staged=None,
        on_totp_activated=None,
        on_recovery_code=None,
    ):
        """Complete OpenAI's mandatory MFA enrollment after account recovery."""
        self._enter_stage("mfa_enroll", "activate")
        factors = self._extract_mfa_enrollment_factors(state)
        active_id = self._extract_mfa_enrollment_factor_id(state)
        factor = next(
            (item for item in factors if item["id"] == active_id),
            factors[0] if len(factors) == 1 else None,
        )
        if factor is None:
            self._set_error("MFA 绑定页面未返回当前验证因子")
            return None

        factor_id = factor["id"]
        factor_type = factor["type"]
        secret = factor["secret"]
        if factor_type == "totp":
            if callable(on_totp_staged):
                try:
                    on_totp_staged(secret)
                except Exception as exc:
                    self._set_error(
                        "新 MFA 秘钥暂存失败，已停止激活: "
                        f"{type(exc).__name__}"
                    )
                    return None
            pending_recovery_code = str(
                self.last_mfa_enrollment.get("recovery_code") or ""
            ).strip()
            if pending_recovery_code and callable(on_recovery_code):
                try:
                    on_recovery_code(pending_recovery_code)
                except Exception as exc:
                    self._log(
                        "新 MFA 恢复码写前记录暂存失败，将在登录完成后落库: "
                        f"{type(exc).__name__}"
                    )
            code = generate_totp(secret)
        elif factor_type == "recovery_code":
            code = secret
        else:
            self._set_error(f"未支持的 MFA 绑定因子: {factor_type}")
            return None

        activate_url = f"{self.oauth_issuer}/api/accounts/mfa/activate"
        referer = (
            state.continue_url
            or state.current_url
            or f"{self.oauth_issuer}/mfa-enroll/{factor_id}"
        )
        headers = self._headers(
            activate_url,
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
            accept="application/json",
            referer=referer,
            origin=self.oauth_issuer,
            content_type="application/json",
            fetch_site="same-origin",
            extra_headers={"oai-device-id": device_id},
        )
        headers.update(generate_datadog_trace())
        kwargs = {
            "json": {
                "id": factor_id,
                "type": factor_type,
                "code": code,
            },
            "headers": headers,
            "timeout": 30,
            "allow_redirects": False,
        }
        if impersonate:
            kwargs["impersonate"] = impersonate

        try:
            self._browser_pause()
            response = self.session.post(activate_url, **kwargs)
            self.last_http_status = int(response.status_code or 0)
            self._log(
                f"/mfa/activate({factor_type}) -> {response.status_code}"
            )
            if response.status_code != 200:
                self._set_error(
                    f"MFA {factor_type} 激活失败: {response.status_code}"
                )
                return None
            payload = response.json()
            if not isinstance(payload, dict):
                self._set_error("MFA 激活响应格式无效")
                return None
            error = payload.get("error")
            if error or payload.get("success") is False:
                error_code = ""
                if isinstance(error, dict):
                    error_code = str(
                        error.get("code") or error.get("type") or ""
                    ).strip()
                elif error:
                    error_code = str(error).strip()
                self._set_error(
                    "MFA 激活被服务端拒绝"
                    + (f": {error_code}" if error_code else "")
                )
                return None

            if factor_type == "totp":
                rotated_at = datetime.now(timezone.utc).isoformat()
                self.last_mfa_enrollment["totp_secret"] = secret
                self.last_mfa_enrollment["rotated_at"] = rotated_at
                if callable(on_totp_activated):
                    try:
                        on_totp_activated(rotated_at)
                    except Exception as exc:
                        self._log(
                            "新 MFA 激活状态写前记录更新失败，"
                            "将由最终凭据落库兜底: "
                            f"{type(exc).__name__}"
                        )
            else:
                self.last_mfa_enrollment["recovery_code"] = secret
                if callable(on_recovery_code):
                    try:
                        on_recovery_code(secret)
                    except Exception as exc:
                        self._log(
                            "新 MFA 恢复码写前记录更新失败，"
                            "将由最终凭据落库兜底: "
                            f"{type(exc).__name__}"
                        )

            next_state = self._state_from_payload(
                payload,
                current_url=str(response.url) or activate_url,
            )
            self._log(
                f"MFA {factor_type} 已激活: {describe_flow_state(next_state)}"
            )
            return next_state
        except TaskInterruption:
            raise
        except Exception as exc:
            self._set_error(
                f"MFA {factor_type} 激活异常: {type(exc).__name__}"
            )
            return None

    @staticmethod
    def _extract_totp_factor_id(state: FlowState) -> str:
        payloads = [state.payload or {}]
        raw_page = (state.raw or {}).get("page") or {}
        if isinstance(raw_page, dict):
            payloads.append(raw_page.get("payload") or {})

        for payload in payloads:
            if not isinstance(payload, dict):
                continue
            factors = payload.get("factors") or []
            for factor in factors:
                if not isinstance(factor, dict):
                    continue
                factor_type = str(
                    factor.get("factor_type") or factor.get("type") or ""
                ).strip().lower()
                if factor_type != "totp":
                    continue
                factor_id = str(
                    factor.get("id") or payload.get("factor_id") or ""
                ).strip()
                if factor_id:
                    return factor_id

        target = str(state.continue_url or state.current_url or "").strip()
        parts = [part for part in urlparse(target).path.split("/") if part]
        for index, part in enumerate(parts):
            if part.lower() == "mfa-challenge" and index + 1 < len(parts):
                return str(parts[index + 1] or "").strip()
        return ""

    def _state_is_add_phone(self, state: FlowState):
        target = f"{state.continue_url} {state.current_url}".lower()
        return state.page_type == "add_phone" or "add-phone" in target

    def _state_is_about_you(self, state: FlowState):
        target = f"{state.continue_url} {state.current_url}".lower()
        return state.page_type == "about_you" or "about-you" in target

    def _state_is_choose_an_account(self, state: FlowState) -> bool:
        target = f"{state.continue_url} {state.current_url}".lower()
        return state.page_type == "choose_an_account" or "choose-an-account" in target

    @staticmethod
    def _extract_choose_account_session_id(page_html: str, email: str = "") -> str:
        body = str(page_html or "")
        matches: list[tuple[int, str]] = []
        for match in re.finditer(
            r"<(?:input|button)\b[^>]*\bname=[\"']session_id[\"'][^>]*>",
            body,
            flags=re.IGNORECASE,
        ):
            value_match = re.search(
                r"\bvalue=[\"']([^\"']+)[\"']",
                match.group(0),
                flags=re.IGNORECASE,
            )
            if value_match:
                matches.append((match.start(), html.unescape(value_match.group(1)).strip()))
        matches = [(position, value) for position, value in matches if value]
        if not matches:
            return ""

        target_email = str(email or "").strip().lower()
        if target_email:
            for index, (position, value) in enumerate(matches):
                end = matches[index + 1][0] if index + 1 < len(matches) else len(body)
                account_fragment = html.unescape(body[position:end]).lower()
                if target_email in account_fragment:
                    return value
        if len(matches) == 1:
            return matches[0][1]
        return ""

    def _submit_choose_account_session(
        self,
        state: FlowState,
        *,
        email: str,
        device_id: str,
        user_agent=None,
        sec_ch_ua=None,
        impersonate=None,
    ):
        """Select the authenticated OpenAI session before continuing Codex OAuth."""
        self._enter_stage("choose_account", "select")
        choose_url = normalize_flow_url(
            state.continue_url or state.current_url,
            auth_base=self.oauth_issuer,
        )
        if not choose_url:
            self._set_error("账号选择页面缺少有效地址")
            return None

        try:
            page_kwargs = {
                "headers": self._headers(
                    choose_url,
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    referer=f"{self.oauth_issuer}/",
                    navigation=True,
                ),
                "allow_redirects": False,
                "timeout": 30,
            }
            if impersonate:
                page_kwargs["impersonate"] = impersonate
            page_response = self.session.get(choose_url, **page_kwargs)
            if page_response.status_code != 200:
                self._set_error(
                    f"加载账号选择页面失败: HTTP {page_response.status_code}"
                )
                return None

            session_id = self._extract_choose_account_session_id(
                page_response.text,
                email=email,
            )
            if not session_id:
                self._set_error("账号选择页面未找到当前登录账号")
                return None

            sentinel_token = get_sentinel_token_via_browser(
                flow="authorize_continue",
                proxy=self.proxy,
                page_url=choose_url,
                headless=self.browser_mode != "headed",
                device_id=device_id,
                log_fn=lambda message: self._log(
                    f"choose_account: {message}"
                ),
            )
            if not sentinel_token:
                sentinel_token = build_sentinel_token(
                    self.session,
                    device_id,
                    flow="authorize_continue",
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    impersonate=impersonate,
                )
            if not sentinel_token:
                self._set_error("无法获取 sentinel token (choose_account)")
                return None

            request_url = f"{self.oauth_issuer}/api/accounts/session/select"
            headers = self._headers(
                request_url,
                user_agent=user_agent,
                sec_ch_ua=sec_ch_ua,
                accept="application/json",
                referer=choose_url,
                origin=self.oauth_issuer,
                content_type="application/json",
                fetch_site="same-origin",
                extra_headers={
                    "oai-device-id": device_id,
                    "openai-sentinel-token": sentinel_token,
                },
            )
            headers.update(generate_datadog_trace())
            post_kwargs = {
                "json": {"session_id": session_id},
                "headers": headers,
                "allow_redirects": False,
                "timeout": 30,
            }
            if impersonate:
                post_kwargs["impersonate"] = impersonate
            response = self.session.post(request_url, **post_kwargs)
            self._log(f"/session/select -> {response.status_code}")
            if response.status_code != 200:
                self._set_error(
                    f"选择当前登录账号失败: HTTP {response.status_code}"
                )
                return None
            next_state = self._state_from_payload(
                response.json(),
                current_url=str(response.url) or request_url,
            )
            self._log(f"账号选择完成 {describe_flow_state(next_state)}")
            return next_state
        except Exception as exc:
            self._set_error(f"选择当前登录账号异常: {exc}")
            return None

    def _state_requires_navigation(self, state: FlowState):
        method = (state.method or "GET").upper()
        if method != "GET":
            return False
        if (
            state.source == "api"
            and state.current_url
            and state.page_type not in {"login_password", "email_otp_verification"}
        ):
            return True
        if state.page_type == "external_url" and state.continue_url:
            return True
        if state.continue_url and state.continue_url != state.current_url:
            return True
        return False

    def _state_supports_workspace_resolution(self, state: FlowState):
        target = f"{state.continue_url} {state.current_url}".lower()
        if state.page_type in {
            "consent",
            "workspace_selection",
            "organization_selection",
        }:
            return True
        if any(
            marker in target
            for marker in (
                "sign-in-with-chatgpt",
                "consent",
                "workspace",
                "organization",
            )
        ):
            return True
        session_data = self._decode_oauth_session_cookie() or {}
        return bool(session_data.get("workspaces"))

    def _state_can_resume_authenticated_flow(self, state: FlowState) -> bool:
        if self._extract_code_from_state(state):
            return True
        if self._state_is_add_phone(state):
            return True
        if self._state_supports_workspace_resolution(state):
            return True
        return state.page_type in {
            "oauth_callback",
            "callback",
        }

    def _follow_flow_state(
        self,
        state: FlowState,
        referer=None,
        user_agent=None,
        impersonate=None,
        max_hops=16,
    ):
        """跟随服务端返回的 continue_url / current_url，返回新的状态或 authorization code。"""
        import re

        current_url = state.continue_url or state.current_url
        last_url = current_url or ""
        referer_url = referer

        if not current_url:
            return None, state

        initial_code = self._extract_code_from_url(current_url)
        if initial_code:
            return initial_code, self._state_from_url(current_url)

        for hop in range(max_hops):
            try:
                headers = self._headers(
                    current_url,
                    user_agent=user_agent,
                    accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    referer=referer_url,
                    navigation=True,
                )
                kwargs = {"headers": headers, "allow_redirects": False, "timeout": 30}
                if impersonate:
                    kwargs["impersonate"] = impersonate

                self._browser_pause(0.12, 0.3)
                r = self.session.get(current_url, **kwargs)
                last_url = str(r.url)
                self._log(f"follow[{hop + 1}] {r.status_code} {last_url[:120]}")
            except Exception as e:
                maybe_localhost = re.search(r"(https?://localhost[^\s\'\"]+)", str(e))
                if maybe_localhost:
                    location = maybe_localhost.group(1)
                    code = self._extract_code_from_url(location)
                    if code:
                        self._log("从 localhost 异常提取到 authorization code")
                        return code, self._state_from_url(location)
                self._log(f"follow[{hop + 1}] 异常: {str(e)[:160]}")
                return None, self._state_from_url(last_url or current_url)

            code = self._extract_code_from_url(last_url)
            if code:
                return code, self._state_from_url(last_url)

            if r.status_code in (301, 302, 303, 307, 308):
                location = normalize_flow_url(
                    r.headers.get("Location", ""), auth_base=self.oauth_issuer
                )
                if not location:
                    return None, self._state_from_url(last_url or current_url)
                code = self._extract_code_from_url(location)
                if code:
                    return code, self._state_from_url(location)
                referer_url = last_url or referer_url
                current_url = location
                continue

            content_type = (r.headers.get("content-type", "") or "").lower()
            if "application/json" in content_type:
                try:
                    next_state = self._state_from_payload(
                        r.json(), current_url=last_url or current_url
                    )
                except Exception:
                    next_state = self._state_from_url(last_url or current_url)
            else:
                next_state = self._state_from_url(last_url or current_url)

            return None, next_state

        return None, self._state_from_url(last_url or current_url)

    def _bootstrap_oauth_session(
        self,
        authorize_url,
        authorize_params,
        device_id=None,
        user_agent=None,
        sec_ch_ua=None,
        impersonate=None,
    ):
        """启动 OAuth 会话，确保 auth 域上的 login_session 已建立。"""
        self.last_http_status = 0
        if device_id:
            seed_oai_device_cookie(self.session, device_id)

        has_login_session = False
        authorize_final_url = ""
        authorize_status = 0

        try:
            headers = self._headers(
                authorize_url,
                user_agent=user_agent,
                sec_ch_ua=sec_ch_ua,
                accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                referer="https://chatgpt.com/",
                navigation=True,
            )
            kwargs = {
                "params": authorize_params,
                "headers": headers,
                "allow_redirects": True,
                "timeout": 30,
            }
            if impersonate:
                kwargs["impersonate"] = impersonate

            self._browser_pause()
            r = self.session.get(authorize_url, **kwargs)
            authorize_status = int(r.status_code or 0)
            self.last_http_status = authorize_status
            authorize_final_url = str(r.url)
            redirects = len(getattr(r, "history", []) or [])
            self._log(f"/oauth/authorize -> {r.status_code}, redirects={redirects}")

            has_login_session = any(
                (cookie.name if hasattr(cookie, "name") else str(cookie))
                == "login_session"
                for cookie in self.session.cookies
            )
            self._log(f"login_session: {'已获取' if has_login_session else '未获取'}")
        except Exception as e:
            self._log(f"/oauth/authorize 异常: {e}")

        if 200 <= authorize_status < 400 and has_login_session:
            return authorize_final_url

        self._log("未获取到 login_session，尝试 /api/oauth/oauth2/auth...")
        oauth2_status = 0
        try:
            oauth2_url = f"{self.oauth_issuer}/api/oauth/oauth2/auth"
            kwargs = {
                "params": authorize_params,
                "headers": self._headers(
                    oauth2_url,
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    referer="https://chatgpt.com/",
                    navigation=True,
                ),
                "allow_redirects": True,
                "timeout": 30,
            }
            if impersonate:
                kwargs["impersonate"] = impersonate

            self._browser_pause()
            r2 = self.session.get(oauth2_url, **kwargs)
            oauth2_status = int(r2.status_code or 0)
            self.last_http_status = oauth2_status
            authorize_final_url = str(r2.url)
            redirects2 = len(getattr(r2, "history", []) or [])
            self._log(
                f"/api/oauth/oauth2/auth -> {r2.status_code}, redirects={redirects2}"
            )

            has_login_session = any(
                (cookie.name if hasattr(cookie, "name") else str(cookie))
                == "login_session"
                for cookie in self.session.cookies
            )
            self._log(
                f"login_session(重试): {'已获取' if has_login_session else '未获取'}"
            )
        except Exception as e:
            self._log(f"/api/oauth/oauth2/auth 异常: {e}")

        if 200 <= oauth2_status < 400 and has_login_session:
            return authorize_final_url
        return ""

    def prepare_phone_verification_transaction(
        self,
        *,
        email: str = "",
        device_id: str,
        user_agent: str,
        sec_ch_ua: str,
        accept_language: str = "",
        impersonate: str = "",
    ):
        """Clone the post-email-OTP auth context and start the phone OAuth transaction.

        The clone is intentional: the original session still has to consume the
        ChatGPT callback to obtain the web Access Token.  The cloned session is
        parked on the Codex OAuth state and is resumed later by the phone flow.
        """
        from .oauth_resume_cache import (
            OAuthResumeContext,
            restore_oauth_resume_context,
            serialize_oauth_resume_context,
        )

        self._enter_stage("phone_oauth_prepare")
        self.last_http_status = 0
        self.last_state = FlowState()

        browser_snapshot = serialize_oauth_resume_context(
            self.session,
            device_id=device_id,
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
            accept_language=accept_language,
            impersonate=impersonate,
            ttl_seconds=1800,
        )
        cloned_context = restore_oauth_resume_context(browser_snapshot)
        if cloned_context is None:
            self._set_error("邮箱登录已通过，但复制手机授权浏览器上下文失败")
            return None

        self.adopt_browser_context(
            cloned_context.session,
            device_id=device_id,
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
            accept_language=accept_language,
        )
        code_verifier, code_challenge = generate_pkce()
        oauth_state = secrets.token_urlsafe(32)
        authorize_url = f"{self.oauth_issuer}/oauth/authorize"
        authorize_params = {
            "response_type": "code",
            "client_id": self.oauth_client_id,
            "redirect_uri": self.oauth_redirect_uri,
            "scope": "openid profile email offline_access",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": oauth_state,
        }
        self._log("邮箱 OTP 已通过，预建手机验证 OAuth 事务...")
        authorize_final_url = self._bootstrap_oauth_session(
            authorize_url,
            authorize_params,
            device_id=device_id,
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
            impersonate=impersonate,
        )
        if not authorize_final_url:
            self._set_error("邮箱登录已通过，但预建手机验证 OAuth 事务失败")
            return None

        state = self._state_from_url(authorize_final_url)
        self.last_state = state
        if self._state_is_choose_an_account(state):
            self._log("预建手机验证 OAuth 命中账号选择页，选择当前登录账号...")
            state = self._submit_choose_account_session(
                state,
                email=email,
                device_id=device_id,
                user_agent=user_agent,
                sec_ch_ua=sec_ch_ua,
                impersonate=impersonate,
            )
            if state is None:
                if not self.last_error:
                    self._set_error("邮箱登录已通过，但选择当前登录账号失败")
                return None
            self.last_state = state
        if not self._state_can_resume_authenticated_flow(state):
            self._set_error(
                "邮箱登录已通过，但手机验证 OAuth 未进入可续接状态: "
                f"{describe_flow_state(state)}"
            )
            return None

        referer = (
            authorize_final_url
            if authorize_final_url.startswith(self.oauth_issuer)
            else f"{self.oauth_issuer}/log-in"
        )
        self.last_state = state
        self._log(
            "手机验证 OAuth 事务已预建并暂停: "
            f"{describe_flow_state(state)}"
        )
        return OAuthResumeContext(
            session=self.session,
            device_id=str(device_id or "").strip(),
            user_agent=str(user_agent or "").strip(),
            sec_ch_ua=str(sec_ch_ua or "").strip(),
            accept_language=str(accept_language or "").strip(),
            impersonate=str(impersonate or "").strip(),
            code_verifier=code_verifier,
            oauth_state=oauth_state,
            authorize_url=authorize_url,
            authorize_params=authorize_params,
            flow_state=state,
            referer=referer,
            expires_at=time.monotonic() + 1800,
        )

    def _bootstrap_chatgpt_entry(
        self,
        email: str,
        device_id: str,
        *,
        user_agent=None,
        sec_ch_ua=None,
        impersonate=None,
    ) -> str:
        """模拟注册链路一致的 ChatGPT 首页 -> CSRF -> signin/openai。"""
        homepage_url = "https://chatgpt.com/"
        csrf_url = "https://chatgpt.com/api/auth/csrf"
        signin_url = "https://chatgpt.com/api/auth/signin/openai"

        try:
            self._log("force_chatgpt_entry: 访问 ChatGPT 首页...")
            self._browser_pause()
            r_home = self.session.get(
                homepage_url,
                headers=self._headers(
                    homepage_url,
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    accept="text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                    navigation=True,
                ),
                allow_redirects=True,
                timeout=30,
            )
            self._log(f"force_chatgpt_entry: 首页状态 {r_home.status_code}")
        except Exception as e:
            self._log(f"force_chatgpt_entry: 首页访问异常: {e}")

        csrf_token = ""
        try:
            self._log("force_chatgpt_entry: 获取 CSRF token...")
            r_csrf = self.session.get(
                csrf_url,
                headers=self._headers(
                    csrf_url,
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    accept="application/json",
                    referer=homepage_url,
                    fetch_site="same-origin",
                ),
                timeout=30,
            )
            if r_csrf.status_code == 200:
                csrf_token = (r_csrf.json() or {}).get("csrfToken", "") or ""
                if csrf_token:
                    self._log(f"force_chatgpt_entry: CSRF token={csrf_token[:16]}...")
        except Exception as e:
            self._log(f"force_chatgpt_entry: 获取 CSRF 异常: {e}")

        authorize_url = ""
        try:
            self._log("force_chatgpt_entry: 提交邮箱获取 authorize URL...")
            params = {
                "prompt": "login",
                "ext-oai-did": device_id,
                "auth_session_logging_id": str(uuid.uuid4()),
                "screen_hint": "login_or_signup",
                "login_hint": email,
            }
            form_data = {
                "callbackUrl": "https://chatgpt.com/",
                "csrfToken": csrf_token,
                "json": "true",
            }
            r_signin = self.session.post(
                signin_url,
                params=params,
                data=form_data,
                headers=self._headers(
                    signin_url,
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    accept="application/json",
                    referer=homepage_url,
                    origin="https://chatgpt.com",
                    content_type="application/x-www-form-urlencoded",
                    fetch_site="same-origin",
                ),
                timeout=30,
            )
            if r_signin.status_code == 200:
                authorize_url = (r_signin.json() or {}).get("url", "") or ""
                if authorize_url:
                    self._log("force_chatgpt_entry: 已获取 authorize URL")
            else:
                self._log(
                    f"force_chatgpt_entry: authorize URL 获取失败 {r_signin.status_code}"
                )
        except Exception as e:
            self._log(f"force_chatgpt_entry: 提交邮箱异常: {e}")

        if not authorize_url:
            return ""

        try:
            self._log("force_chatgpt_entry: 访问 authorize URL...")
            self._browser_pause()
            kwargs = {
                "headers": self._headers(
                    authorize_url,
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    referer=homepage_url,
                    navigation=True,
                ),
                "allow_redirects": True,
                "timeout": 30,
            }
            if impersonate:
                kwargs["impersonate"] = impersonate
            r_auth = self.session.get(authorize_url, **kwargs)
            final_url = str(r_auth.url)
            self._log(
                f"force_chatgpt_entry: authorize 最终跳转 {final_url[:160]}"
            )
            return final_url
        except Exception as e:
            self._log(f"force_chatgpt_entry: 访问 authorize 异常: {e}")
            return authorize_url

    def _submit_authorize_continue(
        self,
        email,
        device_id,
        continue_referer,
        *,
        user_agent=None,
        sec_ch_ua=None,
        impersonate=None,
        authorize_url=None,
        authorize_params=None,
        screen_hint=None,
    ):
        """提交邮箱，获取 OAuth 流程的第一页状态。"""
        self._enter_stage("authorize_continue", f"email={email}")
        self._log("步骤2: POST /api/accounts/authorize/continue")

        self._log(f"authorize_continue: device_id={device_id}")
        sentinel_token = None
        for _sentinel_attempt in range(2):
            sentinel_token = get_sentinel_token_via_browser(
                flow="authorize_continue",
                proxy=self.proxy,
                page_url=continue_referer or f"{self.oauth_issuer}/log-in",
                headless=self.browser_mode != "headed",
                device_id=device_id,
                log_fn=lambda msg: self._log(f"authorize_continue: {msg}"),
            )
            if sentinel_token:
                self._log("authorize_continue: 已通过 Playwright SentinelSDK 获取 token")
                break
            sentinel_token = build_sentinel_token(
                self.session,
                device_id,
                flow="authorize_continue",
                user_agent=user_agent,
                sec_ch_ua=sec_ch_ua,
                impersonate=impersonate,
            )
            if sentinel_token:
                self._log("authorize_continue: 已通过 HTTP PoW 获取 token")
                break
            if _sentinel_attempt == 0:
                self._log("authorize_continue: sentinel token 获取失败，重试一次...")
        if not sentinel_token:
            self._set_error("无法获取 sentinel token (authorize_continue)")
            return None

        request_url = f"{self.oauth_issuer}/api/accounts/authorize/continue"
        headers = self._headers(
            request_url,
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
            accept="application/json",
            referer=continue_referer,
            origin=self.oauth_issuer,
            content_type="application/json",
            fetch_site="same-origin",
            extra_headers={
                "oai-device-id": device_id,
                "openai-sentinel-token": sentinel_token,
            },
        )
        headers.update(generate_datadog_trace())
        payload = {"username": {"kind": "email", "value": email}}
        if screen_hint:
            payload["screen_hint"] = str(screen_hint).strip()

        try:
            kwargs = {
                "json": payload,
                "headers": headers,
                "timeout": 30,
                "allow_redirects": False,
            }
            if impersonate:
                kwargs["impersonate"] = impersonate

            self._browser_pause()
            r = self.session.post(request_url, **kwargs)
            self._log(f"/authorize/continue -> {r.status_code}")
            self._log(
                "authorize_continue 响应: "
                f"referer={(continue_referer or '')[:100]} "
                f"current_url={str(r.url)[:120]}"
            )

            if (
                r.status_code == 400
                and "invalid_auth_step" in (r.text or "")
                and authorize_url
                and authorize_params
            ):
                self._log("invalid_auth_step，重新 bootstrap...")
                authorize_final_url = self._bootstrap_oauth_session(
                    authorize_url,
                    authorize_params,
                    device_id=device_id,
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    impersonate=impersonate,
                )
                continue_referer = (
                    authorize_final_url
                    if authorize_final_url.startswith(self.oauth_issuer)
                    else f"{self.oauth_issuer}/log-in"
                )
                headers["Referer"] = continue_referer
                headers["Sec-Fetch-Site"] = "same-origin"
                headers.update(generate_datadog_trace())
                kwargs = {
                    "json": payload,
                    "headers": headers,
                    "timeout": 30,
                    "allow_redirects": False,
                }
                if impersonate:
                    kwargs["impersonate"] = impersonate
                self._browser_pause()
                r = self.session.post(request_url, **kwargs)
                self._log(f"/authorize/continue(重试) -> {r.status_code}")

            if r.status_code != 200:
                self._set_error(f"提交邮箱失败: {r.status_code} - {r.text[:180]}")
                return None

            data = r.json()
            flow_state = self._state_from_payload(
                data, current_url=str(r.url) or request_url
            )
            self._log(describe_flow_state(flow_state))
            return flow_state
        except Exception as e:
            self._set_error(f"提交邮箱异常: {e}")
            return None

    def _submit_password_verify(
        self,
        password,
        device_id,
        *,
        user_agent=None,
        sec_ch_ua=None,
        impersonate=None,
        referer=None,
    ):
        """提交密码，获取下一步状态。"""
        self._log("步骤3: POST /api/accounts/password/verify")

        self._log(f"password_verify: device_id={device_id}")
        sentinel_pwd = get_sentinel_token_via_browser(
            flow="password_verify",
            proxy=self.proxy,
            page_url=referer or f"{self.oauth_issuer}/log-in/password",
            headless=self.browser_mode != "headed",
            device_id=device_id,
            log_fn=lambda msg: self._log(f"password_verify: {msg}"),
        )
        if sentinel_pwd:
            self._log("password_verify: 已通过 Playwright SentinelSDK 获取 token")
        else:
            sentinel_pwd = build_sentinel_token(
                self.session,
                device_id,
                flow="password_verify",
                user_agent=user_agent,
                sec_ch_ua=sec_ch_ua,
                impersonate=impersonate,
            )
            if sentinel_pwd:
                self._log("password_verify: 已通过 HTTP PoW 获取 token")
            else:
                self._set_error("无法获取 sentinel token (password_verify)")
                return None

        request_url = f"{self.oauth_issuer}/api/accounts/password/verify"
        headers = self._headers(
            request_url,
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
            accept="application/json",
            referer=referer or f"{self.oauth_issuer}/log-in/password",
            origin=self.oauth_issuer,
            content_type="application/json",
            fetch_site="same-origin",
            extra_headers={
                "oai-device-id": device_id,
                "openai-sentinel-token": sentinel_pwd,
            },
        )
        headers.update(generate_datadog_trace())

        try:
            kwargs = {
                "json": {"password": password},
                "headers": headers,
                "timeout": 30,
                "allow_redirects": False,
            }
            if impersonate:
                kwargs["impersonate"] = impersonate

            self._browser_pause()
            r = self.session.post(request_url, **kwargs)
            self._log(f"/password/verify -> {r.status_code}")

            if r.status_code != 200:
                response_text = str(r.text or "")
                error_code = ""
                error_message = ""
                try:
                    error_payload = r.json()
                except Exception:
                    error_payload = None
                if isinstance(error_payload, dict):
                    error_detail = error_payload.get("error") or error_payload.get("错误")
                    if isinstance(error_detail, dict):
                        error_code = str(
                            error_detail.get("code")
                            or error_detail.get("error_code")
                            or error_detail.get("type")
                            or error_detail.get("代码")
                            or ""
                        ).strip()
                        error_message = str(
                            error_detail.get("message")
                            or error_detail.get("消息")
                            or ""
                        ).strip()
                    error_code = error_code or str(
                        error_payload.get("code")
                        or error_payload.get("error_code")
                        or error_payload.get("type")
                        or (
                            error_payload.get("error")
                            if isinstance(error_payload.get("error"), str)
                            else ""
                        )
                        or ""
                    ).strip()
                    error_message = error_message or str(
                        error_payload.get("message")
                        or error_payload.get("error_description")
                        or ""
                    ).strip()
                if _is_password_verify_deactivation_response(
                    r.status_code,
                    error_code,
                    error_message,
                ):
                    terminal_message = error_message or "账号已被删除或停用"
                    self._set_error(terminal_message)
                    raise ChatGPTAccountDeactivatedError(terminal_message)
                self._set_error(
                    f"密码验证失败: {r.status_code} - {response_text[:180]}"
                )
                return None

            data = r.json()
            flow_state = self._state_from_payload(
                data, current_url=str(r.url) or request_url
            )
            self._log(f"verify {describe_flow_state(flow_state)}")
            return flow_state
        except TaskInterruption:
            raise
        except Exception as e:
            self._set_error(f"密码验证异常: {e}")
            return None

    def _request_password_reset_otp(
        self,
        state: FlowState,
        *,
        device_id: str,
        user_agent=None,
        sec_ch_ua=None,
        impersonate=None,
    ):
        """Start OpenAI's password-reset flow for the current auth session."""
        self._enter_stage("password_reset", "send_otp")
        request_url = f"{self.oauth_issuer}/api/accounts/password/send-otp"
        headers = self._headers(
            request_url,
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
            accept="application/json",
            referer=(
                state.current_url
                or state.continue_url
                or f"{self.oauth_issuer}/reset-password"
            ),
            origin=self.oauth_issuer,
            content_type="application/json",
            fetch_site="same-origin",
            extra_headers={"oai-device-id": device_id},
        )
        headers.update(generate_datadog_trace())
        try:
            kwargs = {
                "headers": headers,
                "timeout": 30,
                "allow_redirects": False,
            }
            if impersonate:
                kwargs["impersonate"] = impersonate
            self._browser_pause()
            response = self.session.post(request_url, **kwargs)
            self._log(f"/password/send-otp -> {response.status_code}")
            if response.status_code != 200:
                response_detail = sanitize_chatgpt_log_message(
                    str(response.text or "")[:240]
                ).strip()
                self._set_error(
                    "密码重置验证码发送失败: "
                    f"HTTP {response.status_code}"
                    + (f" - {response_detail}" if response_detail else "")
                )
                return None
            next_state = self._state_from_payload(
                response.json(),
                current_url=str(response.url) or request_url,
            )
            self._log(f"password reset {describe_flow_state(next_state)}")
            return next_state
        except TaskInterruption:
            raise
        except Exception as exc:
            self._set_error(f"密码重置验证码发送异常: {exc}")
            return None

    def _submit_password_reset_new_password(
        self,
        state: FlowState,
        *,
        new_password: str,
        device_id: str,
        user_agent=None,
        sec_ch_ua=None,
        impersonate=None,
    ):
        """Persist the new password after the reset email OTP was verified."""
        password = str(new_password or "")
        if len(password) < 12:
            self._set_error("新密码长度不足 12 个字符")
            return None

        self._enter_stage("password_reset", "new_password")
        page_url = (
            state.current_url
            or state.continue_url
            or f"{self.oauth_issuer}/reset-password/new-password"
        )
        sentinel_token = get_sentinel_token_via_browser(
            flow="password_reset",
            proxy=self.proxy,
            page_url=page_url,
            headless=self.browser_mode != "headed",
            device_id=device_id,
            log_fn=lambda msg: self._log(f"password_reset: {msg}"),
        )
        if not sentinel_token:
            sentinel_token = build_sentinel_token(
                self.session,
                device_id,
                flow="password_reset",
                user_agent=user_agent,
                sec_ch_ua=sec_ch_ua,
                impersonate=impersonate,
            )
        if not sentinel_token:
            self._set_error("无法获取 sentinel token (password_reset)")
            return None

        request_url = f"{self.oauth_issuer}/api/accounts/password/reset"
        headers = self._headers(
            request_url,
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
            accept="application/json",
            referer=page_url,
            origin=self.oauth_issuer,
            content_type="application/json",
            fetch_site="same-origin",
            extra_headers={
                "oai-device-id": device_id,
                "openai-sentinel-token": sentinel_token,
            },
        )
        headers.update(generate_datadog_trace())
        try:
            kwargs = {
                "json": {"password": password},
                "headers": headers,
                "timeout": 30,
                "allow_redirects": False,
            }
            if impersonate:
                kwargs["impersonate"] = impersonate
            self._browser_pause()
            response = self.session.post(request_url, **kwargs)
            self._log(f"/password/reset -> {response.status_code}")
            if response.status_code != 200:
                self._set_error(
                    f"新密码保存失败: HTTP {response.status_code}"
                )
                return None
            next_state = self._state_from_payload(
                response.json(),
                current_url=str(response.url) or request_url,
            )
            self._log(f"password reset {describe_flow_state(next_state)}")
            return next_state
        except TaskInterruption:
            raise
        except Exception as exc:
            self._set_error(f"新密码保存异常: {exc}")
            return None

    def _complete_password_reset(
        self,
        state: FlowState,
        *,
        email: str,
        new_password: str,
        skymail_client,
        device_id: str,
        user_agent=None,
        sec_ch_ua=None,
        impersonate=None,
        on_password_reset=None,
    ):
        """Complete reset OTP + new-password submission in the active session."""
        if skymail_client is None:
            self._set_error("密码重置需要邮箱验证码接口，但当前记录未配置")
            return None
        otp_state = self._request_password_reset_otp(
            state,
            device_id=device_id,
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
            impersonate=impersonate,
        )
        if not otp_state or not self._state_is_email_otp(otp_state):
            if not self.last_error:
                self._set_error("密码重置未进入邮箱验证码步骤")
            return None
        new_password_state = self._handle_otp_verification(
            email,
            device_id,
            user_agent,
            sec_ch_ua,
            impersonate,
            skymail_client,
            otp_state,
            prefer_passwordless_login=False,
        )
        if not new_password_state:
            if not self.last_error:
                self._set_error("密码重置邮箱验证码校验失败")
            return None
        if not self._state_is_password_reset_new_password(new_password_state):
            self._set_error(
                "密码重置验证码通过，但认证服务未进入设置新密码步骤"
            )
            return None
        success_state = self._submit_password_reset_new_password(
            new_password_state,
            new_password=new_password,
            device_id=device_id,
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
            impersonate=impersonate,
        )
        if not success_state or not self._state_is_password_reset_success(
            success_state
        ):
            if not self.last_error:
                self._set_error("新密码提交后未收到成功状态")
            return None
        if callable(on_password_reset):
            try:
                committed = on_password_reset(str(new_password or ""))
                if committed is False:
                    self._set_error(
                        "密码已在认证服务重置，但本地凭据保存失败"
                    )
                    return None
            except Exception as exc:
                self._set_error(
                    "密码已在认证服务重置，但本地凭据保存失败"
                    f"（{type(exc).__name__}）"
                )
                return None
        self._log("密码重置完成，新密码已安全保存；重新开始登录")
        return success_state

    def _submit_totp_mfa_challenge(
        self,
        state: FlowState,
        *,
        totp_secret: str,
        totp_code_provider=None,
        device_id: str,
        user_agent=None,
        sec_ch_ua=None,
        impersonate=None,
    ):
        """初始化并提交 ChatGPT TOTP MFA，不向第三方发送 MFA 秘钥。"""
        self._enter_stage("mfa", "totp")
        factor_id = self._extract_totp_factor_id(state)
        if not factor_id:
            self._set_error("ChatGPT MFA 页面未返回 TOTP 因子")
            return None
        if not str(totp_secret or "").strip() and not callable(totp_code_provider):
            self._set_error("ChatGPT 账号需要 MFA，但导入记录缺少 MFA 秘钥")
            return None

        referer = (
            state.continue_url
            or state.current_url
            or f"{self.oauth_issuer}/mfa-challenge/{factor_id}"
        )
        common_headers = {
            "oai-device-id": device_id,
        }

        issue_url = f"{self.oauth_issuer}/api/accounts/mfa/issue_challenge"
        issue_headers = self._headers(
            issue_url,
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
            accept="*/*",
            referer=referer,
            origin=self.oauth_issuer,
            content_type="application/json",
            fetch_site="same-origin",
            extra_headers=common_headers,
        )
        issue_headers.update(generate_datadog_trace())
        issue_kwargs = {
            "json": {
                "id": factor_id,
                "type": "totp",
                "force_fresh_challenge": False,
            },
            "headers": issue_headers,
            "timeout": 30,
            "allow_redirects": False,
        }
        if impersonate:
            issue_kwargs["impersonate"] = impersonate

        try:
            self._browser_pause()
            issue_response = self.session.post(issue_url, **issue_kwargs)
            self._log(f"/mfa/issue_challenge -> {issue_response.status_code}")
            if issue_response.status_code != 200:
                self._set_error(
                    "ChatGPT MFA challenge 初始化失败: "
                    f"{issue_response.status_code} - {issue_response.text[:180]}"
                )
                return None

            if str(totp_secret or "").strip():
                code = generate_totp(str(totp_secret or ""))
            else:
                code = str(totp_code_provider() or "").strip()
                if not re.fullmatch(r"\d{6}", code):
                    self._set_error("远程 MFA 接口未返回有效的六位验证码")
                    return None
            verify_url = f"{self.oauth_issuer}/api/accounts/mfa/verify"
            verify_headers = self._headers(
                verify_url,
                user_agent=user_agent,
                sec_ch_ua=sec_ch_ua,
                accept="application/json",
                referer=referer,
                origin=self.oauth_issuer,
                content_type="application/json",
                fetch_site="same-origin",
                extra_headers=common_headers,
            )
            verify_headers.update(generate_datadog_trace())
            verify_kwargs = {
                "json": {
                    "id": factor_id,
                    "type": "totp",
                    "code": code,
                },
                "headers": verify_headers,
                "timeout": 30,
                "allow_redirects": False,
            }
            if impersonate:
                verify_kwargs["impersonate"] = impersonate

            self._browser_pause()
            verify_response = self.session.post(verify_url, **verify_kwargs)
            self._log(f"/mfa/verify -> {verify_response.status_code}")
            if verify_response.status_code != 200:
                self._set_error(
                    "ChatGPT MFA 验证失败: "
                    f"{verify_response.status_code} - {verify_response.text[:180]}"
                )
                return None

            verify_payload = verify_response.json()
            verify_error = (
                verify_payload.get("error")
                if isinstance(verify_payload, dict)
                else None
            )
            verify_error_code = ""
            if isinstance(verify_error, dict):
                verify_error_code = str(
                    verify_error.get("code") or verify_error.get("type") or ""
                ).strip()
            if verify_error_code:
                self._set_error(
                    "ChatGPT MFA 验证失败: "
                    f"HTTP 200 - {verify_error_code}"
                )
                return None
            next_state = self._state_from_payload(
                verify_payload,
                current_url=str(verify_response.url) or verify_url,
            )
            if self._state_is_mfa_challenge(next_state):
                self._set_error("ChatGPT MFA 验证失败: 验证后仍停留在 MFA 页面")
                return None
            self._log(f"MFA 通过 {describe_flow_state(next_state)}")
            return next_state
        except Exception as exc:
            self._set_error(f"ChatGPT MFA 验证异常: {exc}")
            return None

    def _submit_email_mfa_challenge(
        self,
        state: FlowState,
        *,
        factor: dict[str, str],
        email: str,
        skymail_client,
        device_id: str,
        user_agent=None,
        sec_ch_ua=None,
        impersonate=None,
    ):
        """通过当前邮箱后端完成 ChatGPT 邮箱型 MFA。"""
        self._enter_stage("mfa", "email")
        factor_id = str(factor.get("id") or "").strip()
        factor_type = str(factor.get("type") or "email").strip().lower()
        if not factor_id:
            self._set_error("ChatGPT 邮箱 MFA 页面未返回验证因子")
            return None
        wait_for_code = getattr(skymail_client, "wait_for_verification_code", None)
        if not callable(wait_for_code):
            self._set_error("ChatGPT 要求邮箱 MFA，但当前邮箱后端无法读取验证码")
            return None

        referer = (
            state.continue_url
            or state.current_url
            or f"{self.oauth_issuer}/mfa-challenge/{factor_id}"
        )
        common_headers = {"oai-device-id": device_id}
        issue_url = f"{self.oauth_issuer}/api/accounts/mfa/issue_challenge"
        issue_headers = self._headers(
            issue_url,
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
            accept="*/*",
            referer=referer,
            origin=self.oauth_issuer,
            content_type="application/json",
            fetch_site="same-origin",
            extra_headers=common_headers,
        )
        issue_headers.update(generate_datadog_trace())
        issue_kwargs = {
            "json": {
                "id": factor_id,
                "type": factor_type,
                "force_fresh_challenge": True,
            },
            "headers": issue_headers,
            "timeout": 30,
            "allow_redirects": False,
        }
        if impersonate:
            issue_kwargs["impersonate"] = impersonate

        try:
            otp_sent_at = time.time()
            self._browser_pause()
            issue_response = self.session.post(issue_url, **issue_kwargs)
            self._log(f"/mfa/issue_challenge({factor_type}) -> {issue_response.status_code}")
            if issue_response.status_code != 200:
                self._set_error(
                    "ChatGPT 邮箱 MFA challenge 初始化失败: "
                    f"{issue_response.status_code} - {issue_response.text[:180]}"
                )
                return None

            try:
                wait_seconds = int(
                    self.config.get("chatgpt_oauth_mfa_otp_wait_seconds", 120)
                    or 120
                )
            except (TypeError, ValueError):
                wait_seconds = 120
            wait_seconds = max(30, min(wait_seconds, 600))
            code = str(
                wait_for_code(
                    email,
                    timeout=wait_seconds,
                    otp_sent_at=otp_sent_at,
                )
                or ""
            ).strip()
            if not code:
                self._set_error("等待 ChatGPT 邮箱 MFA 验证码超时")
                return None

            verify_url = f"{self.oauth_issuer}/api/accounts/mfa/verify"
            verify_headers = self._headers(
                verify_url,
                user_agent=user_agent,
                sec_ch_ua=sec_ch_ua,
                accept="application/json",
                referer=referer,
                origin=self.oauth_issuer,
                content_type="application/json",
                fetch_site="same-origin",
                extra_headers=common_headers,
            )
            verify_headers.update(generate_datadog_trace())
            verify_kwargs = {
                "json": {
                    "id": factor_id,
                    "type": factor_type,
                    "code": code,
                },
                "headers": verify_headers,
                "timeout": 30,
                "allow_redirects": False,
            }
            if impersonate:
                verify_kwargs["impersonate"] = impersonate

            self._browser_pause()
            verify_response = self.session.post(verify_url, **verify_kwargs)
            self._log(f"/mfa/verify({factor_type}) -> {verify_response.status_code}")
            if verify_response.status_code != 200:
                self._set_error(
                    "ChatGPT 邮箱 MFA 验证失败: "
                    f"{verify_response.status_code} - {verify_response.text[:180]}"
                )
                return None

            next_state = self._state_from_payload(
                verify_response.json(),
                current_url=str(verify_response.url) or verify_url,
            )
            self._log(f"邮箱 MFA 通过 {describe_flow_state(next_state)}")
            return next_state
        except TaskInterruption:
            raise
        except Exception as exc:
            self._set_error(f"ChatGPT 邮箱 MFA 验证异常: {exc}")
            return None

    def _submit_recovery_code_mfa_challenge(
        self,
        state: FlowState,
        *,
        factor: dict[str, str],
        recovery_code: str,
        device_id: str,
        user_agent=None,
        sec_ch_ua=None,
        impersonate=None,
    ):
        """Use a stored recovery code without exposing it to logs."""
        self._enter_stage("mfa", "recovery_code")
        factor_id = str(factor.get("id") or "").strip()
        factor_type = str(
            factor.get("type") or "recovery_code"
        ).strip().lower()
        normalized_code = str(recovery_code or "").strip()
        if not factor_id:
            self._set_error("ChatGPT MFA 页面未返回恢复码因子")
            return None
        if not normalized_code:
            self._set_error("ChatGPT 账号需要恢复码，但本地记录为空")
            return None

        referer = (
            state.continue_url
            or state.current_url
            or f"{self.oauth_issuer}/mfa-challenge/{factor_id}"
        )
        common_headers = {"oai-device-id": device_id}
        issue_url = f"{self.oauth_issuer}/api/accounts/mfa/issue_challenge"
        issue_headers = self._headers(
            issue_url,
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
            accept="*/*",
            referer=referer,
            origin=self.oauth_issuer,
            content_type="application/json",
            fetch_site="same-origin",
            extra_headers=common_headers,
        )
        issue_headers.update(generate_datadog_trace())
        issue_kwargs = {
            "json": {
                "id": factor_id,
                "type": factor_type,
                "force_fresh_challenge": False,
            },
            "headers": issue_headers,
            "timeout": 30,
            "allow_redirects": False,
        }
        if impersonate:
            issue_kwargs["impersonate"] = impersonate

        try:
            self._browser_pause()
            issue_response = self.session.post(issue_url, **issue_kwargs)
            self._log(
                "/mfa/issue_challenge(recovery_code) -> "
                f"{issue_response.status_code}"
            )
            if issue_response.status_code != 200:
                self._set_error(
                    "ChatGPT MFA 恢复码 challenge 初始化失败: "
                    f"{issue_response.status_code} - "
                    f"{issue_response.text[:180]}"
                )
                return None

            verify_url = f"{self.oauth_issuer}/api/accounts/mfa/verify"
            verify_headers = self._headers(
                verify_url,
                user_agent=user_agent,
                sec_ch_ua=sec_ch_ua,
                accept="application/json",
                referer=referer,
                origin=self.oauth_issuer,
                content_type="application/json",
                fetch_site="same-origin",
                extra_headers=common_headers,
            )
            verify_headers.update(generate_datadog_trace())
            verify_kwargs = {
                "json": {
                    "id": factor_id,
                    "type": factor_type,
                    "code": normalized_code,
                },
                "headers": verify_headers,
                "timeout": 30,
                "allow_redirects": False,
            }
            if impersonate:
                verify_kwargs["impersonate"] = impersonate

            self._browser_pause()
            verify_response = self.session.post(verify_url, **verify_kwargs)
            self._log(
                "/mfa/verify(recovery_code) -> "
                f"{verify_response.status_code}"
            )
            if verify_response.status_code != 200:
                self._set_error(
                    "ChatGPT MFA 恢复码验证失败: "
                    f"{verify_response.status_code} - "
                    f"{verify_response.text[:180]}"
                )
                return None

            verify_payload = verify_response.json()
            verify_error = (
                verify_payload.get("error")
                if isinstance(verify_payload, dict)
                else None
            )
            verify_error_code = ""
            if isinstance(verify_error, dict):
                verify_error_code = str(
                    verify_error.get("code")
                    or verify_error.get("type")
                    or ""
                ).strip()
            if verify_error_code:
                self._set_error(
                    "ChatGPT MFA 恢复码验证失败: "
                    f"HTTP 200 - {verify_error_code}"
                )
                return None
            next_state = self._state_from_payload(
                verify_payload,
                current_url=str(verify_response.url) or verify_url,
            )
            if self._state_is_mfa_challenge(next_state):
                self._set_error(
                    "ChatGPT MFA 恢复码验证失败: 验证后仍停留在 MFA 页面"
                )
                return None
            self._log(f"MFA 恢复码通过 {describe_flow_state(next_state)}")
            return next_state
        except Exception as exc:
            self._set_error(f"ChatGPT MFA 恢复码验证异常: {exc}")
            return None

    @staticmethod
    def _mfa_code_was_rejected(error_message: str) -> bool:
        normalized_error = str(error_message or "").lower()
        return any(
            marker in normalized_error
            for marker in (
                "incorrect_code",
                "wrong_mfa_code",
                "invalid_totp",
                "invalid_recovery_code",
                "代码错误",
                "验证码错误",
                "恢复码错误",
            )
        )

    def _submit_mfa_challenge(
        self,
        state: FlowState,
        *,
        email: str,
        skymail_client,
        totp_secret: str,
        device_id: str,
        mfa_recovery_code: str = "",
        user_agent=None,
        sec_ch_ua=None,
        impersonate=None,
    ):
        factors = self._extract_mfa_factors(state)
        factor_types = [factor["type"] for factor in factors]
        self._log(
            "ChatGPT MFA 可用方式: "
            + (", ".join(factor_types) if factor_types else "未返回")
        )
        email_factor = next(
            (factor for factor in factors if "email" in factor["type"]),
            None,
        )
        totp_factor = next(
            (factor for factor in factors if factor["type"] == "totp"),
            None,
        )
        recovery_factor = next(
            (
                factor
                for factor in factors
                if factor["type"] == "recovery_code"
            ),
            None,
        )
        recovery_code = str(mfa_recovery_code or "").strip()
        fallback_factor_id = self._extract_totp_factor_id(state)
        if not factors and fallback_factor_id:
            if str(totp_secret or "").strip():
                totp_factor = {
                    "id": fallback_factor_id,
                    "type": "totp",
                }
                if recovery_code:
                    recovery_factor = {
                        "id": fallback_factor_id,
                        "type": "recovery_code",
                    }
            elif recovery_code:
                recovery_factor = {
                    "id": fallback_factor_id,
                    "type": "recovery_code",
                }
            self._log(
                "ChatGPT MFA 页面仅返回挑战地址，已按项目保存的验证凭据继续"
            )
        totp_code_provider = getattr(skymail_client, "get_totp_code", None)
        supports_totp_code = getattr(
            skymail_client,
            "supports_totp_code",
            None,
        )
        if callable(supports_totp_code):
            try:
                if not supports_totp_code():
                    totp_code_provider = None
            except Exception:
                totp_code_provider = None
        has_totp_source = bool(
            str(totp_secret or "").strip()
            or callable(totp_code_provider)
        )
        if totp_factor is not None and has_totp_source:
            totp_result = self._submit_totp_mfa_challenge(
                state,
                totp_secret=totp_secret,
                totp_code_provider=(
                    totp_code_provider
                    if callable(totp_code_provider)
                    else None
                ),
                device_id=device_id,
                user_agent=user_agent,
                sec_ch_ua=sec_ch_ua,
                impersonate=impersonate,
            )
            if totp_result is not None:
                return totp_result
            rejected_totp = self._mfa_code_was_rejected(self.last_error)
            if (
                recovery_factor is not None
                and recovery_code
                and rejected_totp
            ):
                self._log(
                    "已有 TOTP 被拒绝，自动使用项目保存的 MFA 恢复码"
                )
                self.last_error = ""
                recovery_result = self._submit_recovery_code_mfa_challenge(
                    state,
                    factor=recovery_factor,
                    recovery_code=recovery_code,
                    device_id=device_id,
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    impersonate=impersonate,
                )
                if recovery_result is not None:
                    return recovery_result
                rejected_totp = self._mfa_code_was_rejected(
                    self.last_error
                )
            if email_factor is not None and skymail_client is not None and rejected_totp:
                self._log(
                    "已有 MFA 验证码被拒绝，自动改用邮箱验证码继续登录"
                )
                self.last_error = ""
                return self._submit_email_mfa_challenge(
                    state,
                    factor=email_factor,
                    email=email,
                    skymail_client=skymail_client,
                    device_id=device_id,
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    impersonate=impersonate,
                )
            return None
        if recovery_factor is not None and recovery_code:
            return self._submit_recovery_code_mfa_challenge(
                state,
                factor=recovery_factor,
                recovery_code=recovery_code,
                device_id=device_id,
                user_agent=user_agent,
                sec_ch_ua=sec_ch_ua,
                impersonate=impersonate,
            )
        if email_factor is not None and skymail_client is not None:
            return self._submit_email_mfa_challenge(
                state,
                factor=email_factor,
                email=email,
                skymail_client=skymail_client,
                device_id=device_id,
                user_agent=user_agent,
                sec_ch_ua=sec_ch_ua,
                impersonate=impersonate,
            )

        if totp_factor is not None or (not factors and has_totp_source):
            return self._submit_totp_mfa_challenge(
                state,
                totp_secret=totp_secret,
                totp_code_provider=(
                    totp_code_provider
                    if callable(totp_code_provider)
                    else None
                ),
                device_id=device_id,
                user_agent=user_agent,
                sec_ch_ua=sec_ch_ua,
                impersonate=impersonate,
            )

        if factors:
            self._set_error(
                "ChatGPT 要求不受支持的 MFA 方式: " + ", ".join(factor_types)
            )
        else:
            self._set_error("ChatGPT MFA 页面未返回可识别的验证方式")
        return None

    def _send_passwordless_login_otp(
        self,
        email,
        device_id,
        *,
        user_agent=None,
        sec_ch_ua=None,
        impersonate=None,
        referer=None,
    ):
        """在 login_password 状态下直接切到 passwordless OTP。"""
        self._log("步骤3: 命中 login_password，按新链路直接触发 passwordless OTP")

        request_url = f"{self.oauth_issuer}/api/accounts/passwordless/send-otp"
        headers = self._headers(
            request_url,
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
            accept="application/json",
            referer=referer or f"{self.oauth_issuer}/log-in/password",
            origin=self.oauth_issuer,
            content_type="application/json",
            fetch_site="same-origin",
            extra_headers={
                "oai-device-id": device_id,
            },
        )
        headers.update(generate_datadog_trace())

        try:
            kwargs = {
                "headers": headers,
                "timeout": 30,
                "allow_redirects": False,
            }
            if impersonate:
                kwargs["impersonate"] = impersonate

            self._browser_pause()
            r = self.session.post(request_url, **kwargs)
            self._log(f"/passwordless/send-otp -> {r.status_code}")

            if r.status_code != 200:
                self._set_error(f"触发 passwordless OTP 失败: {r.status_code} - {r.text[:180]}")
                return None

            try:
                data = r.json()
            except Exception:
                data = {}

            flow_state = self._state_from_payload(
                data,
                current_url=str(r.url) or f"{self.oauth_issuer}/email-verification",
            )
            if not self._state_is_email_otp(flow_state):
                flow_state = self._state_from_url(f"{self.oauth_issuer}/email-verification")
            self._log(f"passwordless OTP 已触发 {describe_flow_state(flow_state)}")
            return flow_state
        except Exception as e:
            self._set_error(f"触发 passwordless OTP 异常: {e}")
            return None

    def _submit_signup_register(
        self,
        email,
        password,
        device_id,
        *,
        user_agent=None,
        sec_ch_ua=None,
        impersonate=None,
        referer=None,
    ):
        """在 OAuth signup 流程中提交邮箱+密码。"""
        self._enter_stage("authorize_continue", f"register_user email={email}")
        self._log("步骤3: 命中 create_account_password，提交注册密码")

        request_url = f"{self.oauth_issuer}/api/accounts/user/register"
        headers = self._headers(
            request_url,
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
            accept="application/json",
            referer=referer or f"{self.oauth_issuer}/create-account/password",
            origin=self.oauth_issuer,
            content_type="application/json",
            fetch_site="same-origin",
            extra_headers={
                "oai-device-id": device_id,
            },
        )
        headers.update(generate_datadog_trace())

        sentinel_token = get_sentinel_token_via_browser(
            flow="username_password_create",
            proxy=self.proxy,
            page_url=referer or f"{self.oauth_issuer}/create-account/password",
            headless=self.browser_mode != "headed",
            device_id=device_id,
            log_fn=lambda msg: self._log(f"username_password_create: {msg}"),
        )
        if sentinel_token:
            self._log("username_password_create: 已通过 Playwright SentinelSDK 获取 token")
        else:
            sentinel_token = build_sentinel_token(
                self.session,
                device_id,
                flow="username_password_create",
                user_agent=user_agent,
                sec_ch_ua=sec_ch_ua,
                impersonate=impersonate,
            )
            if sentinel_token:
                self._log("username_password_create: 已通过 HTTP PoW 获取 token")
        if sentinel_token:
            headers["openai-sentinel-token"] = sentinel_token

        payload = {
            "username": email,
            "password": password,
        }

        try:
            kwargs = {
                "json": payload,
                "headers": headers,
                "timeout": 30,
                "allow_redirects": False,
            }
            if impersonate:
                kwargs["impersonate"] = impersonate

            self._browser_pause()
            r = self.session.post(request_url, **kwargs)
            self._log(f"/user/register -> {r.status_code}")

            if r.status_code != 200:
                self._set_error(f"注册失败: {r.status_code} - {r.text[:180]}")
                return False

            self._log("注册成功")
            self._log(
                f"signup/register 响应: referer={(referer or '')[:100]} current_url={str(r.url)[:120]}"
            )
            return True
        except Exception as e:
            self._set_error(f"注册异常: {e}")
            return False

    def _send_signup_email_otp(
        self,
        device_id,
        *,
        user_agent=None,
        sec_ch_ua=None,
        impersonate=None,
        referer=None,
    ):
        """在 OAuth signup 流程中触发邮箱验证码。"""
        self._enter_stage("otp", "send signup email otp")
        self._log("步骤4: 触发注册邮箱 OTP")

        request_url = f"{self.oauth_issuer}/api/accounts/email-otp/send"
        headers = self._headers(
            request_url,
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
            accept="text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            referer=referer or f"{self.oauth_issuer}/create-account/password",
            navigation=True,
            fetch_site="same-origin",
        )
        headers.update(generate_datadog_trace())

        try:
            kwargs = {
                "headers": headers,
                "allow_redirects": True,
                "timeout": 30,
            }
            if impersonate:
                kwargs["impersonate"] = impersonate

            self._browser_pause()
            r = self.session.get(request_url, **kwargs)
            self._log(f"/email-otp/send -> {r.status_code}")
            if r.status_code != 200:
                self._set_error(f"发送注册 OTP 失败: {r.status_code} - {r.text[:180]}")
                return None

            verify_url = f"{self.oauth_issuer}/email-verification"
            verify_headers = self._headers(
                verify_url,
                user_agent=user_agent,
                sec_ch_ua=sec_ch_ua,
                accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                referer=referer or f"{self.oauth_issuer}/create-account/password",
                navigation=True,
            )
            verify_kwargs = {
                "headers": verify_headers,
                "allow_redirects": True,
                "timeout": 30,
            }
            if impersonate:
                verify_kwargs["impersonate"] = impersonate

            self._browser_pause(0.12, 0.25)
            r_verify = self.session.get(verify_url, **verify_kwargs)
            self._log(f"/email-verification -> {r_verify.status_code}")

            content_type = (r_verify.headers.get("content-type", "") or "").lower()
            if "application/json" in content_type:
                try:
                    flow_state = self._state_from_payload(
                        r_verify.json(),
                        current_url=str(r_verify.url) or verify_url,
                    )
                except Exception:
                    flow_state = self._state_from_url(str(r_verify.url) or verify_url)
            else:
                flow_state = self._state_from_url(str(r_verify.url) or verify_url)

            if not self._state_is_email_otp(flow_state):
                flow_state = self._state_from_url(verify_url)
            self._log(f"注册 OTP 已触发 {describe_flow_state(flow_state)}")
            return flow_state
        except Exception as e:
            self._set_error(f"发送注册 OTP 异常: {e}")
            return None

    def signup_and_get_tokens(
        self,
        email,
        password,
        first_name,
        last_name,
        birthdate,
        *,
        device_id="",
        user_agent=None,
        sec_ch_ua=None,
        impersonate=None,
        skymail_client=None,
        allow_phone_verification=False,
        signup_source="",
    ):
        """完成 OAuth 单链注册并换取 refresh token。"""
        self.last_error = ""
        self.last_workspace_id = ""
        self.last_state = FlowState()
        self._log(
            "开始 OAuth 注册流程..."
            + (f" (source={signup_source})" if signup_source else "")
        )
        self._log(
            "OAuth 注册策略: 单链路 signup -> otp -> about_you -> phone(如需) -> consent/workspace -> token"
        )

        if not skymail_client:
            self._set_error("OAuth 注册流程缺少接码客户端")
            return None

        device_id = str(device_id or "").strip() or str(uuid.uuid4())
        self.device_id = device_id
        user_agent, sec_ch_ua, impersonate = self._ensure_oauth_fingerprint(
            user_agent, sec_ch_ua, impersonate
        )

        code_verifier, code_challenge = generate_pkce()
        oauth_state = secrets.token_urlsafe(32)
        authorize_params = {
            "response_type": "code",
            "client_id": self.oauth_client_id,
            "audience": "https://api.openai.com/v1",
            "redirect_uri": self.oauth_redirect_uri,
            "scope": "openid profile email offline_access",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": oauth_state,
            "prompt": "login",
            "login_hint": email,
            "screen_hint": "login_or_signup",
            "ext-oai-did": device_id,
            "auth_session_logging_id": str(uuid.uuid4()),
            "ext-passkey-client-capabilities": "1111",
            "codex_cli_simplified_flow": "true",
            "id_token_add_organizations": "true",
        }
        authorize_url = f"{self.oauth_issuer}/oauth/authorize"

        seed_oai_device_cookie(self.session, device_id)

        self._log("步骤1: Bootstrap OAuth session...")
        authorize_final_url = self._bootstrap_oauth_session(
            authorize_url,
            authorize_params,
            device_id=device_id,
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
            impersonate=impersonate,
        )
        if not authorize_final_url:
            self._set_error("Bootstrap 失败")
            return None

        continue_referer = f"{self.oauth_issuer}/create-account"
        state = self._submit_authorize_continue(
            email,
            device_id,
            continue_referer,
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
            impersonate=impersonate,
            authorize_url=authorize_url,
            authorize_params=authorize_params,
            screen_hint="signup",
        )
        if not state:
            if not self.last_error:
                self._set_error("提交邮箱后未进入有效的 OAuth 注册状态")
            return None

        self._log(f"OAuth 注册状态起点: {describe_flow_state(state)}")
        referer = continue_referer
        seen_states = {}
        register_submitted = False

        for step in range(24):
            self.last_state = state
            self._log(f"注册状态步进[{step + 1}/24]: {describe_flow_state(state)}")
            signature = self._state_signature(state)
            seen_states[signature] = seen_states.get(signature, 0) + 1
            if seen_states[signature] > 2:
                self._set_error(f"OAuth 注册状态卡住: {describe_flow_state(state)}")
                return None

            code = self._extract_code_from_state(state)
            if code:
                self._log(f"获取到 authorization code: {code[:20]}...")
                self._log("步骤7: POST /oauth/token")
                tokens = self._exchange_code_for_tokens(
                    code, code_verifier, user_agent, impersonate
                )
                if tokens:
                    self._log("[OK] OAuth 注册成功")
                else:
                    self._log("换取 tokens 失败")
                return tokens

            if self._state_is_create_account_password(state):
                if register_submitted:
                    self._set_error("注册密码阶段重复进入")
                    return None
                ok = self._submit_signup_register(
                    email,
                    password,
                    device_id,
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    impersonate=impersonate,
                    referer=state.current_url or state.continue_url or referer,
                )
                if not ok:
                    return None
                register_submitted = True
                state = self._send_signup_email_otp(
                    device_id,
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    impersonate=impersonate,
                    referer=state.current_url or state.continue_url or referer,
                )
                if not state:
                    if not self.last_error:
                        self._set_error("注册 OTP 触发后未进入邮箱验证码状态")
                    return None
                referer = state.current_url or referer
                continue

            if self._state_is_email_otp(state):
                next_state = self._handle_otp_verification(
                    email,
                    device_id,
                    user_agent,
                    sec_ch_ua,
                    impersonate,
                    skymail_client,
                    state,
                    prefer_passwordless_login=False,
                    allow_cached_code_retry=False,
                )
                if not next_state:
                    if not self.last_error:
                        self._set_error("注册 OTP 验证后未进入下一步状态")
                    return None
                referer = state.current_url or referer
                state = next_state
                continue

            if self._state_is_about_you(state):
                next_state = self._submit_about_you_create_account(
                    first_name,
                    last_name,
                    birthdate,
                    device_id,
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    impersonate=impersonate,
                    referer=state.current_url or state.continue_url or referer,
                )
                if not next_state:
                    if not self.last_error:
                        self._set_error("about_you 提交后未进入下一步 OAuth 状态")
                    return None
                referer = state.current_url or referer
                state = next_state
                continue

            if self._state_is_add_phone(state):
                try:
                    raw_dump = json.dumps(state.raw or {}, ensure_ascii=False)
                except Exception:
                    raw_dump = ""
                if raw_dump:
                    self._log(f"add_phone 状态响应体(raw): {raw_dump}")
                if not allow_phone_verification:
                    if not self.last_error:
                        self._set_error("signup 链路命中 add_phone")
                    return None

                next_state = self._handle_add_phone_verification(
                    device_id,
                    user_agent,
                    sec_ch_ua,
                    impersonate,
                    state,
                )
                if not next_state:
                    if not self.last_error:
                        self._set_error("手机号验证后未进入下一步 OAuth 状态")
                    return None
                referer = state.current_url or referer
                state = next_state
                continue

            if self._state_requires_navigation(state):
                code, next_state = self._follow_flow_state(
                    state,
                    referer=referer,
                    user_agent=user_agent,
                    impersonate=impersonate,
                )
                if code:
                    self._log(f"获取到 authorization code: {code[:20]}...")
                    self._log("步骤7: POST /oauth/token")
                    tokens = self._exchange_code_for_tokens(
                        code, code_verifier, user_agent, impersonate
                    )
                    if tokens:
                        self._log("[OK] OAuth 注册成功")
                    else:
                        self._log("换取 tokens 失败")
                    return tokens
                referer = state.current_url or referer
                state = next_state
                self._log(f"follow state -> {describe_flow_state(state)}")
                continue

            if self._state_supports_workspace_resolution(state):
                self._log("步骤6: 执行 workspace/org 选择")
                consent_entry = (
                    state.continue_url
                    or state.current_url
                    or f"{self.oauth_issuer}/sign-in-with-chatgpt/codex/consent"
                )
                if self._state_is_add_phone(state):
                    consent_entry = f"{self.oauth_issuer}/sign-in-with-chatgpt/codex/consent"
                    self._log("步骤6: 当前处于 add_phone，改用 canonical consent URL 继续")
                code, next_state = self._oauth_submit_workspace_and_org(
                    consent_entry,
                    device_id,
                    user_agent,
                    impersonate,
                )
                if code:
                    self._log(f"获取到 authorization code: {code[:20]}...")
                    self._log("步骤7: POST /oauth/token")
                    tokens = self._exchange_code_for_tokens(
                        code, code_verifier, user_agent, impersonate
                    )
                    if tokens:
                        self._log("[OK] OAuth 注册成功")
                    else:
                        self._log("换取 tokens 失败")
                    return tokens
                if next_state:
                    referer = state.current_url or referer
                    state = next_state
                    self._log(f"workspace state -> {describe_flow_state(state)}")
                    continue
                if not self.last_error:
                    self._set_error(f"workspace/org 选择失败: {describe_flow_state(state)}")
                return None

            self._set_error(f"未支持的 OAuth 注册状态: {describe_flow_state(state)}")
            return None

        self._set_error("OAuth 注册状态机超出最大步数")
        return None

    def _submit_about_you_create_account(
        self,
        first_name,
        last_name,
        birthdate,
        device_id,
        *,
        user_agent=None,
        sec_ch_ua=None,
        impersonate=None,
        referer=None,
    ):
        """在 OAuth 登录态命中 about_you 后提交资料，完成账户创建。"""
        self._enter_stage("about_you", "submit create_account")
        self._log("步骤5: 命中 about_you，提交姓名和生日完成注册")
        self._log(
            "about_you 参数: "
            f"first_name={'已设置' if str(first_name or '').strip() else '缺失'}, "
            f"last_name={'已设置' if str(last_name or '').strip() else '缺失'}, "
            f"birthdate={str(birthdate or '').strip() or '缺失'}"
        )

        full_name = f"{str(first_name or '').strip()} {str(last_name or '').strip()}".strip()
        if not full_name or not str(birthdate or "").strip():
            self._set_error("about_you 资料不完整: 缺少姓名或生日")
            return None

        about_you_url = f"{self.oauth_issuer}/about-you"
        request_url = f"{self.oauth_issuer}/api/accounts/create_account"
        payload = {
            "name": full_name,
            "birthdate": str(birthdate).strip(),
        }
        self._log("about_you 请求体已构建，准备 POST /api/accounts/create_account")

        def _build_create_headers(sentinel_token: str = ""):
            extra_headers = {
                "oai-device-id": device_id,
            }
            if sentinel_token:
                extra_headers["openai-sentinel-token"] = sentinel_token
            headers_local = self._headers(
                request_url,
                user_agent=user_agent,
                sec_ch_ua=sec_ch_ua,
                accept="application/json",
                referer=referer or about_you_url,
                origin=self.oauth_issuer,
                content_type="application/json",
                fetch_site="same-origin",
                extra_headers=extra_headers,
            )
            headers_local.update(generate_datadog_trace())
            return headers_local

        def _post_create(sentinel_token: str = ""):
            kwargs = {
                "json": payload,
                "headers": _build_create_headers(sentinel_token),
                "timeout": 30,
                "allow_redirects": False,
            }
            if impersonate:
                kwargs["impersonate"] = impersonate
            self._browser_pause()
            return self.session.post(request_url, **kwargs)

        try:
            r = _post_create()
            self._log(f"/create_account -> {r.status_code}")
            self._log(
                "about_you 响应: "
                f"current_url={str(r.url)[:120]} referer={(referer or '')[:100]}"
            )

            if (
                r.status_code in (401, 403)
                or "sentinel" in (r.text or "").lower()
                or "challenge" in (r.text or "").lower()
            ):
                self._log("create_account 首次请求需要额外挑战，补发 sentinel 后重试...")
                sentinel_token = build_sentinel_token(
                    self.session,
                    device_id,
                    flow="oauth_create_account",
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    impersonate=impersonate,
                )
                if not sentinel_token:
                    self._set_error("无法获取 sentinel token (oauth_create_account)")
                    return None

                r = _post_create(sentinel_token)
                self._log(f"/create_account(重试) -> {r.status_code}")
                self._log(
                    "about_you 重试响应: "
                    f"current_url={str(r.url)[:120]} referer={(referer or '')[:100]}"
                )

            if r.status_code == 400 and "already_exists" in (r.text or ""):
                consent_state = self._state_from_url(
                    f"{self.oauth_issuer}/sign-in-with-chatgpt/codex/consent"
                )
                self._log(f"about_you 命中 already_exists，转入 {describe_flow_state(consent_state)}")
                return consent_state

            if r.status_code != 200:
                self._set_error(f"about_you 提交失败: {r.status_code} - {r.text[:180]}")
                return None

            try:
                data = r.json()
            except Exception:
                data = {}

            flow_state = self._state_from_payload(
                data,
                current_url=str(r.url) or request_url,
            )
            if self._state_is_add_phone(flow_state):
                try:
                    raw_text = r.text or ""
                except Exception:
                    raw_text = ""
                try:
                    raw_json = json.dumps(data, ensure_ascii=False)
                except Exception:
                    raw_json = ""
                if raw_text:
                    self._log("add_phone 触发响应体(raw): " + raw_text)
                if raw_json and raw_json != raw_text:
                    self._log("add_phone 触发响应体(json): " + raw_json)
            self._log(f"about_you 提交成功 {describe_flow_state(flow_state)}")
            return flow_state
        except Exception as e:
            self._set_error(f"about_you 提交异常: {e}")
            return None

    def _recreate_session(self):
        """重新创建会话容器。"""
        self.session = curl_requests.Session()
        if self.proxy:
            self.session.proxies = build_requests_proxy_config(self.proxy)

    def _capture_prepared_oauth_context(
        self,
        state: FlowState,
        *,
        code_verifier: str,
        oauth_state: str,
        authorize_url: str,
        authorize_params: dict,
        referer: str,
        user_agent: str,
        sec_ch_ua: str,
        impersonate: str,
    ):
        """保存当前独立 OAuth 登录事务，供后续手机验证直接续接。"""
        if not self._state_can_resume_authenticated_flow(state):
            return None

        from .oauth_resume_cache import OAuthResumeContext

        try:
            accept_language = str(
                self.session.headers.get("Accept-Language") or ""
            ).strip()
        except Exception:
            accept_language = ""

        context = OAuthResumeContext(
            session=self.session,
            device_id=str(self.device_id or "").strip(),
            user_agent=str(user_agent or "").strip(),
            sec_ch_ua=str(sec_ch_ua or "").strip(),
            accept_language=accept_language,
            impersonate=str(impersonate or "").strip(),
            code_verifier=str(code_verifier or "").strip(),
            oauth_state=str(oauth_state or "").strip(),
            authorize_url=str(authorize_url or "").strip(),
            authorize_params=dict(authorize_params or {}),
            flow_state=state,
            referer=str(
                state.current_url
                or state.continue_url
                or referer
                or f"{self.oauth_issuer}/log-in"
            ).strip(),
            expires_at=time.monotonic() + 1800,
        )
        self.last_prepared_oauth_context = context
        return context

    def login_and_get_tokens(
        self,
        email,
        password,
        device_id,
        user_agent=None,
        sec_ch_ua=None,
        impersonate=None,
        skymail_client=None,
        prefer_passwordless_login=False,
        allow_phone_verification=True,
        force_new_browser=False,
        resume_authenticated_session=False,
        force_password_login=False,
        totp_secret="",
        mfa_recovery_code="",
        on_mfa_totp_staged=None,
        on_mfa_totp_activated=None,
        on_mfa_recovery_code=None,
        password_reset_required=False,
        on_password_reset=None,
        force_chatgpt_entry=False,
        screen_hint="login",
        complete_about_you_if_needed=False,
        first_name="",
        last_name="",
        birthdate="",
        login_source="",
        stop_after_login=False,
        stop_after_password_reset=False,
        prepared_oauth_context=None,
        _continue_depth=0,
        _password_reset_depth=0,
    ):
        """
        完整的 OAuth 登录流程，获取 tokens

        Args:
            email: 邮箱
            password: 密码
            device_id: 设备 ID
            user_agent: User-Agent
            sec_ch_ua: sec-ch-ua header
            impersonate: curl_cffi impersonate 参数
            skymail_client: Skymail 客户端（用于获取 OTP，如果需要）
            prefer_passwordless_login: 是否强制走 passwordless OTP 链路
            allow_phone_verification: add_phone 后是否允许进入手机号验证码分支
            resume_authenticated_session: 已进入认证后状态时跳过再次提交邮箱
            force_password_login: 即使 prefer_passwordless_login=true，也强制走密码登录
            totp_secret: ChatGPT TOTP MFA 秘钥；仅在服务端要求 MFA 时使用
            mfa_recovery_code: 项目保存的 MFA 恢复码；仅在 TOTP 被拒绝时使用
            on_mfa_totp_staged: 强制重新绑定时，新 TOTP 激活前的安全暂存回调
            on_mfa_totp_activated: 新 TOTP 激活后的回调
            on_mfa_recovery_code: 新恢复码生成后的保存回调
            force_chatgpt_entry: 在 OAuth 前先走 ChatGPT 首页 -> CSRF -> signin/openai
            complete_about_you_if_needed: 命中 about_you 后是否自动提交资料完成注册
            screen_hint: authorize/continue 的 screen_hint（login/signup）
            first_name: about_you 名字
            last_name: about_you 姓氏
            birthdate: about_you 生日，格式 YYYY-MM-DD
            login_source: 当前登录场景，仅用于日志

        Returns:
            dict: tokens 字典，包含 access_token, refresh_token, id_token
        """
        self.last_error = ""
        self.last_workspace_id = ""
        self.last_state = FlowState()
        self.last_prepared_oauth_context = None
        self.last_mfa_enrollment = {}
        self._log(
            "开始 OAuth 登录流程..."
            + (f" (source={login_source})" if login_source else "")
        )
        self._log(
            "OAuth 策略: "
            f"prefer_passwordless_login={'on' if prefer_passwordless_login else 'off'}, "
            f"allow_phone_verification={'on' if allow_phone_verification else 'off'}, "
            f"complete_about_you_if_needed={'on' if complete_about_you_if_needed else 'off'}, "
            f"force_new_browser={'on' if force_new_browser else 'off'}, "
            f"force_password_login={'on' if force_password_login else 'off'}, "
            f"totp_mfa={'on' if str(totp_secret or '').strip() else 'off'}, "
            f"mfa_recovery={'on' if str(mfa_recovery_code or '').strip() else 'off'}, "
            f"force_chatgpt_entry={'on' if force_chatgpt_entry else 'off'}, "
            f"screen_hint={screen_hint or 'login'}, "
            f"stop_after_login={'on' if stop_after_login else 'off'}, "
            "stop_after_password_reset="
            f"{'on' if stop_after_password_reset else 'off'}"
        )

        if force_new_browser:
            self._log("force_new_browser: 重新创建 OAuth 会话容器")
            self._recreate_session()
            device_id = str(uuid.uuid4())
            self._log(f"force_new_browser: 新 device_id={device_id}")
        else:
            if not device_id:
                device_id = str(uuid.uuid4())
                self._log(f"OAuth device_id 缺失，已生成新的 device_id={device_id}")
        self.device_id = str(device_id or "").strip()

        user_agent, sec_ch_ua, impersonate = self._ensure_oauth_fingerprint(
            user_agent, sec_ch_ua, impersonate
        )

        state = None
        continue_referer = ""
        if prepared_oauth_context is not None:
            code_verifier = str(
                getattr(prepared_oauth_context, "code_verifier", "") or ""
            ).strip()
            oauth_state = str(
                getattr(prepared_oauth_context, "oauth_state", "") or ""
            ).strip()
            authorize_url = str(
                getattr(prepared_oauth_context, "authorize_url", "") or ""
            ).strip() or f"{self.oauth_issuer}/oauth/authorize"
            authorize_params = dict(
                getattr(prepared_oauth_context, "authorize_params", {}) or {}
            )
            state = getattr(prepared_oauth_context, "flow_state", None)
            continue_referer = str(
                getattr(prepared_oauth_context, "referer", "") or ""
            ).strip()
            if not code_verifier or not oauth_state or not isinstance(state, FlowState):
                self._set_error(
                    "手机验证授权事务不完整，请重新执行一次邮箱登录；"
                    "本次未获取手机号、未发送短信"
                )
                return None
            if not self._state_can_resume_authenticated_flow(state):
                self._set_error(
                    "手机验证授权事务已失效，请重新执行一次邮箱登录；"
                    "本次未获取手机号、未发送短信"
                )
                return None
            self._log(
                "步骤1: 直接续接邮箱登录时预建的手机 OAuth 事务，"
                "不重新访问 /oauth/authorize，不重新提交邮箱"
            )
        else:
            code_verifier, code_challenge = generate_pkce()
            oauth_state = secrets.token_urlsafe(32)
            authorize_params = {
                "response_type": "code",
                "client_id": self.oauth_client_id,
                "redirect_uri": self.oauth_redirect_uri,
                "scope": "openid profile email offline_access",
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "state": oauth_state,
            }
            authorize_url = f"{self.oauth_issuer}/oauth/authorize"

            seed_oai_device_cookie(self.session, device_id)

            max_entry_attempts = 6
            entry_error = ""
            for entry_attempt in range(max_entry_attempts):
                if entry_attempt > 0:
                    self._log(
                        f"OAuth 登录入口重试 {entry_attempt + 1}/{max_entry_attempts}："
                        "新 Session + 新设备指纹"
                    )
                    self._recreate_session()
                    device_id = str(uuid.uuid4())
                    self.device_id = device_id
                    user_agent, sec_ch_ua, impersonate = self._ensure_oauth_fingerprint(
                        None,
                        None,
                        None,
                    )

                if force_chatgpt_entry:
                    self._log("force_chatgpt_entry: 启动 ChatGPT 首页链路（不影响 OAuth PKCE）")
                    _ = self._bootstrap_chatgpt_entry(
                        email,
                        device_id,
                        user_agent=user_agent,
                        sec_ch_ua=sec_ch_ua,
                        impersonate=impersonate,
                    )

                self.last_error = ""
                self._log("步骤1: Bootstrap OAuth session...")
                authorize_final_url = self._bootstrap_oauth_session(
                    authorize_url,
                    authorize_params,
                    device_id=device_id,
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    impersonate=impersonate,
                )
                if not authorize_final_url:
                    if resume_authenticated_session:
                        self._set_error(
                            "OpenAI 登录会话已失效，请先重新执行邮箱登录；"
                            "本次未获取手机号、未发送短信"
                        )
                        return None
                    entry_error = "OpenAI OAuth bootstrap 未建立有效 login_session"
                    continue

                continue_referer = (
                    authorize_final_url
                    if authorize_final_url.startswith(self.oauth_issuer)
                    else f"{self.oauth_issuer}/log-in"
                )
                resumed_state = self._state_from_url(authorize_final_url)
                if resume_authenticated_session:
                    if self._state_can_resume_authenticated_flow(resumed_state):
                        self._log(
                            "已复用前序认证会话，跳过再次提交邮箱: "
                            f"{describe_flow_state(resumed_state)}"
                        )
                        state = resumed_state
                        break
                    self._set_error(
                        "OpenAI 登录会话已失效，请先重新执行邮箱登录；"
                        "本次未获取手机号、未发送短信"
                    )
                    return None
                state = self._submit_authorize_continue(
                    email,
                    device_id,
                    continue_referer,
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    impersonate=impersonate,
                    authorize_url=authorize_url,
                    authorize_params=authorize_params,
                    screen_hint=str(screen_hint or "login"),
                )
                if state:
                    break

                entry_error = str(self.last_error or "").strip()
                if not self._is_transient_oauth_entry_error(entry_error):
                    if not self.last_error:
                        self._set_error("提交邮箱后未进入有效的 OAuth 状态")
                    return None
                self._log("OAuth 登录入口被验证页拦截，准备更换会话重试")

            if not state:
                self._set_error(
                    "OpenAI 登录入口触发 403 验证页，自动重试后仍未建立有效会话"
                )
                return None

        self._log(f"OAuth 状态起点: {describe_flow_state(state)}")
        seen_states = {}
        referer = continue_referer

        def _should_stop_after_login(state_to_check: FlowState):
            if not stop_after_login:
                return False
            if self._state_is_login_password(state_to_check):
                return False
            if self._state_is_email_otp(state_to_check):
                return False
            if self._state_is_mfa_challenge(state_to_check):
                return False
            if self._state_is_mfa_enroll(state_to_check):
                return False
            if self._state_is_create_account_password(state_to_check):
                return False
            prepared_context = self._capture_prepared_oauth_context(
                state_to_check,
                code_verifier=code_verifier,
                oauth_state=oauth_state,
                authorize_url=authorize_url,
                authorize_params=authorize_params,
                referer=referer,
                user_agent=user_agent,
                sec_ch_ua=sec_ch_ua,
                impersonate=impersonate,
            )
            return prepared_context is not None

        for step in range(20):
            self.last_state = state
            self._log(f"状态步进[{step + 1}/20]: {describe_flow_state(state)}")
            signature = self._state_signature(state)
            seen_states[signature] = seen_states.get(signature, 0) + 1
            if seen_states[signature] > 2:
                self._set_error(f"OAuth 状态卡住: {describe_flow_state(state)}")
                return None

            code = self._extract_code_from_state(state)
            if code:
                self._log(f"获取到 authorization code: {code[:20]}...")
                self._log("步骤7: POST /oauth/token")
                tokens = self._exchange_code_for_tokens(
                    code, code_verifier, user_agent, impersonate
                )
                if tokens:
                    self._log("[OK] OAuth 登录成功")
                else:
                    self._log("换取 tokens 失败")
                return tokens

            if password_reset_required and self._state_is_login_password(state):
                if int(_password_reset_depth or 0) >= 1:
                    self._set_error("密码重置后仍返回重置入口，已停止循环")
                    return None
                reset_state = self._complete_password_reset(
                    state,
                    email=email,
                    new_password=password,
                    skymail_client=skymail_client,
                    device_id=device_id,
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    impersonate=impersonate,
                    on_password_reset=on_password_reset,
                )
                if not reset_state:
                    return None
                if stop_after_password_reset:
                    self.last_state = reset_state
                    self._set_error("密码重置完成，按要求停止")
                    return None
                return self.login_and_get_tokens(
                    email,
                    password,
                    device_id="",
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    impersonate=impersonate,
                    skymail_client=skymail_client,
                    prefer_passwordless_login=False,
                    allow_phone_verification=allow_phone_verification,
                    force_new_browser=True,
                    resume_authenticated_session=False,
                    force_password_login=True,
                    totp_secret=totp_secret,
                    mfa_recovery_code=mfa_recovery_code,
                    on_mfa_totp_staged=on_mfa_totp_staged,
                    on_mfa_totp_activated=on_mfa_totp_activated,
                    on_mfa_recovery_code=on_mfa_recovery_code,
                    password_reset_required=False,
                    on_password_reset=on_password_reset,
                    force_chatgpt_entry=force_chatgpt_entry,
                    screen_hint=screen_hint,
                    complete_about_you_if_needed=complete_about_you_if_needed,
                    first_name=first_name,
                    last_name=last_name,
                    birthdate=birthdate,
                    login_source=login_source,
                    stop_after_login=stop_after_login,
                    stop_after_password_reset=False,
                    prepared_oauth_context=None,
                    _continue_depth=0,
                    _password_reset_depth=int(_password_reset_depth or 0) + 1,
                )

            if self._state_is_google_federated(state):
                if not str(password or ""):
                    self._set_error("Google 联邦登录缺少邮箱密码")
                    return None
                next_state = self._complete_google_federated_login(
                    state,
                    email=email,
                    password=password,
                    user_agent=user_agent,
                )
                if not next_state:
                    return None
                referer = state.current_url or state.continue_url or referer
                state = next_state
                continue

            if prefer_passwordless_login and (not force_password_login) and self._state_is_login_password(state):
                next_state = self._send_passwordless_login_otp(
                    email,
                    device_id,
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    impersonate=impersonate,
                    referer=state.current_url or state.continue_url or referer,
                )
                if not next_state:
                    if not self.last_error:
                        self._set_error("passwordless OTP 触发后未进入邮箱验证码状态")
                    return None
                referer = state.current_url or referer
                state = next_state
                continue

            if self._state_is_create_account_password(state) and force_password_login:
                self._log("命中 create_account_password，按强制密码登录路径继续")
                next_state = self._submit_password_verify(
                    password,
                    device_id,
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    impersonate=impersonate,
                    referer=state.current_url or state.continue_url or f"{self.oauth_issuer}/log-in/password",
                )
                if not next_state:
                    if not self.last_error:
                        self._set_error("密码验证后未进入下一步 OAuth 状态")
                    return None
                if _should_stop_after_login(next_state):
                    self._log(
                        "登录链路已完成（密码验证后进入下一状态），按要求停止"
                    )
                    self.last_state = next_state
                    self._set_error("登录链路已完成，按要求停止")
                    return None
                referer = state.current_url or referer
                state = next_state
                continue

            if self._state_is_login_password(state):
                next_state = self._submit_password_verify(
                    password,
                    device_id,
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    impersonate=impersonate,
                    referer=state.current_url or state.continue_url or referer,
                )
                if not next_state:
                    if not self.last_error:
                        self._set_error("密码验证后未进入下一步 OAuth 状态")
                    return None
                if _should_stop_after_login(next_state):
                    self._log(
                        "登录链路已完成（密码验证后进入下一状态），按要求停止"
                    )
                    self.last_state = next_state
                    self._set_error("登录链路已完成，按要求停止")
                    return None
                referer = state.current_url or referer
                state = next_state
                continue

            if self._state_is_mfa_challenge(state):
                next_state = self._submit_mfa_challenge(
                    state,
                    email=email,
                    skymail_client=skymail_client,
                    totp_secret=str(totp_secret or ""),
                    mfa_recovery_code=str(mfa_recovery_code or ""),
                    device_id=device_id,
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    impersonate=impersonate,
                )
                if not next_state:
                    if not self.last_error:
                        self._set_error("ChatGPT MFA 验证后未进入下一步 OAuth 状态")
                    return None
                if _should_stop_after_login(next_state):
                    self._log(
                        "登录链路已完成（MFA 验证后进入下一状态），按要求停止"
                    )
                    self.last_state = next_state
                    self._set_error("登录链路已完成，按要求停止")
                    return None
                referer = state.current_url or state.continue_url or referer
                state = next_state
                continue

            if self._state_is_mfa_enroll(state):
                next_state = self._submit_mfa_enrollment(
                    state,
                    device_id=device_id,
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    impersonate=impersonate,
                    on_totp_staged=on_mfa_totp_staged,
                    on_totp_activated=on_mfa_totp_activated,
                    on_recovery_code=on_mfa_recovery_code,
                )
                if not next_state:
                    if not self.last_error:
                        self._set_error("ChatGPT MFA 绑定后未进入下一步 OAuth 状态")
                    return None
                referer = state.current_url or state.continue_url or referer
                state = next_state
                continue

            if (
                prefer_passwordless_login
                and self._state_is_add_phone(state)
                and self._state_requires_navigation(state)
            ):
                self._log("步骤5: OTP 后命中 add_phone，先实际访问 continue_url 争取重签 workspace Cookie")
                code, next_state = self._follow_flow_state(
                    state,
                    referer=referer,
                    user_agent=user_agent,
                    impersonate=impersonate,
                )
                if code:
                    self._log(f"获取到 authorization code: {code[:20]}...")
                    self._log("步骤7: POST /oauth/token")
                    tokens = self._exchange_code_for_tokens(
                        code, code_verifier, user_agent, impersonate
                    )
                    if tokens:
                        self._log("[OK] OAuth 登录成功")
                    else:
                        self._log("换取 tokens 失败")
                    return tokens
                referer = state.current_url or referer
                state = next_state
                continue

            if self._state_is_email_otp(state):
                if not skymail_client:
                    self._set_error("当前流程需要邮箱 OTP，但缺少接码客户端")
                    return None
                next_state = self._handle_otp_verification(
                    email,
                    device_id,
                    user_agent,
                    sec_ch_ua,
                    impersonate,
                    skymail_client,
                    state,
                    prefer_passwordless_login=prefer_passwordless_login,
                    allow_cached_code_retry=_continue_depth > 0,
                )
                if not next_state:
                    if not self.last_error:
                        self._set_error("邮箱 OTP 验证后未进入下一步 OAuth 状态")
                    return None
                if _should_stop_after_login(next_state):
                    self._log(
                        "登录链路已完成（OTP 验证后进入下一状态），按要求停止"
                    )
                    self.last_state = next_state
                    self._set_error("登录链路已完成，按要求停止")
                    return None
                referer = state.current_url or referer
                state = next_state
                continue

            if complete_about_you_if_needed and self._state_is_about_you(state):
                self._log("步骤5: 命中 about_you，执行 interrupt 新链路的资料补全提交")
                next_state = self._submit_about_you_create_account(
                    first_name,
                    last_name,
                    birthdate,
                    device_id,
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    impersonate=impersonate,
                    referer=state.current_url or state.continue_url or referer,
                )
                if not next_state:
                    if not self.last_error:
                        self._set_error("about_you 提交后未进入下一步 OAuth 状态")
                    return None
                referer = state.current_url or referer
                state = next_state
                continue

            if self._state_is_add_phone(state):
                try:
                    raw_dump = json.dumps(state.raw or {}, ensure_ascii=False)
                except Exception:
                    raw_dump = ""
                if raw_dump:
                    self._log(f"add_phone 状态响应体(raw): {raw_dump}")
                if not allow_phone_verification:
                    if self._state_supports_workspace_resolution(state):
                        self._log(
                            "步骤5: add_phone 命中，但检测到 workspace 线索，继续尝试 workspace/org 选择"
                        )
                    else:
                        self._log(
                            "步骤5: add_phone 暂无显式 workspace 线索，先尝试 canonical consent URL 抢救"
                        )
                    code, next_state = self._oauth_submit_workspace_and_org(
                        f"{self.oauth_issuer}/sign-in-with-chatgpt/codex/consent",
                        device_id,
                        user_agent,
                        impersonate,
                    )
                    if code:
                        self._log(f"获取到 authorization code: {code[:20]}...")
                        self._log("步骤7: POST /oauth/token")
                        tokens = self._exchange_code_for_tokens(
                            code, code_verifier, user_agent, impersonate
                        )
                        if tokens:
                            self._log("[OK] OAuth 登录成功")
                        else:
                            self._log("换取 tokens 失败")
                        return tokens
                    if next_state:
                        referer = state.current_url or referer
                        state = next_state
                        self._log(f"add_phone -> workspace state -> {describe_flow_state(state)}")
                        continue

                    workspace_error = str(self.last_error or "").strip()
                    self._set_error(
                        "passwordless 登录后仍停留在 add_phone，未获取到 workspace / callback；"
                        "为避免重复发送邮箱验证码，本次不重启登录"
                        + (f" ({workspace_error})" if workspace_error else "")
                    )
                    return None
                else:
                    next_state = self._handle_add_phone_verification(
                        device_id,
                        user_agent,
                        sec_ch_ua,
                        impersonate,
                        state,
                    )
                    if not next_state:
                        if not self.last_error:
                            self._set_error("手机号验证后未进入下一步 OAuth 状态")
                        return None
                    referer = state.current_url or referer
                    state = next_state
                    continue

            if self._state_requires_navigation(state):
                code, next_state = self._follow_flow_state(
                    state,
                    referer=referer,
                    user_agent=user_agent,
                    impersonate=impersonate,
                )
                if code:
                    self._log(f"获取到 authorization code: {code[:20]}...")
                    self._log("步骤7: POST /oauth/token")
                    tokens = self._exchange_code_for_tokens(
                        code, code_verifier, user_agent, impersonate
                    )
                    if tokens:
                        self._log("[OK] OAuth 登录成功")
                    else:
                        self._log("换取 tokens 失败")
                    return tokens
                referer = state.current_url or referer
                state = next_state
                self._log(f"follow state -> {describe_flow_state(state)}")
                continue

            if self._state_supports_workspace_resolution(state):
                self._log("步骤6: 执行 workspace/org 选择")
                consent_entry = (
                    state.continue_url
                    or state.current_url
                    or f"{self.oauth_issuer}/sign-in-with-chatgpt/codex/consent"
                )
                if self._state_is_add_phone(state):
                    consent_entry = (
                        f"{self.oauth_issuer}/sign-in-with-chatgpt/codex/consent"
                    )
                    self._log("步骤6: 当前处于 add_phone，改用 canonical consent URL 继续")
                code, next_state = self._oauth_submit_workspace_and_org(
                    consent_entry,
                    device_id,
                    user_agent,
                    impersonate,
                )
                if code:
                    self._log(f"获取到 authorization code: {code[:20]}...")
                    self._log("步骤7: POST /oauth/token")
                    tokens = self._exchange_code_for_tokens(
                        code, code_verifier, user_agent, impersonate
                    )
                    if tokens:
                        self._log("[OK] OAuth 登录成功")
                    else:
                        self._log("换取 tokens 失败")
                    return tokens
                if next_state:
                    referer = state.current_url or referer
                    state = next_state
                    self._log(f"workspace state -> {describe_flow_state(state)}")
                    continue

                if not self.last_error:
                    self._set_error(
                        f"workspace/org 选择失败: {describe_flow_state(state)}"
                    )
                return None

            self._set_error(f"未支持的 OAuth 状态: {describe_flow_state(state)}")
            return None

        self._set_error("OAuth 状态机超出最大步数")
        return None

    def _extract_code_from_url(self, url):
        """从 URL 中提取 code"""
        if not url or "code=" not in url:
            return None
        try:
            return parse_qs(urlparse(url).query).get("code", [None])[0]
        except Exception:
            return None

    def _oauth_follow_for_code(
        self, start_url, referer, user_agent, impersonate, max_hops=16
    ):
        """跟随 URL 获取 authorization code（手动跟随重定向）"""
        code, next_state = self._follow_flow_state(
            self._state_from_url(start_url),
            referer=referer,
            user_agent=user_agent,
            impersonate=impersonate,
            max_hops=max_hops,
        )
        return code, (next_state.current_url or next_state.continue_url or start_url)

    def _oauth_submit_workspace_and_org(
        self, consent_url, device_id, user_agent, impersonate, max_retries=3
    ):
        """提交 workspace 和 organization 选择（带重试）"""
        self._enter_stage("workspace_select", consent_url[:120] if consent_url else "")
        session_data = None

        for attempt in range(max_retries):
            session_data = self._load_workspace_session_data(
                consent_url=consent_url,
                user_agent=user_agent,
                impersonate=impersonate,
            )
            if session_data:
                break

            if attempt < max_retries - 1:
                self._log(
                    f"无法获取 consent session 数据 (尝试 {attempt + 1}/{max_retries})"
                )
                time.sleep(0.3)
            else:
                self._set_error("无法获取 consent session 数据")
                return None, None

        workspaces = session_data.get("workspaces", [])
        if not workspaces:
            self._set_error("session 中没有 workspace 信息")
            return None, None

        workspace_id = (workspaces[0] or {}).get("id")
        if not workspace_id:
            self._set_error("workspace_id 为空")
            return None, None

        self.last_workspace_id = str(workspace_id).strip()
        self._log(f"选择 workspace: {workspace_id}")

        headers = self._headers(
            f"{self.oauth_issuer}/api/accounts/workspace/select",
            user_agent=user_agent,
            accept="application/json",
            referer=consent_url,
            origin=self.oauth_issuer,
            content_type="application/json",
            fetch_site="same-origin",
            extra_headers={
                "oai-device-id": device_id,
            },
        )
        headers.update(generate_datadog_trace())

        try:
            kwargs = {
                "json": {"workspace_id": workspace_id},
                "headers": headers,
                "allow_redirects": False,
                "timeout": 30,
            }
            if impersonate:
                kwargs["impersonate"] = impersonate

            self._browser_pause()
            r = self.session.post(
                f"{self.oauth_issuer}/api/accounts/workspace/select", **kwargs
            )

            self._log(f"workspace/select -> {r.status_code}")
            self._log(
                f"workspace/select 请求: workspace_id={workspace_id} consent_url={consent_url[:120]}"
            )

            # 检查重定向
            if r.status_code in (301, 302, 303, 307, 308):
                location = normalize_flow_url(
                    r.headers.get("Location", ""), auth_base=self.oauth_issuer
                )
                if "code=" in location:
                    code = self._extract_code_from_url(location)
                    if code:
                        self._log("从 workspace/select 重定向获取到 code")
                        return code, self._state_from_url(location)
                if location:
                    return None, self._state_from_url(location)

            # 如果返回 200，检查响应中的 orgs
            if r.status_code == 200:
                try:
                    data = r.json()
                    orgs = data.get("data", {}).get("orgs", [])
                    workspace_state = self._state_from_payload(
                        data, current_url=str(r.url)
                    )
                    continue_url = workspace_state.continue_url

                    if orgs:
                        org_id = (orgs[0] or {}).get("id")
                        projects = (orgs[0] or {}).get("projects", [])
                        project_id = (projects[0] or {}).get("id") if projects else None

                        if org_id:
                            self._log(f"选择 organization: {org_id}")

                            org_body = {"org_id": org_id}
                            if project_id:
                                org_body["project_id"] = project_id

                            org_referer = (
                                continue_url
                                if continue_url and continue_url.startswith("http")
                                else consent_url
                            )
                            headers = self._headers(
                                f"{self.oauth_issuer}/api/accounts/organization/select",
                                user_agent=user_agent,
                                accept="application/json",
                                referer=org_referer,
                                origin=self.oauth_issuer,
                                content_type="application/json",
                                fetch_site="same-origin",
                                extra_headers={
                                    "oai-device-id": device_id,
                                },
                            )
                            headers.update(generate_datadog_trace())

                            kwargs = {
                                "json": org_body,
                                "headers": headers,
                                "allow_redirects": False,
                                "timeout": 30,
                            }
                            if impersonate:
                                kwargs["impersonate"] = impersonate

                            self._browser_pause()
                            r_org = self.session.post(
                                f"{self.oauth_issuer}/api/accounts/organization/select",
                                **kwargs,
                            )

                            self._log(f"organization/select -> {r_org.status_code}")
                            self._log(
                                f"organization/select 请求: org_id={org_id} project_id={project_id or '-'}"
                            )

                            # 检查重定向
                            if r_org.status_code in (301, 302, 303, 307, 308):
                                location = normalize_flow_url(
                                    r_org.headers.get("Location", ""),
                                    auth_base=self.oauth_issuer,
                                )
                                if "code=" in location:
                                    code = self._extract_code_from_url(location)
                                    if code:
                                        self._log(
                                            "从 organization/select 重定向获取到 code"
                                        )
                                        return code, self._state_from_url(location)
                                if location:
                                    return None, self._state_from_url(location)

                            # 检查 continue_url
                            if r_org.status_code == 200:
                                try:
                                    org_state = self._state_from_payload(
                                        r_org.json(), current_url=str(r_org.url)
                                    )
                                    self._log(
                                        f"organization/select -> {describe_flow_state(org_state)}"
                                    )
                                    if self._extract_code_from_state(org_state):
                                        return self._extract_code_from_state(
                                            org_state
                                        ), org_state
                                    return None, org_state
                                except Exception as e:
                                    self._set_error(
                                        f"解析 organization/select 响应异常: {e}"
                                    )

                    # 如果有 continue_url，跟随它
                    if continue_url:
                        code, _ = self._oauth_follow_for_code(
                            continue_url, consent_url, user_agent, impersonate
                        )
                        if code:
                            return code, self._state_from_url(continue_url)
                    return None, workspace_state

                except Exception as e:
                    self._set_error(f"处理 workspace/select 响应异常: {e}")
                    return None, None

        except Exception as e:
            self._set_error(f"workspace/select 异常: {e}")
            return None, None

        return None, None

    def _load_workspace_session_data(self, consent_url, user_agent, impersonate):
        """优先从 cookie 解码 session，失败时回退到 consent HTML 中提取 workspace 数据。"""
        session_data = self._decode_oauth_session_cookie()
        if session_data and session_data.get("workspaces"):
            return session_data

        html = self._fetch_consent_page_html(consent_url, user_agent, impersonate)
        if not html:
            return session_data

        parsed = self._extract_session_data_from_consent_html(html)
        if parsed and parsed.get("workspaces"):
            self._log(
                f"从 consent HTML 提取到 {len(parsed.get('workspaces', []))} 个 workspace"
            )
            return parsed

        return session_data

    def _fetch_consent_page_html(self, consent_url, user_agent, impersonate):
        """获取 consent 页 HTML，用于解析 React Router stream 中的 session 数据。"""
        try:
            headers = self._headers(
                consent_url,
                user_agent=user_agent,
                accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                referer=f"{self.oauth_issuer}/email-verification",
                navigation=True,
            )
            kwargs = {"headers": headers, "allow_redirects": False, "timeout": 30}
            if impersonate:
                kwargs["impersonate"] = impersonate
            self._browser_pause(0.12, 0.3)
            r = self.session.get(consent_url, **kwargs)
            if r.status_code == 200 and "text/html" in (
                r.headers.get("content-type", "").lower()
            ):
                return r.text
        except Exception:
            pass
        return ""

    def _extract_session_data_from_consent_html(self, html):
        """从 consent HTML 的 React Router stream 中提取 workspace session 数据。"""
        import json
        import re

        if not html or "workspaces" not in html:
            return None

        def _first_match(patterns, text):
            for pattern in patterns:
                m = re.search(pattern, text, re.S)
                if m:
                    return m.group(1)
            return ""

        def _build_from_text(text):
            if not text or "workspaces" not in text:
                return None

            normalized = text.replace('\\"', '"')

            session_id = _first_match(
                [
                    r'"session_id","([^"]+)"',
                    r'"session_id":"([^"]+)"',
                ],
                normalized,
            )
            client_id = _first_match(
                [
                    r'"openai_client_id","([^"]+)"',
                    r'"openai_client_id":"([^"]+)"',
                ],
                normalized,
            )

            start = normalized.find('"workspaces"')
            if start < 0:
                start = normalized.find("workspaces")
            if start < 0:
                return None

            end = normalized.find('"openai_client_id"', start)
            if end < 0:
                end = normalized.find("openai_client_id", start)
            if end < 0:
                end = min(len(normalized), start + 4000)
            else:
                end = min(len(normalized), end + 600)

            workspace_chunk = normalized[start:end]
            ids = re.findall(r'"id"(?:,|:)"([0-9a-fA-F-]{36})"', workspace_chunk)
            if not ids:
                return None

            kinds = re.findall(r'"kind"(?:,|:)"([^"]+)"', workspace_chunk)
            workspaces = []
            seen = set()
            for idx, wid in enumerate(ids):
                if wid in seen:
                    continue
                seen.add(wid)
                item = {"id": wid}
                if idx < len(kinds):
                    item["kind"] = kinds[idx]
                workspaces.append(item)

            if not workspaces:
                return None

            return {
                "session_id": session_id,
                "openai_client_id": client_id,
                "workspaces": workspaces,
            }

        candidates = [html]

        for quoted in re.findall(
            r'streamController\.enqueue\(("(?:\\.|[^"\\])*")\)',
            html,
            re.S,
        ):
            try:
                decoded = json.loads(quoted)
            except Exception:
                continue
            if decoded:
                candidates.append(decoded)

        if '\\"' in html:
            candidates.append(html.replace('\\"', '"'))

        for candidate in candidates:
            parsed = _build_from_text(candidate)
            if parsed and parsed.get("workspaces"):
                return parsed

        return None

    def _decode_oauth_session_cookie(self):
        """解码 oai-client-auth-session cookie"""
        try:
            for cookie in self.session.cookies:
                try:
                    name = cookie.name if hasattr(cookie, "name") else str(cookie)
                    if name == "oai-client-auth-session":
                        value = (
                            cookie.value
                            if hasattr(cookie, "value")
                            else self.session.cookies.get(name)
                        )
                        if value:
                            data = self._decode_cookie_json_value(value)
                            if data:
                                return data
                except Exception:
                    continue
        except Exception:
            pass

        return None

    @staticmethod
    def _decode_cookie_json_value(value):
        import base64
        import json

        raw_value = str(value or "").strip()
        if not raw_value:
            return None

        candidates = [raw_value]
        if "." in raw_value:
            candidates.insert(0, raw_value.split(".", 1)[0])

        for candidate in candidates:
            candidate = candidate.strip()
            if not candidate:
                continue
            padded = candidate + "=" * (-len(candidate) % 4)
            for decoder in (base64.urlsafe_b64decode, base64.b64decode):
                try:
                    decoded = decoder(padded).decode("utf-8")
                    parsed = json.loads(decoded)
                except Exception:
                    continue
                if isinstance(parsed, dict):
                    return parsed

        return None

    def _exchange_code_for_tokens(self, code, code_verifier, user_agent, impersonate):
        """用 authorization code 换取 tokens"""
        self._enter_stage("token_exchange", f"code={str(code or '')[:24]}...")
        url = f"{self.oauth_issuer}/oauth/token"

        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.oauth_redirect_uri,
            "client_id": self.oauth_client_id,
            "code_verifier": code_verifier,
        }

        headers = self._headers(
            url,
            user_agent=user_agent,
            accept="application/json",
            referer=f"{self.oauth_issuer}/sign-in-with-chatgpt/codex/consent",
            origin=self.oauth_issuer,
            content_type="application/x-www-form-urlencoded",
            fetch_site="same-origin",
        )

        try:
            kwargs = {"data": payload, "headers": headers, "timeout": 60}
            if impersonate:
                kwargs["impersonate"] = impersonate

            self._browser_pause()
            r = self.session.post(url, **kwargs)

            if r.status_code == 200:
                self._log("token_exchange 成功")
                return r.json()
            else:
                self._set_error(f"换取 tokens 失败: {r.status_code} - {r.text[:200]}")

        except Exception as e:
            self._set_error(f"换取 tokens 异常: {e}")

        return None

    def _send_phone_number(self, phone, device_id, user_agent, sec_ch_ua, impersonate):
        request_url = f"{self.oauth_issuer}/api/accounts/add-phone/send"
        headers = self._headers(
            request_url,
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
            accept="application/json",
            referer=f"{self.oauth_issuer}/add-phone",
            origin=self.oauth_issuer,
            content_type="application/json",
            fetch_site="same-origin",
            extra_headers={"oai-device-id": device_id},
        )
        headers.update(generate_datadog_trace())

        kwargs = {
            "json": {"phone_number": phone},
            "headers": headers,
            "timeout": 30,
            "allow_redirects": False,
        }
        if impersonate:
            kwargs["impersonate"] = impersonate

        # Keep the browser session, request body and phone stable across a
        # bounded retry window.  The upstream response body and exception text
        # are intentionally never copied into diagnostics or task logs.
        max_attempts = 3
        retryable_statuses = {408, 425, 429, 500, 502, 503, 504}
        self.last_phone_send_diagnostic = {}

        def publish_diagnostic(
            *,
            safe_error_code="",
            http_status=0,
            retry_count=0,
            recovery_status="failed",
        ):
            self._record_phone_provider_diagnostic(
                failure_stage="openai_send",
                safe_error_code=safe_error_code,
                http_status=http_status,
                retry_count=retry_count,
                recovery_status=recovery_status,
            )

        def retry_after_seconds(resp):
            try:
                raw = getattr(resp, "headers", {}) or {}
                value = raw.get("Retry-After")
                if value is None:
                    value = raw.get("retry-after")
                value = float(value)
            except (AttributeError, TypeError, ValueError):
                return None
            if value < 0:
                return None
            return min(value, 60.0)

        def response_strings(value):
            if isinstance(value, str):
                return [value]
            if isinstance(value, dict):
                values = []
                for nested in value.values():
                    values.extend(response_strings(nested))
                return values
            if isinstance(value, (list, tuple)):
                values = []
                for nested in value:
                    values.extend(response_strings(nested))
                return values
            return []

        def explicit_rejection_code(resp):
            try:
                payload = resp.json()
            except Exception:
                return ""
            if not isinstance(payload, (dict, list, tuple)):
                return ""
            combined = " ".join(response_strings(payload)).lower()
            if any(
                marker in combined
                for marker in (
                    "already_use",
                    "already-use",
                    "already in use",
                    "phone_already",
                    "phone number already",
                )
            ):
                return "OPENAI_PHONE_ALREADY_USED"
            if any(
                marker in combined
                for marker in (
                    "similar to yours",
                    "similar_phone",
                    "phone_similar",
                    "similar phone",
                )
            ):
                return "OPENAI_PHONE_SIMILAR_REJECTED"
            if any(
                marker in combined
                for marker in (
                    "unsupported_phone",
                    "phone_unsupported",
                    "unsupported phone",
                    "phone number not supported",
                    "carrier not supported",
                )
            ):
                return "OPENAI_PHONE_UNSUPPORTED"
            if any(
                marker in combined
                for marker in (
                    "invalid_phone",
                    "phone_invalid",
                    "invalid phone",
                    "phone number is invalid",
                    "not a valid mobile number",
                )
            ):
                return "OPENAI_PHONE_INVALID"
            return ""

        for attempt in range(1, max_attempts + 1):
            try:
                self._browser_pause(0.12, 0.25)
                resp = self.session.post(request_url, **kwargs)
            except Exception:
                retry_count = attempt - 1
                if attempt < max_attempts:
                    publish_diagnostic(
                        safe_error_code="OPENAI_SEND_TRANSPORT",
                        retry_count=retry_count + 1,
                        recovery_status="retrying",
                    )
                    self._log(
                        "add-phone/send 网络请求失败，"
                        f"使用同一会话重试 {attempt + 1}/{max_attempts}"
                    )
                    time.sleep(0.25 * (2 ** (attempt - 1)))
                    continue
                publish_diagnostic(
                    safe_error_code="OPENAI_SEND_TRANSPORT",
                    retry_count=retry_count,
                    recovery_status="failed",
                )
                return False, None, "add-phone/send 请求失败: OPENAI_SEND_TRANSPORT"

            try:
                status_code = int(getattr(resp, "status_code", 0) or 0)
            except (TypeError, ValueError):
                status_code = 0
            self.last_http_status = status_code
            self._log(f"/add-phone/send -> {status_code}")

            if status_code != 200:
                rejection_code = ""
                if status_code < 500 and status_code not in {408, 425, 429}:
                    rejection_code = explicit_rejection_code(resp)
                if rejection_code:
                    publish_diagnostic(
                        safe_error_code=rejection_code,
                        http_status=status_code,
                        retry_count=attempt - 1,
                        recovery_status="failed",
                    )
                    return (
                        False,
                        None,
                        f"add-phone/send 被拒绝: {rejection_code} (HTTP {status_code})",
                    )

                if status_code in retryable_statuses and attempt < max_attempts:
                    retry_delay = (
                        retry_after_seconds(resp)
                        if status_code == 429
                        else None
                    )
                    if retry_delay is None:
                        retry_delay = 0.25 * (2 ** (attempt - 1))
                    publish_diagnostic(
                        safe_error_code="OPENAI_SEND_HTTP_RETRY",
                        http_status=status_code,
                        retry_count=attempt,
                        recovery_status="retrying",
                    )
                    self._log(
                        "add-phone/send 暂时失败，"
                        f"使用同一会话重试 {attempt + 1}/{max_attempts}"
                    )
                    time.sleep(float(retry_delay))
                    continue

                safe_code = (
                    "OPENAI_SEND_RETRY_EXHAUSTED"
                    if status_code in retryable_statuses
                    else "OPENAI_SEND_HTTP_ERROR"
                )
                publish_diagnostic(
                    safe_error_code=safe_code,
                    http_status=status_code,
                    retry_count=attempt - 1,
                    recovery_status="failed",
                )
                return (
                    False,
                    None,
                    f"add-phone/send 请求失败: {safe_code} (HTTP {status_code})",
                )

            try:
                data = resp.json()
            except Exception:
                publish_diagnostic(
                    safe_error_code="OPENAI_SEND_INVALID_JSON",
                    http_status=status_code,
                    retry_count=attempt - 1,
                    recovery_status="failed",
                )
                return False, None, "add-phone/send 响应不是有效 JSON"

            if not isinstance(data, (dict, list, tuple)):
                publish_diagnostic(
                    safe_error_code="OPENAI_SEND_INVALID_PAYLOAD",
                    http_status=status_code,
                    retry_count=attempt - 1,
                    recovery_status="failed",
                )
                return False, None, "add-phone/send 响应格式无效"

            try:
                next_state = self._state_from_payload(
                    data,
                    current_url=str(getattr(resp, "url", "") or request_url),
                )
            except Exception:
                publish_diagnostic(
                    safe_error_code="OPENAI_SEND_INVALID_PAYLOAD",
                    http_status=status_code,
                    retry_count=attempt - 1,
                    recovery_status="failed",
                )
                return False, None, "add-phone/send 未返回有效状态"
            publish_diagnostic(
                http_status=status_code,
                retry_count=attempt - 1,
                recovery_status="reconciled" if attempt > 1 else "captured",
            )
            self._log(f"add-phone/send {describe_flow_state(next_state)}")
            return True, next_state, ""

        publish_diagnostic(
            safe_error_code="OPENAI_SEND_RETRY_EXHAUSTED",
            recovery_status="failed",
        )
        return False, None, "add-phone/send 请求失败: OPENAI_SEND_RETRY_EXHAUSTED"

    def _resend_phone_otp(
        self,
        phone_number,
        device_id,
        user_agent,
        sec_ch_ua,
        impersonate,
        state: FlowState,
    ):
        request_url = f"{self.oauth_issuer}/api/accounts/add-phone/send"
        headers = self._headers(
            request_url,
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
            accept="application/json",
            referer=state.current_url
            or state.continue_url
            or f"{self.oauth_issuer}/add-phone",
            origin=self.oauth_issuer,
            content_type="application/json",
            fetch_site="same-origin",
            extra_headers={"oai-device-id": device_id},
        )
        headers.update(generate_datadog_trace())

        kwargs = {
            "json": {"phone_number": phone_number},
            "headers": headers,
            "timeout": 30,
            "allow_redirects": False,
        }
        if impersonate:
            kwargs["impersonate"] = impersonate

        retryable_statuses = {408, 425, 429, 500, 502, 503, 504}
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                self._browser_pause(0.12, 0.25)
                resp = self.session.post(request_url, **kwargs)
            except Exception:
                if attempt < max_attempts:
                    self._record_phone_provider_diagnostic(
                        failure_stage="openai_send",
                        safe_error_code="OPENAI_SEND_TRANSPORT",
                        retry_count=attempt,
                        recovery_status="retrying",
                    )
                    time.sleep(0.25 * (2 ** (attempt - 1)))
                    continue
                self._record_phone_provider_diagnostic(
                    failure_stage="openai_send",
                    safe_error_code="OPENAI_SEND_TRANSPORT",
                    retry_count=attempt - 1,
                    recovery_status="failed",
                )
                return False, "add-phone/send 重发失败: OPENAI_SEND_TRANSPORT"

            try:
                status_code = int(getattr(resp, "status_code", 0) or 0)
            except (TypeError, ValueError):
                status_code = 0
            self.last_http_status = status_code
            self._log(f"/add-phone/send(resend) -> {status_code}")
            if status_code == 200:
                self._record_phone_provider_diagnostic(
                    failure_stage="openai_send",
                    http_status=status_code,
                    retry_count=attempt - 1,
                    recovery_status=(
                        "reconciled" if attempt > 1 else "captured"
                    ),
                )
                return True, ""
            if status_code in retryable_statuses and attempt < max_attempts:
                retry_delay = None
                if status_code == 429:
                    try:
                        retry_delay = float(
                            (getattr(resp, "headers", {}) or {}).get(
                                "Retry-After"
                            )
                        )
                    except (AttributeError, TypeError, ValueError):
                        retry_delay = None
                    if retry_delay is not None:
                        retry_delay = min(max(0.0, retry_delay), 60.0)
                if retry_delay is None:
                    retry_delay = 0.25 * (2 ** (attempt - 1))
                self._record_phone_provider_diagnostic(
                    failure_stage="openai_send",
                    safe_error_code="OPENAI_SEND_HTTP_RETRY",
                    http_status=status_code,
                    retry_count=attempt,
                    recovery_status="retrying",
                )
                time.sleep(float(retry_delay))
                continue
            safe_code = (
                "OPENAI_SEND_RETRY_EXHAUSTED"
                if status_code in retryable_statuses
                else "OPENAI_SEND_HTTP_ERROR"
            )
            self._record_phone_provider_diagnostic(
                failure_stage="openai_send",
                safe_error_code=safe_code,
                http_status=status_code,
                retry_count=attempt - 1,
                recovery_status="failed",
            )
            return (
                False,
                f"add-phone/send 重发失败: {safe_code} (HTTP {status_code})",
            )
        return False, "add-phone/send 重发失败: OPENAI_SEND_RETRY_EXHAUSTED"

    def _get_config_value(self, *keys):
        for key in keys:
            value = str(self.config.get(key, "") or "").strip()
            if value:
                return value
        return ""

    def _get_configured_phone_number(self) -> str:
        return self._get_config_value(
            "chatgpt_phone_number",
            "openai_phone_number",
            "phone_number",
        )

    def _get_configured_phone_codes(self) -> list[str]:
        raw = self._get_config_value(
            "chatgpt_phone_otp_codes",
            "chatgpt_phone_otp_code",
            "openai_phone_otp_codes",
            "openai_phone_otp_code",
            "phone_otp_codes",
            "phone_otp_code",
        )
        if not raw:
            return []
        parts = []
        for chunk in raw.replace("\n", ",").replace(";", ",").split(","):
            code = str(chunk or "").strip()
            if code:
                parts.append(code)
        return parts

    def _validate_phone_otp(
        self, code, device_id, user_agent, sec_ch_ua, impersonate, state: FlowState
    ):
        request_url = f"{self.oauth_issuer}/api/accounts/phone-otp/validate"
        headers = self._headers(
            request_url,
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
            accept="application/json",
            referer=state.current_url
            or state.continue_url
            or f"{self.oauth_issuer}/phone-verification",
            origin=self.oauth_issuer,
            content_type="application/json",
            fetch_site="same-origin",
            extra_headers={"oai-device-id": device_id},
        )
        headers.update(generate_datadog_trace())

        try:
            kwargs = {
                "json": {"code": code},
                "headers": headers,
                "timeout": 30,
                "allow_redirects": False,
            }
            if impersonate:
                kwargs["impersonate"] = impersonate
            self._browser_pause(0.12, 0.25)
            resp = self.session.post(request_url, **kwargs)
        except Exception:
            self._record_phone_provider_diagnostic(
                failure_stage="openai_validate",
                safe_error_code="OPENAI_VALIDATE_TRANSPORT",
                recovery_status="failed",
            )
            return (
                False,
                None,
                "phone-otp/validate 失败: OPENAI_VALIDATE_TRANSPORT",
            )

        try:
            status_code = int(getattr(resp, "status_code", 0) or 0)
        except (TypeError, ValueError):
            status_code = 0
        self.last_http_status = status_code
        self._log(f"/phone-otp/validate -> {status_code}")
        if status_code != 200:
            if status_code == 401:
                self._record_phone_provider_diagnostic(
                    failure_stage="openai_validate",
                    safe_error_code="OPENAI_OTP_INVALID",
                    http_status=status_code,
                    recovery_status="failed",
                )
                return False, None, "手机号验证码错误"
            self._record_phone_provider_diagnostic(
                failure_stage="openai_validate",
                safe_error_code="OPENAI_VALIDATE_HTTP_ERROR",
                http_status=status_code,
                recovery_status="failed",
            )
            return (
                False,
                None,
                "phone-otp/validate 失败: "
                f"OPENAI_VALIDATE_HTTP_ERROR (HTTP {status_code})",
            )

        try:
            data = resp.json()
        except Exception:
            self._record_phone_provider_diagnostic(
                failure_stage="openai_validate",
                safe_error_code="OPENAI_VALIDATE_INVALID_JSON",
                http_status=status_code,
                recovery_status="failed",
            )
            return False, None, "phone-otp/validate 响应不是有效 JSON"

        try:
            next_state = self._state_from_payload(
                data,
                current_url=str(getattr(resp, "url", "") or request_url),
            )
        except Exception:
            self._record_phone_provider_diagnostic(
                failure_stage="openai_validate",
                safe_error_code="OPENAI_VALIDATE_INVALID_PAYLOAD",
                http_status=status_code,
                recovery_status="failed",
            )
            return False, None, "phone-otp/validate 未返回有效状态"
        self._record_phone_provider_diagnostic(
            failure_stage="openai_validate",
            http_status=status_code,
            recovery_status="captured",
        )
        self._log(f"手机号 OTP 验证通过 {describe_flow_state(next_state)}")
        return True, next_state, ""

    def _handle_add_phone_verification(
        self, device_id, user_agent, sec_ch_ua, impersonate, state: FlowState
    ):
        configured_phone = self._get_configured_phone_number()
        configured_codes = self._get_configured_phone_codes()

        if configured_phone:
            self._log(f"步骤5: add_phone 使用配置手机号: {configured_phone}")
            sent, next_state, detail = self._send_phone_number(
                configured_phone,
                device_id,
                user_agent,
                sec_ch_ua,
                impersonate,
            )
            if not sent or not next_state:
                self._set_error(detail or "add-phone/send 未返回有效状态")
                return None

            if (
                next_state.page_type != "phone_otp_verification"
                and "phone-verification"
                not in f"{next_state.continue_url} {next_state.current_url}".lower()
            ):
                if self._state_supports_workspace_resolution(next_state) or self._state_requires_navigation(next_state):
                    self._log(f"add_phone 提交后已进入后续状态: {describe_flow_state(next_state)}")
                    return next_state
                self._set_error(
                    f"add-phone/send 未进入手机验证码页: {describe_flow_state(next_state)}"
                )
                return None

            interactive_broker = self.config.get("chatgpt_interactive_phone_broker")
            if interactive_broker is not None:
                interactive_broker.mark_code_sent(configured_phone)
                while True:
                    try:
                        command = interactive_broker.wait_for_command()
                    except Exception as exc:
                        self._set_error(str(exc) or "手机验证会话已结束")
                        return None

                    if command.kind == "resend":
                        resend_ok, resend_detail = self._resend_phone_otp(
                            configured_phone,
                            device_id,
                            user_agent,
                            sec_ch_ua,
                            impersonate,
                            next_state,
                        )
                        interactive_broker.resolve_command(
                            command.id,
                            ok=resend_ok,
                            message=(
                                "验证码已重新发送"
                                if resend_ok
                                else (resend_detail or "短信验证码重新发送失败")
                            ),
                        )
                        continue

                    valid, validated_state, detail = self._validate_phone_otp(
                        command.payload,
                        device_id,
                        user_agent,
                        sec_ch_ua,
                        impersonate,
                        next_state,
                    )
                    if not valid or not validated_state:
                        interactive_broker.resolve_command(
                            command.id,
                            ok=False,
                            message=detail or "手机号验证码错误或已过期",
                        )
                        continue

                    interactive_broker.mark_phone_verified()
                    interactive_broker.resolve_command(
                        command.id,
                        ok=True,
                        message="手机号验证通过",
                    )
                    return validated_state

            if configured_codes:
                for idx, code in enumerate(configured_codes, start=1):
                    self._log(
                        f"步骤5: 使用配置手机号验证码 {idx}/{len(configured_codes)}: {code}"
                    )
                    valid, validated_state, detail = self._validate_phone_otp(
                        code,
                        device_id,
                        user_agent,
                        sec_ch_ua,
                        impersonate,
                        next_state,
                    )
                    if valid and validated_state:
                        return validated_state
                    self._log(detail or "手机号 OTP 验证失败")

                self._set_error("配置的手机号验证码未通过验证")
                return None

            self._set_error(
                "已提交配置手机号，但未提供 chatgpt_phone_otp_code，当前流程无法继续"
            )
            return None

        progress_broker = self.config.get("chatgpt_phone_progress_broker")
        leadbee_codes = tuple(
            code
            for code in (
                str(self.config.get("leadbee_code") or "").strip(),
                str(self.config.get("chatgpt_leadbee_code") or "").strip(),
            )
            if code
        )

        def _phone_service_log(message):
            safe_message = str(message or "")
            for leadbee_code in leadbee_codes:
                safe_message = safe_message.replace(
                    leadbee_code,
                    "[LeadBee兑换码已脱敏]",
                )
            self._log(safe_message)
            if progress_broker is not None:
                progress_broker.mark_progress(safe_message.strip())

        phone_service = create_phone_service(
            self.config,
            log_fn=_phone_service_log,
        )
        provider_name = str(
            getattr(phone_service, "provider_name", "手机号服务") or "手机号服务"
        )
        if not phone_service.enabled:
            self._set_error(
                f"当前链路需要手机号验证，但未配置可用的{provider_name}能力或固定手机号验证码"
            )
            return None

        requires_explicit_replacement = (
            getattr(phone_service, "requires_explicit_replacement", False) is True
        )

        def _log_phone_failure(message):
            safe_message = re.sub(r"\s+", " ", str(message or "")).strip()
            exchange_code = str(getattr(phone_service, "code", "") or "").strip()
            if exchange_code:
                safe_message = safe_message.replace(
                    exchange_code, "[LeadBee兑换码已脱敏]"
                )
            safe_message = re.sub(
                r"(?i)(bearer\s+)[a-z0-9._~+/=-]+",
                r"\1[凭据已脱敏]",
                safe_message,
            )[:500]
            if safe_message:
                _phone_service_log(safe_message)

        excluded_prefixes = set()
        last_failure = ""
        phone_flow_succeeded = False

        try:
            for attempt in range(phone_service.max_attempts):
                try:
                    entry = phone_service.acquire_phone(
                        exclude_prefixes=excluded_prefixes
                    )
                except Exception as e:
                    last_failure = f"获取手机号失败: {e}"
                    _log_phone_failure(last_failure)
                    break

                if not entry:
                    last_failure = last_failure or f"{provider_name} 中无可用手机号"
                    break

                prefix = phone_service.prefix_hint(entry.phone)
                phone_log_hint = self._phone_log_hint(phone_service, entry.phone)
                if progress_broker is not None:
                    progress_broker.mark_phone_acquired(entry.phone)
                self._log(
                    f"步骤5: add_phone 选择手机号 {attempt + 1}/{phone_service.max_attempts}: {phone_log_hint} ({entry.country_slug})"
                )

                sent, next_state, detail = self._send_phone_number(
                    entry.phone,
                    device_id,
                    user_agent,
                    sec_ch_ua,
                    impersonate,
                )
                if not sent or not next_state:
                    last_failure = detail or "add-phone/send 未返回有效状态"
                    _log_phone_failure(last_failure)
                    replacement_scheduled = self._blacklist_phone_if_needed(
                        phone_service,
                        entry,
                        last_failure,
                    )
                    excluded_prefixes.add(prefix)
                    if requires_explicit_replacement and not replacement_scheduled:
                        break
                    continue

                if (
                    next_state.page_type != "phone_otp_verification"
                    and "phone-verification"
                    not in f"{next_state.continue_url} {next_state.current_url}".lower()
                ):
                    last_failure = f"add-phone/send 未进入手机验证码页: {describe_flow_state(next_state)}"
                    _log_phone_failure(last_failure)
                    replacement_scheduled = self._blacklist_phone_if_needed(
                        phone_service,
                        entry,
                        last_failure,
                        next_state,
                    )
                    excluded_prefixes.add(prefix)
                    if requires_explicit_replacement and not replacement_scheduled:
                        break
                    continue

                session_data = self._decode_oauth_session_cookie() or {}
                verification_channel = (
                    str(session_data.get("phone_verification_channel") or "sms")
                    .strip()
                    .lower()
                    or "sms"
                )
                bound_phone = (
                    str(session_data.get("phone_number") or entry.phone).strip()
                    or entry.phone
                )
                bound_phone_log_hint = self._phone_log_hint(
                    phone_service,
                    bound_phone,
                )
                self._log(
                    f"add_phone 发码成功: phone={bound_phone_log_hint}, channel={verification_channel}"
                )

                if verification_channel != "sms":
                    last_failure = f"add_phone 已切到 {verification_channel} 通道，当前 {provider_name} 仅支持短信接码"
                    _log_phone_failure(last_failure)
                    excluded_prefixes.add(prefix)
                    if requires_explicit_replacement:
                        break
                    continue

                if progress_broker is not None:
                    progress_broker.mark_automatic_sms_sent(entry.phone)
                code = phone_service.wait_for_code(entry)
                if not code:
                    resend_detail = ""
                    if bool(getattr(phone_service, "supports_resend", True)):
                        self._log("手机号验证码暂未收到，尝试重发一次...")
                        resend_ok, resend_detail = self._resend_phone_otp(
                            entry.phone,
                            device_id,
                            user_agent,
                            sec_ch_ua,
                            impersonate,
                            next_state,
                        )
                        if resend_ok:
                            code = phone_service.wait_for_code(entry)
                    if not code:
                        last_failure = (
                            resend_detail or f"手机号 {phone_log_hint} 未收到短信验证码"
                        )
                        _log_phone_failure(last_failure)
                        excluded_prefixes.add(prefix)
                        if requires_explicit_replacement:
                            if attempt + 1 >= phone_service.max_attempts:
                                break
                            request_replacement = getattr(
                                phone_service,
                                "request_replacement",
                                None,
                            )
                            if not callable(request_replacement):
                                break
                            try:
                                replacement_scheduled = bool(
                                    request_replacement(
                                        entry.phone,
                                        reason="sms_not_received",
                                    )
                                )
                            except Exception as exc:
                                replacement_failure = (
                                    f"{provider_name} 换号失败: {exc}"
                                )
                                _log_phone_failure(replacement_failure)
                                last_failure = (
                                    f"{last_failure}; {replacement_failure}"
                                )
                                break
                            if not replacement_scheduled:
                                break
                        continue

                if progress_broker is not None:
                    progress_broker.mark_automatic_code_received()
                valid, validated_state, detail = self._validate_phone_otp(
                    code,
                    device_id,
                    user_agent,
                    sec_ch_ua,
                    impersonate,
                    next_state,
                )
                if not valid or not validated_state:
                    last_failure = detail or "手机号 OTP 验证失败"
                    _log_phone_failure(last_failure)
                    excluded_prefixes.add(prefix)
                    if requires_explicit_replacement:
                        break
                    continue

                phone_flow_succeeded = True
                if progress_broker is not None:
                    progress_broker.mark_phone_verified()
                return validated_state
        finally:
            if (
                not phone_flow_succeeded
                and getattr(phone_service, "supports_cancellation", False) is True
            ):
                cancel_active = getattr(phone_service, "cancel_active", None)
                if callable(cancel_active):
                    cancelled = False
                    cancellation_failure = ""
                    try:
                        cancelled = bool(cancel_active())
                    except Exception as exc:
                        cancellation_failure = str(exc or "").strip()
                        _phone_service_log(
                            f"{provider_name} 自动释放失败: {cancellation_failure}"
                        )
                    if (
                        not cancelled
                        and bool(getattr(phone_service, "card_at_risk", False))
                    ):
                        cancellation_failure = (
                            cancellation_failure
                            or str(
                                getattr(phone_service, "last_cancel_error", "")
                                or ""
                            ).strip()
                            or "服务端未确认恢复"
                        )
                        reuse_failure = (
                            "LeadBee 任务不可取消，卡密不可复用: "
                            f"{cancellation_failure}"
                        )
                        last_failure = "; ".join(
                            part for part in (last_failure, reuse_failure) if part
                        )
                        _log_phone_failure(reuse_failure)

        self._set_error(f"add_phone 阶段失败: {last_failure or '未完成手机号验证'}")
        return None

    def _handle_otp_verification(
        self,
        email,
        device_id,
        user_agent,
        sec_ch_ua,
        impersonate,
        skymail_client,
        state,
        *,
        prefer_passwordless_login=False,
        allow_cached_code_retry=False,
    ):
        """处理 OAuth 阶段的邮箱 OTP 验证，返回服务端声明的下一步状态。"""
        self._enter_stage("otp", f"email={email}")
        self._log("步骤4: 检测到邮箱 OTP 验证")
        # 记录 OTP 发送时间基线——必须在 sentinel token 等耗时操作之前，
        # 否则邮件 created_at 会早于 otp_cutoff 导致验证码被误判为旧邮件。
        _otp_sent_at_baseline = time.time()

        def _resend_email_otp() -> bool:
            prefer_passwordless = bool(
                prefer_passwordless_login
                or allow_cached_code_retry
                or self.config.get("prefer_passwordless_login")
                or self.config.get("force_passwordless_login")
            )
            resend_ok = False
            if prefer_passwordless:
                request_url = f"{self.oauth_issuer}/api/accounts/passwordless/send-otp"
                headers = self._headers(
                    request_url,
                    user_agent=user_agent,
                    sec_ch_ua=sec_ch_ua,
                    accept="application/json",
                    referer=state.current_url
                    or state.continue_url
                    or f"{self.oauth_issuer}/log-in/password",
                    origin=self.oauth_issuer,
                    content_type="application/json",
                    fetch_site="same-origin",
                    extra_headers={
                        "oai-device-id": device_id,
                    },
                )
                headers.update(generate_datadog_trace())
                try:
                    kwargs = {"headers": headers, "timeout": 30, "allow_redirects": False}
                    if impersonate:
                        kwargs["impersonate"] = impersonate
                    self._browser_pause()
                    resp = self.session.post(request_url, **kwargs)
                    self._log(f"/passwordless/send-otp -> {resp.status_code}")
                    if resp.status_code == 200:
                        resend_ok = True
                except Exception as e:
                    self._log(f"passwordless resend 异常: {e}")

            if resend_ok:
                self._log("已触发 passwordless OTP 重发")
                return True

            request_url = f"{self.oauth_issuer}/api/accounts/email-otp/send"
            headers = self._headers(
                request_url,
                user_agent=user_agent,
                sec_ch_ua=sec_ch_ua,
                accept="application/json, text/plain, */*",
                referer=state.current_url
                or state.continue_url
                or f"{self.oauth_issuer}/email-verification",
                fetch_site="same-origin",
                extra_headers={
                    "oai-device-id": device_id,
                },
            )
            headers.update(generate_datadog_trace())
            try:
                kwargs = {"headers": headers, "timeout": 30, "allow_redirects": True}
                if impersonate:
                    kwargs["impersonate"] = impersonate
                self._browser_pause()
                resp = self.session.get(request_url, **kwargs)
                self._log(f"/email-otp/send -> {resp.status_code}")
                if resp.status_code == 200:
                    self._log("已触发 email-otp 重发")
                    return True
                self._log(f"email-otp/send 重发失败: {resp.text[:120]}")
            except Exception as e:
                self._log(f"email-otp/send 重发异常: {e}")
            return False

        request_url = f"{self.oauth_issuer}/api/accounts/email-otp/validate"
        self._log(f"email_otp_validate: device_id={device_id}")
        otp_referer = (
            state.current_url
            or state.continue_url
            or f"{self.oauth_issuer}/email-verification"
        )
        sentinel_otp = build_sentinel_token(
            self.session,
            device_id,
            flow="email_otp_validate",
            user_agent=user_agent,
            sec_ch_ua=sec_ch_ua,
            impersonate=impersonate,
        )
        if sentinel_otp:
            self._log("email_otp_validate: 已通过 HTTP PoW 获取 token")
        else:
            sentinel_otp = get_sentinel_token_via_browser(
                flow="email_otp_validate",
                proxy=self.proxy,
                page_url=otp_referer,
                headless=self.browser_mode != "headed",
                device_id=device_id,
                log_fn=lambda msg: self._log(f"email_otp_validate: {msg}"),
            )
            if sentinel_otp:
                self._log(
                    "email_otp_validate: HTTP PoW 失败，"
                    "已通过 Playwright SentinelSDK 获取 token"
                )
            else:
                self._log("email_otp_validate: 未生成 sentinel token（继续尝试）")

        def _build_otp_headers():
            extra_headers = {
                "oai-device-id": device_id,
            }
            if sentinel_otp:
                extra_headers["openai-sentinel-token"] = sentinel_otp
            headers_otp = self._headers(
                request_url,
                user_agent=user_agent,
                sec_ch_ua=sec_ch_ua,
                accept="application/json",
                referer=otp_referer,
                origin=self.oauth_issuer,
                content_type="application/json",
                fetch_site="same-origin",
                extra_headers=extra_headers,
            )
            headers_otp.update(generate_datadog_trace())
            return headers_otp

        if not hasattr(skymail_client, "_used_codes"):
            skymail_client._used_codes = set()

        tried_codes = set()
        try:
            otp_wait_seconds = int(
                self.config.get(
                    "chatgpt_oauth_otp_wait_seconds",
                    self.config.get("chatgpt_otp_wait_seconds", 600),
                )
                or 600
            )
        except Exception:
            otp_wait_seconds = 600
        otp_wait_seconds = max(30, min(otp_wait_seconds, 3600))
        otp_poll_window = min(30, max(10, otp_wait_seconds))
        try:
            default_resend_wait_seconds = 45 if prefer_passwordless_login else 120
            otp_resend_wait_seconds = int(
                self.config.get(
                    "chatgpt_oauth_otp_resend_wait_seconds",
                    self.config.get(
                        "chatgpt_otp_resend_wait_seconds",
                        default_resend_wait_seconds,
                    ),
                )
                or default_resend_wait_seconds
            )
        except Exception:
            otp_resend_wait_seconds = 45 if prefer_passwordless_login else 120
        otp_resend_wait_seconds = max(30, min(otp_resend_wait_seconds, 900))
        otp_deadline = time.time() + otp_wait_seconds
        otp_sent_at = _otp_sent_at_baseline
        self._log(
            f"OAuth OTP 等待窗口: total={otp_wait_seconds}s, poll_window={otp_poll_window}s, "
            f"每轮最多 5 次无响应后重发，最多 3 轮"
        )

        def validate_otp(code):
            tried_codes.add(code)
            self._log(f"尝试 OTP: {code}")

            try:
                kwargs = {
                    "json": {"code": code},
                    "headers": _build_otp_headers(),
                    "timeout": 30,
                    "allow_redirects": False,
                }
                if impersonate:
                    kwargs["impersonate"] = impersonate
                self._browser_pause(0.12, 0.25)
                resp_otp = self.session.post(request_url, **kwargs)
            except Exception as e:
                self._log(f"email-otp/validate 异常: {e}")
                return None

            self._log(f"/email-otp/validate -> {resp_otp.status_code}")
            if resp_otp.status_code != 200:
                response_text = str(resp_otp.text or "")
                self._log(f"OTP 无效: {response_text[:160]}")
                error_code = ""
                error_message = ""
                try:
                    error_payload = resp_otp.json()
                except Exception:
                    error_payload = None
                if isinstance(error_payload, dict):
                    error_detail = error_payload.get("error") or error_payload.get("错误")
                    if isinstance(error_detail, dict):
                        error_code = str(
                            error_detail.get("code")
                            or error_detail.get("error_code")
                            or error_detail.get("代码")
                            or ""
                        ).strip()
                        error_message = str(
                            error_detail.get("message")
                            or error_detail.get("消息")
                            or ""
                        ).strip()
                if is_account_deactivated_message(
                    error_code,
                    error_message or response_text,
                ):
                    terminal_message = error_message or "账号已被删除或停用"
                    self._set_error(terminal_message)
                    raise ChatGPTAccountDeactivatedError(terminal_message)
                return None

            try:
                otp_data = resp_otp.json()
            except Exception:
                self._log("email-otp/validate 响应不是 JSON")
                return None

            next_state = self._state_from_payload(
                otp_data,
                current_url=str(resp_otp.url)
                or (state.current_url or state.continue_url or request_url),
            )
            self._log(f"OTP 验证通过 {describe_flow_state(next_state)}")
            self._log(
                f"otp 响应详情: current_url={str(resp_otp.url)[:120]} tried_codes={len(tried_codes)}"
            )
            remember_successful_code = getattr(
                skymail_client, "remember_successful_code", None
            )
            if callable(remember_successful_code):
                remember_successful_code(code)
            else:
                skymail_client._used_codes.add(code)
                setattr(skymail_client, "_last_success_code", code)
                setattr(skymail_client, "_last_success_code_at", time.time())
            return next_state

        if allow_cached_code_retry:
            cached_code = ""
            cached_age = None
            get_recent_code = getattr(skymail_client, "get_recent_code", None)
            if callable(get_recent_code):
                cached_code = str(
                    get_recent_code(
                        max_age_seconds=min(180, otp_wait_seconds),
                        prefer_successful=True,
                    )
                    or ""
                ).strip()
                cached_age = (
                    time.time() - float(getattr(skymail_client, "_last_success_code_at", 0) or 0)
                    if cached_code
                    else None
                )
            else:
                cached_code = str(
                    getattr(skymail_client, "_last_success_code", "")
                    or getattr(skymail_client, "_last_code", "")
                    or ""
                ).strip()
                cached_ts = float(
                    getattr(skymail_client, "_last_success_code_at", 0)
                    or getattr(skymail_client, "_last_code_at", 0)
                    or 0
                )
                if cached_code and cached_ts:
                    cached_age = time.time() - cached_ts
                    if cached_age > min(180, otp_wait_seconds):
                        cached_code = ""

            if cached_code:
                age_text = (
                    f"{int(max(0, cached_age or 0))}s前"
                    if cached_age is not None
                    else "近期"
                )
                self._log(
                    f"检测到近期缓存 OTP，先直接尝试: {cached_code} ({age_text})"
                )
                next_state = validate_otp(cached_code)
                if next_state:
                    return next_state
                self._log("缓存 OTP 未通过，继续等待新的 OTP...")

        if hasattr(skymail_client, "wait_for_verification_code"):
            self._log("使用 wait_for_verification_code 进行阻塞式获取新验证码...")
            no_new_count = 0
            resend_round = 0
            _max_no_new = 5
            _max_resend_rounds = 3
            while time.time() < otp_deadline:
                remaining = max(1, int(otp_deadline - time.time()))
                wait_time = min(otp_poll_window, remaining)
                try:
                    code = skymail_client.wait_for_verification_code(
                        email,
                        timeout=wait_time,
                        otp_sent_at=otp_sent_at,
                        exclude_codes=tried_codes,
                    )
                except TaskInterruption:
                    raise
                except MailboxAuthenticationError as e:
                    self._set_error(str(e))
                    break
                except Exception as e:
                    if "手动停止" in str(e):
                        self._set_error("任务已手动停止")
                        return None
                    self._log(f"等待 OTP 异常: {e}")
                    code = None

                if not code:
                    no_new_count += 1
                    self._log(
                        f"暂未收到新的 OTP，继续等待... (本轮第 {no_new_count}/{_max_no_new} 次)"
                    )
                    if no_new_count >= _max_no_new:
                        if resend_round < _max_resend_rounds:
                            resend_round += 1
                            self._log(
                                f"连续 {_max_no_new} 次未收到新 OTP，"
                                f"触发第 {resend_round}/{_max_resend_rounds} 轮重发..."
                            )
                            if _resend_email_otp():
                                otp_sent_at = time.time()
                            no_new_count = 0
                        else:
                            self._log(
                                f"已完成 {_max_resend_rounds} 轮重发仍未收到 OTP，放弃等待"
                            )
                            break
                    if self.last_error:
                        break
                    continue

                if code in tried_codes:
                    self._log(f"跳过已尝试验证码: {code}")
                    continue

                no_new_count = 0
                next_state = validate_otp(code)
                if next_state:
                    return next_state
                if self.last_error:
                    break
        else:
            while time.time() < otp_deadline:
                messages = skymail_client.fetch_emails(email) or []
                candidate_codes = []

                for msg in messages[:12]:
                    content = msg.get("content") or msg.get("text") or ""
                    code = skymail_client.extract_verification_code(content)
                    if code and code not in tried_codes:
                        candidate_codes.append(code)

                if not candidate_codes:
                    elapsed = int(otp_wait_seconds - max(0, otp_deadline - time.time()))
                    self._log(f"等待新的 OTP... ({elapsed}s/{otp_wait_seconds}s)")
                    time.sleep(2)
                    continue

                for otp_code in candidate_codes:
                    next_state = validate_otp(otp_code)
                    if next_state:
                        return next_state

                time.sleep(2)
                if self.last_error:
                    break

        if not self.last_error:
            self._set_error(
                f"OAuth 阶段 OTP 验证失败，已尝试 {len(tried_codes)} 个验证码，等待窗口 {otp_wait_seconds}s"
            )
        return None
