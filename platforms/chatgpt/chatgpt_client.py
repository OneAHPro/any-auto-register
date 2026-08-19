"""
ChatGPT 注册客户端模块
使用 curl_cffi 模拟浏览器行为
"""

import random
import uuid
import time
from urllib.parse import parse_qsl, urlparse, urlsplit
from core.proxy_utils import build_requests_proxy_config

try:
    from curl_cffi import requests as curl_requests
except ImportError:
    print("[FAIL] 需要安装 curl_cffi: pip install curl_cffi")
    import sys

    sys.exit(1)

from .sentinel_token import build_sentinel_token
from .sentinel_browser import get_sentinel_token_via_browser
from .utils import (
    FlowState,
    build_browser_headers,
    decode_jwt_payload,
    describe_flow_state,
    extract_flow_state,
    generate_datadog_trace,
    normalize_flow_url,
    random_delay,
    seed_oai_device_cookie,
)


# Chrome 指纹配置
_CHROME_PROFILES = [
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


def _random_chrome_version():
    """随机选择一个 Chrome 版本"""
    profile = random.choice(_CHROME_PROFILES)
    major = profile["major"]
    build = profile["build"]
    patch = random.randint(*profile["patch_range"])
    full_ver = f"{major}.0.{build}.{patch}"
    ua = f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{full_ver} Safari/537.36"
    return profile["impersonate"], major, full_ver, ua, profile["sec_ch_ua"]


class ChatGPTClient:
    """ChatGPT 注册客户端"""

    BASE = "https://chatgpt.com"
    AUTH = "https://auth.openai.com"

    def __init__(self, proxy=None, verbose=True, browser_mode="protocol"):
        """
        初始化 ChatGPT 客户端

        Args:
            proxy: 代理地址
            verbose: 是否输出详细日志
            browser_mode: protocol | headless | headed
        """
        self.proxy = proxy
        self.verbose = verbose
        self.browser_mode = browser_mode or "protocol"
        self.device_id = str(uuid.uuid4())
        self.accept_language = random.choice(
            [
                "en-US,en;q=0.9",
                "en-US,en;q=0.9,zh-CN;q=0.8",
                "en,en-US;q=0.9",
                "en-US,en;q=0.8",
            ]
        )

        # 随机 Chrome 版本
        (
            self.impersonate,
            self.chrome_major,
            self.chrome_full,
            self.ua,
            self.sec_ch_ua,
        ) = _random_chrome_version()

        # 创建 session
        self.session = curl_requests.Session(impersonate=self.impersonate)

        if self.proxy:
            self.session.proxies = build_requests_proxy_config(self.proxy)

        # 设置基础 headers
        self.session.headers.update(
            {
                "User-Agent": self.ua,
                "Accept-Language": self.accept_language,
                "sec-ch-ua": self.sec_ch_ua,
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-ch-ua-arch": '"x86"',
                "sec-ch-ua-bitness": '"64"',
                "sec-ch-ua-full-version": f'"{self.chrome_full}"',
                "sec-ch-ua-platform-version": f'"{random.randint(10, 15)}.0.0"',
            }
        )

        # 设置 oai-did cookie
        seed_oai_device_cookie(self.session, self.device_id)
        self.last_registration_state = FlowState()
        self.last_stage = ""
        self.last_authorize_status = 0
        self.phone_oauth_resume_context = None
        self.phone_oauth_resume_error = ""
        self.phone_oauth_browser_context = {}
        self.phone_oauth_prepare_diagnostic = {}

    def _get_sentinel_token(self, flow: str, *, page_url: str | None = None):
        prefer_browser = flow in {"username_password_create", "oauth_create_account"}
        if prefer_browser:
            token = get_sentinel_token_via_browser(
                flow=flow,
                proxy=self.proxy,
                page_url=page_url,
                headless=self.browser_mode != "headed",
                device_id=self.device_id,
                log_fn=lambda msg: self._log(msg),
            )
            if token:
                self._log(f"{flow}: 已通过 Playwright SentinelSDK 获取 token")
                return token

        token = build_sentinel_token(
            self.session,
            self.device_id,
            flow=flow,
            user_agent=self.ua,
            sec_ch_ua=self.sec_ch_ua,
            impersonate=self.impersonate,
        )
        if token:
            self._log(f"{flow}: 已通过 HTTP PoW 获取 token")
        return token

    def _log(self, msg):
        """输出日志"""
        if self.verbose:
            print(f"  {msg}")

    def _enter_stage(self, stage: str, detail: str = ""):
        self.last_stage = str(stage or "").strip()
        if self.last_stage:
            message = f"[stage={self.last_stage}]"
            if detail:
                message += f" {detail}"
            self._log(message)

    def _browser_pause(self, low=0.15, high=0.45):
        """在 headed 模式下加入轻微停顿，模拟有头浏览器节奏。"""
        if self.browser_mode == "headed":
            random_delay(low, high)

    def _headers(
        self,
        url,
        *,
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
        return build_browser_headers(
            url=url,
            user_agent=self.ua,
            sec_ch_ua=self.sec_ch_ua,
            chrome_full_version=self.chrome_full,
            accept=accept,
            accept_language=self.accept_language,
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

    def _reset_session(self):
        """重置浏览器指纹与会话，用于绕过偶发的 Cloudflare/SPA 中间页。"""
        self.device_id = str(uuid.uuid4())
        (
            self.impersonate,
            self.chrome_major,
            self.chrome_full,
            self.ua,
            self.sec_ch_ua,
        ) = _random_chrome_version()
        self.accept_language = random.choice(
            [
                "en-US,en;q=0.9",
                "en-US,en;q=0.9,zh-CN;q=0.8",
                "en,en-US;q=0.9",
                "en-US,en;q=0.8",
            ]
        )

        self.session = curl_requests.Session(impersonate=self.impersonate)
        if self.proxy:
            self.session.proxies = build_requests_proxy_config(self.proxy)

        self.session.headers.update(
            {
                "User-Agent": self.ua,
                "Accept-Language": self.accept_language,
                "sec-ch-ua": self.sec_ch_ua,
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-ch-ua-arch": '"x86"',
                "sec-ch-ua-bitness": '"64"',
                "sec-ch-ua-full-version": f'"{self.chrome_full}"',
                "sec-ch-ua-platform-version": f'"{random.randint(10, 15)}.0.0"',
            }
        )
        seed_oai_device_cookie(self.session, self.device_id)

    def _state_from_url(self, url, method="GET"):
        state = extract_flow_state(
            current_url=normalize_flow_url(url, auth_base=self.AUTH),
            auth_base=self.AUTH,
            default_method=method,
        )
        if method:
            state.method = str(method).upper()
        return state

    def _state_from_payload(self, data, current_url=""):
        return extract_flow_state(
            data=data,
            current_url=current_url,
            auth_base=self.AUTH,
        )

    def _state_signature(self, state: FlowState):
        return (
            state.page_type or "",
            state.method or "",
            state.continue_url or "",
            state.current_url or "",
        )

    def _is_registration_complete_state(self, state: FlowState):
        current_url = (state.current_url or "").lower()
        continue_url = (state.continue_url or "").lower()
        page_type = state.page_type or ""
        return (
            page_type in {"callback", "chatgpt_home", "oauth_callback"}
            or ("chatgpt.com" in current_url and "redirect_uri" not in current_url)
            or (
                "chatgpt.com" in continue_url
                and "redirect_uri" not in continue_url
                and page_type != "external_url"
            )
        )

    def _state_is_password_registration(self, state: FlowState):
        return state.page_type in {"create_account_password", "password"}

    def _state_is_email_otp(self, state: FlowState):
        target = f"{state.continue_url} {state.current_url}".lower()
        return (
            state.page_type == "email_otp_verification"
            or "email-verification" in target
            or "email-otp" in target
        )

    def _state_is_about_you(self, state: FlowState):
        target = f"{state.continue_url} {state.current_url}".lower()
        return state.page_type == "about_you" or "about-you" in target

    def _state_requires_navigation(self, state: FlowState):
        if (state.method or "GET").upper() != "GET":
            return False
        if state.page_type == "external_url" and state.continue_url:
            return True
        if state.continue_url and state.continue_url != state.current_url:
            return True
        return False

    def _follow_flow_state(self, state: FlowState, referer=None):
        """跟随服务端返回的 continue_url，推进注册状态机。"""
        target_url = state.continue_url or state.current_url
        if not target_url:
            return False, "缺少可跟随的 continue_url"

        try:
            self._browser_pause()
            r = self.session.get(
                target_url,
                headers=self._headers(
                    target_url,
                    accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    referer=referer,
                    navigation=True,
                ),
                allow_redirects=True,
                timeout=30,
            )
            final_url = str(r.url)
            self._log(f"follow -> {r.status_code} {final_url}")

            content_type = (r.headers.get("content-type", "") or "").lower()
            if "application/json" in content_type:
                try:
                    next_state = self._state_from_payload(
                        r.json(), current_url=final_url
                    )
                except Exception:
                    next_state = self._state_from_url(final_url)
            else:
                next_state = self._state_from_url(final_url)

            self._log(f"follow state -> {describe_flow_state(next_state)}")
            return True, next_state
        except Exception as e:
            self._log(f"跟随 continue_url 失败: {e}")
            return False, str(e)

    def _get_cookie_value(self, name, domain_hint=None):
        """读取当前会话中的 Cookie。"""
        for cookie in self.session.cookies.jar:
            if cookie.name != name:
                continue
            if domain_hint and domain_hint not in (cookie.domain or ""):
                continue
            return cookie.value
        return ""

    def get_next_auth_session_token(self):
        """获取 ChatGPT next-auth 会话 Cookie。"""
        return (
            self._get_cookie_value("__Secure-next-auth.session-token", "chatgpt.com")
            or self._get_cookie_value("__Secure-authjs.session-token", "chatgpt.com")
        )

    def fetch_chatgpt_session(self, max_attempts=5, retry_delay=1.2):
        """请求 ChatGPT Session 接口并返回原始会话数据。"""
        url = f"{self.BASE}/api/auth/session"
        last_error = ""

        for attempt in range(max(1, int(max_attempts or 1))):
            try:
                self._browser_pause()
                response = self.session.get(
                    url,
                    headers=self._headers(
                        url,
                        accept="application/json",
                        referer=f"{self.BASE}/",
                        fetch_site="same-origin",
                    ),
                    timeout=30,
                )
            except Exception as exc:
                last_error = f"/api/auth/session 请求异常: {exc}"
                if attempt < max_attempts - 1:
                    self._log(
                        f"{last_error}，等待 {retry_delay:.1f}s 后重试 "
                        f"({attempt + 1}/{max_attempts})"
                    )
                    time.sleep(retry_delay)
                    continue
                return False, last_error

            if response.status_code != 200:
                last_error = f"/api/auth/session -> HTTP {response.status_code}"
                if attempt < max_attempts - 1:
                    self._log(
                        f"{last_error}，等待 {retry_delay:.1f}s 后重试 "
                        f"({attempt + 1}/{max_attempts})"
                    )
                    time.sleep(retry_delay)
                    continue
                return False, last_error

            try:
                data = response.json()
            except Exception as exc:
                last_error = f"/api/auth/session 返回非 JSON: {exc}"
                if attempt < max_attempts - 1:
                    self._log(
                        f"{last_error}，等待 {retry_delay:.1f}s 后重试 "
                        f"({attempt + 1}/{max_attempts})"
                    )
                    time.sleep(retry_delay)
                    continue
                return False, last_error

            access_token = str(data.get("accessToken") or "").strip()
            if access_token:
                return True, data

            last_error = "/api/auth/session 未返回 accessToken"
            if attempt < max_attempts - 1:
                self._log(
                    f"{last_error}，等待 {retry_delay:.1f}s 后重试 "
                    f"({attempt + 1}/{max_attempts})"
                )
                try:
                    self.session.get(
                        f"{self.BASE}/",
                        headers=self._headers(
                            f"{self.BASE}/",
                            accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                            referer=f"{self.BASE}/",
                            navigation=True,
                        ),
                        allow_redirects=True,
                        timeout=30,
                    )
                except Exception:
                    pass
                time.sleep(retry_delay)
                continue

            return False, last_error

        return False, last_error or "/api/auth/session 未返回 accessToken"

    def reuse_session_and_get_tokens(self):
        """
        承接前序阶段已建立的 ChatGPT 会话，直接读取 Session / AccessToken。

        Returns:
            tuple[bool, dict|str]: 成功时返回标准化 token/session 数据；失败时返回错误信息。
        """
        self._enter_stage("token_exchange", "reuse session -> /api/auth/session")
        state = self.last_registration_state or FlowState()
        self._log("步骤 1/4: 跟随注册回调 external_url ...")
        if state.page_type == "external_url" or self._state_requires_navigation(state):
            ok, followed = self._follow_flow_state(
                state,
                referer=state.current_url or f"{self.AUTH}/about-you",
            )
            if not ok:
                return False, f"注册回调落地失败: {followed}"
            self.last_registration_state = followed
        else:
            self._log("注册回调已落地，跳过额外跟随")

        self._log("步骤 2/4: 检查 __Secure-next-auth.session-token ...")
        session_cookie = ""
        for attempt in range(5):
            session_cookie = self.get_next_auth_session_token()
            if session_cookie:
                break
            self._log(
                f"next-auth session cookie 尚未落地，补一次 ChatGPT 首页触达 "
                f"({attempt + 1}/5)"
            )
            try:
                self._browser_pause(0.2, 0.5)
                self.session.get(
                    f"{self.BASE}/",
                    headers=self._headers(
                        f"{self.BASE}/",
                        accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        referer=state.current_url or f"{self.AUTH}/about-you",
                        navigation=True,
                    ),
                    allow_redirects=True,
                    timeout=30,
                )
            except Exception as exc:
                self._log(f"补触达 ChatGPT 首页异常: {exc}")
            time.sleep(1.0)
        if not session_cookie:
            return False, "缺少 ChatGPT session-token，注册回调可能未完全落地"

        self._log("步骤 3/4: 请求 ChatGPT /api/auth/session ...")
        ok, session_or_error = self.fetch_chatgpt_session()
        if not ok:
            return False, session_or_error

        session_data = session_or_error
        access_token = str(session_data.get("accessToken") or "").strip()
        session_token = str(
            session_data.get("sessionToken") or session_cookie or ""
        ).strip()
        user = session_data.get("user") or {}
        account = session_data.get("account") or {}
        jwt_payload = decode_jwt_payload(access_token)
        auth_payload = jwt_payload.get("https://api.openai.com/auth") or {}

        account_id = (
            str(account.get("id") or "").strip()
            or str(auth_payload.get("chatgpt_account_id") or "").strip()
        )
        user_id = (
            str(user.get("id") or "").strip()
            or str(auth_payload.get("chatgpt_user_id") or "").strip()
            or str(auth_payload.get("user_id") or "").strip()
        )

        normalized = {
            "access_token": access_token,
            "session_token": session_token,
            "account_id": account_id,
            "user_id": user_id,
            "workspace_id": account_id,
            "expires": session_data.get("expires"),
            "user": user,
            "account": account,
            "auth_provider": session_data.get("authProvider"),
            "raw_session": session_data,
        }

        self._log("步骤 4/4: 已从当前会话中提取 accessToken")
        if account_id:
            self._log(f"Session Account ID: {account_id}")
        if user_id:
            self._log(f"Session User ID: {user_id}")
        return True, normalized

    def visit_homepage(self):
        """访问首页，建立 session"""
        self._log("访问 ChatGPT 首页...")
        url = f"{self.BASE}/"
        try:
            self._browser_pause()
            r = self.session.get(
                url,
                headers=self._headers(
                    url,
                    accept="text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                    navigation=True,
                ),
                allow_redirects=True,
                timeout=30,
            )
            return r.status_code == 200
        except Exception as e:
            self._log(f"访问首页失败: {e}")
            return False

    def get_csrf_token(self):
        """获取 CSRF token"""
        self._log("获取 CSRF token...")
        url = f"{self.BASE}/api/auth/csrf"
        try:
            r = self.session.get(
                url,
                headers=self._headers(
                    url,
                    accept="application/json",
                    referer=f"{self.BASE}/",
                    fetch_site="same-origin",
                ),
                timeout=30,
            )

            if r.status_code == 200:
                data = r.json()
                token = data.get("csrfToken", "")
                if token:
                    self._log(f"CSRF token: {token[:20]}...")
                    return token
        except Exception as e:
            self._log(f"获取 CSRF token 失败: {e}")

        return None

    def signin(self, email, csrf_token, screen_hint="login_or_signup"):
        """
        提交邮箱，获取 authorize URL

        Returns:
            str: authorize URL
        """
        self._log(f"提交邮箱: {email}")
        url = f"{self.BASE}/api/auth/signin/openai"

        params = {
            "prompt": "login",
            "ext-oai-did": self.device_id,
            "auth_session_logging_id": str(uuid.uuid4()),
            "screen_hint": str(screen_hint or "login_or_signup"),
            "login_hint": email,
        }

        form_data = {
            "callbackUrl": f"{self.BASE}/",
            "csrfToken": csrf_token,
            "json": "true",
        }

        try:
            self._browser_pause()
            r = self.session.post(
                url,
                params=params,
                data=form_data,
                headers=self._headers(
                    url,
                    accept="application/json",
                    referer=f"{self.BASE}/",
                    origin=self.BASE,
                    content_type="application/x-www-form-urlencoded",
                    fetch_site="same-origin",
                ),
                timeout=30,
            )

            if r.status_code == 200:
                data = r.json()
                authorize_url = data.get("url", "")
                if authorize_url:
                    self._log(f"获取到 authorize URL")
                    return authorize_url
        except Exception as e:
            self._log(f"提交邮箱失败: {e}")

        return None

    def login_existing_account_and_get_session(
        self,
        email,
        skymail_client,
        *,
        password="",
        totp_secret="",
        mfa_recovery_code="",
        password_reset_required=False,
        on_password_reset=None,
        otp_wait_timeout=600,
        otp_resend_wait_timeout=300,
        prepare_phone_oauth=True,
        _password_reset_depth=0,
    ):
        """通过 ChatGPT Web 登录获取 AT，不发起 offline_access token exchange。"""
        from .oauth_client import OAuthClient

        if not str(email or "").strip():
            return False, "邮箱地址为空"
        password_login = bool(str(password or "").strip())
        if skymail_client is None and not password_login:
            return False, "缺少邮箱验证码接收服务"

        self.phone_oauth_resume_context = None
        self.phone_oauth_resume_error = ""
        self.phone_oauth_browser_context = {}
        self.phone_oauth_prepare_diagnostic = {}

        try:
            otp_wait_timeout = max(30, int(otp_wait_timeout or 600))
        except Exception:
            otp_wait_timeout = 600
        try:
            otp_resend_wait_timeout = max(30, int(otp_resend_wait_timeout or 300))
        except Exception:
            otp_resend_wait_timeout = 300

        oauth_helper_config = {
            "chatgpt_oauth_otp_wait_seconds": otp_wait_timeout,
            "chatgpt_oauth_otp_resend_wait_seconds": otp_resend_wait_timeout,
        }
        helper = OAuthClient(
            oauth_helper_config,
            proxy=self.proxy,
            verbose=False,
            browser_mode=self.browser_mode,
        )
        helper._log = self._log

        phone_prepare_attempts = 0
        max_phone_prepare_attempts = 3

        def capture_phone_browser_context() -> bool:
            from .oauth_resume_cache import serialize_oauth_resume_context

            snapshot = serialize_oauth_resume_context(
                self.session,
                device_id=self.device_id,
                user_agent=self.ua,
                sec_ch_ua=self.sec_ch_ua,
                accept_language=self.accept_language,
                impersonate=self.impersonate,
                ttl_seconds=1800,
            )
            if isinstance(snapshot, dict) and int(snapshot.get("version") or 0) == 1:
                existing_cookies = self.phone_oauth_browser_context.get("cookies")
                new_cookies = snapshot.get("cookies")
                existing_has_login = any(
                    isinstance(cookie, dict)
                    and str(cookie.get("name") or "") == "login_session"
                    for cookie in (existing_cookies if isinstance(existing_cookies, list) else [])
                )
                new_has_login = any(
                    isinstance(cookie, dict)
                    and str(cookie.get("name") or "") == "login_session"
                    for cookie in (new_cookies if isinstance(new_cookies, list) else [])
                )
                if new_has_login or not existing_has_login:
                    self.phone_oauth_browser_context = snapshot
                return bool(new_has_login or existing_has_login)
            return False

        def safe_prepare_page_type(value) -> str:
            raw = str(value or "").strip().lower()
            safe = "".join(
                character
                for character in raw
                if character.isalnum() or character in {"_", "-"}
            )[:64]
            return safe or "unknown"

        def prepare_phone_transaction(
            *,
            attempts: int = 1,
            backoff_seconds: tuple[float, ...] = (),
        ):
            nonlocal phone_prepare_attempts
            if self.phone_oauth_resume_context is not None:
                return True

            remaining = min(
                max(1, int(attempts or 1)),
                max_phone_prepare_attempts - phone_prepare_attempts,
            )
            for local_attempt in range(max(0, remaining)):
                if local_attempt < len(backoff_seconds):
                    delay = max(0.0, float(backoff_seconds[local_attempt] or 0))
                    if delay:
                        self._log(
                            "手机验证 OAuth 事务将在短暂退避后重试: "
                            f"attempt={phone_prepare_attempts + 1}"
                        )
                        time.sleep(delay)
                phone_prepare_attempts += 1
                phone_helper = OAuthClient(
                    oauth_helper_config,
                    proxy=self.proxy,
                    verbose=False,
                    browser_mode=self.browser_mode,
                )
                phone_helper._log = self._log
                phone_helper.adopt_browser_context(
                    self.session,
                    device_id=self.device_id,
                    user_agent=self.ua,
                    sec_ch_ua=self.sec_ch_ua,
                    accept_language=self.accept_language,
                )
                try:
                    prepared_context = (
                        phone_helper.prepare_phone_verification_transaction(
                            email=email,
                            device_id=self.device_id,
                            user_agent=self.ua,
                            sec_ch_ua=self.sec_ch_ua,
                            accept_language=self.accept_language,
                            impersonate=self.impersonate,
                        )
                    )
                except Exception as exc:
                    prepared_context = None
                    self.phone_oauth_resume_error = str(exc).strip()[:500]

                if prepared_context is not None:
                    self.phone_oauth_resume_context = prepared_context
                    self.phone_oauth_resume_error = ""
                    prepared_state = getattr(prepared_context, "flow_state", None)
                    self.phone_oauth_prepare_diagnostic = {
                        "stage": "phone_oauth_prepare",
                        "attempt": phone_prepare_attempts,
                        "page_type": safe_prepare_page_type(
                            getattr(prepared_state, "page_type", "")
                        ),
                        "http_status": int(
                            getattr(phone_helper, "last_http_status", 0) or 0
                        ),
                        "recovery_status": "recovered",
                    }
                    self._log(
                        "首轮登录验证已通过，手机验证 OAuth 事务已同步预建"
                    )
                    return True

                failed_state = getattr(phone_helper, "last_state", None)
                failed_page_type = safe_prepare_page_type(
                    getattr(failed_state, "page_type", "")
                )
                self.phone_oauth_prepare_diagnostic = {
                    "stage": "phone_oauth_prepare",
                    "attempt": phone_prepare_attempts,
                    "page_type": failed_page_type,
                    "http_status": int(
                        getattr(phone_helper, "last_http_status", 0) or 0
                    ),
                    "recovery_status": "deferred",
                }
                self.phone_oauth_resume_error = (
                    "手机验证 OAuth 仍停留在登录页"
                    if failed_page_type in {"log_in", "login", "login_password"}
                    else "手机验证 OAuth 未进入可续接状态"
                )
                self._log(
                    "手机验证 OAuth 事务预建暂未就绪: "
                    f"attempt={phone_prepare_attempts}, page_type={failed_page_type}"
                )

            if self.phone_oauth_resume_context is not None:
                self._log(
                    "首轮登录验证已通过，手机验证 OAuth 事务已同步预建"
                )
                return True
            return False

        final_url = ""
        continue_referer = ""
        session_ready = False
        bootstrap_error = "打开 OpenAI 登录页失败"
        max_bootstrap_attempts = 6
        for bootstrap_attempt in range(max_bootstrap_attempts):
            if bootstrap_attempt > 0:
                self._log(
                    f"登录入口重试 {bootstrap_attempt + 1}/{max_bootstrap_attempts}..."
                )
                self._reset_session()
            if not self.visit_homepage():
                bootstrap_error = "访问 ChatGPT 首页失败"
                continue
            csrf_token = self.get_csrf_token()
            if not csrf_token:
                bootstrap_error = "获取 ChatGPT CSRF Token 失败"
                continue
            authorize_url = self.signin(email, csrf_token, screen_hint="login")
            if not authorize_url:
                bootstrap_error = "提交登录邮箱失败"
                continue
            final_url = self.authorize(authorize_url) or ""
            if not final_url:
                bootstrap_error = "打开 OpenAI 登录页失败"
                continue

            helper.adopt_browser_context(
                self.session,
                device_id=self.device_id,
                user_agent=self.ua,
                sec_ch_ua=self.sec_ch_ua,
                accept_language=self.accept_language,
            )
            final_parts = urlsplit(final_url)
            is_openai_auth_page = final_parts.netloc.lower().endswith("openai.com")
            has_login_session = bool(
                self._get_cookie_value("login_session")
            )
            needs_oauth_bootstrap = is_openai_auth_page and (
                self.last_authorize_status in {401, 403} or not has_login_session
            )
            if needs_oauth_bootstrap:
                self._log(
                    "OpenAI 登录会话尚未建立，切换 OAuth bootstrap/fallback..."
                )
                source_parts = urlsplit(authorize_url)
                authorize_base = (
                    f"{source_parts.scheme}://{source_parts.netloc}{source_parts.path}"
                )
                authorize_params = dict(
                    parse_qsl(source_parts.query, keep_blank_values=True)
                )
                bootstrapped_url = helper._bootstrap_oauth_session(
                    authorize_base,
                    authorize_params,
                    device_id=self.device_id,
                    user_agent=self.ua,
                    sec_ch_ua=self.sec_ch_ua,
                    impersonate=self.impersonate,
                )
                if bootstrapped_url and helper._get_cookie_value("login_session"):
                    continue_referer = bootstrapped_url
                    session_ready = True
                    break
                bootstrap_error = "OpenAI 登录会话未建立，请重新开始登录"
                continue

            continue_referer = final_url
            session_ready = True
            break
        if not final_url or not session_ready:
            return False, bootstrap_error

        helper.adopt_browser_context(
            self.session,
            device_id=self.device_id,
            user_agent=self.ua,
            sec_ch_ua=self.sec_ch_ua,
            accept_language=self.accept_language,
        )

        state = helper._state_from_url(final_url)
        referer = continue_referer or final_url
        seen_states = {}
        for step in range(16):
            signature = helper._state_signature(state)
            seen_states[signature] = seen_states.get(signature, 0) + 1
            self._log(
                f"已有账号 Web 登录状态[{step + 1}/16]: "
                f"{describe_flow_state(state)}"
            )
            if seen_states[signature] > 2:
                return False, f"邮箱登录状态卡住: {describe_flow_state(state)}"

            state_url = str(state.continue_url or state.current_url or "").lower()
            if (
                state.page_type == "api_accounts_authorize"
                or (
                    "/api/accounts/authorize" in state_url
                    and "/api/accounts/authorize/continue" not in state_url
                )
            ):
                next_state = helper._submit_authorize_continue(
                    email,
                    self.device_id,
                    referer,
                    user_agent=self.ua,
                    sec_ch_ua=self.sec_ch_ua,
                    impersonate=self.impersonate,
                    screen_hint="login",
                )
                if not next_state:
                    return False, helper.last_error or "提交登录邮箱失败"
                referer = state.current_url or state.continue_url or referer
                state = next_state
                continue

            if (
                state.page_type == "external_url"
                and self._state_requires_navigation(state)
            ):
                success, next_state = self._follow_flow_state(
                    state,
                    referer=state.current_url or referer,
                )
                if not success:
                    return False, f"邮箱登录回调失败: {next_state}"
                referer = state.current_url or referer
                state = next_state
                self.last_registration_state = state
                continue

            if self._is_registration_complete_state(state):
                self.last_registration_state = state
                break

            if helper._state_is_login_password(state):
                if password_reset_required:
                    if int(_password_reset_depth or 0) >= 1:
                        return False, "密码重置后仍返回重置入口，已停止循环"
                    reset_state = helper._complete_password_reset(
                        state,
                        email=email,
                        new_password=str(password or ""),
                        skymail_client=skymail_client,
                        device_id=self.device_id,
                        user_agent=self.ua,
                        sec_ch_ua=self.sec_ch_ua,
                        impersonate=self.impersonate,
                        on_password_reset=on_password_reset,
                    )
                    if not reset_state:
                        return False, helper.last_error or "ChatGPT 密码重置失败"
                    return self.login_existing_account_and_get_session(
                        email,
                        skymail_client,
                        password=str(password or ""),
                        totp_secret=str(totp_secret or ""),
                        mfa_recovery_code=str(mfa_recovery_code or ""),
                        password_reset_required=False,
                        on_password_reset=on_password_reset,
                        otp_wait_timeout=otp_wait_timeout,
                        otp_resend_wait_timeout=otp_resend_wait_timeout,
                        prepare_phone_oauth=prepare_phone_oauth,
                        _password_reset_depth=int(_password_reset_depth or 0) + 1,
                    )
                if password_login:
                    next_state = helper._submit_password_verify(
                        str(password or ""),
                        self.device_id,
                        user_agent=self.ua,
                        sec_ch_ua=self.sec_ch_ua,
                        impersonate=self.impersonate,
                        referer=state.current_url or state.continue_url or referer,
                    )
                else:
                    next_state = helper._send_passwordless_login_otp(
                        email,
                        self.device_id,
                        user_agent=self.ua,
                        sec_ch_ua=self.sec_ch_ua,
                        impersonate=self.impersonate,
                        referer=state.current_url or state.continue_url or referer,
                    )
                if not next_state:
                    return False, helper.last_error or "ChatGPT 密码登录失败"
                referer = state.current_url or referer
                state = next_state
                continue

            if helper._state_is_mfa_challenge(state):
                next_state = helper._submit_mfa_challenge(
                    state,
                    email=email,
                    skymail_client=skymail_client,
                    totp_secret=str(totp_secret or ""),
                    mfa_recovery_code=str(mfa_recovery_code or ""),
                    device_id=self.device_id,
                    user_agent=self.ua,
                    sec_ch_ua=self.sec_ch_ua,
                    impersonate=self.impersonate,
                )
                if not next_state:
                    return False, helper.last_error or "ChatGPT MFA 验证失败"
                if prepare_phone_oauth:
                    capture_phone_browser_context()
                    prepare_phone_transaction(attempts=1)
                referer = state.current_url or state.continue_url or referer
                state = next_state
                self.last_registration_state = state
                continue

            if helper._state_is_email_otp(state):
                next_state = helper._handle_otp_verification(
                    email,
                    self.device_id,
                    self.ua,
                    self.sec_ch_ua,
                    self.impersonate,
                    skymail_client,
                    state,
                    prefer_passwordless_login=True,
                    allow_cached_code_retry=False,
                )
                if not next_state:
                    return False, helper.last_error or "邮箱验证码校验失败"
                if not helper._state_is_mfa_challenge(next_state):
                    if prepare_phone_oauth:
                        capture_phone_browser_context()
                        prepare_phone_transaction(attempts=1)
                referer = state.current_url or referer
                state = next_state
                self.last_registration_state = state
                continue

            if helper._state_is_add_phone(state):
                self._log("Web 邮箱登录已进入 add_phone，开始读取当前 ChatGPT Session")
                self.last_registration_state = state
                break

            if helper._state_is_about_you(state):
                return False, "该邮箱尚未完成 ChatGPT 账号注册"

            if self._state_requires_navigation(state):
                success, next_state = self._follow_flow_state(
                    state,
                    referer=state.current_url or referer,
                )
                if not success:
                    return False, f"邮箱登录跳转失败: {next_state}"
                referer = state.current_url or referer
                state = next_state
                self.last_registration_state = state
                continue

            return False, f"未支持的邮箱登录状态: {describe_flow_state(state)}"
        else:
            return False, "邮箱登录状态机超出最大步数"

        if prepare_phone_oauth:
            capture_phone_browser_context()
        session_ok, session_result = self.fetch_chatgpt_session()
        if not session_ok:
            try:
                self.visit_homepage()
            except Exception:
                pass
            session_ok, session_result = self.fetch_chatgpt_session()
        if not session_ok:
            return False, f"邮箱登录完成但未获取 Access Token: {session_result}"

        session_data = session_result if isinstance(session_result, dict) else {}
        access_token = str(session_data.get("accessToken") or "").strip()
        if not access_token:
            return False, "ChatGPT Session 未返回 Access Token"

        if prepare_phone_oauth:
            capture_phone_browser_context()

        if prepare_phone_oauth and (
            self.phone_oauth_resume_context is None
            and phone_prepare_attempts < max_phone_prepare_attempts
        ):
            self._log(
                "Access Token 已获取，分阶段重试手机验证 OAuth 预建"
            )
            retry_attempts = 2 if phone_prepare_attempts else 1
            prepare_phone_transaction(
                attempts=retry_attempts,
                backoff_seconds=(0.5, 1.0),
            )

        if not prepare_phone_oauth:
            self.phone_oauth_resume_error = ""
            self.phone_oauth_prepare_diagnostic = {}
        elif self.phone_oauth_resume_context is not None:
            self._log(
                "Access Token 已获取，手机验证 OAuth 事务可直接续接；"
                "无需再次登录邮箱"
            )
        else:
            prepare_detail = str(self.phone_oauth_resume_error or "").strip()
            self.phone_oauth_resume_error = (
                "首轮邮箱登录会话未能预建手机验证 OAuth 事务"
                + (f": {prepare_detail}" if prepare_detail else "")
            )[:500]
            self._log(
                "Access Token 已保留，但手机验证 OAuth 事务未就绪；"
                "接码阶段将从已认证浏览器快照建立一次新事务；"
                "不会再次发送邮箱验证码"
            )

        session_cookie = self.get_next_auth_session_token()
        user = session_data.get("user") or {}
        account = session_data.get("account") or {}
        jwt_payload = decode_jwt_payload(access_token)
        auth_payload = jwt_payload.get("https://api.openai.com/auth") or {}
        account_id = str(
            account.get("id")
            or auth_payload.get("chatgpt_account_id")
            or ""
        ).strip()
        user_id = str(
            user.get("id")
            or auth_payload.get("chatgpt_user_id")
            or auth_payload.get("user_id")
            or ""
        ).strip()
        return True, {
            "access_token": access_token,
            "session_token": str(
                session_data.get("sessionToken") or session_cookie or ""
            ).strip(),
            "account_id": account_id,
            "workspace_id": account_id,
            "user_id": user_id,
            "user": user,
            "account": account,
            "expires": session_data.get("expires"),
        }

    def authorize(self, url, max_retries=3):
        """
        访问 authorize URL，跟随重定向（带重试机制）
        这是关键步骤，建立 auth.openai.com 的 session

        Returns:
            str: 最终重定向的 URL
        """
        self.last_authorize_status = 0
        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    self._log(
                        f"访问 authorize URL... (尝试 {attempt + 1}/{max_retries})"
                    )
                    time.sleep(1)  # 重试前等待
                else:
                    self._log("访问 authorize URL...")

                self._browser_pause()
                r = self.session.get(
                    url,
                    headers=self._headers(
                        url,
                        accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        referer=f"{self.BASE}/",
                        navigation=True,
                    ),
                    allow_redirects=True,
                    timeout=30,
                )

                final_url = str(r.url)
                self.last_authorize_status = int(r.status_code or 0)
                redirects = len(getattr(r, "history", []) or [])
                content_type = str(
                    (getattr(r, "headers", {}) or {}).get("content-type", "") or ""
                ).split(";", 1)[0].strip().lower()
                final_page = urlparse(final_url).path or "/"
                has_login_session = bool(
                    self._get_cookie_value("login_session", "auth.openai.com")
                )
                self._log(
                    "authorize 会话诊断: "
                    f"status={r.status_code} "
                    f"redirects={redirects} "
                    f"login_session={'已获取' if has_login_session else '未获取'} "
                    f"final_page={final_page} "
                    f"content_type={content_type or 'unknown'}"
                )
                self._log(f"重定向到: {final_url}")
                return final_url

            except Exception as e:
                error_msg = str(e)
                is_tls_error = (
                    "TLS" in error_msg
                    or "SSL" in error_msg
                    or "curl: (35)" in error_msg
                )

                if is_tls_error and attempt < max_retries - 1:
                    self._log(
                        f"Authorize TLS 错误 (尝试 {attempt + 1}/{max_retries}): {error_msg[:100]}"
                    )
                    continue
                else:
                    self._log(f"Authorize 失败: {e}")
                    return ""

        return ""

    def callback(self, callback_url=None, referer=None):
        """完成注册回调"""
        self._log("执行回调...")
        url = callback_url or f"{self.AUTH}/api/accounts/authorize/callback"
        ok, _ = self._follow_flow_state(
            self._state_from_url(url),
            referer=referer or f"{self.AUTH}/about-you",
        )
        return ok

    def register_user(self, email, password):
        """
        注册用户（邮箱 + 密码）

        Returns:
            tuple: (success, message)
        """
        self._enter_stage("authorize_continue", f"register_user email={email}")
        self._log(f"注册用户: {email}")
        url = f"{self.AUTH}/api/accounts/user/register"

        headers = self._headers(
            url,
            accept="application/json",
            referer=f"{self.AUTH}/create-account/password",
            origin=self.AUTH,
            content_type="application/json",
            fetch_site="same-origin",
        )
        headers.update(generate_datadog_trace())
        headers["oai-device-id"] = self.device_id

        sentinel_token = self._get_sentinel_token(
            "username_password_create",
            page_url=f"{self.AUTH}/create-account/password",
        )
        if sentinel_token:
            headers["openai-sentinel-token"] = sentinel_token

        payload = {
            "username": email,
            "password": password,
        }

        try:
            self._browser_pause()
            r = self.session.post(url, json=payload, headers=headers, timeout=30)

            if r.status_code == 200:
                data = r.json()
                self._log("注册成功")
                self._log(f"authorize_continue/register_user 响应 URL: {str(r.url)[:120]}")
                return True, "注册成功"
            else:
                try:
                    error_data = r.json()
                    error_msg = error_data.get("error", {}).get("message", r.text[:200])
                except:
                    error_msg = r.text[:200]
                self._log(f"注册失败: {r.status_code} - {error_msg}")
                return False, f"HTTP {r.status_code}: {error_msg}"

        except Exception as e:
            self._log(f"注册异常: {e}")
            return False, str(e)

    def send_email_otp(self, referer=None):
        """触发发送邮箱验证码"""
        self._enter_stage("otp", "send email otp")
        self._log("触发发送验证码...")
        url = f"{self.AUTH}/api/accounts/email-otp/send"

        try:
            self._browser_pause()
            r = self.session.get(
                url,
                headers=self._headers(
                    url,
                    accept="application/json, text/plain, */*",
                    referer=referer or f"{self.AUTH}/create-account/password",
                    fetch_site="same-origin",
                ),
                allow_redirects=True,
                timeout=30,
            )
            self._log(f"验证码发送状态: {r.status_code}")
            if r.status_code != 200:
                self._log(f"验证码发送失败响应: {r.text[:180]}")
                return False

            try:
                payload = r.json()
            except Exception:
                payload = {}

            if isinstance(payload, dict) and payload:
                next_state = self._state_from_payload(payload, current_url=str(r.url) or url)
                self._log(f"验证码发送响应: {describe_flow_state(next_state)}")
                self._log(f"otp/send 当前 URL: {str(r.url)[:120]}")
            else:
                self._log("验证码发送响应: 非 JSON（按已触发处理）")
            return True
        except Exception as e:
            self._log(f"发送验证码失败: {e}")
            return False

    def verify_email_otp(self, otp_code, return_state=False):
        """
        验证邮箱 OTP 码

        Args:
            otp_code: 6位验证码

        Returns:
            tuple: (success, message)
        """
        self._enter_stage("otp", f"verify email otp code={otp_code}")
        self._log(f"验证 OTP 码: {otp_code}")
        url = f"{self.AUTH}/api/accounts/email-otp/validate"

        headers = self._headers(
            url,
            accept="application/json",
            referer=f"{self.AUTH}/email-verification",
            origin=self.AUTH,
            content_type="application/json",
            fetch_site="same-origin",
        )
        headers.update(generate_datadog_trace())

        payload = {"code": otp_code}

        try:
            self._browser_pause()
            r = self.session.post(url, json=payload, headers=headers, timeout=30)

            if r.status_code == 200:
                try:
                    data = r.json()
                except Exception:
                    data = {}
                next_state = self._state_from_payload(
                    data, current_url=str(r.url) or f"{self.AUTH}/about-you"
                )
                self._log(f"验证成功 {describe_flow_state(next_state)}")
                self._log(f"otp/validate 当前 URL: {str(r.url)[:120]}")
                return (True, next_state) if return_state else (True, "验证成功")
            else:
                error_msg = r.text[:200]
                self._log(f"验证失败: {r.status_code} - {error_msg}")
                return False, f"HTTP {r.status_code}"

        except Exception as e:
            self._log(f"验证异常: {e}")
            return False, str(e)

    def create_account(self, first_name, last_name, birthdate, return_state=False):
        """
        完成账号创建（提交姓名和生日）

        Args:
            first_name: 名
            last_name: 姓
            birthdate: 生日 (YYYY-MM-DD)

        Returns:
            tuple: (success, message)
        """
        self._enter_stage("about_you", "register create_account")
        name = f"{first_name} {last_name}"
        self._log(f"完成账号创建: {name}")
        url = f"{self.AUTH}/api/accounts/create_account"

        sentinel_token = self._get_sentinel_token(
            "oauth_create_account",
            page_url=f"{self.AUTH}/about-you",
        )
        if sentinel_token:
            self._log("create_account: 已生成 sentinel token")
        else:
            self._log("create_account: 未生成 sentinel token，降级继续请求")

        headers = self._headers(
            url,
            accept="application/json",
            referer=f"{self.AUTH}/about-you",
            origin=self.AUTH,
            content_type="application/json",
            fetch_site="same-origin",
            extra_headers={
                "oai-device-id": self.device_id,
            },
        )
        if sentinel_token:
            headers["openai-sentinel-token"] = sentinel_token
        headers.update(generate_datadog_trace())

        payload = {
            "name": name,
            "birthdate": birthdate,
        }

        try:
            self._browser_pause()
            r = self.session.post(url, json=payload, headers=headers, timeout=30)

            if r.status_code == 200:
                try:
                    data = r.json()
                except Exception:
                    data = {}
                next_state = self._state_from_payload(
                    data, current_url=str(r.url) or self.BASE
                )
                self._log(f"账号创建成功 {describe_flow_state(next_state)}")
                self._log(f"about_you/create_account 当前 URL: {str(r.url)[:120]}")
                return (True, next_state) if return_state else (True, "账号创建成功")
            else:
                error_code = ""
                error_msg = r.text[:200]
                try:
                    error_data = r.json() or {}
                    error_info = error_data.get("error") or {}
                    error_code = str(error_info.get("code") or "").strip()
                    error_msg = str(error_info.get("message") or error_msg).strip()
                except Exception:
                    pass

                detail = f"HTTP {r.status_code}"
                if error_code:
                    detail += f": {error_code}"
                elif error_msg:
                    detail += f": {error_msg}"

                self._log(f"创建失败: {detail} - {error_msg[:200]}")
                return False, detail

        except Exception as e:
            self._log(f"创建异常: {e}")
            return False, str(e)

    def register_complete_flow(
        self,
        email,
        password,
        first_name,
        last_name,
        birthdate,
        skymail_client,
        stop_before_about_you_submission=False,
        otp_wait_timeout=600,
        otp_resend_wait_timeout=300,
    ):
        """
        完整的注册流程（基于原版 run_register 方法）

        Args:
            email: 邮箱
            password: 密码
            first_name: 名
            last_name: 姓
            birthdate: 生日
            skymail_client: Skymail 客户端（用于获取验证码）

        Returns:
            tuple: (success, message)
        """
        from urllib.parse import urlparse

        self._log(
            "注册状态机参数: "
            f"stop_before_about_you_submission={'on' if stop_before_about_you_submission else 'off'}, "
            f"otp_wait_timeout={otp_wait_timeout}s, otp_resend_wait_timeout={otp_resend_wait_timeout}s"
        )

        try:
            otp_wait_timeout = max(30, int(otp_wait_timeout or 600))
        except Exception:
            otp_wait_timeout = 600
        try:
            otp_resend_wait_timeout = max(30, int(otp_resend_wait_timeout or 300))
        except Exception:
            otp_resend_wait_timeout = 300

        max_auth_attempts = 3
        final_url = ""
        final_path = ""

        for auth_attempt in range(max_auth_attempts):
            if auth_attempt > 0:
                self._log(f"预授权阶段重试 {auth_attempt + 1}/{max_auth_attempts}...")
                self._reset_session()

            # 1. 访问首页
            if not self.visit_homepage():
                if auth_attempt < max_auth_attempts - 1:
                    continue
                return False, "访问首页失败"

            # 2. 获取 CSRF token
            csrf_token = self.get_csrf_token()
            if not csrf_token:
                if auth_attempt < max_auth_attempts - 1:
                    continue
                return False, "获取 CSRF token 失败"

            # 3. 提交邮箱，获取 authorize URL
            auth_url = self.signin(email, csrf_token)
            if not auth_url:
                if auth_attempt < max_auth_attempts - 1:
                    continue
                return False, "提交邮箱失败"

            # 4. 访问 authorize URL（关键步骤！）
            final_url = self.authorize(auth_url)
            if not final_url:
                if auth_attempt < max_auth_attempts - 1:
                    continue
                return False, "Authorize 失败"

            final_path = urlparse(final_url).path
            self._log(f"Authorize → {final_path}")

            # /api/accounts/authorize 实际上常对应 Cloudflare 403 中间页，不要继续走 authorize_continue。
            if "api/accounts/authorize" in final_path or final_path == "/error":
                self._log(
                    f"检测到 Cloudflare/SPA 中间页，准备重试预授权: {final_url[:160]}..."
                )
                if auth_attempt < max_auth_attempts - 1:
                    continue
                return False, f"预授权被拦截: {final_path}"

            break

        state = self._state_from_url(final_url)
        self._log(f"注册状态起点: {describe_flow_state(state)}")

        register_submitted = False
        otp_verified = False
        account_created = False
        seen_states = {}

        otp_send_attempts = 0

        for _ in range(12):
            signature = self._state_signature(state)
            seen_states[signature] = seen_states.get(signature, 0) + 1
            self._log(
                f"注册状态推进: step={sum(seen_states.values())} "
                f"state={describe_flow_state(state)} seen={seen_states[signature]}"
            )
            if seen_states[signature] > 2:
                return False, f"注册状态卡住: {describe_flow_state(state)}"

            if self._is_registration_complete_state(state):
                self.last_registration_state = state
                self._log("[OK] 注册流程完成")
                return True, "注册成功"

            if self._state_is_password_registration(state):
                self._enter_stage("authorize_continue", describe_flow_state(state))
                self._log("全新注册流程")
                if register_submitted:
                    return False, "注册密码阶段重复进入"
                success, msg = self.register_user(email, password)
                if not success:
                    return False, f"注册失败: {msg}"
                register_submitted = True
                otp_send_attempts += 1
                self._log(f"发送注册验证码: attempt={otp_send_attempts}")
                if not self.send_email_otp(
                    referer=state.current_url or state.continue_url or f"{self.AUTH}/create-account/password"
                ):
                    self._log("发送验证码接口返回失败，继续等待邮箱中的验证码...")
                else:
                    self._log("发送注册验证码成功，进入收码阶段")
                state = self._state_from_url(f"{self.AUTH}/email-verification")
                continue

            if self._state_is_email_otp(state):
                self._enter_stage("otp", describe_flow_state(state))
                self._log("等待邮箱验证码...")
                otp_code = skymail_client.wait_for_verification_code(
                    email, timeout=otp_wait_timeout
                )
                if not otp_code:
                    self._log(
                        "首次等待未收到验证码，尝试重发一次 email-otp/send "
                        f"后再等待 {otp_resend_wait_timeout}s"
                    )
                    otp_send_attempts += 1
                    resend_ok = self.send_email_otp(
                        referer=state.current_url or state.continue_url or f"{self.AUTH}/email-verification"
                    )
                    if resend_ok:
                        self._log(f"重发验证码成功: attempt={otp_send_attempts}")
                    else:
                        self._log(f"重发验证码失败: attempt={otp_send_attempts}")
                    otp_code = skymail_client.wait_for_verification_code(
                        email, timeout=otp_resend_wait_timeout
                    )
                if not otp_code:
                    return False, "未收到验证码"

                success, next_state = self.verify_email_otp(otp_code, return_state=True)
                if not success:
                    return False, f"验证码失败: {next_state}"
                otp_verified = True
                state = next_state
                self.last_registration_state = state
                continue

            if self._state_is_about_you(state):
                self._enter_stage("about_you", describe_flow_state(state))
                if stop_before_about_you_submission:
                    self.last_registration_state = state
                    self._log(
                        "注册链路已到 about_you，按 interrupt 流程停止。"
                        "下一步交由 OAuth 新会话提交姓名+生日。"
                    )
                    return True, "pending_about_you_submission"
                if account_created:
                    return False, "填写信息阶段重复进入"
                success, next_state = self.create_account(
                    first_name,
                    last_name,
                    birthdate,
                    return_state=True,
                )
                if not success:
                    return False, f"创建账号失败: {next_state}"
                account_created = True
                state = next_state
                self.last_registration_state = state
                continue

            if self._state_requires_navigation(state):
                if "workspace" in f"{state.continue_url} {state.current_url}".lower() or "consent" in f"{state.continue_url} {state.current_url}".lower():
                    self._enter_stage("workspace_select", describe_flow_state(state))
                elif state.page_type == "external_url":
                    self._enter_stage("token_exchange", describe_flow_state(state))
                success, next_state = self._follow_flow_state(
                    state,
                    referer=state.current_url or f"{self.AUTH}/about-you",
                )
                if not success:
                    return False, f"跳转失败: {next_state}"
                state = next_state
                self.last_registration_state = state
                continue

            if (
                (not register_submitted)
                and (not otp_verified)
                and (not account_created)
            ):
                self._log(
                    f"未知起始状态，回退为全新注册流程: {describe_flow_state(state)}"
                )
                state = self._state_from_url(f"{self.AUTH}/create-account/password")
                continue

            return False, f"未支持的注册状态: {describe_flow_state(state)}"

        return False, "注册状态机超出最大步数"
