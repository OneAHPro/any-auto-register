from __future__ import annotations

"""邮箱池基类 - 抽象临时邮箱/收件服务"""

import json
import random
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional, Any, Callable
from .proxy_utils import build_requests_proxy_config


@dataclass
class MailboxAccount:
    email: str
    account_id: str = ""
    extra: dict = None  # 平台额外信息


@dataclass(frozen=True)
class MailboxVerificationResult:
    """Code plus provider freshness metadata; legacy callers still receive str."""

    code: str
    message_id: str = ""
    received_at: Any = None


class MailboxBackendError(RuntimeError):
    """A mailbox transport/auth/parse failure with a secret-free code."""

    error_code = "mailbox_backend"

    def __init__(
        self,
        message: str = "邮箱后端不可用",
        *,
        code: str = "mailbox_backend",
        http_status: int = 0,
    ) -> None:
        self.code = str(code or "mailbox_backend")
        self.http_status = int(http_status or 0)
        super().__init__(str(message or "邮箱后端不可用"))


class MailboxClaimScope:
    """Remember and serialize normal mailbox claims within one task."""

    def __init__(self):
        self._attempted_emails: set[str] = set()
        self._lock = threading.Lock()

    def claim(
        self,
        claim_fn: Callable[[set[str]], MailboxAccount],
    ) -> MailboxAccount:
        with self._lock:
            account = claim_fn(set(self._attempted_emails))
            email = str(getattr(account, "email", "") or "").strip().lower()
            if email:
                self._attempted_emails.add(email)
            return account


class MailboxAuthenticationError(RuntimeError):
    """Terminal mailbox authentication failure after credential refresh."""


class BaseMailbox(ABC):
    def _log(self, message: str) -> None:
        log_fn = getattr(self, "_log_fn", None)
        if callable(log_fn):
            log_fn(message)

    @staticmethod
    def _generate_password_reset_password(length: int = 20) -> str:
        import secrets
        import string

        alphabet = string.ascii_letters + string.digits + "!@#$%"
        required = [
            secrets.choice(string.ascii_uppercase),
            secrets.choice(string.ascii_lowercase),
            secrets.choice(string.digits),
            secrets.choice("!@#$%"),
        ]
        required.extend(
            secrets.choice(alphabet) for _ in range(max(length - 4, 8))
        )
        secrets.SystemRandom().shuffle(required)
        return "".join(required)

    def _checkpoint(self, *, consume_skip: bool = True) -> None:
        task_control = getattr(self, "_task_control", None)
        if task_control is None:
            return
        task_control.checkpoint(
            consume_skip=consume_skip,
            attempt_id=getattr(self, "_task_attempt_token", None),
        )

    def _sleep_with_checkpoint(self, seconds: float) -> None:
        remaining = max(float(seconds or 0), 0.0)
        while remaining > 0:
            self._checkpoint()
            chunk = min(0.25, remaining)
            time.sleep(chunk)
            remaining -= chunk

    @contextmanager
    def pause_active_slot_for_mailbox_wait(self):
        """Yield the task's foreground slot while preserving mailbox state."""
        task_control = getattr(self, "_task_control", None)
        pause_slot = getattr(task_control, "pause_active_slot", None)
        attempt_id = getattr(self, "_task_attempt_token", None)
        if not callable(pause_slot) or attempt_id is None:
            yield False
            return
        with pause_slot(attempt_id) as released:
            yield released

    def _run_polling_wait(
        self,
        *,
        timeout: int,
        poll_interval: float,
        poll_once: Callable[[], Optional[str]],
        timeout_message: str | None = None,
    ) -> str:
        timeout_seconds = max(int(timeout or 0), 1)
        deadline = time.monotonic() + timeout_seconds

        while time.monotonic() < deadline:
            self._checkpoint()
            code = poll_once()
            if code:
                return code

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._sleep_with_checkpoint(min(float(poll_interval), remaining))

        self._checkpoint()
        raise TimeoutError(timeout_message or f"等待验证码超时 ({timeout_seconds}s)")

    @abstractmethod
    def get_email(self) -> MailboxAccount:
        """获取一个可用邮箱"""
        ...

    @abstractmethod
    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        """等待并返回验证码，code_pattern 为自定义正则（默认匹配6位数字）"""
        ...

    def _safe_extract(self, text: str, pattern: str = None) -> Optional[str]:
        """通用验证码提取逻辑：若有捕获组则返回 group(1)，否则返回 group(0)"""
        import re

        text = str(text or "")
        if not text:
            return None

        patterns = []
        if pattern:
            patterns.append(pattern)

        # 先匹配带明显语义的验证码，避免误提取 MIME boundary、时间戳等 6 位数字。
        patterns.extend(
            [
                r"(?is)(?:verification\s+code|one[-\s]*time\s+(?:password|code)|security\s+code|login\s+code|验证码|校验码|动态码|認證碼|驗證碼)[^0-9]{0,30}(\d{6})",
                r"(?is)\bcode\b[^0-9]{0,12}(\d{6})",
                r"(?<!#)(?<!\d)(\d{6})(?!\d)",
            ]
        )

        for regex in patterns:
            m = re.search(regex, text)
            if m:
                # 兼容逻辑：若 pattern 中有捕获组则取 group(1)，否则取 group(0)
                return m.group(1) if m.groups() else m.group(0)
        return None

    def _decode_raw_content(self, raw: str) -> str:
        """解析邮件原始文本 (借鉴自 Fugle)，处理 Quoted-Printable 和 HTML 实体"""
        import quopri, html, re

        text = str(raw or "")
        if not text:
            return ""
        # 简单切分 Header 和 Body
        if "\r\n\r\n" in text:
            text = text.split("\r\n\r\n", 1)[1]
        elif "\n\n" in text:
            text = text.split("\n\n", 1)[1]
        try:
            # 处理 Quoted-Printable
            decoded_bytes = quopri.decodestring(text)
            text = decoded_bytes.decode("utf-8", errors="ignore")
        except Exception:
            pass
        # 清除 HTML 标签并反转义
        text = html.unescape(text)
        text = re.sub(r"(?im)^content-(?:type|transfer-encoding):.*$", " ", text)
        text = re.sub(r"(?im)^--+[_=\w.-]+$", " ", text)
        text = re.sub(r"(?i)----=_part_[\w.]+", " ", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @abstractmethod
    def get_current_ids(self, account: MailboxAccount) -> set:
        """返回当前邮件 ID 集合（用于过滤旧邮件）"""
        ...
    def _yyds_safe_extract(self, text: str, pattern: str = None) -> Optional[str]:
        """通用验证码提取逻辑：若有捕获组则返回 group(1)，否则返回 group(0)"""
        import re

        text = str(text or "")
        if not text:
            return None

        # [修复点 1]：优先过滤掉所有 URL 链接，直接从根源防止提取到追踪链接（如 SendGrid）里的随机数字
        text = re.sub(r"https?://\S+", "", text)

        patterns = []
        if pattern:
            # [修复点 2]：如果外部传入了纯 \d{6} 的粗糙正则，自动为其加上字母数字边界
            if pattern in (r"\d{6}", r"(\d{6})"):
                patterns.append(r"(?<![a-zA-Z0-9])(\d{6})(?![a-zA-Z0-9])")
            else:
                patterns.append(pattern)

        # 先匹配带明显语义的验证码，避免误提取 MIME boundary、时间戳等 6 位数字。
        patterns.extend(
            [
                r"(?is)(?:verification\s+code|one[-\s]*time\s+(?:password|code)|security\s+code|login\s+code|验证码|校验码|动态码|認證碼|驗證碼)[^0-9]{0,30}(\d{6})",
                r"(?is)\bcode\b[^0-9]{0,12}(\d{6})",
                # [修复点 3]：修改兜底正则，严格要求 6 位数字前后不能有字母或数字（防止匹配 u20216706）
                r"(?<![a-zA-Z0-9])(\d{6})(?![a-zA-Z0-9])",
            ]
        )

        for regex in patterns:
            m = re.search(regex, text)
            if m:
                # 兼容逻辑：若 pattern 中有捕获组则取 group(1)，否则取 group(0)
                return m.group(1) if m.groups() else m.group(0)
        return None

    def _yyds_decode_raw_content(self, raw: str) -> str:
        """解析邮件原始文本 (借鉴自 Fugle)，处理 Quoted-Printable 和 HTML 实体"""
        import quopri, html, re

        text = str(raw or "")
        if not text:
            return ""
            
        # [修复点 4]：只有在明确包含常见邮件 Header 时，才进行 \r\n\r\n 切分。
        # 否则会误删 MaliAPI 等直接返回的已解析 JSON 正文内容（遇到普通的正文换行就错误截断了）
        if re.search(r"(?im)^(?:Return-Path|Received|Date|From|To|Subject|Content-Type):", text):
            if "\r\n\r\n" in text:
                text = text.split("\r\n\r\n", 1)[1]
            elif "\n\n" in text:
                text = text.split("\n\n", 1)[1]
                
        try:
            # 处理 Quoted-Printable
            decoded_bytes = quopri.decodestring(text)
            text = decoded_bytes.decode("utf-8", errors="ignore")
        except Exception:
            pass
        # 清除 HTML 标签并反转义
        text = html.unescape(text)
        text = re.sub(r"(?im)^content-(?:type|transfer-encoding):.*$", " ", text)
        text = re.sub(r"(?im)^--+[_=\w.-]+$", " ", text)
        text = re.sub(r"(?i)----=_part_[\w.]+", " ", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

def create_mailbox(
    provider: str, extra: dict = None, proxy: str = None
) -> "BaseMailbox":
    """工厂方法：根据 provider 创建对应的 mailbox 实例"""
    extra = extra or {}
    if provider == "tempmail_lol":
        return TempMailLolMailbox(proxy=proxy)
    elif provider == "skymail":
        return SkyMailMailbox(
            api_base=extra.get("skymail_api_base", "https://api.skymail.ink"),
            auth_token=extra.get("skymail_token", ""),
            domain=extra.get("skymail_domain", ""),
            proxy=proxy,
        )
    elif provider == "cloudmail":
        timeout_raw = extra.get("cloudmail_timeout", extra.get("timeout", 30))
        try:
            timeout_value = int(timeout_raw)
        except (TypeError, ValueError):
            timeout_value = 30
        return CloudMailMailbox(
            api_base=extra.get("cloudmail_api_base")
            or extra.get("base_url")
            or "",
            admin_email=extra.get("cloudmail_admin_email")
            or extra.get("admin_email")
            or "",
            admin_password=extra.get("cloudmail_admin_password")
            or extra.get("admin_password")
            or extra.get("api_key")
            or "",
            domain=extra.get("cloudmail_domain") or extra.get("domain") or "",
            subdomain=extra.get("cloudmail_subdomain")
            or extra.get("subdomain")
            or "",
            timeout=timeout_value,
            proxy=proxy,
        )
    elif provider == "duckmail":
        return DuckMailMailbox(
            api_url=(extra.get("duckmail_api_url") or "https://www.duckmail.sbs"),
            provider_url=(
                extra.get("duckmail_provider_url") or "https://api.duckmail.sbs"
            ),
            bearer=(extra.get("duckmail_bearer") or "kevin273945"),
            domain=extra.get("duckmail_domain", ""),
            api_key=extra.get("duckmail_api_key", ""),
            proxy=proxy,
        )
    elif provider == "freemail":
        return FreemailMailbox(
            api_url=extra.get("freemail_api_url", ""),
            admin_token=extra.get("freemail_admin_token", ""),
            username=extra.get("freemail_username", ""),
            password=extra.get("freemail_password", ""),
            domain=extra.get("freemail_domain", ""),
            proxy=proxy,
        )
    elif provider == "moemail":
        return MoeMailMailbox(
            api_url=extra.get("moemail_api_url", "https://sall.cc"),
            api_key=extra.get("moemail_api_key", ""),
            proxy=proxy,
        )
    elif provider == "maliapi":
        return MaliAPIMailbox(
            api_url=extra.get("maliapi_base_url", "https://maliapi.215.im/v1"),
            api_key=extra.get("maliapi_api_key", ""),
            domain=extra.get("maliapi_domain", ""),
            auto_domain_strategy=extra.get("maliapi_auto_domain_strategy", ""),
            proxy=proxy,
        )
    elif provider == "gptmail":
        return GPTMailMailbox(
            api_url=extra.get("gptmail_base_url", "https://mail.chatgpt.org.uk"),
            api_key=extra.get("gptmail_api_key", ""),
            domain=extra.get("gptmail_domain", ""),
            proxy=proxy,
        )
    elif provider == "applemail":
        return AppleMailMailbox(
            api_url=extra.get("applemail_base_url", "https://www.appleemail.top"),
            pool_file=extra.get("applemail_pool_file", ""),
            pool_dir=extra.get("applemail_pool_dir", "mail"),
            mailboxes=extra.get("applemail_mailboxes", "INBOX,Junk"),
            proxy=proxy,
        )
    elif provider == "opentrashmail":
        return OpenTrashMailMailbox(
            api_url=extra.get("opentrashmail_api_url", ""),
            domain=extra.get("opentrashmail_domain", ""),
            password=extra.get("opentrashmail_password", ""),
            proxy=proxy,
        )
    elif provider == "cfworker":
        return CFWorkerMailbox(
            api_url=extra.get("cfworker_api_url", ""),
            admin_token=extra.get("cfworker_admin_token", ""),
            domain=extra.get("cfworker_domain", ""),
            domain_override=extra.get("cfworker_domain_override", ""),
            domains=extra.get("cfworker_domains", ""),
            enabled_domains=extra.get("cfworker_enabled_domains", ""),
            subdomain=extra.get("cfworker_subdomain", ""),
            domain_level_count=extra.get("email_domain_level_count", 2),
            random_subdomain=extra.get("cfworker_random_subdomain", False),
            random_name_subdomain=extra.get("cfworker_random_name_subdomain", False),
            fingerprint=extra.get("cfworker_fingerprint", ""),
            custom_auth=extra.get("cfworker_custom_auth", ""),
            proxy=proxy,
        )
    elif provider == "luckmail":
        return LuckMailMailbox(
            base_url=extra.get("luckmail_base_url") or "https://mails.luckyous.com/",
            api_key=extra.get("luckmail_api_key", ""),
            project_code=extra.get("luckmail_project_code", ""),
            email_type=extra.get("luckmail_email_type", ""),
            domain=extra.get("luckmail_domain", ""),
            proxy=proxy,
        )
    elif provider in {"outlook", "microsoft"}:
        return OutlookMailbox(
            imap_server=extra.get("outlook_imap_server", ""),
            imap_port=extra.get("outlook_imap_port", ""),
            token_endpoint=extra.get("outlook_token_endpoint", ""),
            backend=extra.get("outlook_backend", ""),
            graph_api_base=extra.get("outlook_graph_api_base", ""),
            proxy=proxy,
            lease_owner=extra.get("mailbox_lease_owner", ""),
            lease_seconds=extra.get("mailbox_lease_seconds", 900),
        )
    else:  # laoudo
        return LaoudoMailbox(
            auth_token=extra.get("laoudo_auth", ""),
            email=extra.get("laoudo_email", ""),
            account_id=extra.get("laoudo_account_id", ""),
        )


class AppleMailMailbox(BaseMailbox):
    """小苹果取件邮箱服务，基于本地邮箱池文件轮转邮箱账号"""

    def __init__(
        self,
        api_url: str = "https://www.appleemail.top",
        pool_file: str = "",
        pool_dir: str = "mail",
        mailboxes: str = "INBOX,Junk",
        proxy: str = None,
    ):
        self.api = (api_url or "https://www.appleemail.top").rstrip("/")
        self.pool_file = str(pool_file or "").strip()
        self.pool_dir = str(pool_dir or "mail").strip() or "mail"
        self.mailboxes = self._normalize_mailboxes(mailboxes)
        self.proxy_url = str(proxy or "").strip()
        self.proxy = build_requests_proxy_config(proxy)
        self._proxy = self.proxy
        self._email = None
        self._selected_record = None
        self._selected_pool_path = None
        self._icloud_clients = {}
        self._mailapi_backend = None
        self._claim_scope: MailboxClaimScope | None = None

    @staticmethod
    def _normalize_mailboxes(value: Any) -> list[str]:
        if isinstance(value, (list, tuple, set)):
            items = [str(item or "").strip() for item in value]
        else:
            raw = str(value or "INBOX,Junk").strip() or "INBOX,Junk"
            items = [item.strip() for item in raw.split(",")]

        result = []
        seen = set()
        for item in items:
            if not item:
                continue
            if item not in seen:
                seen.add(item)
                result.append(item)
        return result or ["INBOX", "Junk"]

    def _headers(self) -> dict[str, str]:
        return {"accept": "application/json"}

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any],
        timeout: int = 15,
    ) -> Any:
        import requests

        response = requests.request(
            method,
            f"{self.api}{path}",
            params=payload,
            json=None,
            headers=self._headers(),
            proxies=self.proxy,
            timeout=timeout,
        )
        try:
            data = response.json()
        except Exception as exc:
            preview = (response.text or "")[:200]
            raise RuntimeError(
                f"AppleMail API {path} 返回非 JSON: HTTP {response.status_code} {preview}"
            ) from exc

        if response.status_code >= 400:
            if isinstance(data, dict):
                message = (
                    data.get("detail")
                    or data.get("message")
                    or data.get("error")
                    or response.text
                )
            else:
                message = response.text
            raise RuntimeError(
                f"AppleMail API {path} 失败: {str(message or f'HTTP {response.status_code}').strip()}"
            )

        if isinstance(data, dict) and data.get("success") is False:
            message = (
                data.get("message")
                or data.get("detail")
                or data.get("error")
                or "unknown error"
            )
            raise RuntimeError(f"AppleMail API {path} 失败: {str(message).strip()}")

        return data

    @staticmethod
    def _unwrap_message_payload(payload: Any) -> list[dict[str, Any]]:
        if payload is None:
            return []
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            for key in ("data", "result", "results", "messages", "mails", "emails", "items", "list"):
                if key in payload:
                    nested = AppleMailMailbox._unwrap_message_payload(payload.get(key))
                    if nested:
                        return nested
            if any(
                key in payload
                for key in (
                    "id",
                    "message_id",
                    "uid",
                    "mail_id",
                    "subject",
                    "content",
                    "text",
                    "html",
                    "body",
                    "preview",
                    "verification_code",
                    "code",
                    "otp",
                )
            ):
                return [payload]

            collected = []
            for value in payload.values():
                collected.extend(AppleMailMailbox._unwrap_message_payload(value))
            return collected
        return []

    @staticmethod
    def _resolve_message_id(message: dict[str, Any], mailbox: str) -> str:
        import hashlib

        for key in ("id", "message_id", "uid", "mail_id", "mid", "_id"):
            value = str(message.get(key) or "").strip()
            if value:
                return value

        raw = json.dumps(message, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha1(f"{mailbox}:{raw}".encode("utf-8")).hexdigest()
        return f"{mailbox}:{digest}"

    def _build_search_text(self, message: dict[str, Any]) -> str:
        parts = []
        for key in (
            "subject",
            "from",
            "from_address",
            "sender",
            "preview",
            "text",
            "content",
            "body",
            "html",
            "html_content",
            "raw",
            "raw_content",
            "mail_text",
        ):
            value = message.get(key)
            if value:
                parts.append(str(value))

        if not parts:
            parts.append(json.dumps(message, ensure_ascii=False))

        text = " ".join(parts).strip()
        return self._decode_raw_content(text) or text

    def _extract_code_from_message(
        self,
        message: dict[str, Any],
        code_pattern: str = None,
    ) -> Optional[str]:
        for key in ("verification_code", "code", "otp", "captcha", "verify_code"):
            value = str(message.get(key) or "").strip()
            if value:
                code = self._safe_extract(value, code_pattern)
                if code:
                    return code
        return self._safe_extract(self._build_search_text(message), code_pattern)

    def _resolve_mailboxes_for_account(self, account: MailboxAccount) -> list[str]:
        account_mailbox = ""
        if isinstance(account.extra, dict):
            account_mailbox = str(account.extra.get("mailbox") or "").strip()

        result = []
        seen = set()
        for mailbox in ([account_mailbox] if account_mailbox else []) + list(self.mailboxes):
            name = str(mailbox or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            result.append(name)
        return result or ["INBOX"]

    def _build_request_payload(self, account: MailboxAccount, mailbox: str) -> dict[str, Any]:
        extra = account.extra or {}
        refresh_token = str(extra.get("refresh_token") or "").strip()
        client_id = str(extra.get("client_id") or "").strip()
        if not refresh_token or not client_id:
            raise RuntimeError("AppleMail 邮箱记录缺少 refresh_token 或 client_id")

        return {
            "refresh_token": refresh_token,
            "client_id": client_id,
            "email": account.email,
            "mailbox": mailbox,
        }

    @staticmethod
    def _is_icloud_web_account(account: MailboxAccount) -> bool:
        extra = account.extra or {}
        return str(extra.get("account_type") or "").strip() == "icloud_web"

    @staticmethod
    def _is_chatgpt_password_totp_account(account: MailboxAccount) -> bool:
        extra = account.extra or {}
        return str(extra.get("account_type") or "").strip() in {
            "chatgpt_google_password",
            "chatgpt_password_totp",
            "chatgpt_password_remote_totp",
        }

    @staticmethod
    def _has_mailapi_url(account: MailboxAccount) -> bool:
        extra = account.extra or {}
        return bool(
            str(
                extra.get("mailapi_url")
                or extra.get("mail_api_url")
                or ""
            ).strip()
        )

    @staticmethod
    def _is_chatgpt_url_mail_account(account: MailboxAccount) -> bool:
        extra = account.extra or {}
        return str(extra.get("account_type") or "").strip() in {
            "chatgpt_password_url_otp",
            "chatgpt_password_reset_url_mail",
        }

    def _get_mailapi_backend(self):
        if self._mailapi_backend is None:
            self._mailapi_backend = MailApiUrlOtpBackend(self)
        return self._mailapi_backend

    def _get_icloud_client(self, account: MailboxAccount):
        key = str(account.email or "").strip().lower()
        client = self._icloud_clients.get(key)
        if client is not None:
            return client
        extra = account.extra or {}
        from .icloud_mail import ICloudMailClient

        client = ICloudMailClient(
            email=account.email,
            password=str(extra.get("password") or ""),
            mfa_secret=str(extra.get("mfa_secret") or ""),
            proxy_url=self.proxy_url,
        )
        self._icloud_clients[key] = client
        return client

    def _list_messages(self, account: MailboxAccount, mailbox: str) -> list[dict[str, Any]]:
        if self._is_chatgpt_password_totp_account(account):
            raise RuntimeError(
                "ChatGPT 密码 + MFA 凭据不包含邮箱收件能力"
            )
        if self._is_icloud_web_account(account):
            return self._get_icloud_client(account).list_messages(mailbox)
        data = self._request_json(
            "GET",
            "/api/mail-all",
            payload=self._build_request_payload(account, mailbox),
            timeout=15,
        )
        if isinstance(data, dict):
            new_refresh_token = str(data.get("new_refresh_token") or "").strip()
            if new_refresh_token:
                if account.extra is None:
                    account.extra = {}
                account.extra["refresh_token"] = new_refresh_token
        return self._unwrap_message_payload(data)

    def get_email(self) -> MailboxAccount:
        if self._claim_scope is not None:
            return self._claim_scope.claim(
                lambda exclude_emails: self._claim_email(
                    exclude_emails=exclude_emails,
                )
            )
        return self._claim_email()

    def bind_claim_scope(self, scope: MailboxClaimScope | None) -> None:
        self._claim_scope = scope

    def get_email_by_address(self, email: str) -> MailboxAccount:
        try:
            return self._claim_email(email=email)
        except RuntimeError as claim_error:
            from .applemail_pool import load_used_applemail_record_by_email

            try:
                pool_path, record = load_used_applemail_record_by_email(
                    email=email,
                    pool_file=self.pool_file,
                    pool_dir=self.pool_dir,
                )
            except RuntimeError:
                raise claim_error
            return self._build_pool_account(pool_path, record)

    def _claim_email(
        self,
        email: str = "",
        *,
        exclude_emails=None,
    ) -> MailboxAccount:
        from .applemail_pool import take_next_applemail_record

        pool_path, record = take_next_applemail_record(
            pool_file=self.pool_file,
            pool_dir=self.pool_dir,
            email=email,
            exclude_emails=exclude_emails,
        )
        return self._build_pool_account(pool_path, record)

    def _build_pool_account(
        self,
        pool_path,
        record: dict[str, Any],
    ) -> MailboxAccount:
        self._selected_pool_path = pool_path
        self._selected_record = record
        self._email = record["email"]
        account_type = str(record.get("account_type") or "").strip()
        direct_icloud = account_type == "icloud_web"
        chatgpt_credentials = account_type in {
            "chatgpt_google_password",
            "chatgpt_password_totp",
            "chatgpt_password_remote_totp",
        }
        url_credentials = account_type in {
            "chatgpt_password_url_otp",
            "chatgpt_password_reset_url_mail",
        }
        provider_label = (
            "ChatGPT 登录"
            if chatgpt_credentials or url_credentials
            else "iCloud"
            if direct_icloud
            else "AppleMail"
        )
        self._log(f"[{provider_label}] 使用邮箱池: {pool_path.name}")
        self._log(f"[{provider_label}] 分配邮箱: {record['email']}")
        if account_type == "chatgpt_password_remote_totp":
            totp_url = str(record.get("totp_url") or "").strip()
            if not totp_url:
                raise RuntimeError("ChatGPT 远程 MFA 记录缺少 2FA 地址")
            extra = {
                "provider": "chatgpt_credentials",
                "account_type": account_type,
                "password": str(record.get("password") or ""),
                "totp_url": totp_url,
                "pool_file": pool_path.name,
            }
        elif url_credentials:
            extra = {
                "provider": "chatgpt_credentials",
                "account_type": account_type,
                "password": str(record.get("password") or ""),
                "mail_api_url": record["mail_api_url"],
                "mailapi_url": record["mail_api_url"],
                "mailbox": record.get("mailbox") or "INBOX",
                "pool_file": pool_path.name,
            }
            if str(record.get("totp_url") or "").strip():
                extra["totp_url"] = str(record["totp_url"])
            if account_type == "chatgpt_password_url_otp":
                if "totp_url" not in extra:
                    raise RuntimeError("ChatGPT URL 登录记录缺少 2FA 地址")
            elif bool(record.get("password_reset_required", True)):
                extra["password_reset_required"] = True
                extra["new_password"] = self._generate_password_reset_password()
            else:
                extra["password_reset_required"] = False
        elif chatgpt_credentials:
            extra = {
                "provider": "chatgpt_credentials",
                "account_type": account_type,
                "password": record["password"],
                "pool_file": pool_path.name,
            }
            if account_type == "chatgpt_password_totp":
                extra["totp_secret"] = record["totp_secret"]
            elif account_type == "chatgpt_password_remote_totp":
                extra["totp_url"] = str(record["totp_url"] or "")
            mail_api_url = str(record.get("mail_api_url") or "").strip()
            if mail_api_url:
                extra["mail_api_url"] = mail_api_url
                extra["mailapi_url"] = mail_api_url
        elif direct_icloud:
            extra = {
                "provider": "icloud",
                "account_type": "icloud_web",
                "password": record["password"],
                "mfa_secret": record["mfa_secret"],
                "mailbox": record.get("mailbox") or "INBOX",
                "pool_file": pool_path.name,
            }
        else:
            extra = {
                "provider": "applemail",
                "client_id": record["client_id"],
                "refresh_token": record["refresh_token"],
                "mailbox": record.get("mailbox") or "INBOX",
                "pool_file": pool_path.name,
            }
        extra["_pool_claim_id"] = str(record.get("pool_claim_id") or "")
        extra["_pool_state"] = str(record.get("pool_state") or "")
        return MailboxAccount(
            email=record["email"],
            account_id=record["email"],
            extra=extra,
        )

    def requeue_account(self, account: MailboxAccount) -> bool:
        from .applemail_pool import requeue_applemail_record

        selected = dict(self._selected_record or {})
        account_extra = dict(getattr(account, "extra", None) or {})
        account_email = str(getattr(account, "email", "") or "").strip()
        selected_email = str(selected.get("email") or "").strip()
        if selected_email and account_email and selected_email.lower() != account_email.lower():
            return False
        claim_id = str(account_extra.get("_pool_claim_id") or "").strip()
        if (
            not claim_id
            and str(selected.get("pool_state") or "").strip().lower() == "claimed"
        ):
            claim_id = str(selected.get("pool_claim_id") or "").strip()
        if not claim_id:
            return False
        restored = requeue_applemail_record(
            pool_file=str(self._selected_pool_path or self.pool_file),
            pool_dir=self.pool_dir,
            claim_id=claim_id,
        )
        if restored:
            selected["enabled"] = True
            selected["pool_state"] = "available"
            selected.pop("pool_claim_id", None)
            self._selected_record = selected
        return restored

    def fail_account(
        self,
        account: MailboxAccount,
        *,
        error: str = "",
        task_id: str = "",
    ) -> bool:
        from .applemail_pool import mark_applemail_record_failed

        selected = dict(self._selected_record or {})
        account_extra = dict(getattr(account, "extra", None) or {})
        claim_id = str(account_extra.get("_pool_claim_id") or "").strip()
        if not claim_id:
            claim_id = str(selected.get("pool_claim_id") or "").strip()
        return mark_applemail_record_failed(
            pool_file=str(self._selected_pool_path or self.pool_file),
            pool_dir=self.pool_dir,
            claim_id=claim_id,
            email=str(getattr(account, "email", "") or ""),
            error=error,
            task_id=task_id,
        )

    def discard_account(self, account: MailboxAccount, *, reason: str = "") -> bool:
        del reason
        return self.mark_account_used(account)

    def mark_account_used(self, account: MailboxAccount) -> bool:
        from .applemail_pool import mark_applemail_record_used

        selected = dict(self._selected_record or {})
        account_extra = dict(getattr(account, "extra", None) or {})
        account_email = str(getattr(account, "email", "") or "").strip()
        selected_email = str(selected.get("email") or "").strip()
        if selected_email and account_email and selected_email.lower() != account_email.lower():
            return False
        if (
            str(account_extra.get("_pool_state") or "").strip().lower() == "used"
            or str(selected.get("pool_state") or "").strip().lower() == "used"
        ):
            return True
        claim_id = str(account_extra.get("_pool_claim_id") or "").strip()
        if (
            not claim_id
            and str(selected.get("pool_state") or "").strip().lower() == "claimed"
        ):
            claim_id = str(selected.get("pool_claim_id") or "").strip()
        if not claim_id:
            return False
        marked = mark_applemail_record_used(
            pool_file=str(self._selected_pool_path or self.pool_file),
            pool_dir=self.pool_dir,
            claim_id=claim_id,
        )
        if marked:
            selected["enabled"] = False
            selected["pool_state"] = "used"
            selected.pop("pool_claim_id", None)
            self._selected_record = selected
            transient_extra = getattr(account, "extra", None)
            if isinstance(transient_extra, dict):
                transient_extra["_pool_state"] = "used"
        return marked

    def commit_password_reset(
        self,
        account: MailboxAccount,
        new_password: str = "",
    ) -> bool:
        from .applemail_pool import commit_applemail_password_reset

        selected = dict(self._selected_record or {})
        account_extra = dict(getattr(account, "extra", None) or {})
        account_email = str(getattr(account, "email", "") or "").strip()
        selected_email = str(selected.get("email") or "").strip()
        if selected_email and account_email and selected_email.lower() != account_email.lower():
            return False
        claim_id = str(account_extra.get("_pool_claim_id") or "").strip()
        if (
            not claim_id
            and str(selected.get("pool_state") or "").strip().lower() == "claimed"
        ):
            claim_id = str(selected.get("pool_claim_id") or "").strip()
        if not claim_id and not account_email:
            return False
        password = str(new_password or account_extra.get("new_password") or "")
        committed = commit_applemail_password_reset(
            pool_file=str(self._selected_pool_path or self.pool_file),
            pool_dir=self.pool_dir,
            claim_id=claim_id,
            email=account_email,
            new_password=password,
        )
        if committed:
            selected["password"] = password
            selected["password_reset_required"] = False
            self._selected_record = selected
            if isinstance(account.extra, dict):
                account.extra["password"] = password
                account.extra["password_reset_required"] = False
                account.extra.pop("new_password", None)
        return committed

    def get_current_ids(self, account: MailboxAccount) -> set:
        if self._is_chatgpt_url_mail_account(account) or (
            self._is_chatgpt_password_totp_account(account)
            and self._has_mailapi_url(account)
        ):
            return self._get_mailapi_backend().get_current_ids(account)
        if self._is_chatgpt_password_totp_account(account):
            return set()
        ids = set()
        errors = []
        for mailbox in self._resolve_mailboxes_for_account(account):
            try:
                messages = self._list_messages(account, mailbox)
            except Exception as exc:
                errors.append(exc)
                continue
            ids.update(
                self._resolve_message_id(message, mailbox)
                for message in messages
            )
        if errors and self._is_icloud_web_account(account):
            raise RuntimeError(str(errors[0])) from errors[0]
        return ids

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        if self._is_chatgpt_url_mail_account(account) or (
            self._is_chatgpt_password_totp_account(account)
            and self._has_mailapi_url(account)
        ):
            return self._get_mailapi_backend().wait_for_code(
                account,
                keyword=keyword,
                timeout=timeout,
                before_ids=before_ids,
                code_pattern=code_pattern,
                **kwargs,
            )
        if self._is_chatgpt_password_totp_account(account):
            raise RuntimeError(
                "当前记录是 ChatGPT 密码 + MFA 登录凭据，无法读取邮箱验证码"
            )
        seen = {str(mid) for mid in (before_ids or set())}
        exclude_codes = {
            str(code).strip()
            for code in (kwargs.get("exclude_codes") or set())
            if str(code or "").strip()
        }

        def poll_once() -> Optional[str]:
            for mailbox in self._resolve_mailboxes_for_account(account):
                try:
                    messages = self._list_messages(account, mailbox)
                except Exception:
                    if self._is_icloud_web_account(account):
                        raise
                    continue

                for message in messages:
                    message_id = self._resolve_message_id(message, mailbox)
                    if message_id in seen:
                        continue
                    seen.add(message_id)

                    search_text = self._build_search_text(message)
                    if keyword and keyword.lower() not in search_text.lower():
                        continue

                    code = self._extract_code_from_message(message, code_pattern)
                    if code and code in exclude_codes:
                        continue
                    if code:
                        provider_label = (
                            "iCloud"
                            if self._is_icloud_web_account(account)
                            else "AppleMail"
                        )
                        self._log(f"[{provider_label}] {mailbox} 收到验证码: {code}")
                        return code
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )

    def get_totp_code(self, account: MailboxAccount) -> str:
        import re
        import requests
        from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

        extra = account.extra or {}
        configured_totp_url = str(extra.get("totp_url") or "").strip()
        if not configured_totp_url:
            raise RuntimeError("远程 2FA 地址为空")

        source_urls = [configured_totp_url]
        for key in ("mail_api_url", "mailapi_url"):
            candidate = str(extra.get(key) or "").strip()
            if candidate and candidate not in source_urls:
                source_urls.append(candidate)

        def build_request_url(source_url: str) -> str:
            try:
                parsed = urlsplit(source_url)
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    raise ValueError
                path = parsed.path.rstrip("/")
                if path.endswith("/view"):
                    path = f"{path[:-5]}/api/v1/2fa"
                elif not path.endswith("/api/v1/2fa"):
                    return source_url
                query = dict(parse_qsl(parsed.query, keep_blank_values=True))
                query.setdefault("email", str(account.email or "").strip())
                return urlunsplit(
                    (parsed.scheme, parsed.netloc, path, urlencode(query), "")
                )
            except Exception as exc:
                raise RuntimeError("远程 2FA 地址格式无效") from exc

        def fetch_once(source_url: str) -> tuple[str, float | None]:
            try:
                response = requests.get(
                    build_request_url(source_url),
                    headers={"accept": "application/json, text/plain;q=0.9"},
                    proxies=self.proxy,
                    timeout=15,
                )
                if int(response.status_code or 0) >= 400:
                    raise RuntimeError(f"HTTP {response.status_code}")
                response_text = str(getattr(response, "text", "") or "").strip()
                if re.fullmatch(r"[0-9]{6}", response_text):
                    return response_text, None
                try:
                    payload = response.json()
                except Exception:
                    raise RuntimeError("响应格式无效") from None
                if not isinstance(payload, dict) or payload.get("ok") is False:
                    raise RuntimeError("响应状态无效")
                response_email = str(payload.get("email") or "").strip().lower()
                if response_email and response_email != str(account.email or "").strip().lower():
                    raise RuntimeError("响应邮箱不匹配")
                code = str(payload.get("code") or "").strip()
                result_payload = None
                if not code:
                    expected_email = str(account.email or "").strip().lower()
                    results = payload.get("results")
                    if isinstance(results, list):
                        result_payload = next((
                            item
                            for item in results
                            if isinstance(item, dict)
                            and str(item.get("email") or "").strip().lower()
                            == expected_email
                        ), None)
                    if isinstance(result_payload, dict):
                        code = str(result_payload.get("code") or "").strip()
                if not re.fullmatch(r"\d{6}", code):
                    raise RuntimeError("验证码格式无效")
                try:
                    remaining = float(
                        (result_payload or payload).get("remaining")
                    )
                except (TypeError, ValueError):
                    remaining = None
                return code, remaining
            except Exception as exc:
                status_match = re.fullmatch(r"HTTP (\d{3})", str(exc))
                suffix = f": {status_match.group(0)}" if status_match else ""
                failure_message = f"远程 2FA 获取失败{suffix}"
            raise RuntimeError(failure_message)

        failures = []
        selected_source_url = ""
        code = ""
        remaining = None
        for source_url in source_urls:
            try:
                code, remaining = fetch_once(source_url)
                selected_source_url = source_url
                break
            except RuntimeError as exc:
                failures.append(exc)
        if not selected_source_url:
            raise failures[0]

        if selected_source_url != configured_totp_url:
            extra["totp_url"] = selected_source_url
            extra["mail_api_url"] = configured_totp_url
            extra["mailapi_url"] = configured_totp_url
            self._log("[2FA] 已自动识别反向 URL 字段顺序")

        if remaining is not None and 0 <= remaining <= 5:
            self._sleep_with_checkpoint(remaining + 0.5)
            code, _remaining = fetch_once(selected_source_url)
        self._log("[2FA] 已获取一次性验证码")
        return code


class LaoudoMailbox(BaseMailbox):
    """laoudo.com 邮箱服务"""

    def __init__(self, auth_token: str, email: str, account_id: str):
        self.auth = auth_token
        self._email = email
        self._account_id = account_id
        self.api = "https://laoudo.com/api/email"
        self._ua = "Mozilla/5.0"

    def get_email(self) -> MailboxAccount:
        if not self._email:
            raise RuntimeError(
                "Laoudo 邮箱未配置或已失效，请检查 laoudo_auth、laoudo_email、laoudo_account_id 配置，"
                "或切换到 tempmail_lol（无需配置）"
            )
        return MailboxAccount(email=self._email, account_id=self._account_id)

    def get_current_ids(self, account: MailboxAccount) -> set:
        from curl_cffi import requests as curl_requests

        try:
            r = curl_requests.get(
                f"{self.api}/list",
                params={
                    "accountId": account.account_id,
                    "allReceive": 0,
                    "emailId": 0,
                    "timeSort": 1,
                    "size": 50,
                    "type": 0,
                },
                headers={"authorization": self.auth, "user-agent": self._ua},
                timeout=15,
                impersonate="chrome131",
            )
            if r.status_code == 200:
                mails = r.json().get("data", {}).get("list", []) or []
                return {
                    m.get("id") or m.get("emailId")
                    for m in mails
                    if m.get("id") or m.get("emailId")
                }
        except Exception:
            pass
        return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        from curl_cffi import requests as curl_requests

        seen = set(before_ids) if before_ids else set()
        h = {"authorization": self.auth, "user-agent": self._ua}

        def poll_once() -> Optional[str]:
            try:
                r = curl_requests.get(
                    f"{self.api}/list",
                    params={
                        "accountId": account.account_id,
                        "allReceive": 0,
                        "emailId": 0,
                        "timeSort": 1,
                        "size": 50,
                        "type": 0,
                    },
                    headers=h,
                    timeout=15,
                    impersonate="chrome131",
                )
                if r.status_code == 200:
                    mails = r.json().get("data", {}).get("list", []) or []
                    for mail in mails:
                        mid = mail.get("id") or mail.get("emailId")
                        if not mid or mid in seen:
                            continue
                        seen.add(mid)
                        text = (
                            str(mail.get("subject", ""))
                            + " "
                            + str(mail.get("content") or mail.get("html") or "")
                        )
                        if keyword and keyword.lower() not in text.lower():
                            continue
                        code = self._safe_extract(text, code_pattern)
                        if code:
                            return code
            except Exception:
                pass
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=4,
            poll_once=poll_once,
        )


