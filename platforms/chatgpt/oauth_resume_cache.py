from __future__ import annotations

import threading
import time

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

try:
    from curl_cffi import requests as curl_requests
except ImportError:
    import requests as curl_requests


PERSISTED_OAUTH_RESUME_TTL_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class OAuthResumeContext:
    session: Any
    device_id: str = ""
    user_agent: str = ""
    sec_ch_ua: str = ""
    accept_language: str = ""
    impersonate: str = ""
    code_verifier: str = ""
    oauth_state: str = ""
    authorize_url: str = ""
    authorize_params: dict[str, Any] = field(default_factory=dict)
    flow_state: Any = None
    referer: str = ""
    expires_at: float = 0.0


class OAuthResumeContextCache:
    def __init__(
        self,
        *,
        ttl_seconds: int = 1800,
        max_entries: int = 512,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self._clock = clock
        self._lock = threading.Lock()
        self._contexts: OrderedDict[str, OAuthResumeContext] = OrderedDict()

    @staticmethod
    def _key(email: str) -> str:
        return str(email or "").strip().lower()

    def remember(
        self,
        email: str,
        *,
        session: Any,
        device_id: str = "",
        user_agent: str = "",
        sec_ch_ua: str = "",
        accept_language: str = "",
        impersonate: str = "",
        code_verifier: str = "",
        oauth_state: str = "",
        authorize_url: str = "",
        authorize_params: Optional[dict[str, Any]] = None,
        flow_state: Any = None,
        referer: str = "",
    ) -> bool:
        key = self._key(email)
        if not key or session is None:
            return False
        context = OAuthResumeContext(
            session=session,
            device_id=str(device_id or "").strip(),
            user_agent=str(user_agent or "").strip(),
            sec_ch_ua=str(sec_ch_ua or "").strip(),
            accept_language=str(accept_language or "").strip(),
            impersonate=str(impersonate or "").strip(),
            code_verifier=str(code_verifier or "").strip(),
            oauth_state=str(oauth_state or "").strip(),
            authorize_url=str(authorize_url or "").strip(),
            authorize_params=dict(authorize_params or {}),
            flow_state=flow_state,
            referer=str(referer or "").strip(),
            expires_at=self._clock() + self.ttl_seconds,
        )
        with self._lock:
            self._contexts.pop(key, None)
            self._contexts[key] = context
            while len(self._contexts) > self.max_entries:
                self._contexts.popitem(last=False)
        return True

    def take(self, email: str) -> Optional[OAuthResumeContext]:
        key = self._key(email)
        if not key:
            return None
        with self._lock:
            context = self._contexts.pop(key, None)
        if context is None or self._clock() >= context.expires_at:
            return None
        return context


def serialize_oauth_resume_context(
    session: Any,
    *,
    device_id: str = "",
    user_agent: str = "",
    sec_ch_ua: str = "",
    accept_language: str = "",
    impersonate: str = "",
    code_verifier: str = "",
    oauth_state: str = "",
    authorize_url: str = "",
    authorize_params: Optional[dict[str, Any]] = None,
    flow_state: Any = None,
    referer: str = "",
    ttl_seconds: int = PERSISTED_OAUTH_RESUME_TTL_SECONDS,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Create a bounded, JSON-safe snapshot of the authenticated browser context."""
    cookie_jar = getattr(getattr(session, "cookies", None), "jar", None)
    cookies: list[dict[str, Any]] = []
    try:
        iterator = iter(cookie_jar or [])
    except TypeError:
        iterator = iter(())
    for cookie in iterator:
        name = str(getattr(cookie, "name", "") or "").strip()
        value = str(getattr(cookie, "value", "") or "")
        domain = str(getattr(cookie, "domain", "") or "").strip().lower()
        if not name or not value:
            continue
        if domain and not any(
            allowed in domain for allowed in ("openai.com", "chatgpt.com")
        ):
            continue
        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": domain,
                "path": str(getattr(cookie, "path", "/") or "/"),
                "secure": bool(getattr(cookie, "secure", False)),
            }
        )
    if not cookies:
        return {}
    now = float(clock())
    flow_state_snapshot = {}
    if flow_state is not None:
        flow_state_snapshot = {
            "page_type": str(getattr(flow_state, "page_type", "") or ""),
            "continue_url": str(getattr(flow_state, "continue_url", "") or ""),
            "method": str(getattr(flow_state, "method", "GET") or "GET"),
            "current_url": str(getattr(flow_state, "current_url", "") or ""),
            "source": str(getattr(flow_state, "source", "") or ""),
            "payload": dict(getattr(flow_state, "payload", {}) or {}),
            "raw": dict(getattr(flow_state, "raw", {}) or {}),
        }
    has_prepared_transaction = bool(
        str(code_verifier or "").strip()
        and str(oauth_state or "").strip()
        and flow_state_snapshot
    )
    snapshot = {
        "version": 2 if has_prepared_transaction else 1,
        "created_at": now,
        "expires_at": now + max(1, int(ttl_seconds)),
        "device_id": str(device_id or "").strip(),
        "user_agent": str(user_agent or "").strip(),
        "sec_ch_ua": str(sec_ch_ua or "").strip(),
        "accept_language": str(accept_language or "").strip(),
        "impersonate": str(impersonate or "").strip(),
        "cookies": cookies,
    }
    if has_prepared_transaction:
        snapshot.update(
            {
                "code_verifier": str(code_verifier or "").strip(),
                "oauth_state": str(oauth_state or "").strip(),
                "authorize_url": str(authorize_url or "").strip(),
                "authorize_params": dict(authorize_params or {}),
                "flow_state": flow_state_snapshot,
                "referer": str(referer or "").strip(),
            }
        )
    return snapshot


def restore_oauth_resume_context(
    snapshot: Any,
    *,
    clock: Callable[[], float] = time.time,
    session_factory: Optional[Callable[..., Any]] = None,
) -> Optional[OAuthResumeContext]:
    """Restore a persisted browser snapshot without contacting OpenAI."""
    if not isinstance(snapshot, dict) or int(snapshot.get("version") or 0) not in {1, 2}:
        return None
    expires_at = float(snapshot.get("expires_at") or 0)
    if expires_at <= float(clock()):
        return None
    cookies = snapshot.get("cookies")
    if not isinstance(cookies, list) or not cookies:
        return None

    factory = session_factory or curl_requests.Session
    impersonate = str(snapshot.get("impersonate") or "").strip()
    session = factory(**({"impersonate": impersonate} if impersonate else {}))
    headers = {
        "User-Agent": str(snapshot.get("user_agent") or "").strip(),
        "sec-ch-ua": str(snapshot.get("sec_ch_ua") or "").strip(),
        "Accept-Language": str(snapshot.get("accept_language") or "").strip(),
    }
    try:
        session.headers.update({key: value for key, value in headers.items() if value})
    except Exception:
        pass

    restored_count = 0
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name") or "").strip()
        value = str(cookie.get("value") or "")
        if not name or not value:
            continue
        try:
            session.cookies.set(
                name,
                value,
                domain=str(cookie.get("domain") or ""),
                path=str(cookie.get("path") or "/"),
                secure=bool(cookie.get("secure", False)),
            )
            restored_count += 1
        except Exception:
            continue
    if not restored_count:
        return None
    flow_state = None
    if int(snapshot.get("version") or 0) == 2:
        state_data = snapshot.get("flow_state")
        if isinstance(state_data, dict) and state_data:
            from .utils import FlowState

            flow_state = FlowState(
                page_type=str(state_data.get("page_type") or ""),
                continue_url=str(state_data.get("continue_url") or ""),
                method=str(state_data.get("method") or "GET"),
                current_url=str(state_data.get("current_url") or ""),
                source=str(state_data.get("source") or ""),
                payload=dict(state_data.get("payload") or {}),
                raw=dict(state_data.get("raw") or {}),
            )
    return OAuthResumeContext(
        session=session,
        device_id=str(snapshot.get("device_id") or "").strip(),
        user_agent=str(snapshot.get("user_agent") or "").strip(),
        sec_ch_ua=str(snapshot.get("sec_ch_ua") or "").strip(),
        accept_language=str(snapshot.get("accept_language") or "").strip(),
        impersonate=impersonate,
        code_verifier=str(snapshot.get("code_verifier") or "").strip(),
        oauth_state=str(snapshot.get("oauth_state") or "").strip(),
        authorize_url=str(snapshot.get("authorize_url") or "").strip(),
        authorize_params=dict(snapshot.get("authorize_params") or {}),
        flow_state=flow_state,
        referer=str(snapshot.get("referer") or "").strip(),
        expires_at=expires_at,
    )


oauth_resume_cache = OAuthResumeContextCache()
