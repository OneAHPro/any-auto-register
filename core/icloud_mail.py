from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import struct
import tempfile
import threading
import time

from pathlib import Path
from typing import Any, Callable, Optional

from .proxy_utils import build_requests_proxy_config


_MFA_SECRET_PATTERN = re.compile(r"^[A-Z2-7]{16,128}$")


def normalize_mfa_secret(value: str) -> str:
    normalized = re.sub(r"[\s-]+", "", str(value or "")).upper()
    if not _MFA_SECRET_PATTERN.fullmatch(normalized):
        raise ValueError("iCloud MFA 秘钥格式无效，需为 Base32 字符串")
    return normalized


def generate_totp(
    secret: str,
    *,
    timestamp: Optional[float] = None,
    interval: int = 30,
    digits: int = 6,
) -> str:
    """Generate the Apple login TOTP without sending the secret externally."""
    normalized = normalize_mfa_secret(secret)
    padded = normalized + "=" * ((8 - len(normalized) % 8) % 8)
    try:
        key = base64.b32decode(padded, casefold=True)
    except Exception as exc:
        raise ValueError("iCloud MFA 秘钥格式无效，无法生成动态码") from exc

    current = time.time() if timestamp is None else float(timestamp)
    counter = max(0, int(current) // max(1, int(interval)))
    digest = hmac.new(
        key,
        struct.pack(">Q", counter),
        hashlib.sha1,
    ).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    modulus = 10 ** max(1, int(digits))
    return str(binary % modulus).zfill(max(1, int(digits)))


def _default_cookie_directory() -> str:
    runtime_dir = Path(
        str(os.getenv("APP_RUNTIME_DIR") or "/runtime").strip() or "/runtime"
    )
    preferred = runtime_dir / "icloud_sessions"
    try:
        preferred.mkdir(parents=True, exist_ok=True, mode=0o700)
        preferred.chmod(0o700)
        return str(preferred)
    except OSError:
        fallback = Path(tempfile.gettempdir()) / "any-auto-register-icloud-sessions"
        fallback.mkdir(parents=True, exist_ok=True, mode=0o700)
        fallback.chmod(0o700)
        return str(fallback)


class ICloudMailClient:
    """Read-only iCloud Mail client using Apple account + TOTP authentication."""

    def __init__(
        self,
        *,
        email: str,
        password: str,
        mfa_secret: str,
        service_factory: Optional[Callable[..., Any]] = None,
        time_fn: Callable[[], float] = time.time,
        cookie_directory: str = "",
        proxy_url: str = "",
    ):
        self.email = str(email or "").strip()
        self.password = str(password or "")
        self.mfa_secret = normalize_mfa_secret(mfa_secret)
        self._service_factory = service_factory
        self._time_fn = time_fn
        self.cookie_directory = str(
            cookie_directory or _default_cookie_directory()
        ).strip()
        self.proxy_url = str(proxy_url or "").strip()
        self._proxy_config = build_requests_proxy_config(self.proxy_url) or {}
        self._service = None
        self._authentication_lock = threading.Lock()

        if not self.email:
            raise ValueError("iCloud 邮箱地址为空")
        if not self.password:
            raise ValueError("iCloud 登录密码为空")

    def _factory(self):
        if self._service_factory is not None:
            return self._service_factory
        try:
            from pyicloud import PyiCloudService
        except ImportError as exc:
            raise RuntimeError("iCloud 取码依赖未安装：pyicloud") from exc
        return PyiCloudService

    def _redact_error(self, exc: Exception) -> str:
        detail = str(exc or "").strip() or type(exc).__name__
        for secret in (self.password, self.mfa_secret):
            if secret:
                detail = detail.replace(secret, "***")
        return detail[:300]

    def _configure_service_proxy(self, service: Any) -> None:
        if not self._proxy_config:
            return
        session = getattr(service, "session", None)
        if session is None:
            raise RuntimeError("iCloud 服务未提供可配置代理的 HTTP Session")
        proxies = getattr(session, "proxies", None)
        if proxies is None:
            session.proxies = dict(self._proxy_config)
            return
        proxies.update(self._proxy_config)

    def _create_service(self):
        factory = self._factory()
        kwargs = {
            "cookie_directory": self.cookie_directory,
            "with_family": False,
        }
        if not self._proxy_config or self._service_factory is not None:
            service = factory(self.email, self.password, **kwargs)
            self._configure_service_proxy(service)
            return service

        proxy_config = dict(self._proxy_config)

        class ProxyConfiguredService(factory):
            def __init__(inner_self, *args, **inner_kwargs):
                inner_self._icloud_defer_authentication = True
                super().__init__(*args, **inner_kwargs)
                session = getattr(inner_self, "session", None)
                if session is None:
                    raise RuntimeError(
                        "iCloud 服务未提供可配置代理的 HTTP Session"
                    )
                proxies = getattr(session, "proxies", None)
                if proxies is None:
                    session.proxies = dict(proxy_config)
                else:
                    proxies.update(proxy_config)
                inner_self._icloud_defer_authentication = False
                super().authenticate()

            def authenticate(inner_self, *args, **inner_kwargs):
                if getattr(inner_self, "_icloud_defer_authentication", False):
                    return None
                return super().authenticate(*args, **inner_kwargs)

        return ProxyConfiguredService(self.email, self.password, **kwargs)

    def _authenticate(self):
        if self._service is not None:
            return self._service
        with self._authentication_lock:
            if self._service is not None:
                return self._service
            return self._authenticate_locked()

    def _authenticate_locked(self):
        if self._service is not None:
            return self._service

        try:
            service = self._create_service()
        except Exception as exc:
            raise RuntimeError(
                f"iCloud 登录失败: {self._redact_error(exc)}"
            ) from exc

        requires_2fa = bool(getattr(service, "requires_2fa", False))
        requires_2sa = bool(getattr(service, "requires_2sa", False))
        if requires_2fa:
            code = generate_totp(
                self.mfa_secret,
                timestamp=self._time_fn(),
            )
            try:
                accepted = bool(service.validate_2fa_code(code))
            except Exception as exc:
                raise RuntimeError(
                    f"iCloud MFA 校验失败: {self._redact_error(exc)}"
                ) from exc
            if not accepted:
                raise RuntimeError("iCloud MFA 动态码未通过，请检查 MFA 秘钥和系统时间")
        elif requires_2sa:
            try:
                trusted_devices = list(service.trusted_devices or [])
            except Exception as exc:
                raise RuntimeError(
                    f"iCloud 受信任设备读取失败: {self._redact_error(exc)}"
                ) from exc
            if not trusted_devices:
                raise RuntimeError("iCloud 两步验证未找到可用的受信任设备")
            device = trusted_devices[0]
            try:
                sent = bool(service.send_verification_code(device))
            except Exception as exc:
                raise RuntimeError(
                    f"iCloud 两步验证码发送失败: {self._redact_error(exc)}"
                ) from exc
            if not sent:
                raise RuntimeError("iCloud 两步验证码发送失败")
            code = generate_totp(
                self.mfa_secret,
                timestamp=self._time_fn(),
            )
            try:
                accepted = bool(service.validate_verification_code(device, code))
            except Exception as exc:
                raise RuntimeError(
                    f"iCloud 两步验证码校验失败: {self._redact_error(exc)}"
                ) from exc
            if not accepted:
                raise RuntimeError(
                    "iCloud 两步验证码未通过，请检查 MFA 秘钥和系统时间"
                )

        if bool(getattr(service, "requires_2fa", False)) or bool(
            getattr(service, "requires_2sa", False)
        ):
            raise RuntimeError("iCloud 登录会话未完成信任验证")

        try:
            service.get_webservice_url("mccgateway")
        except Exception as exc:
            raise RuntimeError(
                "iCloud 账号未提供 MailWS 服务，可能尚未启用 iCloud Mail"
            ) from exc

        self._service = service
        return service

    @staticmethod
    def _message_id(mailbox: str, thread: dict[str, Any]) -> str:
        thread_id = str(
            thread.get("threadId")
            or thread.get("messageId")
            or thread.get("uid")
            or "thread"
        ).strip()
        timestamp = str(thread.get("timestamp") or "0").strip()
        modseq = str(thread.get("modseq") or "0").strip()
        fingerprint_source = "\n".join(
            (
                str(thread.get("subject") or ""),
                str(thread.get("preview") or ""),
            )
        )
        fingerprint = hashlib.sha1(
            fingerprint_source.encode("utf-8", errors="ignore")
        ).hexdigest()[:12]
        return f"{mailbox}:{thread_id}:{timestamp}:{modseq}:{fingerprint}"

    def list_messages(self, mailbox: str = "INBOX", *, limit: int = 50):
        service = self._authenticate()
        folder = str(mailbox or "INBOX").strip() or "INBOX"
        max_results = max(1, min(int(limit or 50), 100))
        base_url = str(service.get_webservice_url("mccgateway") or "").rstrip("/")
        url = f"{base_url}/mailws2/v1/thread/search"
        payload = {
            "responseType": "THREAD_DIGEST",
            "includeFolderStatus": True,
            "maxResults": max_results,
            "sessionHeaders": {
                "folder": folder,
                "modseq": None,
                "threadmodseq": None,
                "condstore": 1,
                "qresync": 1,
                "threadmode": 1,
            },
        }
        try:
            response = service.session.post(
                url,
                params=dict(getattr(service, "params", {}) or {}),
                json=payload,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            raise RuntimeError(
                f"iCloud 邮件列表读取失败: {self._redact_error(exc)}"
            ) from exc

        if not isinstance(data, dict):
            raise RuntimeError("iCloud MailWS 返回格式无效")
        if data.get("errorCode"):
            error_code = str(data.get("errorCode") or "UNKNOWN")[:80]
            raise RuntimeError(f"iCloud MailWS 返回错误: {error_code}")

        threads = data.get("threadList") or []
        if not isinstance(threads, list):
            raise RuntimeError("iCloud MailWS 未返回有效邮件列表")

        messages = []
        for thread in threads:
            if not isinstance(thread, dict):
                continue
            senders = thread.get("senders") or []
            if isinstance(senders, (list, tuple)):
                sender_text = ", ".join(str(item or "") for item in senders)
            else:
                sender_text = str(senders or "")
            messages.append(
                {
                    "id": self._message_id(folder, thread),
                    "subject": str(thread.get("subject") or ""),
                    "sender": sender_text,
                    "preview": str(thread.get("preview") or ""),
                    "received_at": thread.get("timestamp"),
                }
            )
        return messages