class AitreMailbox(BaseMailbox):
    """mail.aitre.cc 临时邮箱"""

    def __init__(self, email: str):
        self._email = email
        self.api = "https://mail.aitre.cc/api/tempmail"

    def get_email(self) -> MailboxAccount:
        return MailboxAccount(email=self._email)

    def get_current_ids(self, account: MailboxAccount) -> set:
        import requests

        try:
            r = requests.get(
                f"{self.api}/emails", params={"email": account.email}, timeout=10
            )
            emails = r.json().get("emails", [])
            return {str(m["id"]) for m in emails if "id" in m}
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        import requests

        seen = set(before_ids) if before_ids else set()
        last_check = None

        def poll_once() -> Optional[str]:
            nonlocal last_check
            params = {"email": account.email}
            if last_check:
                params["lastCheck"] = last_check
            try:
                r = requests.get(f"{self.api}/poll", params=params, timeout=10)
                data = r.json()
                last_check = data.get("lastChecked")
                if data.get("count", 0) > 0:
                    r2 = requests.get(
                        f"{self.api}/emails",
                        params={"email": account.email},
                        timeout=10,
                    )
                    for mail in r2.json().get("emails", []):
                        mid = str(mail.get("id", ""))
                        if mid in seen:
                            continue
                        seen.add(mid)
                        text = mail.get("preview", "") + mail.get("content", "")
                        if keyword and keyword.lower() not in text.lower():
                            continue
                        code = self._safe_extract(text, code_pattern)
                        if code:
                            return code
            except Exception:
                pass
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )


class TempMailLolMailbox(BaseMailbox):
    """tempmail.lol 免费临时邮箱（无需注册，自动生成）"""

    def __init__(self, proxy: str = None):
        self.api = "https://api.tempmail.lol/v2"
        self.proxy = build_requests_proxy_config(proxy)
        self._token = None
        self._email = None

    def get_email(self) -> MailboxAccount:
        import requests

        r = requests.post(
            f"{self.api}/inbox/create", json={}, proxies=self.proxy, timeout=15
        )
        data = r.json()
        email = data.get("address") or data.get("email", "")
        if not email:
            raise RuntimeError(f"tempmail.lol API 返回空邮箱: {data}")
        self._email = email
        self._token = data.get("token", "")
        print(f"[TempMailLol] 生成邮箱: {self._email}")
        return MailboxAccount(email=self._email, account_id=self._token)

    def get_current_ids(self, account: MailboxAccount) -> set:
        import requests

        try:
            r = requests.get(
                f"{self.api}/inbox",
                params={"token": account.account_id},
                proxies=self.proxy,
                timeout=10,
            )
            return {str(m["id"]) for m in r.json().get("emails", [])}
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        import requests

        seen = set(before_ids or [])
        otp_sent_at = kwargs.get("otp_sent_at")

        def poll_once() -> Optional[str]:
            try:
                r = requests.get(
                    f"{self.api}/inbox",
                    params={"token": account.account_id},
                    proxies=self.proxy,
                    timeout=10,
                )
                for mail in sorted(
                    r.json().get("emails", []),
                    key=lambda x: x.get("date", 0),
                    reverse=True,
                ):
                    mid = str(mail.get("id", ""))
                    if mid in seen:
                        continue
                    if otp_sent_at and mail.get("date", 0) / 1000 < otp_sent_at:
                        continue
                    seen.add(mid)
                    text = (
                        mail.get("subject", "")
                        + " "
                        + mail.get("body", "")
                        + " "
                        + mail.get("html", "")
                    )
                    if keyword and keyword.lower() not in text.lower():
                        continue
                    code = self._safe_extract(text, code_pattern)
                    if code:
                        return code
            except Exception:
                pass
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )


class SkyMailMailbox(BaseMailbox):
    """SkyMail / CloudMail 自建邮箱服务"""

    def __init__(self, api_base: str, auth_token: str, domain: str, proxy: str = None):
        self.api = (api_base or "").rstrip("/")
        self.auth_token = auth_token or ""
        self.domain = domain or ""
        self.proxy = build_requests_proxy_config(proxy)

    def _headers(self) -> dict:
        return {
            "accept": "application/json",
            "content-type": "application/json",
            "authorization": self.auth_token,
        }

    def _ensure_config(self) -> None:
        if not self.api or not self.auth_token or not self.domain:
            raise RuntimeError(
                "SkyMail 未配置完整：请设置 skymail_api_base、skymail_token、skymail_domain"
            )

    def _gen_prefix(self) -> str:
        import random
        import string

        length = random.randint(8, 13)
        chars = string.ascii_lowercase + string.digits
        return "".join(random.choice(chars) for _ in range(length))

    def get_email(self) -> MailboxAccount:
        import requests

        self._ensure_config()
        email = f"{self._gen_prefix()}@{self.domain}"
        payload = {"list": [{"email": email}]}
        r = requests.post(
            f"{self.api}/api/public/addUser",
            json=payload,
            headers=self._headers(),
            proxies=self.proxy,
            timeout=15,
        )
        if r.status_code != 200:
            raise RuntimeError(f"SkyMail 创建邮箱失败: {r.status_code} {r.text[:200]}")

        data = r.json()
        if data.get("code") != 200:
            raise RuntimeError(f"SkyMail 创建邮箱失败: {data}")

        self._log(f"[SkyMail] 生成邮箱: {email}")
        return MailboxAccount(email=email, account_id=email)

    def _list_mails(self, email: str) -> list:
        import requests

        payload = {
            "toEmail": email,
            "num": 1,
            "size": 20,
        }
        r = requests.post(
            f"{self.api}/api/public/emailList",
            json=payload,
            headers=self._headers(),
            proxies=self.proxy,
            timeout=15,
        )
        if r.status_code != 200:
            return []
        data = r.json()
        if data.get("code") != 200:
            return []
        return data.get("data") or []

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            mails = self._list_mails(account.account_id or account.email)
            ids = set()
            for i, msg in enumerate(mails):
                mid = msg.get("id") or msg.get("mailId") or msg.get("messageId")
                if mid:
                    ids.add(str(mid))
                else:
                    digest = (
                        str(msg.get("date") or msg.get("time") or "")
                        + "|"
                        + str(msg.get("subject") or "")
                    )
                    ids.add(f"idx-{i}-{digest}")
            return ids
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        target = account.account_id or account.email
        seen = set(before_ids or [])

        def poll_once() -> Optional[str]:
            try:
                mails = self._list_mails(target)
                for i, msg in enumerate(mails):
                    mid = msg.get("id") or msg.get("mailId") or msg.get("messageId")
                    if not mid:
                        digest = (
                            str(msg.get("date") or msg.get("time") or "")
                            + "|"
                            + str(msg.get("subject") or "")
                        )
                        mid = f"idx-{i}-{digest}"
                    mid = str(mid)
                    if mid in seen:
                        continue
                    seen.add(mid)

                    content = " ".join(
                        [
                            str(msg.get("subject") or ""),
                            str(msg.get("content") or ""),
                            str(msg.get("text") or ""),
                            str(msg.get("html") or ""),
                        ]
                    )
                    if keyword and keyword.lower() not in content.lower():
                        continue

                    code = self._safe_extract(content, code_pattern)
                    if code:
                        self._log(f"[SkyMail] 命中验证码: {code}")
                        return code
            except Exception:
                pass
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )


class CloudMailMailbox(BaseMailbox):
    """CloudMail 自建邮箱服务（genToken + emailList）"""

    _token_lock = threading.Lock()
    _token_cache: dict[str, tuple[str, float]] = {}
    _seen_ids_lock = threading.Lock()
    _seen_ids: dict[str, set[str]] = {}

    def __init__(
        self,
        api_base: str,
        admin_email: str,
        admin_password: str,
        domain: Any = "",
        subdomain: str = "",
        timeout: int = 30,
        proxy: str = None,
    ):
        self.api = str(api_base or "").rstrip("/")
        self.admin_email = str(admin_email or "").strip()
        self.admin_password = str(admin_password or "").strip()
        self.domain = domain
        self.subdomain = str(subdomain or "").strip()
        self.timeout = max(int(timeout or 30), 5)
        self.proxy = build_requests_proxy_config(proxy)

    @staticmethod
    def _extract_domain_from_url(url: str) -> str:
        from urllib.parse import urlparse

        parsed = urlparse(str(url or ""))
        host = (parsed.netloc or parsed.path.split("/")[0] or "").strip()
        if ":" in host:
            host = host.split(":", 1)[0].strip()
        return host

    @staticmethod
    def _normalize_domain(value: str) -> str:
        domain = str(value or "").strip().lstrip("@")
        if "://" in domain:
            domain = CloudMailMailbox._extract_domain_from_url(domain)
        return domain.strip()

    def _domain_candidates(self) -> list[str]:
        candidates: list[str] = []

        if isinstance(self.domain, (list, tuple, set)):
            iterable = self.domain
        else:
            raw = str(self.domain or "").strip()
            parsed = None
            if raw.startswith("[") and raw.endswith("]"):
                try:
                    parsed = json.loads(raw)
                except Exception:
                    parsed = None
            if isinstance(parsed, list):
                iterable = parsed
            elif raw:
                normalized = (
                    raw.replace(";", "\n")
                    .replace(",", "\n")
                    .replace("|", "\n")
                    .splitlines()
                )
                iterable = [item for item in normalized if item]
            else:
                iterable = []

        for item in iterable:
            normalized = self._normalize_domain(item)
            if normalized:
                candidates.append(normalized)

        if not candidates:
            inferred = self._normalize_domain(self._extract_domain_from_url(self.api))
            if inferred:
                candidates.append(inferred)
        return candidates

    def _resolve_admin_email(self) -> str:
        if self.admin_email:
            return self.admin_email
        domains = self._domain_candidates()
        if domains:
            return f"admin@{domains[0]}"
        return "admin@example.com"

    def _cache_key(self) -> str:
        return f"{self.api}|{self._resolve_admin_email()}|{self.admin_password}"

    def _ensure_config(self) -> None:
        if not self.api or not self.admin_password:
            raise RuntimeError(
                "CloudMail 未配置完整：请设置 cloudmail_api_base 与 cloudmail_admin_password"
            )

    def _headers(self, token: str = "") -> dict:
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
        }
        if token:
            headers["authorization"] = token
        return headers

    def _generate_token(self) -> str:
        import requests

        self._ensure_config()
        payload = {
            "email": self._resolve_admin_email(),
            "password": self.admin_password,
        }
        r = requests.post(
            f"{self.api}/api/public/genToken",
            json=payload,
            headers=self._headers(),
            proxies=self.proxy,
            timeout=self.timeout,
        )
        if r.status_code != 200:
            raise RuntimeError(
                f"CloudMail 生成 token 失败: {r.status_code} {str(r.text or '')[:200]}"
            )

        try:
            data = r.json()
        except Exception:
            data = {}
        if data.get("code") != 200:
            raise RuntimeError(f"CloudMail 生成 token 失败: {data}")
        token = ((data.get("data") or {}).get("token") or "").strip()
        if not token:
            raise RuntimeError("CloudMail 生成 token 失败: 响应未返回 token")
        return token

    def _get_token(self, *, force_refresh: bool = False) -> str:
        cache_key = self._cache_key()
        now = time.time()
        with CloudMailMailbox._token_lock:
            if not force_refresh:
                cached = CloudMailMailbox._token_cache.get(cache_key)
                if cached and now < cached[1]:
                    return cached[0]

            token = self._generate_token()
            CloudMailMailbox._token_cache[cache_key] = (token, now + 3600)
            return token

    def _list_mails(self, email: str, *, retry_auth: bool = True) -> list:
        import requests

        token = self._get_token()
        payload = {
            "toEmail": email,
            "timeSort": "desc",
        }
        r = requests.post(
            f"{self.api}/api/public/emailList",
            json=payload,
            headers=self._headers(token),
            proxies=self.proxy,
            timeout=self.timeout,
        )
        if r.status_code == 401 and retry_auth:
            token = self._get_token(force_refresh=True)
            r = requests.post(
                f"{self.api}/api/public/emailList",
                json=payload,
                headers=self._headers(token),
                proxies=self.proxy,
                timeout=self.timeout,
            )
        if r.status_code != 200:
            return []

        try:
            data = r.json()
        except Exception:
            data = {}
        if data.get("code") != 200:
            return []
        return data.get("data") or []

    def _gen_prefix(self) -> str:
        import random
        import string

        first = random.choice(string.ascii_lowercase)
        rest = "".join(random.choices(string.ascii_lowercase + string.digits, k=9))
        return first + rest

    def _build_email(self) -> str:
        domains = self._domain_candidates()
        if not domains:
            raise RuntimeError("CloudMail 未配置可用域名")
        domain = random.choice(domains)
        if self.subdomain:
            domain = f"{self.subdomain}.{domain}"
        return f"{self._gen_prefix()}@{domain}"

    @staticmethod
    def _parse_message_timestamp(message: dict) -> Optional[float]:
        from datetime import datetime

        keys = [
            "time",
            "date",
            "created",
            "createdAt",
            "created_at",
            "receivedAt",
            "received_at",
            "sendTime",
            "timestamp",
        ]
        for key in keys:
            value = message.get(key)
            if value in (None, ""):
                continue
            if isinstance(value, (int, float)):
                numeric = float(value)
                return numeric / 1000 if numeric > 10_000_000_000 else numeric
            text = str(value).strip()
            if not text:
                continue
            try:
                numeric = float(text)
                return numeric / 1000 if numeric > 10_000_000_000 else numeric
            except (TypeError, ValueError):
                pass
            try:
                return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
        return None

    @staticmethod
    def _mail_id(message: dict, index: int = 0) -> str:
        for key in ("emailId", "id", "mailId", "messageId"):
            value = message.get(key)
            if value not in (None, ""):
                return str(value)
        digest = (
            str(message.get("date") or message.get("time") or "")
            + "|"
            + str(message.get("subject") or "")
        )
        return f"idx-{index}-{digest}"

    def _remember_seen_id(self, email: str, message_id: str) -> None:
        with CloudMailMailbox._seen_ids_lock:
            CloudMailMailbox._seen_ids.setdefault(email, set()).add(message_id)

    def _load_seen_ids(self, email: str) -> set[str]:
        with CloudMailMailbox._seen_ids_lock:
            return set(CloudMailMailbox._seen_ids.get(email, set()))

    def get_email(self) -> MailboxAccount:
        self._ensure_config()
        email = self._build_email()
        self._log(f"[CloudMail] 生成邮箱: {email}")
        return MailboxAccount(email=email, account_id=email)

    def get_current_ids(self, account: MailboxAccount) -> set:
        target = account.account_id or account.email
        try:
            mails = self._list_mails(target)
            return {self._mail_id(msg, idx) for idx, msg in enumerate(mails)}
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        target = account.account_id or account.email
        seen = set(before_ids or set())
        seen.update(self._load_seen_ids(target))
        otp_sent_at = kwargs.get("otp_sent_at")
        exclude_codes = {
            str(code).strip()
            for code in (kwargs.get("exclude_codes") or set())
            if str(code or "").strip()
        }

        def poll_once() -> Optional[str]:
            try:
                mails = self._list_mails(target)
                for idx, msg in enumerate(mails):
                    mid = self._mail_id(msg, idx)
                    if mid in seen:
                        continue
                    seen.add(mid)
                    self._remember_seen_id(target, mid)

                    msg_ts = self._parse_message_timestamp(msg)
                    if otp_sent_at and msg_ts and msg_ts < float(otp_sent_at):
                        continue

                    content = " ".join(
                        [
                            str(msg.get("subject") or ""),
                            str(msg.get("content") or ""),
                            str(msg.get("text") or ""),
                            str(msg.get("html") or ""),
                        ]
                    )
                    if keyword and keyword.lower() not in content.lower():
                        continue
                    code = self._safe_extract(content, code_pattern)
                    if code and code in exclude_codes:
                        continue
                    if code:
                        self._log(f"[CloudMail] 命中验证码: {code}")
                        return code
            except Exception:
                pass
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )


