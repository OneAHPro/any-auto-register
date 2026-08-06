"""ChatGPT account password/MFA hardening protocol primitives."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import struct
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from curl_cffi import requests as cffi_requests

from core.proxy_utils import build_requests_proxy_config


MFA_INFO_URL = "https://chatgpt.com/backend-api/accounts/mfa_info"
MFA_ENROLL_URL = "https://chatgpt.com/backend-api/accounts/mfa/enroll"
MFA_ACTIVATE_URL = (
    "https://chatgpt.com/backend-api/accounts/mfa/user/activate_enrollment"
)
MFA_DISABLE_URL = (
    "https://chatgpt.com/backend-api/accounts/mfa/user/disable_in_house"
)


class ChatGPTMFAError(RuntimeError):
    """A redacted, typed failure from the ChatGPT MFA backend."""


def normalize_totp_secret(secret: str) -> str:
    normalized = re.sub(r"[\s-]+", "", str(secret or "")).upper()
    if len(normalized) < 16 or not re.fullmatch(r"[A-Z2-7]+", normalized):
        raise ValueError("TOTP secret is not valid Base32")
    padded = normalized + "=" * (-len(normalized) % 8)
    try:
        base64.b32decode(padded, casefold=True)
    except Exception as exc:
        raise ValueError("TOTP secret is not valid Base32") from exc
    return normalized


def generate_totp(
    secret: str,
    *,
    timestamp: float | None = None,
    interval: int = 30,
    digits: int = 6,
) -> str:
    """Generate an RFC 6238 SHA-1 TOTP entirely on the local machine."""
    normalized = normalize_totp_secret(secret)
    padded = normalized + "=" * (-len(normalized) % 8)
    key = base64.b32decode(padded, casefold=True)
    counter = int(time.time() if timestamp is None else timestamp) // int(interval)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(binary % (10**digits)).zfill(digits)


@dataclass(frozen=True)
class MFAInventory:
    enabled: bool
    has_totp: bool
    default_factor_id: str
    factors: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class MFAEnrollment:
    session_id: str
    secret: str


class ChatGPTMFAClient:
    """Minimal client for ChatGPT's current native MFA settings endpoints."""

    def __init__(
        self,
        *,
        access_token: str,
        account_id: str = "",
        proxy: str = "",
        transport=None,
        timeout: int = 30,
    ):
        token = str(access_token or "").strip()
        if not token:
            raise ValueError("ChatGPT access token is required")
        self._access_token = token
        self._account_id = str(account_id or "").strip()
        self._proxies = build_requests_proxy_config(str(proxy or "").strip())
        self._transport = transport or cffi_requests
        self._timeout = max(int(timeout or 30), 1)

    def _request(self, method: str, url: str, payload: dict | None = None) -> dict:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._access_token}",
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/136.0.0.0 Safari/537.36"
            ),
        }
        if self._account_id:
            headers["OpenAI-Account-ID"] = self._account_id
        kwargs: dict[str, Any] = {
            "headers": headers,
            "proxies": self._proxies,
            "timeout": self._timeout,
            "impersonate": "chrome136",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
            kwargs["json"] = payload
        try:
            response = self._transport.request(method, url, **kwargs)
        except Exception as exc:
            raise ChatGPTMFAError(
                f"ChatGPT MFA request failed ({type(exc).__name__})"
            ) from None
        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code < 200 or status_code >= 300:
            raise ChatGPTMFAError(f"ChatGPT MFA request returned HTTP {status_code}")
        try:
            data = response.json()
        except Exception:
            data = {}
        return data if isinstance(data, dict) else {}

    def get_inventory(self) -> MFAInventory:
        data = self._request("GET", MFA_INFO_URL)
        raw_factors = data.get("factors")
        factor_items: list[dict[str, Any]] = []
        if isinstance(raw_factors, list):
            factor_items.extend(
                dict(item) for item in raw_factors if isinstance(item, dict)
            )
        elif isinstance(raw_factors, dict):
            for grouped_type, grouped_items in raw_factors.items():
                if isinstance(grouped_items, dict):
                    grouped_items = [grouped_items]
                if not isinstance(grouped_items, list):
                    continue
                for item in grouped_items:
                    if not isinstance(item, dict):
                        continue
                    normalized = dict(item)
                    normalized.setdefault("factor_type", str(grouped_type or ""))
                    factor_items.append(normalized)
        factors = tuple(factor_items)
        has_totp = any(
            str(
                factor.get("factor_type")
                or factor.get("type")
                or factor.get("factor")
                or ""
            ).strip().lower()
            == "totp"
            for factor in factors
        )
        return MFAInventory(
            enabled=bool(data.get("mfa_enabled_v2") or data.get("enabled")),
            has_totp=has_totp,
            default_factor_id=str(
                data.get("native_default_factor_id") or ""
            ).strip(),
            factors=factors,
        )

    def start_totp_enrollment(self) -> MFAEnrollment:
        data = self._request(
            "POST",
            MFA_ENROLL_URL,
            {
                "factor_type": "totp",
                "phone_number": None,
                "phone_verification_channel": None,
            },
        )
        session_id = str(data.get("session_id") or data.get("sessionId") or "").strip()
        secret = str(data.get("secret") or "").strip()
        if not secret:
            otpauth_uri = str(
                data.get("otpauth_uri")
                or data.get("otpauth_url")
                or data.get("uri")
                or ""
            ).strip()
            if otpauth_uri.lower().startswith("otpauth://"):
                parsed = urlparse(otpauth_uri)
                secret = str(parse_qs(parsed.query).get("secret", [""])[0]).strip()
        try:
            normalized_secret = normalize_totp_secret(secret)
        except ValueError:
            normalized_secret = ""
        if not session_id or not normalized_secret:
            raise ChatGPTMFAError("ChatGPT MFA enrollment response is incomplete")
        return MFAEnrollment(session_id=session_id, secret=normalized_secret)

    def activate_totp_enrollment(self, session_id: str, code: str) -> bool:
        normalized_session = str(session_id or "").strip()
        normalized_code = str(code or "").strip()
        if not normalized_session or not re.fullmatch(r"[0-9]{6}", normalized_code):
            raise ValueError("MFA activation requires a session and six-digit code")
        data = self._request(
            "POST",
            MFA_ACTIVATE_URL,
            {
                "code": normalized_code,
                "factor_type": "totp",
                "session_id": normalized_session,
            },
        )
        if data.get("success") is False:
            raise ChatGPTMFAError("ChatGPT MFA activation was rejected")
        return True

    def disable_factor(self, factor_id: str) -> bool:
        normalized_factor_id = str(factor_id or "").strip()
        if not normalized_factor_id:
            raise ValueError("MFA factor ID is required")
        data = self._request(
            "POST",
            MFA_DISABLE_URL,
            {"factor_id": normalized_factor_id},
        )
        if data.get("success") is False:
            raise ChatGPTMFAError("ChatGPT MFA factor reset was rejected")
        return True
