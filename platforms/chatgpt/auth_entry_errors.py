"""Classify authorize failures without exposing transaction query parameters."""

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import math
import re
from urllib.parse import parse_qs, urlsplit

from .auth_outcomes import AuthFailureDomain, AuthOutcome


@dataclass(frozen=True)
class AuthEntryError:
    code: str
    rate_limited: bool = False
    retryable: bool = False

    @property
    def message(self):
        label = 'OpenAI 登录入口限流' if self.rate_limited else 'OpenAI 登录入口返回错误'
        return f'{label}（{self.code}）'

    def outcome(self):
        return AuthOutcome.failure(
            stage='authorize',
            domain=AuthFailureDomain.RATE_LIMIT if self.rate_limited else AuthFailureDomain.SESSION,
            code=self.code, retryable=self.retryable,
        )


def password_entry_ready(url, status):
    parts = urlsplit(str(url or ''))
    return (
        200 <= status < 400
        and parts.hostname == 'auth.openai.com'
        and parts.path.rstrip('/') == '/log-in/password'
    )


def auth_entry_error(url, status=0):
    parts = urlsplit(str(url or ''))
    is_error_page = parts.hostname == 'auth.openai.com' and parts.path.rstrip('/') == '/error'
    code = ''
    if is_error_page:
        raw = parse_qs(parts.query).get('payload', [''])[0]
        if len(raw) <= 16384:
            try:
                # parse_qs treats an unescaped base64 '+' as a space.
                raw = raw.replace(' ', '+')
                payload = json.loads(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)))
                candidate = payload.get('errorCode', '') if isinstance(payload, dict) else ''
                if isinstance(candidate, str) and re.fullmatch(r'[a-zA-Z0-9_\-]{1,80}', candidate):
                    code = candidate.lower()
            except (ValueError, UnicodeError):
                pass
    if status == 429 or code in {'rate_limit_exceeded', 'too_many_requests'}:
        return AuthEntryError('rate_limit_exceeded', rate_limited=True, retryable=True)
    if not code and status in {500, 502, 503, 504}:
        code = 'temporarily_unavailable'
    if code or is_error_page:
        return AuthEntryError(code or 'unknown_auth_error', retryable=code in {
            'temporarily_unavailable', 'server_error', 'session_expired', 'invalid_auth_step',
        })
    return None


def response_retry_after(response):
    """Honor Retry-After from either the error redirect or its final page."""
    delays = [0.0]
    history = getattr(response, 'history', [])
    responses = [response] + (list(history) if isinstance(history, (list, tuple)) else [])
    for item in responses:
        headers = getattr(item, 'headers', {}) or {}
        raw = headers.get('Retry-After') or headers.get('retry-after')
        if not isinstance(raw, (str, int, float)):
            continue
        try:
            delay = float(raw)
        except (ValueError, TypeError):
            try:
                date = parsedate_to_datetime(str(raw))
                delay = (date - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                continue
        if math.isfinite(delay) and delay >= 0:
            delays.append(delay)
    return max(delays)