class DuckMailMailbox(BaseMailbox):
    """DuckMail 自动生成邮箱（随机创建账号）"""

    def __init__(
        self,
        api_url: str = "https://www.duckmail.sbs",
        provider_url: str = "https://api.duckmail.sbs",
        bearer: str = "kevin273945",
        domain: str = "",
        api_key: str = "",
        proxy: str = None,
    ):
        self.api = (api_url or "https://www.duckmail.sbs").rstrip("/")
        self.provider_url = (provider_url or "https://api.duckmail.sbs").rstrip("/")
        self.bearer = bearer or "kevin273945"
        self.domain = str(domain or "").strip()
        self.api_key = str(api_key or "").strip()
        self.proxy = build_requests_proxy_config(proxy)
        self._token = None
        self._address = None
        # 如果配置了 API Key，直接请求 DuckMail API；否则走前端代理
        self._direct = bool(self.api_key)

    def _proxy_headers(self) -> dict:
        return {
            "authorization": f"Bearer {self.bearer}",
            "content-type": "application/json",
            "x-api-provider-base-url": self.provider_url,
        }

    def _direct_headers(self, token: str = "") -> dict:
        auth = token or self.api_key
        return {
            "authorization": f"Bearer {auth}",
            "content-type": "application/json",
        }

    def _request(self, method: str, endpoint: str, token: str = "", **kwargs):
        """统一请求方法，根据模式选择直连或代理"""
        import requests

        if self._direct:
            url = f"{self.provider_url}{endpoint}"
            headers = self._direct_headers(token)
        else:
            from urllib.parse import quote

            url = f"{self.api}/api/mail?endpoint={quote(endpoint, safe='')}"
            headers = (
                self._proxy_headers()
                if not token
                else {
                    "authorization": f"Bearer {token}",
                    "x-api-provider-base-url": self.provider_url,
                }
            )
        r = requests.request(
            method, url, headers=headers, proxies=self.proxy, timeout=15, **kwargs
        )
        return r

    def get_email(self) -> MailboxAccount:
        import random, string

        username = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
        password = "Test" + "".join(random.choices(string.digits, k=8)) + "!"
        domain = self.domain or self.provider_url.replace("https://api.", "").replace(
            "https://", ""
        )
        address = f"{username}@{domain}"
        print(f"[DuckMail] 创建账号: {address} direct={self._direct}")
        # 创建账号
        r = self._request(
            "POST", "/accounts", json={"address": address, "password": password}
        )
        if r.status_code >= 400 or not r.text.strip().startswith("{"):
            raise RuntimeError(
                f"[DuckMail] 创建账号失败: HTTP {r.status_code} body={r.text[:300]}"
            )
        data = r.json()
        self._address = data.get("address", address)
        # 登录获取 token
        r2 = self._request(
            "POST", "/token", json={"address": self._address, "password": password}
        )
        if r2.status_code >= 400 or not r2.text.strip().startswith(("{", "[")):
            raise RuntimeError(
                f"[DuckMail] 登录失败: HTTP {r2.status_code} body={r2.text[:300]}"
            )
        self._token = r2.json().get("token", "")
        return MailboxAccount(email=self._address, account_id=self._token)

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            r = self._request("GET", "/messages?page=1", token=account.account_id)
            return {str(m["id"]) for m in r.json().get("hydra:member", [])}
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        from datetime import datetime
        import re

        seen = set(before_ids or [])
        exclude_codes = {
            str(code).strip()
            for code in (kwargs.get("exclude_codes") or set())
            if str(code or "").strip()
        }
        otp_sent_at = kwargs.get("otp_sent_at")

        def _parse_message_timestamp(*values) -> Optional[float]:
            for value in values:
                if value in (None, ""):
                    continue
                if isinstance(value, (int, float)):
                    numeric = float(value)
                    return numeric / 1000 if numeric > 10_000_000_000 else numeric
                text = str(value).strip()
                if not text:
                    continue
                try:
                    numeric = float(text)
                    return numeric / 1000 if numeric > 10_000_000_000 else numeric
                except (TypeError, ValueError):
                    pass
                try:
                    normalized = text.replace("Z", "+00:00")
                    return datetime.fromisoformat(normalized).timestamp()
                except ValueError:
                    continue
            return None

        def poll_once() -> Optional[str]:
            try:
                r = self._request("GET", "/messages?page=1", token=account.account_id)
                msgs = r.json().get("hydra:member", [])
                for msg in msgs:
                    mid = str(msg.get("id") or msg.get("msgid") or "")
                    if mid in seen:
                        continue
                    seen.add(mid)
                    # 请求邮件详情获取完整 text
                    try:
                        r2 = self._request(
                            "GET", f"/messages/{mid}", token=account.account_id
                        )
                        detail = r2.json()
                        body = (
                            str(detail.get("text") or "")
                            + " "
                            + str(detail.get("subject") or "")
                        )
                    except Exception:
                        detail = {}
                        body = str(msg.get("subject") or "")
                    message_ts = _parse_message_timestamp(
                        detail.get("createdAt"),
                        detail.get("created_at"),
                        detail.get("receivedAt"),
                        detail.get("received_at"),
                        detail.get("date"),
                        detail.get("created"),
                        msg.get("createdAt"),
                        msg.get("created_at"),
                        msg.get("receivedAt"),
                        msg.get("received_at"),
                        msg.get("date"),
                        msg.get("created"),
                    )
                    if otp_sent_at and message_ts and message_ts < float(otp_sent_at):
                        continue
                    body = re.sub(
                        r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", "", body
                    )
                    code = self._safe_extract(body, code_pattern)
                    if code and code in exclude_codes:
                        continue
                    if code:
                        return code
            except Exception:
                pass
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )


class MaliAPIMailbox(BaseMailbox):
    """YYDS Mail / MaliAPI 临时邮箱服务"""

    def __init__(
        self,
        api_url: str = "https://maliapi.215.im/v1",
        api_key: str = "",
        domain: str = "",
        auto_domain_strategy: str = "",
        proxy: str = None,
    ):
        self.api = (api_url or "https://maliapi.215.im/v1").rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.domain = str(domain or "").strip()
        self.auto_domain_strategy = str(auto_domain_strategy or "").strip()
        self.proxy = build_requests_proxy_config(proxy)
        self._email = None
        self._temp_token = None

    def _headers(self, bearer: str = "") -> dict[str, str]:
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
        }
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict = None,
        params: dict = None,
        bearer: str = "",
    ) -> Any:
        import requests

        response = requests.request(
            method,
            f"{self.api}{path}",
            headers=self._headers(bearer),
            json=json_body,
            params=params,
            proxies=self.proxy,
            timeout=15,
        )
        try:
            payload = response.json()
        except Exception:
            payload = {}

        if response.status_code >= 400:
            error = response.text or f"HTTP {response.status_code}"
            error_code = ""
            if isinstance(payload, dict):
                error = str(payload.get("error") or error).strip()
                error_code = str(payload.get("errorCode") or "").strip()
            if error_code:
                raise RuntimeError(f"MaliAPI 请求失败: {error} ({error_code})")
            raise RuntimeError(f"MaliAPI 请求失败: {str(error).strip()}")

        if isinstance(payload, dict):
            if payload.get("success") is False:
                error = str(payload.get("error") or "unknown error").strip()
                error_code = str(payload.get("errorCode") or "").strip()
                if error_code:
                    raise RuntimeError(f"MaliAPI 请求失败: {error} ({error_code})")
                raise RuntimeError(f"MaliAPI 请求失败: {error}")
            if "data" in payload:
                return payload.get("data")
        return payload

    def _ensure_api_key(self) -> None:
        if not self.api_key:
            raise RuntimeError("MaliAPI 未配置：请在全局设置中填写 maliapi_api_key")

    def _list_messages(self, account: MailboxAccount) -> list[dict]:
        data = self._request("GET", "/messages", params={"address": account.email})
        if isinstance(data, dict):
            messages = data.get("messages", [])
        else:
            messages = data
        return [item for item in (messages or []) if isinstance(item, dict)]

    def _get_message_detail(self, message_id: str) -> dict:
        data = self._request("GET", f"/messages/{message_id}")
        if isinstance(data, dict) and isinstance(data.get("message"), dict):
            return data["message"]
        return data if isinstance(data, dict) else {}

    def get_email(self) -> MailboxAccount:
        self._ensure_api_key()
        body = {}
        if self.domain:
            body["domain"] = self.domain
        if self.auto_domain_strategy:
            body["autoDomainStrategy"] = self.auto_domain_strategy

        data = self._request("POST", "/accounts", json_body=body)
        if not isinstance(data, dict):
            raise RuntimeError(f"MaliAPI 返回异常: {data}")

        email = str(data.get("address") or data.get("email") or "").strip()
        temp_token = str(
            data.get("tempToken") or data.get("temp_token") or data.get("token") or ""
        ).strip()
        inbox_id = str(data.get("id") or "").strip()
        if not email:
            raise RuntimeError(f"MaliAPI 返回空邮箱: {data}")

        self._email = email
        self._temp_token = temp_token
        self._log(f"[MaliAPI] 生成邮箱: {email}")
        return MailboxAccount(
            email=email,
            account_id=temp_token or inbox_id or email,
            extra={
                "provider": "maliapi",
                "temp_token": temp_token,
                "inbox_id": inbox_id,
            },
        )

    def get_current_ids(self, account: MailboxAccount) -> set:
        self._ensure_api_key()
        try:
            return {
                str(message.get("id"))
                for message in self._list_messages(account)
                if message.get("id") is not None
            }
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        import re

        self._ensure_api_key()
        seen = {str(mid) for mid in (before_ids or set())}

        def poll_once() -> Optional[str]:
            try:
                for message in self._list_messages(account):
                    message_id = str(message.get("id") or "").strip()
                    if not message_id or message_id in seen:
                        continue
                    seen.add(message_id)

                    try:
                        detail = self._get_message_detail(message_id)
                    except Exception:
                        detail = message

                    search_text = " ".join(
                        [
                            str(detail.get("subject") or message.get("subject") or ""),
                            str(detail.get("text") or ""),
                            str(detail.get("html") or ""),
                            str(message.get("subject") or ""),
                            str(message.get("snippet") or ""),
                        ]
                    ).strip()
                    search_text = self._yyds_decode_raw_content(search_text) or search_text
                    search_text = re.sub(
                        r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
                        "",
                        search_text,
                    )
                    if keyword and keyword.lower() not in search_text.lower():
                        continue

                    code = self._yyds_safe_extract(search_text, code_pattern)
                    if code:
                        self._log(f"[MaliAPI] 收到验证码: {code}")
                        return code
            except Exception:
                pass
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )


class GPTMailMailbox(BaseMailbox):
    """GPTMail 临时邮箱服务"""

    def __init__(
        self,
        api_url: str = "https://mail.chatgpt.org.uk",
        api_key: str = "",
        domain: str = "",
        proxy: str = None,
    ):
        self.api = (api_url or "https://mail.chatgpt.org.uk").rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.domain = self._normalize_domain(domain)
        self.proxy = build_requests_proxy_config(proxy)
        self._email = None

    @staticmethod
    def _normalize_domain(value: Any) -> str:
        domain = str(value or "").strip().lower()
        if domain.startswith("@"):
            domain = domain[1:]
        return domain

    @staticmethod
    def _generate_local_part() -> str:
        import string

        prefix = "".join(random.choices(string.ascii_lowercase, k=6))
        suffix = "".join(random.choices(string.digits, k=4))
        return f"{prefix}{suffix}"

    def _headers(self) -> dict[str, str]:
        headers = {"accept": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        timeout: int = 15,
    ) -> Any:
        import requests

        response = requests.request(
            method,
            f"{self.api}{path}",
            params=params,
            json=json_body,
            headers=self._headers(),
            proxies=self.proxy,
            timeout=timeout,
        )
        try:
            payload = response.json()
        except Exception as exc:
            preview = (response.text or "")[:200]
            raise RuntimeError(
                f"GPTMail API {path} 返回非 JSON: HTTP {response.status_code} {preview}"
            ) from exc

        if response.status_code >= 400:
            error = payload.get("error") if isinstance(payload, dict) else ""
            message = str(error or response.text or f"HTTP {response.status_code}").strip()
            raise RuntimeError(f"GPTMail API {path} 失败: {message}")

        if isinstance(payload, dict) and payload.get("success") is False:
            error = str(payload.get("error") or "unknown error").strip()
            raise RuntimeError(f"GPTMail API {path} 失败: {error}")

        if isinstance(payload, dict) and "data" in payload:
            return payload.get("data")
        return payload

    def _list_messages(self, email: str) -> list[dict]:
        data = self._request_json("GET", "/api/emails", params={"email": email}, timeout=10)
        if isinstance(data, dict):
            messages = data.get("emails", [])
        else:
            messages = data
        return [item for item in (messages or []) if isinstance(item, dict)]

    def _get_message_detail(self, message_id: str) -> dict[str, Any]:
        data = self._request_json("GET", f"/api/email/{message_id}", timeout=10)
        return data if isinstance(data, dict) else {}

    def get_email(self) -> MailboxAccount:
        if self.domain:
            email = f"{self._generate_local_part()}@{self.domain}"
            self._email = email
            self._log(f"[GPTMail] 本地拼装邮箱: {email}")
            return MailboxAccount(
                email=email,
                account_id=email,
                extra={"provider": "gptmail", "domain": self.domain, "local_address": True},
            )

        data = self._request_json("GET", "/api/generate-email")
        if not isinstance(data, dict):
            raise RuntimeError(f"GPTMail 返回异常: {data}")

        email = str(data.get("email") or "").strip()
        if not email:
            raise RuntimeError(f"GPTMail 返回空邮箱: {data}")

        self._email = email
        self._log(f"[GPTMail] 生成邮箱: {email}")
        return MailboxAccount(
            email=email,
            account_id=email,
            extra={"provider": "gptmail"},
        )

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            return {
                str(message.get("id"))
                for message in self._list_messages(account.email)
                if message.get("id") is not None
            }
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        import re

        seen = {str(mid) for mid in (before_ids or set())}
        exclude_codes = {
            str(code) for code in (kwargs.get("exclude_codes") or set()) if code
        }

        def poll_once() -> Optional[str]:
            try:
                messages = self._list_messages(account.email)
                for message in messages:
                    message_id = str(message.get("id") or "").strip()
                    if not message_id or message_id in seen:
                        continue
                    seen.add(message_id)

                    try:
                        detail = self._get_message_detail(message_id)
                    except Exception:
                        detail = {}

                    search_text = " ".join(
                        [
                            str(message.get("subject") or ""),
                            str(message.get("from_address") or ""),
                            str(message.get("content") or ""),
                            str(message.get("html_content") or ""),
                            str(detail.get("subject") or ""),
                            str(detail.get("content") or ""),
                            str(detail.get("html_content") or ""),
                            str(detail.get("raw_headers") or ""),
                        ]
                    ).strip()
                    search_text = self._decode_raw_content(search_text) or search_text
                    search_text = re.sub(
                        r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
                        "",
                        search_text,
                    )
                    if keyword and keyword.lower() not in search_text.lower():
                        continue

                    code = self._safe_extract(search_text, code_pattern)
                    if code and code in exclude_codes:
                        continue
                    if code:
                        self._log(f"[GPTMail] 收到验证码: {code}")
                        return code
            except Exception:
                pass
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )


class OpenTrashMailMailbox(BaseMailbox):
    """OpenTrashMail 临时邮箱服务"""

    def __init__(
        self,
        api_url: str = "",
        domain: str = "",
        password: str = "",
        proxy: str = None,
    ):
        self.api = str(api_url or "").strip().rstrip("/")
        self.domain = self._normalize_domain(domain)
        self.password = str(password or "").strip()
        self.proxy = build_requests_proxy_config(proxy)

    @staticmethod
    def _normalize_domain(value: Any) -> str:
        domain = str(value or "").strip().lower()
        if domain.startswith("@"):
            domain = domain[1:]
        return domain

    @staticmethod
    def _generate_local_part() -> str:
        import string

        prefix = "".join(random.choices(string.ascii_lowercase, k=8))
        suffix = "".join(random.choices(string.digits, k=2))
        return f"{prefix}{suffix}"

    def _headers(self) -> dict[str, str]:
        return {"accept": "application/json, text/plain, */*"}

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        timeout: int = 15,
    ):
        import requests

        request_params = dict(params or {})
        if self.password and "password" not in request_params:
            request_params["password"] = self.password

        return requests.request(
            method,
            f"{self.api}{path}",
            params=request_params or None,
            json=None,
            headers=self._headers(),
            proxies=self.proxy,
            timeout=timeout,
        )

    def _require_api(self) -> None:
        if not self.api:
            raise RuntimeError(
                "OpenTrashMail 未配置 API URL，请检查 opentrashmail_api_url"
            )

    def _build_email_path(self, email: str) -> str:
        from urllib.parse import quote

        return quote(str(email or "").strip(), safe="@")

    def _parse_random_email(self, html_text: str) -> str:
        import re

        text = str(html_text or "")
        if not text:
            return ""

        match = re.search(r"/address/([^\"'<>\s]+@[^\"'<>\s]+)", text, re.IGNORECASE)
        if match:
            return str(match.group(1) or "").strip()

        match = re.search(
            r"([a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,})",
            text,
            re.IGNORECASE,
        )
        if match:
            return str(match.group(1) or "").strip()
        return ""

    def _list_messages(self, email: str) -> list[dict[str, Any]]:
        self._require_api()
        response = self._request(
            "GET",
            f"/json/{self._build_email_path(email)}",
            timeout=10,
        )
        if response.status_code == 404:
            return []
        try:
            payload = response.json()
        except Exception as exc:
            preview = (response.text or "")[:200]
            raise RuntimeError(
                f"OpenTrashMail 收件箱返回非 JSON: HTTP {response.status_code} {preview}"
            ) from exc

        if response.status_code >= 400:
            if isinstance(payload, dict) and payload.get("error"):
                error = payload.get("error")
            else:
                error = response.text or f"HTTP {response.status_code}"
            raise RuntimeError(f"OpenTrashMail 收件箱查询失败: {str(error).strip()}")

        if not payload:
            return []

        messages: list[dict[str, Any]] = []
        if isinstance(payload, dict):
            for message_id, item in payload.items():
                if not isinstance(item, dict):
                    continue
                message = dict(item)
                message.setdefault("id", str(message_id))
                messages.append(message)
        elif isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict):
                    messages.append(item)
        return messages

    def _get_message_detail(self, email: str, message_id: str) -> dict[str, Any]:
        self._require_api()
        response = self._request(
            "GET",
            f"/json/{self._build_email_path(email)}/{message_id}",
            timeout=10,
        )
        if response.status_code == 404:
            return {}
        try:
            payload = response.json()
        except Exception as exc:
            preview = (response.text or "")[:200]
            raise RuntimeError(
                f"OpenTrashMail 邮件详情返回非 JSON: HTTP {response.status_code} {preview}"
            ) from exc

        if response.status_code >= 400:
            if isinstance(payload, dict) and payload.get("error"):
                error = payload.get("error")
            else:
                error = response.text or f"HTTP {response.status_code}"
            raise RuntimeError(f"OpenTrashMail 邮件详情查询失败: {str(error).strip()}")

        return payload if isinstance(payload, dict) else {}

    def get_email(self) -> MailboxAccount:
        if self.domain:
            email = f"{self._generate_local_part()}@{self.domain}"
            self._log(f"[OpenTrashMail] 本地拼装邮箱: {email}")
            return MailboxAccount(
                email=email,
                account_id=email,
                extra={
                    "provider": "opentrashmail",
                    "domain": self.domain,
                    "local_address": True,
                },
            )

        self._require_api()
        response = self._request("GET", "/api/random", timeout=15)
        if response.status_code >= 400:
            raise RuntimeError(
                f"OpenTrashMail 随机邮箱生成失败: HTTP {response.status_code}"
            )

        email = self._parse_random_email(response.text)
        if not email:
            preview = (response.text or "")[:200]
            raise RuntimeError(f"OpenTrashMail 未能解析随机邮箱: {preview}")

        self._log(f"[OpenTrashMail] 生成邮箱: {email}")
        return MailboxAccount(
            email=email,
            account_id=email,
            extra={"provider": "opentrashmail"},
        )

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            return {
                str(message.get("id"))
                for message in self._list_messages(account.email)
                if message.get("id") is not None
            }
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        import re

        seen = {str(mid) for mid in (before_ids or set())}
        exclude_codes = {
            str(code) for code in (kwargs.get("exclude_codes") or set()) if code
        }

        def poll_once() -> Optional[str]:
            try:
                messages = self._list_messages(account.email)
                for message in messages:
                    message_id = str(message.get("id") or "").strip()
                    if not message_id or message_id in seen:
                        continue
                    seen.add(message_id)

                    detail = self._get_message_detail(account.email, message_id)
                    parsed = detail.get("parsed") if isinstance(detail, dict) else {}
                    if not isinstance(parsed, dict):
                        parsed = {}

                    decoded_raw = self._decode_raw_content(detail.get("raw") or "")
                    search_text = " ".join(
                        [
                            str(message.get("subject") or ""),
                            str(message.get("from") or ""),
                            str(message.get("body") or ""),
                            str(detail.get("from") or ""),
                            str(parsed.get("subject") or ""),
                            str(parsed.get("body") or ""),
                            str(parsed.get("htmlbody") or ""),
                            decoded_raw,
                        ]
                    ).strip()
                    search_text = re.sub(
                        r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
                        "",
                        search_text,
                    )
                    if keyword and keyword.lower() not in search_text.lower():
                        continue

                    code = self._safe_extract(search_text, code_pattern)
                    if code and code in exclude_codes:
                        continue
                    if code:
                        self._log(f"[OpenTrashMail] 收到验证码: {code}")
                        return code
            except Exception:
                pass
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )


class CFWorkerMailbox(BaseMailbox):
    """Cloudflare Worker 自建临时邮箱服务"""

    def __init__(
        self,
        api_url: str,
        admin_token: str = "",
        domain: str = "",
        domain_override: str = "",
        domains: Any = None,
        enabled_domains: Any = None,
        subdomain: str = "",
        domain_level_count: Any = 2,
        random_subdomain: Any = False,
        random_name_subdomain: Any = False,
        fingerprint: str = "",
        custom_auth: str = "",
        proxy: str = None,
    ):
        self.api = api_url.rstrip("/")
        self.admin_token = admin_token
        self.domain = self._normalize_domain(domain)
        self.domain_override = self._normalize_domain(domain_override)
        self.domains = self._parse_domains(domains)
        raw_enabled_domains = self._parse_domains(enabled_domains)
        if self.domains:
            allowed = set(self.domains)
            self.enabled_domains = [d for d in raw_enabled_domains if d in allowed]
        else:
            self.enabled_domains = raw_enabled_domains
        self.subdomain = self._normalize_subdomain(subdomain)
        self.domain_level_count = self._parse_domain_level_count(domain_level_count)
        self.random_subdomain = self._to_bool(random_subdomain)
        self.random_name_subdomain = self._to_bool(random_name_subdomain)
        self.fingerprint = fingerprint
        self.custom_auth = custom_auth
        self.proxy = build_requests_proxy_config(proxy)
        self._token = None

    def _headers(self) -> dict:
        h = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "x-admin-auth": self.admin_token,
        }
        if self.fingerprint:
            h["x-fingerprint"] = self.fingerprint
        if self.custom_auth:
            h["x-custom-auth"] = self.custom_auth
        return h

    def _ensure_api_configured(self) -> None:
        if not self.api:
            raise RuntimeError("CF Worker API URL 未配置")

    def _read_json(self, response, action: str):
        try:
            return response.json()
        except Exception:
            body = (response.text or "").strip()
            snippet = body[:200] if body else "<empty>"
            raise RuntimeError(
                f"CF Worker {action} 返回非 JSON 响应: HTTP {response.status_code}, body={snippet}"
            )

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        payload: Optional[dict] = None,
        timeout: int = 15,
    ):
        import requests

        url = f"{self.api}{path}"
        response = requests.request(
            method,
            url,
            params=params,
            json=payload,
            headers=self._headers(),
            proxies=self.proxy,
            timeout=timeout,
        )
        body = (response.text or "").strip()
        preview = body[:200] or "<empty>"

        if response.status_code >= 400:
            if "private site password" in body.lower():
                raise RuntimeError(
                    "CFWorker API 需要私有站点密码，请配置 cfworker_custom_auth"
                )
            raise RuntimeError(
                f"CFWorker API {path} 失败: HTTP {response.status_code} {preview}"
            )

        try:
            return response.json()
        except Exception as e:
            raise RuntimeError(
                f"CFWorker API {path} 返回非 JSON: HTTP {response.status_code} {preview}"
            ) from e

    def _generate_local_part(self) -> str:
        import string

        # 避免纯数字开头，提高邮箱格式“像真人”的程度
        prefix = "".join(random.choices(string.ascii_lowercase, k=6))
        suffix = "".join(random.choices(string.digits, k=4))
        return f"{prefix}{suffix}"

    @staticmethod
    def _normalize_domain(domain: Any) -> str:
        value = str(domain or "").strip().lower()
        if value.startswith("@"):
            value = value[1:]
        return value

    @staticmethod
    def _normalize_subdomain(value: Any) -> str:
        sub = str(value or "").strip().lower().strip(".")
        if sub.startswith("@"):
            sub = sub[1:]
        parts = [part for part in sub.split(".") if part]
        return ".".join(parts)

    @staticmethod
    def _to_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        text = str(value or "").strip().lower()
        return text in {"1", "true", "yes", "on"}

    @staticmethod
    def _parse_domain_level_count(value: Any) -> int:
        try:
            parsed = int(str(value or "").strip() or "2")
        except (TypeError, ValueError):
            return 2
        return parsed if parsed >= 2 else 2

    @classmethod
    def _parse_domains(cls, value: Any) -> list[str]:
        if not value:
            return []

        items: list[Any]
        if isinstance(value, (list, tuple, set)):
            items = list(value)
        elif isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                items = parsed
            else:
                items = [
                    part for chunk in text.splitlines() for part in chunk.split(",")
                ]
        else:
            items = [value]

        domains: list[str] = []
        seen = set()
        for item in items:
            domain = cls._normalize_domain(item)
            if not domain or domain in seen:
                continue
            seen.add(domain)
            domains.append(domain)
        return domains

    def _pick_domain(self) -> str:
        if self.domain_override:
            return self.domain_override
        if self.enabled_domains:
            return random.choice(self.enabled_domains)
        return self.domain

    def _generate_subdomain_label(self, length: int = 6) -> str:
        import string

        alphabet = string.ascii_lowercase + string.digits
        return "".join(random.choices(alphabet, k=length))

    def _compose_domain(self, base_domain: str) -> str:
        domain = self._normalize_domain(base_domain)
        if not domain:
            return ""

        sub_parts: list[str] = []
        if self.random_name_subdomain:
            try:
                import names
                import random

                name_func = random.choice([names.get_first_name, names.get_last_name])
                sub_parts.append(name_func().lower().replace(" ", ""))
            except ImportError:
                sub_parts.append(self._generate_subdomain_label())
        elif self.random_subdomain:
            sub_parts.append(self._generate_subdomain_label())
        if self.subdomain:
            sub_parts.append(self.subdomain)

        base_level_count = len([part for part in domain.split(".") if part])
        expected_total_levels = max(self.domain_level_count, 2)
        missing_levels = max(expected_total_levels - (base_level_count + len(sub_parts)), 0)
        if missing_levels > 0:
            fillers = [self._generate_subdomain_label() for _ in range(missing_levels)]
            sub_parts = fillers + sub_parts

        if not sub_parts:
            return domain
        return f"{'.'.join(sub_parts)}.{domain}"

    def get_email(self) -> MailboxAccount:
        self._ensure_api_configured()
        name = self._generate_local_part()
        payload = {"enablePrefix": True, "name": name}
        selected_domain = self._compose_domain(self._pick_domain())
        if selected_domain:
            payload["domain"] = selected_domain
            self._log(f"[CFWorker] 本次使用域名: {selected_domain}")
        data = self._request_json(
            "POST", "/admin/new_address", payload=payload, timeout=15
        )
        email = data.get("email", data.get("address", ""))
        token = data.get("token", data.get("jwt", ""))
        if not email or not token:
            raise RuntimeError(
                f"CFWorker API /admin/new_address 返回缺少 email/jwt: {data}"
            )
        self._token = token
        print(
            f"[CFWorker] 生成邮箱: {email} token={token[:40] if token else 'NONE'}..."
        )
        return MailboxAccount(
            email=email,
            account_id=token,
            extra={"cfworker_domain": selected_domain} if selected_domain else None,
        )

    def _get_mails(self, email: str) -> list:
        self._ensure_api_configured()
        data = self._request_json(
            "GET",
            "/admin/mails",
            params={"limit": 20, "offset": 0, "address": email},
            timeout=10,
        )
        return data.get("results", data) if isinstance(data, dict) else data

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            mails = self._get_mails(account.email)
            return {str(m.get("id", "")) for m in mails}
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        import re
        from datetime import datetime, timezone

        seen = set(before_ids or [])
        exclude_codes = set(kwargs.get("exclude_codes") or [])
        otp_sent_at = kwargs.get("otp_sent_at")
        otp_cutoff = float(otp_sent_at) - 2 if otp_sent_at else None

        def poll_once() -> Optional[str]:
            try:
                mails = self._get_mails(account.email)
                for mail in sorted(mails, key=lambda x: x.get("id", 0), reverse=True):
                    mid = str(mail.get("id", ""))
                    if not mid or mid in seen:
                        continue

                    created_at = str(mail.get("created_at", "") or "").strip()
                    if otp_cutoff and created_at:
                        try:
                            mail_ts = (
                                datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
                                .replace(tzinfo=timezone.utc)
                                .timestamp()
                            )
                            if mail_ts < otp_cutoff:
                                self._log(
                                    f"[CFWorker] \u8df3\u8fc7\u65e7\u90ae\u4ef6 id={mid} created_at={created_at}"
                                )
                                continue
                        except Exception:
                            pass

                    # 仅在通过时间边界筛选后再标记为已处理，避免边界邮件被过早加入 seen。
                    seen.add(mid)

                    raw = str(mail.get("raw", ""))
                    subject = str(mail.get("subject", ""))
                    search_text = f"{subject} {self._decode_raw_content(raw)}".strip()
                    search_text = re.sub(
                        r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
                        "",
                        search_text,
                    )
                    search_text = re.sub(r"m=\+\d+\.\d+", "", search_text)
                    search_text = re.sub(r"\bt=\d+\b", "", search_text)
                    if keyword and keyword.lower() not in search_text.lower():
                        continue

                    code = self._safe_extract(search_text, code_pattern)
                    if code and code in exclude_codes:
                        self._log(
                            f"[CFWorker] \u8df3\u8fc7\u5df2\u7528\u9a8c\u8bc1\u7801 id={mid} created_at={created_at} code={code}"
                        )
                        continue
                    if code:
                        self._log(
                            f"[CFWorker] \u547d\u4e2d\u65b0\u9a8c\u8bc1\u7801 id={mid} created_at={created_at} code={code}"
                        )
                        return code
            except Exception:
                pass
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
            timeout_message=f"\u7b49\u5f85\u9a8c\u8bc1\u7801\u8d85\u65f6 ({timeout}s)",
        )


class MoeMailMailbox(BaseMailbox):
    """MoeMail (sall.cc) 邮箱服务 - 自动注册账号并生成临时邮箱"""

    def __init__(
        self, api_url: str = "https://sall.cc", api_key: str = "", proxy: str = None
    ):
        self.api = api_url.rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.proxy = build_requests_proxy_config(proxy)
        self._session_token = None
        self._email = None

    def _api_headers(self) -> dict:
        if not self.api_key:
            return {}
        return {"X-API-Key": self.api_key}

    def _register_and_login(self) -> str:
        import requests, random, string

        s = requests.Session()
        s.proxies = self.proxy
        ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
        s.headers.update(
            {"user-agent": ua, "origin": self.api, "referer": f"{self.api}/zh-CN/login"}
        )
        s.headers.update(self._api_headers())
        # 注册
        username = "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
        password = "Test" + "".join(random.choices(string.digits, k=8)) + "!"
        print(f"[MoeMail] 注册账号: {username} / {password}")
        r_reg = s.post(
            f"{self.api}/api/auth/register",
            json={"username": username, "password": password, "turnstileToken": ""},
            timeout=15,
        )
        print(f"[MoeMail] 注册结果: {r_reg.status_code} {r_reg.text[:80]}")
        # 获取 CSRF
        csrf_r = s.get(f"{self.api}/api/auth/csrf", timeout=10)
        csrf = csrf_r.json().get("csrfToken", "")
        # 登录
        s.post(
            f"{self.api}/api/auth/callback/credentials",
            headers={"content-type": "application/x-www-form-urlencoded"},
            data=f"username={username}&password={password}&csrfToken={csrf}&redirect=false&callbackUrl={self.api}",
            allow_redirects=True,
            timeout=15,
        )
        self._session = s
        for cookie in s.cookies:
            if "session-token" in cookie.name:
                self._session_token = cookie.value
                print(f"[MoeMail] 登录成功")
                return cookie.value
        print(f"[MoeMail] 登录失败，cookies: {[c.name for c in s.cookies]}")
        return ""

    def get_email(self) -> MailboxAccount:
        # 每次调用都重新注册新账号，保证邮箱唯一
        self._session_token = None
        self._register_and_login()
        import random, string

        name = "".join(random.choices(string.ascii_letters + string.digits, k=8))
        # 获取可用域名列表，随机选一个
        domain = "sall.cc"
        try:
            cfg_r = self._session.get(
                f"{self.api}/api/config", headers=self._api_headers(), timeout=10
            )
            domains = [
                d.strip()
                for d in cfg_r.json().get("emailDomains", "sall.cc").split(",")
                if d.strip()
            ]
            if domains:
                domain = random.choice(domains)
        except Exception:
            pass
        r = self._session.post(
            f"{self.api}/api/emails/generate",
            headers=self._api_headers(),
            json={"name": name, "domain": domain, "expiryTime": 86400000},
            timeout=15,
        )
        data = r.json()
        self._email = data.get("email", data.get("address", ""))
        email_id = data.get("id", "")
        print(
            f"[MoeMail] 生成邮箱: {self._email} id={email_id} domain={domain} status={r.status_code}"
        )
        if not email_id:
            print(f"[MoeMail] 生成失败: {data}")
        if email_id:
            self._email_count = getattr(self, "_email_count", 0) + 1
        return MailboxAccount(email=self._email, account_id=str(email_id))

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            r = self._session.get(
                f"{self.api}/api/emails/{account.account_id}",
                headers=self._api_headers(),
                timeout=10,
            )
            return {str(m.get("id", "")) for m in r.json().get("messages", [])}
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        import re

        seen = set(before_ids or [])

        def poll_once() -> Optional[str]:
            try:
                r = self._session.get(
                    f"{self.api}/api/emails/{account.account_id}",
                    headers=self._api_headers(),
                    timeout=10,
                )
                msgs = r.json().get("messages", [])
                for msg in msgs:
                    mid = str(msg.get("id", ""))
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    body = (
                        str(
                            msg.get("content")
                            or msg.get("text")
                            or msg.get("body")
                            or msg.get("html")
                            or ""
                        )
                        + " "
                        + str(msg.get("subject") or "")
                    )
                    body = re.sub(
                        r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", "", body
                    )
                    code = self._safe_extract(body, code_pattern)
                    if code:
                        return code
            except Exception:
                pass
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )


class LuckMailMailbox(BaseMailbox):
    """LuckMail 混合模式：ChatGPT 走购买邮箱，其他平台走订单接码"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        project_code: str = "",
        email_type: str = "",
        domain: str = "",
        proxy: str = None,
    ):
        if not base_url or not api_key:
            raise RuntimeError(
                "LuckMail 未配置：请在全局设置中填写 luckmail_base_url 和 luckmail_api_key"
            )
        from .luckmail import LuckMailClient

        self._client = LuckMailClient(
            base_url=base_url,
            api_key=api_key,
            proxy_url=proxy,
        )
        self._project_code = project_code
        self._email_type = email_type or None
        self._domain = domain or None
        self._order_no = None
        self._token = None
        self._email = None

    def _use_purchase_mode(self, account: MailboxAccount = None) -> bool:
        if (
            account
            and account.account_id
            and str(account.account_id).startswith("tok_")
        ):
            return True
        if self._token:
            return True
        return self._project_code == "openai"

    def _resolve_token(self, account: MailboxAccount = None) -> str:
        token = (account.account_id if account else "") or self._token
        if token:
            self._token = token
            return token

        email = (account.email if account else "") or self._email
        if not email:
            return ""

        try:
            purchases = self._client.user.get_purchases(
                page=1,
                page_size=100,
                keyword=email,
            )
        except Exception:
            return ""

        email_lower = str(email).strip().lower()
        for item in purchases.list:
            if str(item.email_address).strip().lower() == email_lower and item.token:
                self._token = item.token
                self._email = item.email_address
                return item.token
        return ""

    def _cancel_order_silently(self, order_no: str) -> None:
        if not order_no:
            return
        try:
            self._client.user.cancel_order(order_no)
            self._log(f"[LuckMail] 已取消订单: {order_no}")
        except Exception:
            pass

    def _extract_code_from_token_mails(
        self,
        token: str,
        code_pattern: str = None,
        before_ids: set = None,
        exclude_codes: set = None,
    ) -> Optional[str]:
        try:
            mail_list = self._client.user.get_token_mails(token)
        except Exception:
            return None

        seen = {str(mid) for mid in (before_ids or set())}
        excluded = {str(code) for code in (exclude_codes or set()) if code}
        for mail in mail_list.mails:
            message_id = str(mail.message_id or "")
            if message_id and message_id in seen:
                continue
            body = " ".join(
                [
                    str(mail.subject or ""),
                    str(mail.body or ""),
                    str(mail.html_body or ""),
                ]
            )
            code = self._safe_extract(body, code_pattern)
            if code and code in excluded:
                continue
            if code:
                return code
        return None

    def get_email(self) -> MailboxAccount:
        if not self._project_code:
            raise RuntimeError("LuckMail 未设置 project_code，无法创建邮箱")

        if self._use_purchase_mode():
            self._log(
                f"[LuckMail] 分支: ChatGPT + LuckMail -> 购买邮箱接口 "
                f"(project_code={self._project_code}, email_type={self._email_type or '-'}, domain={self._domain or '-'})"
            )
            try:
                result = self._client.user.purchase_emails(
                    project_code=self._project_code,
                    quantity=1,
                    email_type=self._email_type,
                    domain=self._domain,
                )
            except Exception as e:
                raise RuntimeError(f"LuckMail 购买邮箱失败: {e}") from e

            purchases = (result or {}).get("purchases") or []
            if not purchases:
                raise RuntimeError(f"LuckMail 购买邮箱返回为空: {result}")

            item = purchases[0]
            email = str(item.get("email_address") or "").strip()
            token = str(item.get("token") or "").strip()
            if not email or not token:
                raise RuntimeError(f"LuckMail 返回缺少 email/token: {item}")

            self._email = email
            self._token = token
            self._log(f"[LuckMail] 已购邮箱: {email}")
            if item.get("warranty_until"):
                self._log(f"[LuckMail] 质保到期: {item.get('warranty_until')}")
            return MailboxAccount(
                email=email,
                account_id=token,
                extra={
                    "provider": "luckmail",
                    "token": token,
                    "project_code": self._project_code,
                },
            )

        self._log(
            f"[LuckMail] 分支: 其他平台 + LuckMail -> 创建订单/订单接码 "
            f"(project_code={self._project_code}, email_type={self._email_type or '-'})"
        )
        try:
            body = {"project_code": self._project_code}
            if self._email_type:
                body["email_type"] = self._email_type
            order = self._client.user._sync_create_order(body)
        except Exception as e:
            raise RuntimeError(f"LuckMail 创建订单失败: {e}") from e
        self._order_no = order.order_no
        email = order.email_address
        self._email = email
        self._log(f"[LuckMail] 订单 {order.order_no} 分配邮箱: {email}")
        self._log(f"[LuckMail] 超时时间: {order.expired_at}")
        return MailboxAccount(email=email, account_id=order.order_no)

    def get_current_ids(self, account: MailboxAccount) -> set:
        if not self._use_purchase_mode(account):
            return set()
        token = self._resolve_token(account)
        if not token:
            return set()
        try:
            mail_list = self._client.user.get_token_mails(token)
            return {str(m.message_id) for m in (mail_list.mails or []) if m.message_id}
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        if not self._use_purchase_mode(account):
            self._log("[LuckMail] 等验证码分支: 订单接码")
            order_no = account.account_id or self._order_no
            if not order_no:
                raise RuntimeError("LuckMail 未创建订单，无法等待验证码")

            def on_poll_order(result):
                self._log(f"[LuckMail] 轮询中... 状态: {result.status}")

            deadline = time.monotonic() + max(int(timeout or 0), 1)
            last_status = "pending"
            try:
                while time.monotonic() < deadline:
                    self._checkpoint()
                    remaining = max(1, int(deadline - time.monotonic()))
                    slice_timeout = min(remaining, 6)
                    try:
                        code_result = self._client.user._sync_wait_for_code(
                            order_no=order_no,
                            timeout=slice_timeout,
                            interval=3.0,
                            on_poll=on_poll_order,
                        )
                    except Exception as e:
                        raise TimeoutError(f"LuckMail 等待验证码失败: {e}") from e

                    last_status = str(code_result.status or "pending")
                    if code_result.status == "success" and code_result.verification_code:
                        code = code_result.verification_code
                        self._log(f"[LuckMail] 收到验证码: {code}")
                        return code
                    if code_result.status in {"cancelled", "timeout"}:
                        break
            except Exception:
                self._cancel_order_silently(order_no)
                raise

            self._cancel_order_silently(order_no)
            raise TimeoutError(
                f"LuckMail 等待验证码超时 ({timeout}s)，最终状态: {last_status}"
            )

        token = self._resolve_token(account)
        if not token:
            raise RuntimeError("LuckMail 未找到已购邮箱 Token，无法等待验证码")
        self._log("[LuckMail] 等验证码分支: 已购邮箱 Token 收码")

        exclude_codes = {
            str(code) for code in (kwargs.get("exclude_codes") or set()) if code
        }
        seen_message_ids = {str(mid) for mid in (before_ids or set()) if mid}
        if before_ids is None:
            seen_message_ids = self.get_current_ids(account)
            if seen_message_ids:
                self._log(
                    f"[LuckMail] 已建立旧邮件基线，先跳过 {len(seen_message_ids)} 封历史邮件"
                )

        saw_new_mail = False

        def poll_once() -> Optional[str]:
            nonlocal saw_new_mail
            found_new_mail = False
            try:
                mail_list = self._client.user.get_token_mails(token)
            except Exception as e:
                raise TimeoutError(f"LuckMail 等待验证码失败: {e}") from e

            for mail in mail_list.mails:
                message_id = str(mail.message_id or "").strip()
                if message_id and message_id in seen_message_ids:
                    continue

                found_new_mail = True
                saw_new_mail = True
                if message_id:
                    seen_message_ids.add(message_id)

                body = " ".join(
                    [
                        str(mail.subject or ""),
                        str(mail.body or ""),
                        str(mail.html_body or ""),
                    ]
                )
                code = self._safe_extract(body, code_pattern)
                if code and code in exclude_codes:
                    self._log(
                        f"[LuckMail] 跳过已使用验证码 message_id={message_id or '-'} code={code}"
                    )
                    continue
                if code:
                    self._log(f"[LuckMail] 收到验证码: {code}")
                    return code

            self._log(
                f"[LuckMail] 轮询中... 新邮件: {'是' if found_new_mail else '否'}"
            )

            if found_new_mail:
                self._log("[LuckMail] 新邮件还不是可用验证码，继续等下一封...")
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
            timeout_message=(
                f"LuckMail 等待验证码超时 ({timeout}s)，最终状态: "
                f"has_new_mail={saw_new_mail}"
            ),
        )


class OutlookMailboxBackend(ABC):
    """Outlook 收信后端策略。"""

    backend_name: str = ""

    def __init__(self, mailbox: "OutlookMailbox"):
        self.mailbox = mailbox

    @staticmethod
    def _safe_otp_log(message: str) -> str:
        try:
            from platforms.chatgpt.log_sanitizer import sanitize_chatgpt_log_message

            return sanitize_chatgpt_log_message(message)
        except Exception:
            return str(message or "").rsplit(":", 1)[0] + ": [验证码已隐藏]"

    @abstractmethod
    def get_current_ids(self, account: MailboxAccount) -> set:
        ...

    @abstractmethod
    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
        code_pattern: str | None = None,
        **kwargs,
    ) -> str:
        ...


class OutlookImapMailboxBackend(OutlookMailboxBackend):
    backend_name = "imap"

    def get_current_ids(self, account: MailboxAccount) -> set:
        imap_conn = None
        try:
            imap_conn = self.mailbox._open_imap(account)
            seen: set[str] = set()
            for folder in self.mailbox._imap_folder_names:
                try:
                    status, _ = imap_conn.select(folder, readonly=True)
                    if status != "OK":
                        continue
                    status, data = imap_conn.uid("search", None, "ALL")
                    if status != "OK":
                        continue
                    ids = data[0].split() if data and data[0] else []
                    for uid in ids[-100:]:
                        uid_str = (
                            uid.decode("utf-8", errors="ignore")
                            if isinstance(uid, bytes)
                            else str(uid)
                        )
                        if uid_str:
                            seen.add(f"{folder}:{uid_str}")
                except Exception as exc:
                    self.mailbox._log(
                        f"[微软邮箱][IMAP] folder={folder} 获取当前邮件 ID 失败: {exc}"
                    )
                    continue
            return seen
        finally:
            try:
                if imap_conn:
                    imap_conn.logout()
            except Exception:
                pass

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
        code_pattern: str | None = None,
        **kwargs,
    ) -> str:
        from email import message_from_bytes
        from email.policy import default as email_default_policy

        seen = {str(mid) for mid in (before_ids or set())}
        exclude_codes = {
            str(code).strip()
            for code in (kwargs.get("exclude_codes") or set())
            if str(code or "").strip()
        }
        keyword_lower = str(keyword or "").strip().lower()
        imap_conn = None

        def close_connection() -> None:
            nonlocal imap_conn
            if imap_conn is None:
                return
            try:
                imap_conn.logout()
            except Exception:
                pass
            imap_conn = None

        def poll_once() -> Optional[str]:
            nonlocal imap_conn
            import imaplib

            if imap_conn is None:
                try:
                    imap_conn = self.mailbox._open_imap(account)
                    self.mailbox._log(
                        "[微软邮箱][IMAP] 登录成功，本轮复用同一连接查询全部文件夹"
                    )
                except MailboxAuthenticationError as exc:
                    self.mailbox._log(
                        f"[微软邮箱][IMAP] 鉴权失败，已停止 OTP 等待: {exc}"
                    )
                    raise
                except Exception as exc:
                    self.mailbox._log(
                        f"[微软邮箱][IMAP] 连接失败，本轮稍后重试: {exc}"
                    )
                    return None

            for folder in self.mailbox._imap_folder_names:
                try:
                    self.mailbox._log(f"[微软邮箱][IMAP] folder={folder} 开始轮询")
                    status, _ = imap_conn.select(folder, readonly=True)
                    if status != "OK":
                        self.mailbox._log(
                            f"[微软邮箱][IMAP] folder={folder} select 失败: status={status}"
                        )
                        continue
                    status, data = imap_conn.uid("search", None, "ALL")
                    if status != "OK":
                        self.mailbox._log(
                            f"[微软邮箱][IMAP] folder={folder} search 失败: status={status}"
                        )
                        continue
                    ids = data[0].split() if data and data[0] else []
                    if len(ids) > 50:
                        ids = ids[-50:]
                    new_uids = []
                    for uid in ids:
                        uid_str = (
                            uid.decode("utf-8", errors="ignore")
                            if isinstance(uid, bytes)
                            else str(uid)
                        )
                        seen_key = f"{folder}:{uid_str}"
                        if not uid_str or seen_key in seen:
                            continue
                        seen.add(seen_key)
                        new_uids.append(uid)
                    self.mailbox._log(
                        f"[微软邮箱][IMAP] folder={folder} uid_total={len(ids)} new_uid_count={len(new_uids)}"
                    )
                    for uid in new_uids:
                        status, msg_data = imap_conn.uid("fetch", uid, "(RFC822)")
                        if status != "OK":
                            self.mailbox._log(
                                f"[微软邮箱][IMAP] folder={folder} fetch 失败: uid={uid!r} status={status}"
                            )
                            continue
                        raw = None
                        for item in msg_data or []:
                            if isinstance(item, tuple) and item[1]:
                                raw = item[1]
                                break
                        if not raw:
                            self.mailbox._log(
                                f"[微软邮箱][IMAP] folder={folder} fetch 空响应: uid={uid!r}"
                            )
                            continue
                        msg = message_from_bytes(raw, policy=email_default_policy)
                        subject = self.mailbox._decode_header_value(msg.get("Subject", ""))
                        text = self.mailbox._extract_message_text(msg)
                        self.mailbox._log(
                            f"[微软邮箱][IMAP] folder={folder} 命中新邮件 subject={subject or '-'}"
                        )
                        if keyword_lower and keyword_lower not in text.lower():
                            self.mailbox._log(
                                f"[微软邮箱][IMAP] folder={folder} 跳过关键字不匹配邮件"
                            )
                            continue
                        code = self.mailbox._safe_extract(text, code_pattern)
                        if not code:
                            self.mailbox._log(
                                f"[微软邮箱][IMAP] folder={folder} 未提取到验证码"
                            )
                            continue
                        if code in exclude_codes:
                            self.mailbox._log(
                                self._safe_otp_log(
                                    f"[微软邮箱][IMAP] folder={folder} "
                                    f"跳过已尝试验证码: {code}"
                                )
                            )
                            continue
                        self.mailbox._log(
                            self._safe_otp_log(
                                f"[微软邮箱][IMAP] folder={folder} "
                                f"验证码提取成功: {code}"
                            )
                        )
                        return code
                except imaplib.IMAP4.abort as exc:
                    self.mailbox._log(
                        f"[微软邮箱][IMAP] 连接中断，下轮自动重连: {exc}"
                    )
                    close_connection()
                    break
                except Exception as exc:
                    self.mailbox._log(
                        f"[微软邮箱][IMAP] folder={folder} 不可用，已跳过: {exc}"
                    )
                    continue
            return None

        try:
            return self.mailbox._run_polling_wait(
                timeout=timeout,
                poll_interval=5,
                poll_once=poll_once,
            )
        finally:
            close_connection()


class OutlookGraphMailboxBackend(OutlookMailboxBackend):
    backend_name = "graph"

    def get_current_ids(self, account: MailboxAccount) -> set:
        access_token = self.mailbox._get_oauth_access_token(
            account,
            preferred_backend=self.backend_name,
        )
        seen: set[str] = set()
        for folder in self.mailbox._graph_folder_names:
            try:
                messages = self.mailbox._graph_list_messages(
                    access_token=access_token,
                    folder=folder,
                )
                for message in messages:
                    message_id = str(message.get("id") or "").strip()
                    if message_id:
                        seen.add(f"{folder}:{message_id}")
            except RuntimeError as exc:
                if "HTTP 401" in str(exc):
                    # 401 → token 失效，强制刷新后重试一次
                    self.mailbox._log(
                        f"[微软邮箱][Graph] get_current_ids folder={folder} 遇到 401，强制刷新 token"
                    )
                    _cache = (account.extra or {}).get("_oauth_token_cache")
                    if isinstance(_cache, dict):
                        _cache.pop(
                            self.mailbox._normalize_backend_name(self.backend_name), None
                        )
                    access_token = self.mailbox._get_oauth_access_token(
                        account,
                        preferred_backend=self.backend_name,
                    )
                    try:
                        messages = self.mailbox._graph_list_messages(
                            access_token=access_token,
                            folder=folder,
                        )
                        for message in messages:
                            message_id = str(message.get("id") or "").strip()
                            if message_id:
                                seen.add(f"{folder}:{message_id}")
                    except Exception:
                        pass
                else:
                    raise
        return seen

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
        code_pattern: str | None = None,
        **kwargs,
    ) -> str:
        seen = {str(mid) for mid in (before_ids or set())}
        exclude_codes = {
            str(code).strip()
            for code in (kwargs.get("exclude_codes") or set())
            if str(code or "").strip()
        }
        keyword_lower = str(keyword or "").strip().lower()

        # 标记是否已做过一次 401 强制刷 token，避免无限循环
        _token_refreshed = False

        def _force_refresh_token() -> str:
            """清除 OAuth 缓存，强制重新获取 access token。"""
            _cache = (account.extra or {}).get("_oauth_token_cache")
            if isinstance(_cache, dict):
                _cache.pop(
                    self.mailbox._normalize_backend_name(self.backend_name), None
                )
            return self.mailbox._get_oauth_access_token(
                account,
                preferred_backend=self.backend_name,
            )

        def poll_once() -> Optional[str]:
            nonlocal _token_refreshed
            access_token = self.mailbox._get_oauth_access_token(
                account,
                preferred_backend=self.backend_name,
            )
            for folder in self.mailbox._graph_folder_names:
                try:
                    self.mailbox._log(f"[微软邮箱][Graph] folder={folder} 开始轮询")
                    messages = self.mailbox._graph_list_messages(
                        access_token=access_token,
                        folder=folder,
                    )
                    new_messages = []
                    for message in messages:
                        message_id = str(message.get("id") or "").strip()
                        seen_key = f"{folder}:{message_id}"
                        if not message_id or seen_key in seen:
                            continue
                        seen.add(seen_key)
                        new_messages.append(message)
                    self.mailbox._log(
                        f"[微软邮箱][Graph] folder={folder} message_total={len(messages)} new_count={len(new_messages)}"
                    )
                    for message in new_messages:
                        subject = str(message.get("subject") or "").strip()
                        text = self.mailbox._graph_message_text(message)
                        self.mailbox._log(
                            f"[微软邮箱][Graph] folder={folder} 命中新邮件 subject={subject or '-'}"
                        )
                        if keyword_lower and keyword_lower not in text.lower():
                            self.mailbox._log(
                                f"[微软邮箱][Graph] folder={folder} 跳过关键字不匹配邮件"
                            )
                            continue
                        code = self.mailbox._safe_extract(text, code_pattern)
                        if not code:
                            message_id = str(message.get("id") or "").strip()
                            if message_id:
                                detail = self.mailbox._graph_get_message(
                                    access_token=access_token,
                                    message_id=message_id,
                                )
                                text = self.mailbox._graph_message_text(detail)
                                code = self.mailbox._safe_extract(text, code_pattern)
                        if not code:
                            self.mailbox._log(
                                f"[微软邮箱][Graph] folder={folder} 未提取到验证码"
                            )
                            continue
                        if code in exclude_codes:
                            self.mailbox._log(
                                self._safe_otp_log(
                                    f"[微软邮箱][Graph] folder={folder} "
                                    f"跳过已尝试验证码: {code}"
                                )
                            )
                            continue
                        self.mailbox._log(
                            self._safe_otp_log(
                                f"[微软邮箱][Graph] folder={folder} "
                                f"验证码提取成功: {code}"
                            )
                        )
                        return code
                except Exception as exc:
                    exc_str = str(exc)
                    # 401 → token 失效，强制刷新后重试一次
                    if "HTTP 401" in exc_str and not _token_refreshed:
                        _token_refreshed = True
                        self.mailbox._log(
                            f"[微软邮箱][Graph] folder={folder} 遇到 401，强制刷新 token 后重试"
                        )
                        try:
                            access_token = _force_refresh_token()
                            messages = self.mailbox._graph_list_messages(
                                access_token=access_token,
                                folder=folder,
                            )
                            new_messages = []
                            for message in messages:
                                message_id = str(message.get("id") or "").strip()
                                seen_key = f"{folder}:{message_id}"
                                if not message_id or seen_key in seen:
                                    continue
                                seen.add(seen_key)
                                new_messages.append(message)
                            for message in new_messages:
                                subject = str(message.get("subject") or "").strip()
                                text = self.mailbox._graph_message_text(message)
                                if keyword_lower and keyword_lower not in text.lower():
                                    continue
                                code = self.mailbox._safe_extract(text, code_pattern)
                                if not code:
                                    mid = str(message.get("id") or "").strip()
                                    if mid:
                                        detail = self.mailbox._graph_get_message(
                                            access_token=access_token,
                                            message_id=mid,
                                        )
                                        text = self.mailbox._graph_message_text(detail)
                                        code = self.mailbox._safe_extract(text, code_pattern)
                                if code and code not in exclude_codes:
                                    self.mailbox._log(
                                        self._safe_otp_log(
                                            f"[微软邮箱][Graph] folder={folder} "
                                            f"刷新 token 后验证码提取成功: {code}"
                                        )
                                    )
                                    return code
                        except Exception as retry_exc:
                            self.mailbox._log(
                                f"[微软邮箱][Graph] folder={folder} 刷新 token 后仍然失败: {retry_exc}"
                            )
                        continue
                    self.mailbox._log(
                        f"[微软邮箱][Graph] folder={folder} 查询异常: {exc}"
                    )
                    continue
            return None

        return self.mailbox._run_polling_wait(
            timeout=timeout,
            poll_interval=5,
            poll_once=poll_once,
        )


class MailApiUrlOtpBackend(OutlookMailboxBackend):
    backend_name = "mailapi_url"

    def __init__(self, mailbox: "OutlookMailbox"):
        super().__init__(mailbox)
        self._detail_discovery_log_keys: set[tuple[str, tuple[str, ...]]] = set()
        self._old_message_log_keys: set[tuple[str, str, float]] = set()

    @staticmethod
    def _code_key(code: str) -> str:
        return f"mailapi_code:{str(code or '').strip()}"

    @staticmethod
    def _parse_timestamp(value: Any) -> Optional[float]:
        from datetime import datetime, timezone, timedelta
        import re

        if value in (None, ""):
            return None
        if isinstance(value, (int, float)):
            numeric = float(value)
            return numeric / 1000 if numeric > 10_000_000_000 else numeric
        text = str(value).strip()
        if not text:
            return None
        try:
            numeric = float(text)
            return numeric / 1000 if numeric > 10_000_000_000 else numeric
        except (TypeError, ValueError):
            pass
        chinese_datetime = re.search(
            r"(?P<year>20\d{2})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日"
            r"\s*(?P<hour>\d{1,2}):(?P<minute>\d{2}):(?P<second>\d{2})",
            text,
        )
        if chinese_datetime:
            parts = {
                key: int(raw)
                for key, raw in chinese_datetime.groupdict().items()
            }
            tzinfo = (
                timezone(timedelta(hours=8))
                if "北京时间" in text
                else None
            )
            try:
                return datetime(**parts, tzinfo=tzinfo).timestamp()
            except ValueError:
                return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
        try:
            from email.utils import parsedate_to_datetime

            parsed = parsedate_to_datetime(text)
            if parsed is None:
                return None
            return parsed.timestamp()
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _decode_mailapi_data_uri(value: Any) -> str:
        import base64
        import binascii
        import re
        from urllib.parse import unquote_to_bytes

        text = str(value or "")
        match = re.match(r"^data:([^,]*?),(.*)$", text, re.IGNORECASE | re.DOTALL)
        if not match:
            return text

        metadata, payload = match.groups()
        try:
            encoded_payload = unquote_to_bytes(payload)
            if re.search(r"(?:^|;)base64(?:;|$)", metadata, re.IGNORECASE):
                decoded = base64.b64decode(encoded_payload, validate=False)
            else:
                decoded = encoded_payload
            charset_match = re.search(
                r"(?:^|;)charset=([^;]+)",
                metadata,
                re.IGNORECASE,
            )
            charset = charset_match.group(1).strip() if charset_match else "utf-8"
            return decoded.decode(charset, errors="replace")
        except (binascii.Error, LookupError, UnicodeError, ValueError):
            return text

    @classmethod
    def _parse_mailapi_html_message(cls, raw_text: str) -> Optional[dict[str, Any]]:
        import hashlib
        import re

        raw = str(raw_text or "")
        latest_view = re.search(
            r"<main\b(?=[^>]*\bdata-view\s*=\s*(['\"])latest\1)[^>]*>"
            r"(.*?)</main\s*>",
            raw,
            re.IGNORECASE | re.DOTALL,
        )
        if latest_view:
            latest_body = latest_view.group(2)

            def latest_class_text(class_name: str) -> str:
                element = re.search(
                    rf"<(?P<tag>[A-Za-z][\w:-]*)\b[^>]*\bclass\s*=\s*"
                    rf"(?P<quote>['\"])[^'\"]*\b{class_name}\b[^'\"]*"
                    rf"(?P=quote)[^>]*>(?P<body>.*?)</(?P=tag)\s*>",
                    latest_body,
                    re.IGNORECASE | re.DOTALL,
                )
                return (
                    cls._mailapi_visible_text(element.group("body"))
                    if element
                    else ""
                )

            code = latest_class_text("code")
            subject = latest_class_text("su") or latest_class_text("subject")
            received_text = latest_class_text("dt") or latest_class_text("date")
            if re.fullmatch(r"\d{6}", code) and cls._is_openai_history_subject(
                subject
            ):
                identity = "|".join((subject, received_text, code))
                return {
                    "content": f"{subject} verification code {code}",
                    "received_at": cls._parse_timestamp(received_text),
                    "message_id": "mailapi_message:" + hashlib.sha256(
                        identity.encode("utf-8", errors="ignore")
                    ).hexdigest(),
                    "bounded_content": True,
                    "status": None,
                }

        # Newer MailAPI pages use a `latest-view` section with a dedicated
        # `latest-code` output instead of the older `main[data-view=latest]`
        # or `article.mail-card` markup.  Keep extraction bounded to that
        # section so unrelated six-digit values elsewhere on the page are not
        # treated as OTPs.
        latest_section = re.search(
            r"<(?P<tag>[A-Za-z][\w:-]*)\b[^>]*\bclass\s*=\s*"
            r"(?P<quote>['\"])[^'\"]*\blatest-view\b[^'\"]*"
            r"(?P=quote)[^>]*>(?P<body>.*?)</(?P=tag)\s*>",
            raw,
            re.IGNORECASE | re.DOTALL,
        )
        if latest_section:
            latest_body = latest_section.group("body")

            def latest_section_text(pattern: str) -> str:
                element = re.search(
                    pattern,
                    latest_body,
                    re.IGNORECASE | re.DOTALL,
                )
                return (
                    cls._mailapi_visible_text(element.group("body"))
                    if element
                    else ""
                )

            code = latest_section_text(
                r"<(?P<tag>[A-Za-z][\w:-]*)\b[^>]*\bid\s*=\s*"
                r"(?P<quote>['\"])latest-code(?P=quote)[^>]*>"
                r"(?P<body>.*?)</(?P=tag)\s*>"
            )
            subject = latest_section_text(
                r"<h2\b[^>]*>(?P<body>.*?)</h2\s*>"
            ) or latest_section_text(
                r"<(?P<tag>[A-Za-z][\w:-]*)\b[^>]*\bclass\s*=\s*"
                r"(?P<quote>['\"])[^'\"]*\bsubject\b[^'\"]*"
                r"(?P=quote)[^>]*>(?P<body>.*?)</(?P=tag)\s*>"
            )
            received_text = latest_section_text(
                r"<dd\b[^>]*>(?P<body>.*?)</dd\s*>"
            ) or latest_section_text(
                r"<(?P<tag>[A-Za-z][\w:-]*)\b[^>]*\bclass\s*=\s*"
                r"(?P<quote>['\"])[^'\"]*\b(?:date|time)\b[^'\"]*"
                r"(?P=quote)[^>]*>(?P<body>.*?)</(?P=tag)\s*>"
            )
            if re.fullmatch(r"\d{6}", code) and cls._is_openai_history_subject(
                subject
            ):
                identity = "|".join((subject, received_text, code))
                return {
                    "content": f"{subject} verification code {code}",
                    "received_at": cls._parse_timestamp(received_text),
                    "message_id": "mailapi_message:" + hashlib.sha256(
                        identity.encode("utf-8", errors="ignore")
                    ).hexdigest(),
                    "bounded_content": True,
                    "status": None,
                }

        for article_match in re.finditer(
            r"<article\b([^>]*)>(.*?)</article\s*>",
            raw,
            re.IGNORECASE | re.DOTALL,
        ):
            article_attrs, article_body = article_match.groups()
            class_match = re.search(
                r"\bclass\s*=\s*(['\"])(.*?)\1",
                article_attrs,
                re.IGNORECASE | re.DOTALL,
            )
            class_names = {
                name.lower()
                for name in re.split(
                    r"\s+",
                    class_match.group(2).strip() if class_match else "",
                )
                if name
            }
            if "mail-card" not in class_names:
                continue

            card_html = article_match.group(0)
            received_text = ""
            for date_match in re.finditer(
                r"<(?:span|time)\b([^>]*)>(.*?)</(?:span|time)\s*>",
                article_body,
                re.IGNORECASE | re.DOTALL,
            ):
                date_attrs, date_body = date_match.groups()
                date_class_match = re.search(
                    r"\bclass\s*=\s*(['\"])(.*?)\1",
                    date_attrs,
                    re.IGNORECASE | re.DOTALL,
                )
                date_classes = {
                    name.lower()
                    for name in re.split(
                        r"\s+",
                        date_class_match.group(2).strip()
                        if date_class_match
                        else "",
                    )
                    if name
                }
                if "date" not in date_classes:
                    continue
                received_text = cls._mailapi_visible_text(date_body)
                break

            visible_card = cls._mailapi_visible_text(card_html)
            identity = f"{received_text}|{visible_card}"
            message_id = "mailapi_message:" + hashlib.sha256(
                identity.encode("utf-8", errors="ignore")
            ).hexdigest()
            return {
                "content": card_html,
                "received_at": cls._parse_timestamp(received_text),
                "message_id": message_id,
                "bounded_content": True,
                "status": None,
            }

        latest_mail_page = "最新邮件信息" in cls._mailapi_visible_text(raw)
        if latest_mail_page:
            received_match = re.search(
                r"<[^>]+\bclass\s*=\s*(['\"])[^'\"]*\b(?:time|date)\b[^'\"]*\1[^>]*>"
                r"(.*?)</[^>]+>",
                raw,
                re.IGNORECASE | re.DOTALL,
            )
            subject_match = re.search(
                r"<[^>]+\bclass\s*=\s*(['\"])[^'\"]*\bsubject\b[^'\"]*\1[^>]*>"
                r"(.*?)</[^>]+>",
                raw,
                re.IGNORECASE | re.DOTALL,
            )
            received_text = (
                cls._mailapi_visible_text(received_match.group(2))
                if received_match
                else ""
            )
            subject = (
                cls._mailapi_visible_text(subject_match.group(2))
                if subject_match
                else ""
            )
            code = str(cls._extract_code_from_latest_page(raw) or "").strip()
            if code and cls._is_openai_history_subject(subject):
                identity = "|".join((subject, received_text, code))
                message_id = "mailapi_message:" + hashlib.sha256(
                    identity.encode("utf-8", errors="ignore")
                ).hexdigest()
                return {
                    "content": f"{subject} authentication code {code}",
                    "received_at": cls._parse_timestamp(received_text),
                    "message_id": message_id,
                    "bounded_content": True,
                    "status": None,
                }

        # Some MailAPI providers render the newest message directly in a
        # `.panel` page instead of wrapping it in the historical
        # `<article class="mail-card">` shape.  Keep the extraction bounded to
        # the panel and its metadata so tracking links, styles, and mailbox
        # chrome cannot become message content or fake OTP candidates.
        panel_match = re.search(
            r"<section\b([^>]*)\bclass\s*=\s*(['\"])([^'\"]*\bpanel\b[^'\"]*)\2[^>]*>"
            r"(.*?)</section\s*>",
            raw,
            re.IGNORECASE | re.DOTALL,
        )
        if panel_match is None:
            panel_match = re.search(
                r"<div\b([^>]*)\bclass\s*=\s*(['\"])([^'\"]*\bpanel\b[^'\"]*)\2[^>]*>"
                r"(.*?)</div\s*>\s*</(?:main|body|html)>",
                raw,
                re.IGNORECASE | re.DOTALL,
            )
        if panel_match is None:
            return None

        panel_html = panel_match.group(0)
        panel_body = panel_match.group(4)

        def _class_body(class_name: str) -> str:
            match = re.search(
                rf"<(?P<tag>[A-Za-z][\w:-]*)\b[^>]*\bclass\s*=\s*(['\"])[^'\"]*\b{class_name}\b[^'\"]*\2[^>]*>",
                panel_body,
                re.IGNORECASE | re.DOTALL,
            )
            if match is None:
                return ""
            tag = str(match.group("tag") or "")
            depth = 1
            tag_pattern = re.compile(
                rf"<(?P<closing>/)?{re.escape(tag)}\b[^>]*>",
                re.IGNORECASE | re.DOTALL,
            )
            for tag_match in tag_pattern.finditer(panel_body, match.end()):
                if tag_match.group("closing"):
                    depth -= 1
                else:
                    depth += 1
                if depth == 0:
                    return panel_body[match.end() : tag_match.start()]
            return panel_body[match.end() :]

        subject = cls._mailapi_visible_text(_class_body("subject"))
        content_html = _class_body("content") or panel_body
        received_text = ""
        labeled_time = re.search(
            r"<[^>]+\bclass\s*=\s*(['\"])[^'\"]*\blabel\b[^'\"]*\1[^>]*>"
            r"\s*(?:接收时间|时间|日期|Date|Received)\s*[：:]?\s*</[^>]+>\s*"
            r"<[^>]+\bclass\s*=\s*(['\"])[^'\"]*\bvalue\b[^'\"]*\2[^>]*>"
            r"(.*?)</[^>]+>",
            panel_body,
            re.IGNORECASE | re.DOTALL,
        )
        if labeled_time:
            received_text = cls._mailapi_visible_text(labeled_time.group(3))
        if not received_text:
            date_match = re.search(
                r"(?:20\d{2}(?:[-/]\d{1,2}[-/]\d{1,2}|年\d{1,2}月\d{1,2}日)"
                r"\s+\d{1,2}:\d{2}:\d{2}(?:\s*[（(]?北京时间[）)]?)?)",
                panel_body,
                re.IGNORECASE,
            )
            received_text = date_match.group(0) if date_match else ""

        visible_panel = cls._mailapi_visible_text(panel_html)
        identity = "|".join((subject, received_text, visible_panel))
        message_id = "mailapi_message:" + hashlib.sha256(
            identity.encode("utf-8", errors="ignore")
        ).hexdigest()
        return {
            "content": content_html,
            "received_at": cls._parse_timestamp(received_text),
            "message_id": message_id,
            "bounded_content": True,
            "visible_html_content": True,
            "status": None,
        }

    @classmethod
    def _extract_code_from_latest_page(cls, raw: str) -> str:
        import re

        visible = cls._mailapi_visible_text(raw)
        match = re.search(
            r"(?is)\b(?:verification|authentication|login|security)\s+code\b"
            r".{0,160}?\b(\d{6})\b",
            visible,
        )
        if match:
            return match.group(1)
        return ""

    @staticmethod
    def _is_openai_history_subject(value: Any) -> bool:
        subject = " ".join(str(value or "").strip().lower().split())
        return bool(
            "openai" in subject
            or "chatgpt" in subject
            or subject == "your authentication code"
        )

    @classmethod
    def _mailapi_history_item_code(cls, item: dict[str, Any]) -> str:
        import re

        explicit_values = [
            item.get("verificationCode"),
            item.get("verification_code"),
            item.get("code"),
            item.get("otp"),
        ]
        codes = item.get("codes")
        if isinstance(codes, list):
            explicit_values.extend(codes[:10])
        for value in explicit_values:
            explicit = str(value or "").strip()
            if re.fullmatch(r"\d{6}", explicit):
                return explicit

        subject = " ".join(str(item.get("subject") or "").strip().split())
        if not re.search(
            r"(?is)\b(?:verification|authentication|login|security)\s+code\b|"
            r"\btemporary\b.{0,32}\bcode\b|"
            r"验证码|校验码|动态码|登录代码|登錄代碼|登入代碼|"
            r"認証コード|認證碼|驗證碼",
            subject,
        ):
            return ""

        content = " ".join(
            str(item.get(key) or "")
            for key in (
                "subject",
                "preview",
                "snippet",
                "text_body",
                "text",
                "body",
                "content",
            )
        )
        match = re.search(
            r"(?is)(?:verification|authentication|login|security)\s+code\b"
            r"[^0-9]{0,160}(\d{6})\b|"
            r"(?:验证码|校验码|动态码|登录代码|登錄代碼|"
            r"登入代碼|認証コード|認證碼|驗證碼)"
            r"[^0-9]{0,80}(\d{6})\b",
            content,
        )
        if not match:
            return ""
        return str(match.group(1) or match.group(2) or "").strip()

    @staticmethod
    def _decode_mailapi_raw_mime(value: Any) -> str:
        from email import policy
        from email.parser import Parser

        raw = str(value or "")
        if not raw:
            return ""
        try:
            message = Parser(policy=policy.default).parsestr(raw)
        except Exception:
            return raw

        parts = list(message.walk()) if message.is_multipart() else [message]
        decoded_parts = []
        for part in parts:
            if part.is_multipart():
                continue
            content_type = str(part.get_content_type() or "").lower()
            if content_type not in {"text/plain", "text/html"}:
                continue
            try:
                content = part.get_content()
            except Exception:
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    charset = str(part.get_content_charset() or "utf-8")
                    content = payload.decode(charset, errors="replace")
                else:
                    content = str(payload or "")
            decoded_parts.append(str(content or ""))
        return " ".join(decoded_parts).strip() or raw

    @staticmethod
    def _mailapi_recipient_emails(value: Any) -> list[str]:
        """Extract normalized recipient addresses from common MailAPI shapes."""
        import re

        values = value if isinstance(value, (list, tuple)) else [value]
        emails: list[str] = []
        for item in values:
            if isinstance(item, dict):
                nested = (
                    item.get("email")
                    or item.get("address")
                    or item.get("value")
                    or ""
                )
            else:
                nested = item
            for email in re.findall(
                r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}",
                str(nested or ""),
                re.IGNORECASE,
            ):
                normalized = email.strip().lower()
                if normalized and normalized not in emails:
                    emails.append(normalized)
        return emails

    @classmethod
    def _parse_mailapi_message(
        cls,
        text: str,
        *,
        yisen: bool = False,
    ) -> dict[str, Any]:
        import json

        raw_text = str(text or "")
        try:
            payload = json.loads(raw_text)
        except (TypeError, ValueError):
            payload = None

        if not isinstance(payload, dict):
            html_message = cls._parse_mailapi_html_message(raw_text)
            if html_message is not None:
                return html_message
            return {
                "content": raw_text,
                "received_at": None,
                "message_id": "",
                "status": None,
            }

        yisen_results = payload.get("results") if yisen else None
        if isinstance(yisen_results, list):
            candidates = []
            for index, item in enumerate(yisen_results[:50]):
                if not isinstance(item, dict):
                    continue
                metadata = item.get("metadata")
                if isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except (TypeError, ValueError):
                        metadata = {}
                if not isinstance(metadata, dict):
                    metadata = {}
                subject = str(metadata.get("subject") or "").strip()
                if not cls._is_openai_history_subject(subject):
                    continue
                source = str(item.get("source") or "").strip()
                raw_message = str(item.get("raw") or "")[:1_000_000].strip()
                decoded_message = cls._decode_mailapi_raw_mime(raw_message)
                visible_message = cls._mailapi_visible_text(decoded_message)
                code = cls._mailapi_history_item_code(
                    {
                        "subject": subject,
                        "content": visible_message or source,
                    }
                )
                if not code:
                    continue
                received_value = item.get("created_at")
                received_at = None
                if isinstance(received_value, str):
                    import re

                    timestamp_text = received_value.strip()
                    if timestamp_text and not re.search(
                        r"(?:Z|[+-]\d{2}:?\d{2})$",
                        timestamp_text,
                        re.IGNORECASE,
                    ):
                        # Yisen returns naive `created_at` values in UTC.
                        received_at = cls._parse_timestamp(
                            f"{timestamp_text}Z"
                        )
                if received_at is None:
                    received_at = cls._parse_timestamp(received_value)
                if received_at is None:
                    continue
                raw_id = str(
                    item.get("message_id") or item.get("id") or ""
                ).strip()
                if not raw_id:
                    import hashlib

                    raw_id = hashlib.sha256(
                        "|".join(
                            (
                                str(item.get("address") or "").strip(),
                                str(received_value or "").strip(),
                                code,
                            )
                        ).encode("utf-8", errors="ignore")
                    ).hexdigest()
                candidates.append(
                    (
                        received_at,
                        -index,
                        {
                            "content": " ".join(
                                part
                                for part in (subject, visible_message, code)
                                if part
                            ),
                            "received_at": received_at,
                            "message_id": f"mailapi_message:{raw_id}",
                            "status": True,
                            "response_email": str(
                                item.get("address") or ""
                            ).strip(),
                            "mailapi_history": True,
                        },
                    )
                )
            if candidates:
                return max(candidates, key=lambda candidate: candidate[:2])[2]
            return {
                "content": "",
                "received_at": None,
                "message_id": "",
                "status": False,
                "response_email": "",
                "mailapi_history": True,
            }

        messages = payload.get("messages")
        if isinstance(messages, list):
            candidates = []
            for index, item in enumerate(messages):
                if not isinstance(item, dict):
                    continue
                verification_code = cls._mailapi_history_item_code(item)
                subject = str(item.get("subject") or "").strip()
                if (
                    not verification_code
                    or not cls._is_openai_history_subject(subject)
                ):
                    continue
                received_value = next(
                    (
                        item.get(key)
                        for key in (
                            "receivedAt",
                            "received_at",
                            "createdAt",
                            "created_at",
                            "timestamp",
                            "date",
                        )
                        if item.get(key) not in (None, "")
                    ),
                    None,
                )
                received_at = cls._parse_timestamp(received_value)
                if received_at is None:
                    continue
                candidates.append(
                    (
                        received_at if received_at is not None else float("-inf"),
                        -index,
                        item,
                        verification_code,
                        received_value,
                        received_at,
                    )
                )
            response_email = str(payload.get("email") or "").strip()
            if candidates:
                (
                    _ranked_at,
                    _ranked_index,
                    newest,
                    verification_code,
                    received_value,
                    received_at,
                ) = max(candidates, key=lambda candidate: candidate[:2])
                subject = str(newest.get("subject") or "").strip()
                response_emails = cls._mailapi_recipient_emails(
                    newest.get("to")
                    or newest.get("recipient")
                    or newest.get("recipients")
                    or newest.get("address")
                    or newest.get("email")
                )
                identity = "|".join(
                    (
                        response_email,
                        str(received_value or "").strip(),
                        verification_code,
                    )
                )
                import hashlib

                return {
                    "content": " ".join(
                        part
                        for part in (
                            subject,
                            f"verification code {verification_code}",
                        )
                        if part
                    ),
                    "received_at": received_at,
                    "message_id": "mailapi_message:" + hashlib.sha256(
                        identity.encode("utf-8", errors="ignore")
                    ).hexdigest(),
                    "status": payload.get("ok", payload.get("status")),
                    "response_email": response_email,
                    "response_emails": response_emails,
                    "mailapi_history": True,
                }
            return {
                "content": "",
                "received_at": None,
                "message_id": "",
                "status": False,
                "response_email": response_email,
                "mailapi_history": True,
            }

        content_parts = []
        for key in ("subject", "msg", "content", "text", "body", "html"):
            value = payload.get(key)
            if value is None or value is False or value == "":
                continue
            if key == "html" and isinstance(value, bool):
                continue
            content_parts.append(cls._decode_mailapi_data_uri(value))
        content = " ".join(content_parts).strip()
        received_value = next(
            (
                payload.get(key)
                for key in (
                    "received_at",
                    "receivedAt",
                    "created_at",
                    "createdAt",
                    "timestamp",
                    "date",
                )
                if payload.get(key) not in (None, "")
            ),
            None,
        )
        identity_parts = [
            str(payload.get("email") or "").strip(),
            str(received_value or "").strip(),
            str(payload.get("request_id") or payload.get("message_id") or "").strip(),
        ]
        identity = "|".join(part for part in identity_parts if part)
        message_id = ""
        if identity:
            import hashlib

            message_id = "mailapi_message:" + hashlib.sha256(
                identity.encode("utf-8", errors="ignore")
            ).hexdigest()
        return {
            "content": content or raw_text,
            "received_at": cls._parse_timestamp(received_value),
            "message_id": message_id,
            "status": payload.get("status"),
        }

    @staticmethod
    def _decode_mailapi_script_string(value: str) -> str:
        """Decode the small subset of JS escaping used by MailAPI page URLs."""
        import re
        from html import unescape

        text = unescape(str(value or ""))
        return re.sub(r"\\([\\/'\"])", r"\1", text)

    @classmethod
    def _find_legacy_mailapi_detail_urls(
        cls,
        page_text: str,
        source_url: str,
    ) -> list[str]:
        """Resolve ordered message URLs from a legacy MailAPI HTML list page."""
        import re
        from html import unescape
        from urllib.parse import quote, urljoin

        raw = str(page_text or "")
        list_marker_match = re.search(
            r"\bid\s*=\s*(['\"])message-list\1",
            raw,
            re.IGNORECASE,
        )
        detail_base_match = re.search(
            r"\bdetailBase\s*=\s*(['\"])(.*?)\1",
            raw,
            flags=re.IGNORECASE | re.DOTALL,
        )
        detail_suffix_match = re.search(
            r"\bdetailSuffix\s*=\s*(['\"])(.*?)\1",
            raw,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if (
            not list_marker_match
            or not detail_base_match
            or not detail_suffix_match
            or detail_base_match.start() <= list_marker_match.end()
        ):
            return []

        list_region = raw[list_marker_match.end() : detail_base_match.start()]
        items: list[tuple[str, str]] = []
        for anchor_match in re.finditer(
            r"<a\b([^>]*)>(.*?)</a\s*>",
            list_region,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            attrs = anchor_match.group(1)
            class_match = re.search(
                r"\bclass\s*=\s*(['\"])(.*?)\1",
                attrs,
                flags=re.IGNORECASE | re.DOTALL,
            )
            data_id_match = re.search(
                r"\bdata-id\s*=\s*(['\"])(.*?)\1",
                attrs,
                flags=re.IGNORECASE | re.DOTALL,
            )
            class_names = {
                name.lower()
                for name in re.split(
                    r"\s+",
                    unescape(class_match.group(2)).strip()
                    if class_match
                    else "",
                )
                if name
            }
            if "item" not in class_names or not data_id_match:
                continue
            items.append((data_id_match.group(2), anchor_match.group(2)))
        if not items:
            return []

        verification_subject = re.compile(
            r"(?is)\b(?:verification|login|security)\b.{0,32}\bcode\b|"
            r"\b(?:temporary|one[-\s]*time)\b.{0,32}"
            r"\b(?:code|password)\b|\botp\b|"
            r"验证码|校验码|动态码|登录代码|登錄代碼|登入代碼|"
            r"認証コード|認證碼|驗證碼"
        )
        detail_base = cls._decode_mailapi_script_string(detail_base_match.group(2))
        detail_suffix = cls._decode_mailapi_script_string(
            detail_suffix_match.group(2)
        )
        if not detail_base:
            return []

        preferred = [
            item
            for item in items
            if verification_subject.search(
                unescape(re.sub(r"<[^>]+>", " ", item[1]))
            )
        ]
        ordered = [*preferred, *(item for item in items if item not in preferred)]
        urls: list[str] = []
        for message_id, _body in ordered:
            normalized_id = unescape(str(message_id or "")).strip()
            if not normalized_id:
                continue
            detail_path = (
                f"{detail_base.rstrip('/')}/{quote(normalized_id, safe='')}"
                f"{detail_suffix}"
            )
            resolved = urljoin(str(source_url or ""), detail_path)
            if resolved not in urls:
                urls.append(resolved)
        return urls

    @classmethod
    def _find_legacy_mailapi_detail_url(
        cls,
        page_text: str,
        source_url: str,
    ) -> Optional[str]:
        """Retain the historical singular resolver for compatibility."""
        return next(
            iter(cls._find_legacy_mailapi_detail_urls(page_text, source_url)),
            None,
        )

    @staticmethod
    def _mailapi_visible_text(value: Any) -> str:
        import re
        from html import unescape

        return " ".join(
            unescape(re.sub(r"<[^>]+>", " ", str(value or ""))).split()
        )

    @classmethod
    def _mailapi_same_origin_url(cls, value: Any, source_url: str) -> Optional[str]:
        from html import unescape
        from urllib.parse import urljoin, urlsplit
        import re

        candidate = unescape(str(value or "")).strip()
        if not candidate or candidate.startswith(("#", "javascript:", "mailto:")):
            return None
        resolved = urljoin(str(source_url or ""), candidate)
        source = urlsplit(str(source_url or ""))
        target = urlsplit(resolved)
        if target.scheme not in {"http", "https"}:
            return None
        if source.netloc and target.netloc.lower() != source.netloc.lower():
            return None
        target_path = target.path.lower()
        if target_path.startswith("/cdn-cgi/l/email-protection"):
            return None
        if re.search(
            r"(?:^|/)(?:delete|remove|logout|signout|unsubscribe|settings?)(?:/|$)",
            target_path,
        ):
            return None
        return resolved

    @classmethod
    def _mailapi_detail_score(cls, url: str, label: str = "") -> int:
        import re

        haystack = f"{url} {label}".lower()
        score = 0
        if re.search(r"verification|login|security|otp|验证码|校验码|动态码", haystack):
            score += 50
        if re.search(r"detail|message|messages|mail|inbox|read", haystack):
            score += 20
        if re.search(r"delete|logout|unsubscribe|settings", haystack):
            score -= 100
        return score

    @classmethod
    def _find_json_mailapi_detail_urls(
        cls,
        payload: Any,
        source_url: str,
    ) -> list[tuple[int, str]]:
        """Find detail links in arbitrary nested MailAPI JSON responses."""
        url_keys = {
            "url",
            "href",
            "link",
            "detail_url",
            "detailurl",
            "message_url",
            "messageurl",
            "read_url",
            "readurl",
            "detail",
        }
        found: list[tuple[int, str]] = []

        def walk(node: Any, context: str = "") -> None:
            if isinstance(node, dict):
                labels = " ".join(
                    str(node.get(key) or "")
                    for key in ("subject", "title", "name", "snippet", "preview")
                )
                next_context = f"{context} {labels}".strip()
                for key, value in node.items():
                    normalized_key = str(key or "").lower().replace("-", "_")
                    if normalized_key in url_keys and isinstance(value, str):
                        if normalized_key == "detail":
                            detail_value = value.strip()
                            if not (
                                "/" in detail_value
                                or detail_value.startswith(("?", "http://", "https://"))
                            ):
                                walk(value, next_context)
                                continue
                        resolved = cls._mailapi_same_origin_url(value, source_url)
                        if resolved:
                            found.append(
                                (
                                    cls._mailapi_detail_score(resolved, next_context),
                                    resolved,
                                )
                            )
                    walk(value, next_context)
            elif isinstance(node, list):
                for item in node[:50]:
                    walk(item, context)

        walk(payload)
        return found

    @classmethod
    def _find_mailapi_detail_urls(
        cls,
        page_text: str,
        source_url: str,
    ) -> list[str]:
        """Discover same-origin message detail URLs without provider-specific markup.

        MailAPI providers commonly return a list page first, but the list markup,
        URL path, and JSON field names vary.  This bounded discoverer accepts
        direct links, data-* attributes, nested JSON links, and simple endpoint
        templates exposed in inline scripts.  It deliberately stays same-origin
        and caps the candidate set so polling cannot fan out indefinitely.
        """
        import json
        import re
        from html import unescape
        from urllib.parse import quote

        raw = str(page_text or "")
        ranked: list[tuple[int, int, str]] = []
        sequence = 0

        def add(value: Any, label: str = "", score: Optional[int] = None) -> None:
            nonlocal sequence
            if re.search(
                r"\b(?:delete|remove|logout|sign[ -]?out|unsubscribe|settings?)\b",
                f"{value} {label}".lower(),
            ):
                return
            resolved = cls._mailapi_same_origin_url(value, source_url)
            if not resolved:
                return
            rank = (
                int(score)
                if score is not None
                else cls._mailapi_detail_score(resolved, label)
            )
            ranked.append((rank, sequence, resolved))
            sequence += 1

        # Preserve the proven yangyang-style resolver as the highest-confidence
        # candidate, while allowing all other providers to use generic links.
        for index, legacy_url in enumerate(
            cls._find_legacy_mailapi_detail_urls(raw, source_url)
        ):
            add(legacy_url, score=1000 - index)

        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            payload = None
        if payload is not None:
            for score, url in cls._find_json_mailapi_detail_urls(payload, source_url):
                add(url, score=score + 100)

        # Score direct controls by their own visible label before considering
        # larger container elements.  This prevents a nearby verification row
        # from accidentally boosting an older sibling link.
        for match in re.finditer(
            r"<(?:a|button)\b([^>]*)>(.*?)</(?:a|button)\s*>",
            raw,
            re.IGNORECASE | re.DOTALL,
        ):
            attrs, body = match.groups()
            label = cls._mailapi_visible_text(body)
            for attr_match in re.finditer(
                r"\b(?:href|data-(?:href|url|detail-url|message-url|link))\s*=\s*(['\"])(.*?)\1",
                attrs,
                re.IGNORECASE | re.DOTALL,
            ):
                add(attr_match.group(2), label)

        # Read every relevant opening tag independently, including nested rows.
        # A short following-text window gives data-* controls useful context.
        element_pattern = re.compile(
            r"<(?:a|button|article|div|li|tr|section)\b([^>]*)>",
            re.IGNORECASE | re.DOTALL,
        )
        attribute_pattern = re.compile(
            r"\b(?:href|data-(?:href|url|detail-url|message-url|link))\s*=\s*(['\"])(.*?)\1",
            re.IGNORECASE | re.DOTALL,
        )
        for match in element_pattern.finditer(raw):
            attrs = match.group(1) or ""
            label = cls._mailapi_visible_text(
                raw[match.end() : match.end() + 320]
            )
            for attr_match in attribute_pattern.finditer(attrs):
                add(attr_match.group(2), label)

        # Some pages keep only data-id plus a fetch() endpoint template in a
        # script.  Resolve the common placeholder forms without executing JS.
        ids = [
            unescape(item[1]).strip()
            for item in re.findall(
                r"\b(?:data-(?:message-)?id|messageId|message_id)\s*=\s*(['\"])(.*?)\1",
                raw,
                re.IGNORECASE | re.DOTALL,
            )
            if str(item[1] or "").strip()
        ]
        templates = []
        for match in re.finditer(
            r"(['\"])([^'\"]*(?:message|messages|mail|detail|read)[^'\"]*)\1",
            raw,
            re.IGNORECASE,
        ):
            value = unescape(match.group(2)).strip()
            if "/" in value or "=" in value:
                templates.append(value)
        for template in templates[:20]:
            for message_id in ids[:20]:
                encoded_id = quote(message_id, safe="")
                candidate = template
                replaced = False
                for marker in ("{id}", "{message_id}", ":id", "%s", "%ID%"):
                    if marker in candidate:
                        candidate = candidate.replace(marker, encoded_id)
                        replaced = True
                if replaced:
                    add(candidate, "message detail template", 30)

        if not ranked and ids:
            # Last-resort bounded guesses for APIs that render only data-id.  They
            # remain same-origin and are attempted only when no explicit link was
            # discoverable.
            for message_id in ids[:5]:
                encoded_id = quote(message_id, safe="")
                for suffix in (
                    f"/message/{encoded_id}",
                    f"/messages/{encoded_id}",
                    f"/api/messages/{encoded_id}",
                ):
                    add(suffix, "message id fallback", 1)

        result: list[str] = []
        seen: set[str] = set()
        for _score, _sequence, url in sorted(ranked, key=lambda item: (-item[0], item[1])):
            if url in seen:
                continue
            seen.add(url)
            result.append(url)
            if len(result) >= 8:
                break
        return result

    @classmethod
    def _find_mailapi_detail_url(
        cls,
        page_text: str,
        source_url: str,
    ) -> Optional[str]:
        return next(iter(cls._find_mailapi_detail_urls(page_text, source_url)), None)

    def _request_mailapi(
        self,
        url: str,
        *,
        cookies: Any = None,
        headers: Optional[dict[str, str]] = None,
        timeout: int = 15,
    ):
        import requests

        request_kwargs = {
            "timeout": max(1, int(timeout or 15)),
            "proxies": getattr(self.mailbox, "_proxy", None),
        }
        if cookies:
            request_kwargs["cookies"] = cookies
        if headers:
            request_kwargs["headers"] = dict(headers)
        try:
            response = requests.get(url, **request_kwargs)
        except requests.Timeout as exc:
            raise MailboxBackendError(
                "MailAPI 请求超时",
                code="timeout",
            ) from exc
        except requests.ConnectionError as exc:
            raise MailboxBackendError(
                "MailAPI 网络连接失败",
                code="transport",
            ) from exc
        except Exception as exc:
            raise MailboxBackendError(
                "MailAPI 请求失败",
                code="transport",
            ) from exc
        if response.status_code >= 400:
            raise MailboxBackendError(
                "MailAPI 取码请求被拒绝",
                code=f"http_{int(response.status_code)}",
                http_status=int(response.status_code),
            )
        return response

    @staticmethod
    def _parse_quickmail_private_url(
        value: Any,
    ) -> Optional[tuple[str, str, str]]:
        """Parse QuickMail's fragment-only private inbox link."""
        import re
        from urllib.parse import unquote, urlsplit

        raw = str(value or "").strip()
        try:
            parsed = urlsplit(raw)
        except ValueError:
            return None
        route = str(parsed.fragment or "").lstrip("/")
        if not route.startswith("open/"):
            return None
        parts = route.split("/", 2)
        if len(parts) < 2:
            return None
        token = unquote(parts[1]).strip()
        if not re.fullmatch(r"qm_[A-Za-z0-9_-]{32,128}", token):
            return None
        hinted_email = unquote(parts[2]).strip() if len(parts) > 2 else ""
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None
        api_root = (
            f"{parsed.scheme}://{parsed.netloc}"
            f"{(parsed.path or '/quick/').rstrip('/')}/api"
        )
        return api_root, token, hinted_email

    def _fetch_quickmail_private_text(
        self,
        account: MailboxAccount,
        *,
        api_root: str,
        token: str,
        hinted_email: str,
    ) -> str:
        import json
        from urllib.parse import quote

        import requests

        session = requests.Session()
        proxy_config = getattr(self.mailbox, "_proxy", None)
        if proxy_config:
            session.proxies.update(proxy_config)

        auth_response = session.post(
            f"{api_root}/token-auth",
            json={"token": token},
            timeout=15,
        )
        if auth_response.status_code >= 400:
            raise RuntimeError(
                f"QuickMail 私密链接认证失败: HTTP {auth_response.status_code}"
            )
        try:
            auth_payload = auth_response.json()
        except (TypeError, ValueError) as exc:
            raise RuntimeError("QuickMail 私密链接认证返回非 JSON") from exc

        mailbox = (
            auth_payload.get("mailbox")
            if isinstance(auth_payload, dict)
            else None
        )
        if not isinstance(mailbox, dict):
            config_response = session.get(f"{api_root}/config", timeout=15)
            if config_response.status_code >= 400:
                raise RuntimeError(
                    f"QuickMail 收件箱配置读取失败: HTTP {config_response.status_code}"
                )
            try:
                config_payload = config_response.json()
            except (TypeError, ValueError) as exc:
                raise RuntimeError("QuickMail 收件箱配置返回非 JSON") from exc
            mailbox = (
                config_payload.get("mailbox")
                if isinstance(config_payload, dict)
                else None
            )
        if not isinstance(mailbox, dict):
            raise RuntimeError("QuickMail 私密链接未返回收件箱")

        mailbox_id = str(
            mailbox.get("mailbox_id") or mailbox.get("id") or ""
        ).strip()
        response_email = str(
            mailbox.get("full_address")
            or hinted_email
            or account.email
            or ""
        ).strip()
        account_email = str(account.email or "").strip()
        if account_email and response_email.lower() != account_email.lower():
            raise RuntimeError("QuickMail 私密链接邮箱与当前账号不一致")
        if not mailbox_id or not response_email:
            raise RuntimeError("QuickMail 收件箱标识不完整")

        emails_response = session.get(
            f"{api_root}/mailboxes/{quote(mailbox_id, safe='')}/emails",
            params={"address": response_email},
            timeout=15,
        )
        if emails_response.status_code >= 400:
            raise RuntimeError(
                f"QuickMail 邮件列表读取失败: HTTP {emails_response.status_code}"
            )
        try:
            emails_payload = emails_response.json()
        except (TypeError, ValueError) as exc:
            raise RuntimeError("QuickMail 邮件列表返回非 JSON") from exc

        rows = (
            emails_payload.get("data")
            if isinstance(emails_payload, dict)
            else None
        )
        normalized_messages = []
        for item in rows if isinstance(rows, list) else []:
            if not isinstance(item, dict):
                continue
            subject = str(item.get("subject") or "").strip()
            body = " ".join(
                str(item.get(key) or "")
                for key in ("body_text", "body_html", "text", "content")
            )
            code = self._extract_code(f"{subject} {body}", None)
            if not code:
                continue
            normalized_messages.append(
                {
                    "verificationCode": code,
                    "subject": subject,
                    "receivedAt": item.get("received_at")
                    or item.get("receivedAt")
                    or item.get("created_at")
                    or item.get("createdAt"),
                }
            )
        return json.dumps(
            {
                "ok": True,
                "email": response_email,
                "messages": normalized_messages,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _collect_mailapi_cookies(response: Any):
        import requests

        cookie_jar = requests.cookies.RequestsCookieJar()
        response_chain = [*(getattr(response, "history", None) or []), response]
        for item in response_chain:
            cookies = getattr(item, "cookies", None)
            if cookies:
                cookie_jar.update(cookies)
        return cookie_jar

    @staticmethod
    def _naturalflower_mailbox_api_url(value: Any) -> Optional[str]:
        from urllib.parse import parse_qs, quote, unquote, urlsplit, urlunsplit

        source = urlsplit(str(value or "").strip())
        if source.hostname != "pickup.naturalflower.cn":
            return None
        token = str(parse_qs(source.query).get("token", [""])[0] or "").strip()
        if not token and source.path.startswith("/p/"):
            token = unquote(source.path[3:]).strip()
        if not token:
            return None
        return urlunsplit(
            (
                source.scheme,
                source.netloc,
                f"/api/public/mailbox/{quote(token, safe='')}",
                "",
                "",
            )
        )

    @classmethod
    def _mailapi_javascript_page_api_url(
        cls,
        page_text: str,
        source_url: str,
        expected_email: str,
    ) -> Optional[str]:
        """Resolve the bounded same-origin JSON API used by an inbox shell."""
        import re
        import time
        from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

        raw = str(page_text or "")
        source = urlsplit(str(source_url or ""))
        if not source.path.startswith("/m/"):
            return None
        if not re.search(r"\bfetch\s*\(\s*api\b", raw):
            return None
        if not (
            re.search(r"encodeURIComponent\s*\(\s*email\s*\)", raw)
            and re.search(r"encodeURIComponent\s*\(\s*token\s*\)", raw)
        ):
            return None

        def assigned_value(name: str) -> str:
            match = re.search(
                rf"\b(?:const|let|var)\s+{name}\s*=\s*"
                rf"(?P<quote>['\"])(?P<value>.*?)(?P=quote)\s*;",
                raw,
                re.IGNORECASE | re.DOTALL,
            )
            return (
                cls._decode_mailapi_script_string(match.group("value"))
                if match
                else ""
            )

        page_email = assigned_value("email").strip()
        token = assigned_value("token").strip()
        if (
            not page_email
            or page_email.lower() != str(expected_email or "").strip().lower()
            or not token
            or len(token) > 4096
        ):
            return None
        endpoint_match = re.search(
            r"(?P<quote>['\"])(?P<path>/api/messages)\?email=(?P=quote)",
            raw,
            re.IGNORECASE,
        )
        if endpoint_match is None:
            return None
        endpoint = urljoin(str(source_url or ""), endpoint_match.group("path"))
        safe_endpoint = cls._mailapi_same_origin_url(endpoint, source_url)
        if not safe_endpoint:
            return None
        parsed_endpoint = urlsplit(safe_endpoint)
        query = urlencode(
            {
                "email": page_email,
                "token": token,
                "force": "1",
                "t": str(int(time.time() * 1000)),
            }
        )
        return urlunsplit(
            (
                parsed_endpoint.scheme,
                parsed_endpoint.netloc,
                parsed_endpoint.path,
                query,
                "",
            )
        )

    def _fetch_mailapi_text(self, account: MailboxAccount) -> str:
        from urllib.parse import urlsplit

        extra = account.extra or {}
        url = str(extra.get("mailapi_url") or "").strip()
        if not url:
            raise RuntimeError("mailapi_url 为空，无法轮询取码")
        quickmail_private = self._parse_quickmail_private_url(url)
        if quickmail_private is not None:
            api_root, token, hinted_email = quickmail_private
            return self._fetch_quickmail_private_text(
                account,
                api_root=api_root,
                token=token,
                hinted_email=hinted_email,
            )
        headers = None
        is_yisen = str(urlsplit(url).hostname or "").lower() == "mail.yisen.uk"
        if is_yisen:
            token = str(extra.get("mailapi_token") or "").strip()
            if not token:
                raise RuntimeError("Yisen MailAPI 凭据缺失")
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "x-lang": "zh-CN",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/136.0.0.0 Safari/537.36"
                ),
            }
        response = self._request_mailapi(url, headers=headers)
        text = str(response.text or "")
        direct_message = self._parse_mailapi_message(text, yisen=is_yisen)
        direct_content = str(direct_message.get("content") or "")
        if (
            direct_content
            and direct_content != text
            and self._extract_message_code(direct_message, text, None)
        ):
            return text
        source_url = str(getattr(response, "url", "") or url)
        page_api_url = self._mailapi_javascript_page_api_url(
            text,
            source_url,
            str(account.email or ""),
        )
        if page_api_url:
            cookies = self._collect_mailapi_cookies(response)
            api_response = self._request_mailapi(
                page_api_url,
                cookies=cookies,
                timeout=5,
            )
            return str(api_response.text or "")
        naturalflower_api_url = self._naturalflower_mailbox_api_url(source_url)
        if naturalflower_api_url:
            cookies = self._collect_mailapi_cookies(response)
            api_response = self._request_mailapi(
                naturalflower_api_url,
                cookies=cookies,
                timeout=5,
            )
            return str(api_response.text or "")
        detail_urls = self._find_mailapi_detail_urls(text, source_url)
        if not detail_urls:
            return text

        detail_log_key = (source_url, tuple(detail_urls[:3]))
        if detail_log_key not in self._detail_discovery_log_keys:
            if len(self._detail_discovery_log_keys) >= 128:
                self._detail_discovery_log_keys.clear()
            self._detail_discovery_log_keys.add(detail_log_key)
            self.mailbox._log(
                "[MailAPI] 入口为邮件列表，自动发现并跟进邮件详情"
            )
        cookies = self._collect_mailapi_cookies(response)
        # The newest/highest-confidence messages are ranked first.  Bound the
        # detail fan-out so a slow stale message cannot add up to eight 15s
        # waits to every OTP poll.
        for detail_url in detail_urls[:3]:
            try:
                detail_response = self._request_mailapi(
                    detail_url,
                    cookies=cookies,
                    timeout=5,
                )
                detail_text = str(detail_response.text or "")
                if not detail_text:
                    continue
                detail_message = self._parse_mailapi_message(detail_text)
                if self._extract_message_code(detail_message, detail_text, None):
                    return detail_text
            except MailboxBackendError:
                raise
            except Exception:
                continue
        # A discovered detail may be stale, inaccessible, or a tracking page.
        # Preserve the original list/single-message page so its own visible OTP
        # can still be parsed on this poll.
        return text

    def _extract_code(self, text: str, code_pattern: str | None) -> str:
        normalized_text = self.mailbox._decode_raw_content(text) or str(text or "")
        return str(self.mailbox._safe_extract(normalized_text, code_pattern) or "").strip()

    def _extract_message_code(
        self,
        message: dict[str, Any],
        raw_text: str,
        code_pattern: str | None,
    ) -> str:
        import re

        content = str(message.get("content") or "")
        bounded_content = bool(message.get("bounded_content"))
        if bounded_content and message.get("visible_html_content"):
            visible = self._mailapi_visible_text(content)
            code = str(
                self.mailbox._safe_extract(visible, code_pattern) or ""
            ).strip()
        else:
            visible = self.mailbox._decode_raw_content(content) or content
            code = self._extract_code(content, code_pattern)
        if (
            not code
            and not bounded_content
            and content != str(raw_text or "")
        ):
            code = self._extract_code(str(raw_text or ""), code_pattern)
        if not code:
            return ""

        if bounded_content and message.get("visible_html_content"):
            normalized = " ".join(visible.split())
        else:
            raw_visible = self.mailbox._decode_raw_content(str(raw_text or "")) or str(
                raw_text or ""
            )
            normalized_visible = " ".join(visible.split())
            normalized_raw_visible = " ".join(raw_visible.split())
            normalized = (
                normalized_visible
                if normalized_visible == normalized_raw_visible
                else " ".join(
                    f"{normalized_visible} {normalized_raw_visible}".split()
                )
            )
        semantic_otp = re.search(
            r"(?is)\b(?:verification|authentication|login|security)\s+code\b|"
            r"\b(?:temporary|one[-\s]*time)\b.{0,32}\b(?:code|password)\b|"
            r"\botp\b|验证码|校验码|动态码|登录代码|登錄代碼|登入代碼|"
            r"認証コード|認證碼|驗證碼",
            normalized,
        )
        isolated_code = re.fullmatch(r"\D{0,20}\d{6}\D{0,20}", normalized)
        if not semantic_otp and not isolated_code:
            return ""
        return code

    @staticmethod
    def _message_matches_account(
        message: dict[str, Any],
        account: MailboxAccount,
    ) -> bool:
        if not message.get("mailapi_history"):
            return True
        response_email = str(message.get("response_email") or "").strip().lower()
        account_email = str(account.email or "").strip().lower()
        if not account_email:
            return False
        if response_email:
            return response_email == account_email
        response_emails = {
            str(value or "").strip().lower()
            for value in (message.get("response_emails") or [])
            if str(value or "").strip()
        }
        return account_email in response_emails

    def get_current_ids(
        self,
        account: MailboxAccount,
        *,
        strict_backend_errors: bool = False,
    ) -> set:
        try:
            text = self._fetch_mailapi_text(account)
            from urllib.parse import urlsplit

            url = str((account.extra or {}).get("mailapi_url") or "")
            is_yisen = str(urlsplit(url).hostname or "").lower() == "mail.yisen.uk"
            message = self._parse_mailapi_message(text, yisen=is_yisen)
            if (
                message.get("status") is False
                or not self._message_matches_account(message, account)
            ):
                return set()
            code = self._extract_message_code(message, text, None)
            if not code:
                return set()
            message_id = str(message.get("message_id") or "").strip()
            return {message_id or self._code_key(code)}
        except MailboxBackendError as exc:
            if strict_backend_errors:
                raise
            self.mailbox._log(
                f"[MailAPI] 邮件基线读取失败: {getattr(exc, 'code', 'backend_error')}"
            )
            return set()
        except Exception as exc:
            if strict_backend_errors:
                raise MailboxBackendError(
                    "MailAPI 邮件基线解析失败",
                    code="parse_error",
                ) from exc
            self.mailbox._log("[MailAPI] 邮件基线解析失败")
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
        code_pattern: str | None = None,
        **kwargs,
    ) -> str:
        strict_backend_errors = bool(kwargs.pop("strict_backend_errors", False))
        seen = {str(mid) for mid in (before_ids or set())}
        exclude_codes = {
            str(code).strip()
            for code in (kwargs.get("exclude_codes") or set())
            if str(code or "").strip()
        }
        keyword_lower = str(keyword or "").strip().lower()
        return_metadata = bool(kwargs.get("return_metadata"))
        try:
            otp_sent_at = float(kwargs.get("otp_sent_at") or 0)
        except (TypeError, ValueError):
            otp_sent_at = 0.0
        try:
            poll_interval = float(kwargs.get("poll_interval") or 3)
        except (TypeError, ValueError):
            poll_interval = 3.0
        poll_interval = min(max(poll_interval, 1.0), 30.0)

        def poll_once() -> Optional[str]:
            try:
                text = self._fetch_mailapi_text(account)
            except MailboxBackendError as exc:
                if strict_backend_errors:
                    raise
                source = getattr(exc, "__cause__", None)
                source_name = type(source).__name__ if source is not None else type(exc).__name__
                self.mailbox._log(
                    f"[MailAPI] 拉取失败: {source_name} "
                    f"({getattr(exc, 'code', 'backend_error')})"
                )
                return None
            except Exception as exc:
                self.mailbox._log(
                    f"[MailAPI] 拉取失败: {type(exc).__name__ or 'Error'}"
                )
                return None

            from urllib.parse import urlsplit

            url = str((account.extra or {}).get("mailapi_url") or "")
            is_yisen = str(urlsplit(url).hostname or "").lower() == "mail.yisen.uk"
            message = self._parse_mailapi_message(text, yisen=is_yisen)
            if message.get("status") is False:
                return None
            if not self._message_matches_account(message, account):
                self.mailbox._log(
                    "[MailAPI] 邮件历史响应邮箱与当前账号不一致，已忽略"
                )
                return None
            content = str(message.get("content") or "")
            searchable_content = (
                content if content == str(text or "") else f"{content} {text}"
            )
            if keyword_lower and keyword_lower not in searchable_content.lower():
                return None
            code = self._extract_message_code(message, text, code_pattern)
            if not code:
                return None
            received_at = message.get("received_at")
            if (
                otp_sent_at
                and received_at
                and float(received_at) < otp_sent_at - 5.0
            ):
                old_message_identity = str(
                    message.get("message_id") or self._code_key(code)
                )
                old_log_key = (
                    str(account.email or "").strip().lower(),
                    old_message_identity,
                    round(otp_sent_at, 3),
                )
                if old_log_key not in self._old_message_log_keys:
                    if len(self._old_message_log_keys) >= 512:
                        self._old_message_log_keys.clear()
                    self._old_message_log_keys.add(old_log_key)
                    self.mailbox._log(
                        "[MailAPI] 当前只有发送前的旧验证码，继续等待新邮件"
                    )
                return None
            received_after_challenge = bool(
                otp_sent_at
                and received_at
                and float(received_at) >= otp_sent_at
            )
            if code in exclude_codes and not received_after_challenge:
                try:
                    from platforms.chatgpt.log_sanitizer import (
                        sanitize_chatgpt_log_message,
                    )

                    safe_log = sanitize_chatgpt_log_message(
                        f"[MailAPI] 跳过已尝试验证码: {code}"
                    )
                except Exception:
                    safe_log = "[MailAPI] 跳过已尝试验证码: [验证码已隐藏]"
                self.mailbox._log(safe_log)
                return None
            code_key = self._code_key(code)
            message_id = str(message.get("message_id") or "").strip()
            code_seen_before = code_key in seen
            message_seen_before = bool(message_id and message_id in seen)
            yisen_history_message = bool(
                is_yisen and message.get("mailapi_history") and not otp_sent_at
            )
            baseline_raced_with_new_message = bool(
                received_after_challenge
            )
            baseline_freshness_is_unverifiable = bool(
                otp_sent_at and not received_at
            )
            if (
                code_seen_before
                and not baseline_raced_with_new_message
                and not baseline_freshness_is_unverifiable
                and not yisen_history_message
            ):
                return None
            if (
                message_seen_before
                and received_at
                and not baseline_raced_with_new_message
                and not yisen_history_message
            ):
                return None
            seen.add(code_key)
            if message_id:
                seen.add(message_id)
            try:
                from platforms.chatgpt.log_sanitizer import (
                    sanitize_chatgpt_log_message,
                )

                safe_log = sanitize_chatgpt_log_message(
                    f"[MailAPI] 收到验证码: {code}"
                )
            except Exception:
                safe_log = "[MailAPI] 收到验证码: [验证码已隐藏]"
            self.mailbox._log(safe_log)
            if return_metadata:
                return MailboxVerificationResult(
                    code=code,
                    message_id=message_id,
                    received_at=received_at,
                )
            return code

        return self.mailbox._run_polling_wait(
            timeout=timeout,
            poll_interval=poll_interval,
            poll_once=poll_once,
        )


class OutlookMailbox(BaseMailbox):
    """微软邮箱（Outlook / Hotmail）本地账号池（Graph / IMAP 策略）"""

    # 类级别锁：保证多线程并发时取号互斥，防止多个实例取到同一个邮箱
    _pop_lock = threading.Lock()

    def __init__(
        self,
        imap_server: str = "",
        imap_port: int | str = 993,
        token_endpoint: str = "",
        backend: str = "graph",
        graph_api_base: str = "",
        proxy: str = None,
        lease_owner: str = "",
        lease_seconds: int = 900,
    ):
        self._lock = threading.Lock()
        self._claim_scope: MailboxClaimScope | None = None
        self._active_lease_account: MailboxAccount | None = None
        self._last_lease_renew_monotonic = 0.0
        self._lease_owner = str(lease_owner or "").strip()[:160]
        try:
            self._lease_seconds = max(1, int(lease_seconds or 900))
        except (TypeError, ValueError):
            self._lease_seconds = 900
        # Renew at least once per third of the lease lifetime.  The upper
        # bound keeps normal polling inexpensive while still handling short
        # TTLs used by tests or deliberately aggressive workers.
        self._lease_renew_interval = max(
            1.0, min(30.0, float(self._lease_seconds) / 3.0)
        )
        self._proxy = build_requests_proxy_config(proxy)
        self._imap_servers = []
        if imap_server:
            self._imap_servers.append(str(imap_server).strip())
        else:
            try:
                from platforms.chatgpt.constants import OUTLOOK_IMAP_SERVERS

                self._imap_servers.extend(
                    [
                        str(OUTLOOK_IMAP_SERVERS.get("NEW") or "").strip(),
                        str(OUTLOOK_IMAP_SERVERS.get("OLD") or "").strip(),
                    ]
                )
            except Exception:
                self._imap_servers.extend(
                    ["outlook.live.com", "outlook.office365.com"]
                )
        self._imap_servers = [
            host for host in self._imap_servers if isinstance(host, str) and host
        ]
        try:
            self._imap_port = int(imap_port or 993)
        except (TypeError, ValueError):
            self._imap_port = 993
        self._token_endpoint = str(token_endpoint or "").strip()
        self._backend_name = self._normalize_backend_name(backend)
        self._graph_api_base = (
            str(graph_api_base or "").strip() or "https://graph.microsoft.com/v1.0"
        )
        self._imap_folder_names = ["INBOX", "Junk", "Deleted Items", "Trash"]
        self._graph_folder_names = ["inbox", "junkemail", "deleteditems"]
        self._backends: dict[str, OutlookMailboxBackend] = {
            "imap": OutlookImapMailboxBackend(self),
            "graph": OutlookGraphMailboxBackend(self),
            "mailapi_url": MailApiUrlOtpBackend(self),
        }

    def _checkpoint(self, *, consume_skip: bool = True) -> None:
        """Renew a live mailbox lease while a long OTP wait is running."""
        super()._checkpoint(consume_skip=consume_skip)
        account = self._active_lease_account
        if account is None:
            return
        now = time.monotonic()
        if now - self._last_lease_renew_monotonic < self._lease_renew_interval:
            return
        if self.renew_account_lease(account):
            self._last_lease_renew_monotonic = now

    @staticmethod
    def _normalize_backend_name(value: Any) -> str:
        backend = str(value or "graph").strip().lower() or "graph"
        return backend if backend in {"graph", "imap"} else "graph"

    @staticmethod
    def _normalize_account_type(value: Any) -> str:
        account_type = str(value or "").strip().lower()
        if account_type in {"mailapi_url", "microsoft_oauth"}:
            return account_type
        return "microsoft_oauth"

    def _is_mailapi_account(self, account: MailboxAccount) -> bool:
        extra = getattr(account, "extra", None) or {}
        account_type = self._normalize_account_type(extra.get("account_type"))
        if account_type == "mailapi_url":
            return True
        return bool(str(extra.get("mailapi_url") or "").strip())

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.now(timezone.utc)

    def _resolve_lease_owner(self, lease_owner: str = "") -> str:
        owner = str(
            lease_owner
            or self._lease_owner
            or getattr(self, "_task_attempt_token", "")
            or ""
        ).strip()
        if owner:
            if not self._lease_owner:
                self._lease_owner = owner[:160]
            return owner[:160]
        self._lease_owner = f"mailbox-{uuid.uuid4().hex}"
        return self._lease_owner

    @staticmethod
    def _lease_extra(account: MailboxAccount) -> dict[str, Any]:
        extra = getattr(account, "extra", None)
        return extra if isinstance(extra, dict) else {}

    @staticmethod
    def _payload_from_row(row) -> dict[str, Any]:
        return {
            "id": row.id,
            "email": row.email,
            "password": row.password,
            "client_id": row.client_id,
            "refresh_token": row.refresh_token,
            "account_type": getattr(row, "account_type", "microsoft_oauth"),
            "mailapi_url": getattr(row, "mailapi_url", ""),
            "mailapi_token": getattr(row, "mailapi_token", ""),
            "state": str(getattr(row, "state", "available") or "available"),
            "lease_owner": str(getattr(row, "lease_owner", "") or ""),
            "lease_expires_at": getattr(row, "lease_expires_at", None),
            "lease_version": int(getattr(row, "lease_version", 0) or 0),
            "bound_account_id": int(getattr(row, "bound_account_id", 0) or 0),
            "bound_at": getattr(row, "bound_at", None),
            "created_at": getattr(row, "created_at", None),
        }

    def _claim_account_payload(
        self,
        *,
        target_email: str = "",
        lease_owner: str = "",
        lease_seconds: int | None = None,
        exclude_emails=None,
    ) -> dict[str, Any]:
        """Claim one row with a cross-process SQLite CAS.

        Selection and mutation happen in a short transaction.  The email
        credentials remain in ``outlook_accounts`` for restart recovery; only
        the allocation state changes.
        """
        from sqlalchemy import func, or_, update
        from sqlmodel import Session, select
        from core.db import OutlookAccountModel, engine

        owner = self._resolve_lease_owner(lease_owner)
        try:
            ttl = max(1, int(lease_seconds or self._lease_seconds))
        except (TypeError, ValueError):
            ttl = self._lease_seconds
        normalized_email = str(target_email or "").strip().lower()
        normalized_excluded = {
            str(value or "").strip().lower()
            for value in (exclude_emails or ())
            if str(value or "").strip()
        }
        now = self._utcnow()
        expires = now + timedelta(seconds=ttl)
        with OutlookMailbox._pop_lock:
            with Session(engine) as session:
                query = select(OutlookAccountModel).where(
                    OutlookAccountModel.state.in_(["available", "failed"])
                )
                # ``enabled`` is a compatibility projection still written by
                # the import UI.  Keep it as a safety guard for rows created
                # by older/import paths that set enabled=False without
                # explicitly setting the new state column.
                query = query.where(
                    or_(
                        OutlookAccountModel.state == "failed",
                        OutlookAccountModel.enabled == True,
                    )
                )
                # Never hand out a row that already carries a local-account
                # binding, even if an older writer left its state projection
                # inconsistent.
                query = query.where(
                    or_(
                        OutlookAccountModel.bound_account_id == 0,
                        OutlookAccountModel.bound_account_id.is_(None),
                    )
                )
                if normalized_email:
                    query = query.where(
                        func.lower(OutlookAccountModel.email) == normalized_email
                    )
                if normalized_excluded:
                    query = query.where(
                        func.lower(OutlookAccountModel.email).notin_(
                            normalized_excluded
                        )
                    )
                candidates = session.exec(
                    query.order_by(OutlookAccountModel.id)
                ).all()
                for candidate in candidates:
                    old_version = int(candidate.lease_version or 0)
                    candidate_state = str(candidate.state or "available")
                    result = session.exec(
                        update(OutlookAccountModel)
                        .where(OutlookAccountModel.id == candidate.id)
                        .where(OutlookAccountModel.state == candidate_state)
                        .where(OutlookAccountModel.lease_version == old_version)
                        .values(
                            state="leased",
                            enabled=False,
                            lease_owner=owner,
                            lease_expires_at=expires,
                            lease_version=old_version + 1,
                            quarantine_reason="",
                            last_error="",
                            updated_at=now,
                        )
                    )
                    if int(getattr(result, "rowcount", 0) or 0) != 1:
                        session.rollback()
                        continue
                    session.commit()
                    row = session.get(OutlookAccountModel, candidate.id)
                    if row is None:
                        break
                    return self._payload_from_row(row)
        if normalized_email:
            raise RuntimeError(f"指定邮箱不在可用池中: {target_email}")
        raise RuntimeError("微软邮箱账号池为空，请先在设置页批量导入")

    def claim_account(
        self,
        target_email: str = "",
        lease_owner: str = "",
        lease_seconds: int | None = None,
        exclude_emails=None,
        **kwargs,
    ) -> MailboxAccount:
        """Lease a mailbox without deleting its credentials row."""
        if not target_email and kwargs.get("email"):
            target_email = kwargs["email"]
        if not lease_owner and kwargs.get("owner"):
            lease_owner = kwargs["owner"]
        if lease_seconds is None:
            lease_seconds = kwargs.get(
                "lease_ttl",
                kwargs.get("ttl_seconds", kwargs.get("ttl")),
            )
        payload = self._claim_account_payload(
            target_email=target_email,
            lease_owner=lease_owner,
            lease_seconds=lease_seconds,
            exclude_emails=exclude_emails,
        )
        return self._mailbox_account_from_payload(payload)

    def _pop_account(self, email: str = "") -> dict:
        """Legacy payload API backed by :meth:`claim_account`."""
        return self._claim_account_payload(target_email=email)

    def get_email(self) -> MailboxAccount:
        if self._claim_scope is not None:
            return self._claim_scope.claim(
                lambda exclude_emails: self.claim_account(
                    exclude_emails=exclude_emails,
                )
            )
        return self.claim_account()

    def bind_claim_scope(self, scope: MailboxClaimScope | None) -> None:
        self._claim_scope = scope

    def get_email_by_address(self, email: str) -> MailboxAccount:
        """Claim the exact available mailbox used by a retry binding."""
        return self.claim_account(target_email=email)

    def _mailbox_account_from_payload(self, payload: dict) -> MailboxAccount:
        email = str(payload.get("email") or "").strip()
        if not email:
            raise RuntimeError("微软邮箱账号邮箱为空")
        password = str(payload.get("password") or "")
        client_id = str(payload.get("client_id") or "")
        refresh_token = str(payload.get("refresh_token") or "")
        account_type = self._normalize_account_type(payload.get("account_type"))
        mailapi_url = str(payload.get("mailapi_url") or "").strip()
        mailapi_token = str(payload.get("mailapi_token") or "").strip()
        state = str(payload.get("state") or "leased").strip().lower() or "leased"
        lease_owner = str(payload.get("lease_owner") or "").strip()
        lease_version = int(payload.get("lease_version") or 0)
        bound_account_id = int(payload.get("bound_account_id") or 0)
        lease_expires_at = payload.get("lease_expires_at")
        created_at = payload.get("created_at")
        auth_mode = (
            "mailapi_url"
            if account_type == "mailapi_url"
            else ("oauth" if client_id and refresh_token else "password")
        )
        self._log(f"[微软邮箱] 领取账号: {email}（租约状态={state}）")
        self._log(
            "[微软邮箱] 账号认证信息: "
            f"has_password={bool(password)} "
            f"has_client_id={bool(client_id)} "
            f"has_refresh_token={bool(refresh_token)} "
            f"has_mailapi_url={bool(mailapi_url)} "
            f"has_mailapi_token={bool(mailapi_token)} "
            f"account_type={account_type} "
            f"auth_mode={auth_mode}"
        )
        account = MailboxAccount(
            email=email,
            account_id=str(payload.get("id") or ""),
            extra={
                "provider": "microsoft",
                "password": password,
                "client_id": client_id,
                "refresh_token": refresh_token,
                "account_type": account_type,
                "mailapi_url": mailapi_url,
                "mailapi_token": mailapi_token,
                "outlook_backend": self._backend_name,
                "_outlook_row_id": str(payload.get("id") or ""),
                "_outlook_lease_owner": lease_owner,
                "_outlook_lease_version": lease_version,
                "_outlook_state": state,
                "_outlook_bound_account_id": bound_account_id,
                "_outlook_lease_expires_at": (
                    lease_expires_at.isoformat()
                    if isinstance(lease_expires_at, datetime)
                    else str(lease_expires_at or "")
                ),
                "_outlook_created_at": (
                    created_at.isoformat()
                    if isinstance(created_at, datetime)
                    else str(created_at or "")
                ),
            },
        )
        self._active_lease_account = account
        self._last_lease_renew_monotonic = time.monotonic()
        return account

    def _lease_identity(self, account: MailboxAccount) -> tuple[int, str, int, int]:
        extra = self._lease_extra(account)
        try:
            row_id = int(extra.get("_outlook_row_id") or getattr(account, "account_id", 0) or 0)
        except (TypeError, ValueError):
            row_id = 0
        try:
            version = int(extra.get("_outlook_lease_version") or 0)
        except (TypeError, ValueError):
            version = 0
        try:
            bound_id = int(
                extra.get("_outlook_bound_account_id")
                or extra.get("chatgpt_local_account_id")
                or 0
            )
        except (TypeError, ValueError):
            bound_id = 0
        owner = str(extra.get("_outlook_lease_owner") or "").strip()
        return row_id, owner, version, bound_id

    @classmethod
    def _lease_created_at(cls, account: MailboxAccount) -> datetime | None:
        extra = cls._lease_extra(account)
        value = extra.get("_outlook_created_at")
        if isinstance(value, datetime):
            return value
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None

    @staticmethod
    def _created_at_identity(value: Any) -> str:
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value or "").strip().replace("Z", "+00:00")

    @staticmethod
    def _set_lease_projection(
        account: MailboxAccount,
        *,
        state: str,
        lease_owner: str = "",
        lease_version: int = 0,
        bound_account_id: int = 0,
        lease_expires_at=None,
    ) -> None:
        extra = dict(getattr(account, "extra", None) or {})
        extra.update(
            {
                "_outlook_state": str(state or ""),
                "_outlook_lease_owner": str(lease_owner or ""),
                "_outlook_lease_version": int(lease_version or 0),
                "_outlook_bound_account_id": int(bound_account_id or 0),
                "_outlook_lease_expires_at": (
                    lease_expires_at.isoformat()
                    if isinstance(lease_expires_at, datetime)
                    else str(lease_expires_at or "")
                ),
            }
        )
        account.extra = extra

    def bind_account(self, account: MailboxAccount, account_id: int) -> bool:
        """Fence a lease to the ChatGPT account that consumed it."""
        from sqlalchemy import func, update
        from sqlmodel import Session, select
        from core.db import AccountModel, OutlookAccountModel, engine

        try:
            local_account_id = int(account_id or 0)
        except (TypeError, ValueError):
            return False
        row_id, owner, version, _bound_id = self._lease_identity(account)
        expected_created_at_identity = self._created_at_identity(
            self._lease_extra(account).get("_outlook_created_at")
        )
        if row_id <= 0 or local_account_id <= 0:
            return False
        mailbox_email = str(getattr(account, "email", "") or "").strip().lower()
        if not mailbox_email:
            return False
        with self._lock:
            with Session(engine) as session:
                identity_query = (
                    select(AccountModel.id)
                    .where(AccountModel.id == local_account_id)
                    .where(func.lower(AccountModel.platform) == "chatgpt")
                    .where(func.lower(AccountModel.email) == mailbox_email)
                )
                if session.exec(identity_query).first() is None:
                    return False
                identity_exists = identity_query.exists()
                row = session.get(OutlookAccountModel, row_id)
                if row is None:
                    return False
                if expected_created_at_identity and self._created_at_identity(
                    row.created_at
                ) != expected_created_at_identity:
                    return False
                current_version = int(row.lease_version or 0)
                if (
                    str(row.state or "").lower() == "bound"
                    and int(row.bound_account_id or 0) == local_account_id
                ):
                    self._set_lease_projection(
                        account,
                        state="bound",
                        lease_version=current_version,
                        bound_account_id=local_account_id,
                    )
                    if self._active_lease_account is account:
                        self._active_lease_account = None
                    return True
                if (
                    str(row.state or "").lower() == "bound"
                    and int(row.bound_account_id or 0) == 0
                    and str(row.email or "").strip().lower()
                    == str(getattr(account, "email", "") or "").strip().lower()
                ):
                    # Persisted projections from older callers may not carry
                    # a fencing version.  If one is present, require an
                    # exact match so a stale retry cannot bind a newer row.
                    if version > 0 and version != current_version:
                        return False
                    result = session.exec(
                        update(OutlookAccountModel)
                        .where(OutlookAccountModel.id == row_id)
                        .where(OutlookAccountModel.state == "bound")
                        .where(OutlookAccountModel.bound_account_id == 0)
                        .where(OutlookAccountModel.lease_version == current_version)
                        .where(OutlookAccountModel.created_at == row.created_at)
                        .where(identity_exists)
                        .values(
                            bound_account_id=local_account_id,
                            lease_version=current_version + 1,
                            bound_at=row.bound_at or self._utcnow(),
                            updated_at=self._utcnow(),
                        )
                    )
                    if int(getattr(result, "rowcount", 0) or 0) != 1:
                        session.rollback()
                        return False
                    session.commit()
                    self._set_lease_projection(
                        account,
                        state="bound",
                        lease_version=current_version + 1,
                        bound_account_id=local_account_id,
                    )
                    if self._active_lease_account is account:
                        self._active_lease_account = None
                    return True
                if (
                    str(row.state or "").lower() != "leased"
                    or not owner
                    or version != current_version
                ):
                    return False
                new_version = int(row.lease_version or 0) + 1
                result = session.exec(
                    update(OutlookAccountModel)
                    .where(OutlookAccountModel.id == row_id)
                    .where(OutlookAccountModel.state == "leased")
                    .where(OutlookAccountModel.lease_owner == owner)
                    .where(OutlookAccountModel.lease_version == version)
                    .where(OutlookAccountModel.created_at == row.created_at)
                    .where(identity_exists)
                    .values(
                        state="bound",
                        enabled=False,
                        lease_owner="",
                        lease_expires_at=None,
                        lease_version=new_version,
                        bound_account_id=local_account_id,
                        bound_at=self._utcnow(),
                        last_used=self._utcnow(),
                        updated_at=self._utcnow(),
                    )
                )
                if int(getattr(result, "rowcount", 0) or 0) != 1:
                    session.rollback()
                    return False
                session.commit()
                self._set_lease_projection(
                    account,
                    state="bound",
                    lease_version=new_version,
                    bound_account_id=local_account_id,
                )
                if self._active_lease_account is account:
                    self._active_lease_account = None
                return True

    def release_account(
        self,
        account: MailboxAccount,
        *,
        uncertain: bool = False,
        failed: bool = False,
    ) -> bool:
        """Release an owned lease, or quarantine it when remote state is unclear."""
        from sqlalchemy import update
        from sqlmodel import Session
        from core.db import OutlookAccountModel, engine

        row_id, owner, version, _bound_id = self._lease_identity(account)
        expected_created_at = self._lease_created_at(account)
        if row_id <= 0 or not owner:
            return False
        state = "failed" if failed else ("quarantined" if uncertain else "available")
        reason = (
            "login_failed_before_commit"
            if failed
            else ("remote_state_uncertain" if uncertain else "")
        )
        extra = self._lease_extra(account)
        last_error = str(extra.get("_outlook_last_error") or "")[:500]
        # A persisted retry context can intentionally contain only the lease
        # pointer.  Do not turn missing credential keys into blank values and
        # erase the durable mailbox credentials during release.
        credential_values: dict[str, Any] = {}
        if "password" in extra:
            credential_values["password"] = str(extra.get("password") or "")
        if "client_id" in extra:
            credential_values["client_id"] = str(extra.get("client_id") or "")
        if "refresh_token" in extra:
            credential_values["refresh_token"] = str(
                extra.get("refresh_token") or ""
            )
        if "account_type" in extra:
            credential_values["account_type"] = self._normalize_account_type(
                extra.get("account_type")
            )
        if "mailapi_url" in extra or "mail_api_url" in extra:
            credential_values["mailapi_url"] = str(
                extra.get("mailapi_url") or extra.get("mail_api_url") or ""
            )
        if "mailapi_token" in extra:
            credential_values["mailapi_token"] = str(
                extra.get("mailapi_token") or ""
            )
        with self._lock:
            with Session(engine) as session:
                query = (
                    update(OutlookAccountModel)
                    .where(OutlookAccountModel.id == row_id)
                    .where(OutlookAccountModel.state == "leased")
                    .where(OutlookAccountModel.lease_owner == owner)
                    .where(OutlookAccountModel.lease_version == version)
                )
                if expected_created_at is not None:
                    query = query.where(
                        OutlookAccountModel.created_at == expected_created_at
                    )
                result = session.exec(
                    query.values(
                        **credential_values,
                        state=state,
                        enabled=(state == "available"),
                        lease_owner="",
                        lease_expires_at=None,
                        lease_version=version + 1,
                        bound_account_id=0,
                        bound_at=None,
                        quarantine_reason=reason,
                        last_error=last_error,
                        updated_at=self._utcnow(),
                    )
                )
                if int(getattr(result, "rowcount", 0) or 0) != 1:
                    session.rollback()
                    return False
                session.commit()
        self._set_lease_projection(
            account,
            state=state,
            lease_version=version + 1,
        )
        if self._active_lease_account is account:
            self._active_lease_account = None
        return True

    def fail_account(
        self,
        account: MailboxAccount,
        *,
        error: str = "",
        task_id: str = "",
    ) -> bool:
        extra = self._lease_extra(account)
        extra["_outlook_last_error"] = str(error or "")[:500]
        if task_id:
            extra["_outlook_last_task_id"] = str(task_id)[:128]
        return self.release_account(account, failed=True)

    def discard_account(self, account: MailboxAccount, *, reason: str = "") -> bool:
        """Archive an owned lease after a Free-plan gate or explicit discard."""
        from sqlalchemy import update
        from sqlmodel import Session
        from core.db import OutlookAccountModel, engine

        row_id, owner, version, _bound_id = self._lease_identity(account)
        expected_created_at = self._lease_created_at(account)
        if row_id <= 0 or not owner:
            return False
        with self._lock:
            with Session(engine) as session:
                query = (
                    update(OutlookAccountModel)
                    .where(OutlookAccountModel.id == row_id)
                    .where(OutlookAccountModel.state == "leased")
                    .where(OutlookAccountModel.lease_owner == owner)
                    .where(OutlookAccountModel.lease_version == version)
                )
                if expected_created_at is not None:
                    query = query.where(
                        OutlookAccountModel.created_at == expected_created_at
                    )
                result = session.exec(
                    query.values(
                        state="discarded",
                        enabled=False,
                        lease_owner="",
                        lease_expires_at=None,
                        lease_version=version + 1,
                        bound_account_id=0,
                        bound_at=None,
                        quarantine_reason=str(reason or "discarded")[:200],
                        last_error="",
                        updated_at=self._utcnow(),
                    )
                )
                if int(getattr(result, "rowcount", 0) or 0) != 1:
                    session.rollback()
                    return False
                session.commit()
        self._set_lease_projection(
            account,
            state="discarded",
            lease_version=version + 1,
        )
        if self._active_lease_account is account:
            self._active_lease_account = None
        return True

    def renew_account_lease(
        self,
        account: MailboxAccount,
        lease_seconds: int | None = None,
    ) -> bool:
        """Extend a live lease and advance its fencing version."""
        from sqlalchemy import update
        from sqlmodel import Session
        from core.db import OutlookAccountModel, engine

        row_id, owner, version, _bound_id = self._lease_identity(account)
        expected_created_at = self._lease_created_at(account)
        if row_id <= 0 or not owner:
            return False
        try:
            ttl = max(1, int(lease_seconds or self._lease_seconds))
        except (TypeError, ValueError):
            ttl = self._lease_seconds
        now = self._utcnow()
        expires = now + timedelta(seconds=ttl)
        with self._lock:
            with Session(engine) as session:
                query = (
                    update(OutlookAccountModel)
                    .where(OutlookAccountModel.id == row_id)
                    .where(OutlookAccountModel.state == "leased")
                    .where(OutlookAccountModel.lease_owner == owner)
                    .where(OutlookAccountModel.lease_version == version)
                    .where(OutlookAccountModel.lease_expires_at > now)
                )
                if expected_created_at is not None:
                    query = query.where(
                        OutlookAccountModel.created_at == expected_created_at
                    )
                result = session.exec(
                    query.values(
                        lease_expires_at=expires,
                        lease_version=version + 1,
                        updated_at=self._utcnow(),
                    )
                )
                if int(getattr(result, "rowcount", 0) or 0) != 1:
                    session.rollback()
                    return False
                session.commit()
        self._set_lease_projection(
            account,
            state="leased",
            lease_owner=owner,
            lease_version=version + 1,
            lease_expires_at=expires,
        )
        return True

    def recover_expired_leases(self, now: datetime | None = None) -> int:
        from core.db import recover_expired_outlook_leases

        return recover_expired_outlook_leases(now=now)

    def _seal_account_bound(self, account: MailboxAccount) -> bool:
        """Make a consumed lease non-allocatable before its local ID exists."""
        from sqlalchemy import update
        from sqlmodel import Session
        from core.db import OutlookAccountModel, engine

        row_id, owner, version, _bound_id = self._lease_identity(account)
        expected_created_at = self._lease_created_at(account)
        if row_id <= 0 or not owner:
            return False
        now = self._utcnow()
        with self._lock:
            with Session(engine) as session:
                query = (
                    update(OutlookAccountModel)
                    .where(OutlookAccountModel.id == row_id)
                    .where(OutlookAccountModel.state == "leased")
                    .where(OutlookAccountModel.lease_owner == owner)
                    .where(OutlookAccountModel.lease_version == version)
                )
                if expected_created_at is not None:
                    query = query.where(
                        OutlookAccountModel.created_at == expected_created_at
                    )
                result = session.exec(
                    query.values(
                        state="bound",
                        enabled=False,
                        lease_owner="",
                        lease_expires_at=None,
                        lease_version=version + 1,
                        bound_account_id=0,
                        bound_at=now,
                        last_used=now,
                        updated_at=now,
                    )
                )
                if int(getattr(result, "rowcount", 0) or 0) != 1:
                    session.rollback()
                    return False
                session.commit()
        self._set_lease_projection(
            account,
            state="bound",
            lease_version=version + 1,
        )
        return True

    def mark_account_used(self, account) -> bool:
        """Bind a claimed mailbox to a persisted ChatGPT account.

        The registration code historically calls this method with either the
        transient ``MailboxAccount`` or the saved platform ``AccountModel``;
        accept both shapes while requiring a concrete local account id before
        changing a lease to ``bound``.
        """
        if isinstance(account, MailboxAccount):
            mailbox_account = account
            extra = dict(getattr(account, "extra", None) or {})
            local_id = extra.get("chatgpt_local_account_id") or extra.get(
                "_outlook_bound_account_id"
            )
        else:
            try:
                local_id = int(getattr(account, "id", 0) or 0)
            except (TypeError, ValueError):
                local_id = 0
            saved_extra = {}
            try:
                saved_extra = dict(account.get_extra() or {})
            except Exception:
                saved_extra = {}
            context = saved_extra.get("mailbox_login_context")
            if not isinstance(context, dict):
                return False
            context_extra = dict(context.get("extra") or {})
            mailbox_account = MailboxAccount(
                email=str(context.get("email") or getattr(account, "email", "")),
                account_id=str(context.get("account_id") or ""),
                extra=context_extra,
            )
            context_extra["chatgpt_local_account_id"] = local_id
            mailbox_account.extra = context_extra
        try:
            local_id = int(local_id or 0)
        except (TypeError, ValueError):
            local_id = 0
        if local_id <= 0:
            # The transient registration callback runs before AccountModel is
            # inserted.  Keep the lease owned until the caller can bind it to
            # the real local account id; sealing here would lose the owner and
            # make the post-save CAS impossible.
            return False
        return self.bind_account(mailbox_account, local_id)

    def requeue_account(
        self,
        account: MailboxAccount,
        *,
        uncertain: bool = False,
    ) -> bool:
        """Compatibility wrapper for releasing the exact owned lease."""
        return self.release_account(account, uncertain=uncertain)

    def commit_password_reset(
        self,
        account: MailboxAccount,
        new_password: str = "",
    ) -> bool:
        """Persist a ChatGPT password alongside a MailAPI mailbox record."""
        from sqlalchemy import update
        from sqlmodel import Session
        from core.db import _utcnow, engine, OutlookAccountModel

        password = str(new_password or "").strip()
        if not str(getattr(account, "email", "") or "").strip() or len(password) < 12:
            return False
        account_extra = dict(getattr(account, "extra", None) or {})
        mailapi_token = str(account_extra.get("mailapi_token") or "").strip()
        row_id, owner, version, bound_id = self._lease_identity(account)
        expected_created_at = self._lease_created_at(account)
        if row_id <= 0:
            return False
        now = _utcnow()
        with self._lock:
            with Session(engine) as session:
                values = {
                    "password": password,
                    "updated_at": now,
                    "lease_version": version + 1,
                }
                if mailapi_token:
                    values["mailapi_token"] = mailapi_token
                query = update(OutlookAccountModel).where(
                    OutlookAccountModel.id == row_id
                ).where(OutlookAccountModel.lease_version == version)
                if expected_created_at is not None:
                    query = query.where(
                        OutlookAccountModel.created_at == expected_created_at
                    )
                if owner:
                    query = query.where(OutlookAccountModel.state == "leased")
                    query = query.where(OutlookAccountModel.lease_owner == owner)
                    query = query.where(OutlookAccountModel.lease_expires_at > now)
                elif bound_id > 0:
                    query = query.where(OutlookAccountModel.state == "bound")
                    query = query.where(
                        OutlookAccountModel.bound_account_id == bound_id
                    )
                else:
                    return False
                result = session.exec(query.values(**values))
                if int(getattr(result, "rowcount", 0) or 0) != 1:
                    session.rollback()
                    return False
                session.commit()
        account_extra["password"] = password
        account_extra["password_reset_required"] = False
        account_extra.pop("new_password", None)
        account_extra["_outlook_lease_version"] = version + 1
        account.extra = account_extra
        return True

    def _token_endpoints(self) -> list[str]:
        if self._token_endpoint:
            return [self._token_endpoint]
        try:
            from platforms.chatgpt.constants import MICROSOFT_TOKEN_ENDPOINTS

            return [
                MICROSOFT_TOKEN_ENDPOINTS.get("CONSUMERS", ""),
                MICROSOFT_TOKEN_ENDPOINTS.get("LIVE", ""),
                MICROSOFT_TOKEN_ENDPOINTS.get("COMMON", ""),
            ]
        except Exception:
            return [
                "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
                "https://login.live.com/oauth20_token.srf",
                "https://login.microsoftonline.com/common/oauth2/v2.0/token",
            ]

    def _oauth_scope_candidates(
        self,
        preferred_backend: str | None = None,
    ) -> list[tuple[str, str]]:
        candidates: list[tuple[str, str]] = []
        try:
            from platforms.chatgpt.constants import MICROSOFT_SCOPES

            scope_map = {
                "imap_new": str(MICROSOFT_SCOPES.get("IMAP_NEW") or "").strip(),
                "outlook_default": "https://outlook.office.com/.default offline_access",
                "graph_default": str(MICROSOFT_SCOPES.get("GRAPH_API") or "").strip(),
                "empty": "",
            }
        except Exception:
            scope_map = {
                "imap_new": "https://outlook.office.com/IMAP.AccessAsUser.All offline_access",
                "outlook_default": "https://outlook.office.com/.default offline_access",
                "graph_default": "https://graph.microsoft.com/.default",
                "empty": "",
            }

        backend = self._normalize_backend_name(preferred_backend or self._backend_name)
        ordered_labels = (
            ["graph_default", "outlook_default", "imap_new", "empty"]
            if backend == "graph"
            else ["imap_new", "outlook_default", "graph_default", "empty"]
        )
        raw_candidates = [(label, scope_map.get(label, "")) for label in ordered_labels]

        seen = set()
        for label, scope in raw_candidates:
            key = (str(label or "").strip(), str(scope or "").strip())
            if key in seen:
                continue
            seen.add(key)
            candidates.append(key)
        return candidates

    @staticmethod
    def _redact_oauth_error_detail(value: Any, *secrets: str) -> str:
        import re

        detail = str(value or "")
        for secret in secrets:
            normalized = str(secret or "").strip()
            if normalized:
                detail = detail.replace(normalized, "***")
        detail = re.sub(
            r"(?i)(\b(?:access_token|refresh_token|id_token|client_secret|password|assertion)\b\s*[\"']?\s*[:=]\s*[\"']?)[^\s\"',}&]+",
            r"\1***",
            detail,
        )
        detail = re.sub(
            r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+",
            r"\1***",
            detail,
        )
        return detail[:500]

    def _oauth_error_detail(self, response: Any, *secrets: str) -> str:
        payload: dict[str, Any] = {}
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                payload = parsed
        except Exception:
            try:
                parsed = json.loads(str(getattr(response, "text", "") or ""))
                if isinstance(parsed, dict):
                    payload = parsed
            except Exception:
                payload = {}

        parts = []
        error = str(payload.get("error") or "").strip()
        description = str(payload.get("error_description") or "").strip()
        if error:
            parts.append(error)
        if description:
            parts.append(description)
        error_codes = payload.get("error_codes")
        if isinstance(error_codes, list) and error_codes:
            parts.append(f"error_code={error_codes[0]}")
        if not parts:
            parts.append(f"HTTP {getattr(response, 'status_code', 0) or 0}")
        return self._redact_oauth_error_detail(": ".join(parts), *secrets)

    def probe_oauth_availability(
        self,
        *,
        email: str,
        client_id: str,
        refresh_token: str,
        preferred_backend: str | None = None,
    ) -> dict[str, Any]:
        if not client_id or not refresh_token:
            self._log(
                f"[微软邮箱] OAuth token 跳过: email={email} has_client_id={bool(client_id)} has_refresh_token={bool(refresh_token)}"
            )
            return {
                "ok": False,
                "reason": "missing_oauth_credentials",
                "message": "缺少 client_id 或 refresh_token，无法通过微软邮箱可用性检测",
            }

        import requests

        last_error = ""
        for endpoint in self._token_endpoints():
            endpoint = str(endpoint or "").strip()
            if not endpoint:
                continue
            for scope_label, scope in self._oauth_scope_candidates(preferred_backend):
                payload = {
                    "client_id": client_id,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                }
                if scope:
                    payload["scope"] = scope
                try:
                    self._log(
                        "[微软邮箱] OAuth token 请求: "
                        f"email={email} endpoint={endpoint} scope_label={scope_label} has_scope={bool(scope)}"
                    )
                    resp = requests.post(
                        endpoint,
                        data=payload,
                        timeout=20,
                        proxies=self._proxy,
                    )
                    self._log(
                        "[微软邮箱] OAuth token 响应: "
                        f"email={email} endpoint={endpoint} scope_label={scope_label} status={resp.status_code}"
                    )
                except Exception as exc:
                    last_error = self._redact_oauth_error_detail(
                        exc,
                        refresh_token,
                    )
                    self._log(
                        "[微软邮箱] OAuth token 请求异常: "
                        f"email={email} endpoint={endpoint} scope_label={scope_label} error={last_error}"
                    )
                    continue

                body_text = str(resp.text or "")
                if resp.status_code >= 400:
                    safe_error = self._oauth_error_detail(resp, refresh_token)
                    self._log(f"[微软邮箱] OAuth token 失败响应: {safe_error[:200]}")
                    lowered = body_text.lower()
                    if "invalid_grant" in lowered and "service abuse mode" in lowered:
                        return {
                            "ok": False,
                            "reason": "service_abuse_mode",
                            "message": "微软邮箱可用性检测未通过，账号处于 service abuse mode",
                            "status_code": resp.status_code,
                            "endpoint": endpoint,
                            "scope_label": scope_label,
                        }
                    last_error = safe_error
                    continue

                try:
                    data = resp.json() if resp.content else {}
                    access_token = str(data.get("access_token") or "").strip()
                    if access_token:
                        expires_in = data.get("expires_in")
                        try:
                            expires_in_value = max(int(expires_in or 0), 0)
                        except (TypeError, ValueError):
                            expires_in_value = 0
                        self._log(
                            f"[微软邮箱] OAuth access token 获取成功: {email} (scope_label={scope_label})"
                        )
                        return {
                            "ok": True,
                            "reason": "ok",
                            "message": "微软邮箱可用性检测通过",
                            "access_token": access_token,
                            "refresh_token": str(
                                data.get("refresh_token") or refresh_token
                            ).strip(),
                            "scope_label": scope_label,
                            "endpoint": endpoint,
                            "expires_in": expires_in_value,
                        }

                    self._log(
                        f"[微软邮箱] OAuth token 响应未包含 access_token: keys={sorted(list(data.keys()))[:10]}"
                    )
                    last_error = "OAuth 响应未包含 access_token"
                except Exception as exc:
                    last_error = "OAuth 响应解析失败"
                    self._log(
                        "[微软邮箱] OAuth token 响应解析异常: "
                        f"email={email} endpoint={endpoint} scope_label={scope_label} error={type(exc).__name__ or 'Error'}"
                    )
                    continue

        return {
            "ok": False,
            "reason": "oauth_token_failed",
            "message": f"微软邮箱可用性检测未通过: {last_error or 'OAuth token 获取失败'}",
        }

    def _fetch_oauth_token_bundle(
        self,
        *,
        email: str,
        client_id: str,
        refresh_token: str,
        preferred_backend: str | None = None,
    ) -> dict[str, Any]:
        probe = self.probe_oauth_availability(
            email=email,
            client_id=client_id,
            refresh_token=refresh_token,
            preferred_backend=preferred_backend,
        )
        if probe.get("ok"):
            return {
                "access_token": str(probe.get("access_token") or ""),
                "refresh_token": str(
                    probe.get("refresh_token") or refresh_token
                ).strip(),
                "scope_label": probe.get("scope_label", ""),
                "expires_in": probe.get("expires_in", 0),
                "endpoint": probe.get("endpoint", ""),
            }
        self._log(f"[微软邮箱] OAuth token 获取失败，回退密码登录: {email}")
        return {"reason": str(probe.get("reason") or "")}

    def _fetch_oauth_token(
        self,
        *,
        email: str,
        client_id: str,
        refresh_token: str,
        preferred_backend: str | None = None,
    ) -> str:
        bundle = self._fetch_oauth_token_bundle(
            email=email,
            client_id=client_id,
            refresh_token=refresh_token,
            preferred_backend=preferred_backend,
        )
        return str(bundle.get("access_token") or "").strip()

    def _get_oauth_access_token(
        self,
        account: MailboxAccount,
        *,
        preferred_backend: str | None = None,
        force_refresh: bool = False,
    ) -> str:
        extra = account.extra or {}
        client_id = str(extra.get("client_id") or "").strip()
        refresh_token = str(extra.get("refresh_token") or "").strip()
        email_addr = str(account.email or "").strip()
        if not client_id or not refresh_token:
            raise RuntimeError("微软邮箱 OAuth 凭据缺失，无法获取 access token")

        cache = extra.setdefault("_oauth_token_cache", {})
        cache_key = self._normalize_backend_name(preferred_backend or self._backend_name)
        if force_refresh and isinstance(cache, dict):
            cache.pop(cache_key, None)
        cached = cache.get(cache_key) if isinstance(cache, dict) else None
        now = time.time()
        if isinstance(cached, dict):
            access_token = str(cached.get("access_token") or "").strip()
            expires_at = float(cached.get("expires_at") or 0)
            if access_token and expires_at > now + 60:
                return access_token

        bundle = self._fetch_oauth_token_bundle(
            email=email_addr,
            client_id=client_id,
            refresh_token=refresh_token,
            preferred_backend=cache_key,
        )
        access_token = str(bundle.get("access_token") or "").strip()
        if not access_token:
            reason = bundle.get("reason", "")
            suffix = f" [{reason}]" if reason else ""
            raise RuntimeError(f"微软邮箱 OAuth access token 获取失败{suffix}")

        rotated_refresh_token = str(bundle.get("refresh_token") or "").strip()
        if rotated_refresh_token and rotated_refresh_token != refresh_token:
            extra["refresh_token"] = rotated_refresh_token

        expires_in = bundle.get("expires_in")
        try:
            expires_in_value = max(int(expires_in or 0), 0)
        except (TypeError, ValueError):
            expires_in_value = 0
        if isinstance(cache, dict):
            cache[cache_key] = {
                "access_token": access_token,
                "expires_at": now + expires_in_value if expires_in_value else now + 300,
                "scope_label": bundle.get("scope_label", ""),
            }
        return access_token

    def _imap_auth_oauth(self, imap_conn, *, email: str, access_token: str) -> None:
        auth_string = f"user={email}\x01auth=Bearer {access_token}\x01\x01"
        imap_conn.authenticate("XOAUTH2", lambda _: auth_string.encode("utf-8"))

    @staticmethod
    def _is_imap_authentication_error(exc: Exception) -> bool:
        message = str(exc or "").strip().lower()
        return any(
            marker in message
            for marker in (
                "authenticate failed",
                "authentication failed",
                "login failed",
                "invalid credentials",
                "credentials rejected",
                "auth failed",
            )
        )

    def _open_imap(self, account: MailboxAccount):
        import imaplib

        email_addr = str(account.email or "").strip()
        extra = account.extra or {}
        password = str(extra.get("password") or "").strip()
        client_id = str(extra.get("client_id") or "").strip()
        refresh_token = str(extra.get("refresh_token") or "").strip()

        access_token = ""
        if client_id and refresh_token:
            access_token = self._get_oauth_access_token(
                account,
                preferred_backend="imap",
            )

        last_error = None
        fresh_oauth_auth_rejected = False
        password_auth_rejected = False
        oauth_refresh_attempted = False

        def connect_with_oauth(host: str, token: str):
            connection = None
            try:
                connection = imaplib.IMAP4_SSL(
                    host,
                    self._imap_port,
                    timeout=30,
                )
                self._imap_auth_oauth(
                    connection,
                    email=email_addr,
                    access_token=token,
                )
                return connection
            except Exception:
                try:
                    if connection is not None:
                        connection.logout()
                except Exception:
                    pass
                raise

        for host in self._imap_servers:
            if not host:
                continue
            if access_token:
                try:
                    return connect_with_oauth(host, access_token)
                except Exception as exc:
                    last_error = exc
                    is_auth_failure = self._is_imap_authentication_error(exc)
                    if is_auth_failure and oauth_refresh_attempted:
                        fresh_oauth_auth_rejected = True
                    if is_auth_failure and not oauth_refresh_attempted:
                        oauth_refresh_attempted = True
                        self._log(
                            "[微软邮箱][IMAP] "
                            f"host={host} auth_mode=oauth 鉴权被拒绝，"
                            "清除 access token 缓存并用 refresh token 重试一次"
                        )
                        try:
                            access_token = self._get_oauth_access_token(
                                account,
                                preferred_backend="imap",
                                force_refresh=True,
                            )
                        except Exception as refresh_exc:
                            last_error = refresh_exc
                            self._log(
                                "[微软邮箱][IMAP] "
                                f"host={host} auth_mode=oauth access token 刷新失败"
                            )
                        else:
                            try:
                                return connect_with_oauth(host, access_token)
                            except Exception as refreshed_auth_exc:
                                last_error = refreshed_auth_exc
                                fresh_oauth_auth_rejected = (
                                    fresh_oauth_auth_rejected
                                    or self._is_imap_authentication_error(
                                        refreshed_auth_exc
                                    )
                                )
                                self._log(
                                    "[微软邮箱][IMAP] "
                                    f"host={host} auth_mode=oauth 刷新后重试失败"
                                )
            if password:
                imap_conn = None
                try:
                    imap_conn = imaplib.IMAP4_SSL(host, self._imap_port, timeout=30)
                    imap_conn.login(email_addr, password)
                    return imap_conn
                except Exception as exc:
                    last_error = exc
                    password_auth_rejected = (
                        password_auth_rejected
                        or self._is_imap_authentication_error(exc)
                    )
                    try:
                        if imap_conn is not None:
                            imap_conn.logout()
                    except Exception:
                        pass

        if fresh_oauth_auth_rejected or (
            password_auth_rejected and not (client_id and refresh_token)
        ):
            raise MailboxAuthenticationError(
                "微软邮箱 IMAP 鉴权失败：刷新令牌并重试后仍被服务端拒绝，"
                "请检查邮箱凭据、IMAP 权限或稍后重试"
            ) from last_error
        raise RuntimeError(f"微软邮箱 IMAP 登录失败: {last_error}")

    def _resolve_backend(self, account: MailboxAccount) -> OutlookMailboxBackend:
        extra = account.extra or {}
        if self._is_mailapi_account(account):
            return self._backends["mailapi_url"]
        override = self._normalize_backend_name(
            extra.get("outlook_backend") or self._backend_name
        )
        if override == "graph":
            has_oauth = bool(
                str(extra.get("client_id") or "").strip()
                and str(extra.get("refresh_token") or "").strip()
            )
            if not has_oauth:
                self._log(
                    "[微软邮箱] Graph 后端需要 OAuth 凭据，当前账号缺少 client_id/refresh_token，自动切换 IMAP"
                )
                override = "imap"
        return self._backends.get(override) or self._backends["graph"]

    def _graph_headers(self, *, access_token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Prefer": 'outlook.body-content-type="text"',
        }

    def _graph_request_json(
        self,
        *,
        method: str,
        path: str,
        access_token: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        import requests

        url = f"{self._graph_api_base.rstrip('/')}/{path.lstrip('/')}"
        resp = requests.request(
            method,
            url,
            headers=self._graph_headers(access_token=access_token),
            params=params or None,
            timeout=20,
            proxies=self._proxy,
        )
        if resp.status_code >= 400:
            preview = (resp.text or "")[:300]
            raise RuntimeError(
                f"Outlook Graph 请求失败: HTTP {resp.status_code} {preview}"
            )
        return resp.json() if resp.content else {}

    def _graph_list_messages(
        self,
        *,
        access_token: str,
        folder: str,
    ) -> list[dict[str, Any]]:
        data = self._graph_request_json(
            method="GET",
            path=f"/me/mailFolders/{folder}/messages",
            access_token=access_token,
            params={
                "$top": "25",
                "$orderby": "receivedDateTime DESC",
                "$select": "id,subject,bodyPreview,body,receivedDateTime,from,internetMessageId",
            },
        )
        value = data.get("value") or []
        return value if isinstance(value, list) else []

    def _graph_get_message(
        self,
        *,
        access_token: str,
        message_id: str,
    ) -> dict[str, Any]:
        from urllib.parse import quote

        return self._graph_request_json(
            method="GET",
            path=f"/me/messages/{quote(str(message_id or '').strip(), safe='')}",
            access_token=access_token,
            params={
                "$select": "id,subject,bodyPreview,body,uniqueBody,receivedDateTime,from,internetMessageId",
            },
        )

    def _graph_message_text(self, message: dict[str, Any]) -> str:
        subject = str((message or {}).get("subject") or "").strip()
        preview = str((message or {}).get("bodyPreview") or "").strip()

        body = (message or {}).get("body") or {}
        body_content = (
            str(body.get("content") or "").strip() if isinstance(body, dict) else ""
        )
        unique_body = (message or {}).get("uniqueBody") or {}
        unique_body_content = (
            str(unique_body.get("content") or "").strip()
            if isinstance(unique_body, dict)
            else ""
        )
        combined = " ".join(
            part for part in [subject, preview, body_content, unique_body_content] if part
        )
        return self._decode_raw_content(combined)

    def _decode_header_value(self, value: str) -> str:
        from email.header import decode_header

        if not value:
            return ""
        parts = decode_header(value)
        decoded = []
        for part, charset in parts:
            if isinstance(part, bytes):
                try:
                    decoded.append(part.decode(charset or "utf-8", errors="ignore"))
                except Exception:
                    decoded.append(part.decode("utf-8", errors="ignore"))
            else:
                decoded.append(str(part))
        return "".join(decoded)

    def _extract_message_text(self, message) -> str:
        subject = self._decode_header_value(message.get("Subject", ""))
        body_chunks = []
        if message.is_multipart():
            for part in message.walk():
                if part.get_content_maintype() == "multipart":
                    continue
                content_type = part.get_content_type()
                if content_type not in ("text/plain", "text/html"):
                    continue
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                charset = part.get_content_charset() or "utf-8"
                try:
                    body_chunks.append(payload.decode(charset, errors="ignore"))
                except Exception:
                    body_chunks.append(payload.decode("utf-8", errors="ignore"))
        else:
            payload = message.get_payload(decode=True)
            if payload is None:
                payload = message.get_payload()
            if isinstance(payload, bytes):
                try:
                    body_chunks.append(payload.decode("utf-8", errors="ignore"))
                except Exception:
                    body_chunks.append(payload.decode("latin1", errors="ignore"))
            elif payload:
                body_chunks.append(str(payload))

        combined = (subject + " " + " ".join(body_chunks)).strip()
        return self._decode_raw_content(combined)

    def get_current_ids(
        self,
        account: MailboxAccount,
        *,
        strict_backend_errors: bool = False,
    ) -> set:
        try:
            backend = self._resolve_backend(account)
            self._log(f"[微软邮箱] 当前收信后端: {backend.backend_name}")
            if strict_backend_errors:
                try:
                    return backend.get_current_ids(
                        account,
                        strict_backend_errors=True,
                    )
                except TypeError as exc:
                    if "strict_backend_errors" not in str(exc):
                        raise
            return backend.get_current_ids(account)
        except MailboxBackendError:
            raise
        except Exception as exc:
            self._log(f"[微软邮箱] 获取当前邮件 ID 失败: {exc}")
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        backend = self._resolve_backend(account)
        self._log(f"[微软邮箱] OTP 收信后端: {backend.backend_name}")
        return backend.wait_for_code(
            account,
            keyword=keyword,
            timeout=timeout,
            before_ids=before_ids,
            code_pattern=code_pattern,
            **kwargs,
        )


class FreemailMailbox(BaseMailbox):
    """
    Freemail 自建邮箱服务（基于 Cloudflare Worker）
    项目: https://github.com/idinging/freemail
    支持管理员令牌或账号密码两种认证方式
    """

    def __init__(
        self,
        api_url: str,
        admin_token: str = "",
        username: str = "",
        password: str = "",
        domain: str = "",
        proxy: str = None,
    ):
        self.api = api_url.rstrip("/")
        self.admin_token = admin_token
        self.username = username
        self.password = password
        self.domain = str(domain or "").strip().lstrip("@")
        self.proxy = build_requests_proxy_config(proxy)
        self._session = None
        self._email = None
        self._domains = None

    def _get_session(self):
        import requests

        s = requests.Session()
        s.proxies = self.proxy
        if self.admin_token:
            s.headers.update({"Authorization": f"Bearer {self.admin_token}"})
        elif self.username and self.password:
            s.post(
                f"{self.api}/api/login",
                json={"username": self.username, "password": self.password},
                timeout=15,
            )
        self._session = s
        return s

    def get_email(self) -> MailboxAccount:
        if not self._session:
            self._get_session()

        target_domain = self.domain
        domain_index = 0
        if target_domain:
            domains = self._ensure_domains()
            if domains:
                lookup = str(target_domain).lower()
                for idx, domain in enumerate(domains):
                    if str(domain or "").strip().lower() == lookup:
                        domain_index = idx
                        break

        params = {"domainIndex": domain_index} if target_domain else {}
        r = self._session.get(f"{self.api}/api/generate", params=params, timeout=15)
        data = r.json()
        email = str(data.get("email", "") or "")
        if target_domain and email and "@" in email:
            actual_domain = email.split("@", 1)[1].strip().lower()
            if actual_domain != target_domain.lower():
                self._log(
                    f"[Freemail] 指定域名 {target_domain} 未命中，实际返回 {actual_domain}"
                )

        self._email = email
        print(f"[Freemail] 生成邮箱: {email}")
        return MailboxAccount(email=email, account_id=email)

    def _ensure_domains(self) -> list:
        if self._domains is not None:
            return self._domains
        self._domains = []
        if not self._session:
            self._get_session()
        try:
            r = self._session.get(f"{self.api}/api/domains", timeout=15)
            payload = r.json()
            normalized = []
            def _append_domain(value):
                domain = str(value or "").strip().lstrip("@")
                if domain and domain not in normalized:
                    normalized.append(domain)
            if isinstance(payload, list):
                for item in payload:
                    if isinstance(item, dict):
                        _append_domain(
                            item.get("domain")
                            or item.get("name")
                            or item.get("value")
                        )
                    else:
                        _append_domain(item)
            elif isinstance(payload, dict):
                candidates = payload.get("domains") or payload.get("data") or []
                if isinstance(candidates, list):
                    for item in candidates:
                        if isinstance(item, dict):
                            _append_domain(
                                item.get("domain")
                                or item.get("name")
                                or item.get("value")
                            )
                        else:
                            _append_domain(item)
            self._domains = normalized
        except Exception:
            self._domains = []
        return self._domains

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            r = self._session.get(
                f"{self.api}/api/emails",
                params={"mailbox": account.email, "limit": 50},
                timeout=10,
            )
            return {str(m["id"]) for m in r.json() if "id" in m}
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        seen = set(before_ids or [])
        exclude_codes = {
            str(code).strip()
            for code in (kwargs.get("exclude_codes") or set())
            if str(code or "").strip()
        }

        def poll_once() -> Optional[str]:
            try:
                r = self._session.get(
                    f"{self.api}/api/emails",
                    params={"mailbox": account.email, "limit": 20},
                    timeout=10,
                )
                for msg in r.json():
                    mid = str(msg.get("id", ""))
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    # 直接用 verification_code 字段
                    code = str(msg.get("verification_code") or "").strip()
                    if code and code != "None":
                        if code in exclude_codes:
                            continue
                        return code
                    # 兜底：从 preview 提取
                    text = (
                        str(msg.get("preview", "")) + " " + str(msg.get("subject", ""))
                    )
                    code = self._safe_extract(text, code_pattern)
                    if code:
                        if code in exclude_codes:
                            continue
                        return code
            except Exception:
                pass
            return None

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=3,
            poll_once=poll_once,
        )
