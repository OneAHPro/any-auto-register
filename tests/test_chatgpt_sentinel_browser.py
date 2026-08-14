import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

from core.task_runtime import (
    RegisterTaskControl,
    RegisterTaskStore,
    StopTaskRequested,
    bind_task_attempt_context,
)
from platforms.chatgpt.sentinel_browser import (
    _register_current_attempt_driver_interrupt,
    get_sentinel_token_via_browser,
)


class _FakeDriverProcess:
    def __init__(self, release: threading.Event):
        self.release = release
        self.terminated = threading.Event()
        self.terminate_calls = 0
        self.pid = 12345
        self.returncode = None

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.terminated.set()
        self.release.set()


class _FakePage:
    def __init__(
        self,
        *,
        block_evaluate: bool = False,
        evaluate_error: BaseException | None = None,
        wait_error: BaseException | None = None,
    ):
        self.block_evaluate = block_evaluate
        self.evaluate_error = evaluate_error
        self.wait_error = wait_error
        self.evaluate_started = threading.Event()
        self.release_evaluate = threading.Event()
        self.evaluate_script = ""
        self.evaluate_arg = None
        self.goto_url = ""
        self.goto_kwargs = {}

    def goto(self, url, **kwargs) -> None:
        self.goto_url = str(url)
        self.goto_kwargs = dict(kwargs)

    def wait_for_function(self, *_args, **_kwargs) -> None:
        if self.wait_error is not None:
            raise self.wait_error

    def evaluate(self, script, arg):
        self.evaluate_script = script
        self.evaluate_arg = arg
        self.evaluate_started.set()
        if self.block_evaluate:
            self.release_evaluate.wait(timeout=2)
            raise RuntimeError("Playwright driver was terminated")
        if self.evaluate_error is not None:
            raise self.evaluate_error
        return {
            "success": True,
            "token": json.dumps({"p": "p", "t": "t", "c": "c"}),
        }


class _FakeBrowserContext:
    def __init__(self, page: _FakePage):
        self.page = page
        self.cookies = []

    def add_cookies(self, cookies) -> None:
        self.cookies.extend(cookies)

    def new_page(self) -> _FakePage:
        return self.page


class _FakeBrowser:
    def __init__(self, page: _FakePage, *, block_close: bool = False):
        self.page = page
        self.block_close = block_close
        self.close_calls = 0
        self.close_started = threading.Event()
        self.release_close = threading.Event()
        self.context = None

    def new_context(self, **_kwargs) -> _FakeBrowserContext:
        self.context = _FakeBrowserContext(self.page)
        return self.context

    def close(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        if self.block_close:
            self.release_close.wait(timeout=2)


class _FakeChromium:
    def __init__(self, browser: _FakeBrowser):
        self.browser = browser

    def launch(self, **_kwargs) -> _FakeBrowser:
        return self.browser


class _FakePlaywright:
    def __init__(self, page: _FakePage, *, block_browser_close: bool = False):
        self.driver = _FakeDriverProcess(page.release_evaluate)
        self.browser = _FakeBrowser(page, block_close=block_browser_close)
        self.chromium = _FakeChromium(self.browser)
        self.stop_calls = 0
        self._impl_obj = SimpleNamespace(
            _connection=SimpleNamespace(
                _transport=SimpleNamespace(_proc=self.driver)
            )
        )

    def stop(self) -> None:
        self.stop_calls += 1


class _FakePlaywrightManager:
    def __init__(self, playwright: _FakePlaywright):
        self.playwright = playwright

    def start(self) -> _FakePlaywright:
        return self.playwright

    def __enter__(self) -> _FakePlaywright:
        return self.playwright

    def __exit__(self, *_args) -> None:
        self.playwright.stop()


def _browser_patches(manager: _FakePlaywrightManager):
    return (
        patch("playwright.sync_api.sync_playwright", return_value=manager),
        patch(
            "platforms.chatgpt.sentinel_browser.resolve_browser_headless",
            return_value=(True, "test"),
        ),
        patch("platforms.chatgpt.sentinel_browser.ensure_browser_display_available"),
    )


def test_sentinel_sdk_evaluate_has_a_bounded_javascript_timeout():
    page = _FakePage()
    playwright = _FakePlaywright(page)
    manager = _FakePlaywrightManager(playwright)
    sync_patch, headless_patch, display_patch = _browser_patches(manager)

    with sync_patch, headless_patch, display_patch:
        token = get_sentinel_token_via_browser(
            flow="password_verify",
            timeout_ms=90_000,
        )

    assert token
    assert "Promise.race" in page.evaluate_script
    assert page.evaluate_arg == {
        "flow": "password_verify",
        "timeoutMs": 45_000,
    }


def test_browser_loads_fixed_sentinel_frame_and_scopes_device_cookie_to_both_hosts():
    page = _FakePage()
    playwright = _FakePlaywright(page)
    manager = _FakePlaywrightManager(playwright)
    sync_patch, headless_patch, display_patch = _browser_patches(manager)
    logs = []

    with sync_patch, headless_patch, display_patch:
        token = get_sentinel_token_via_browser(
            flow="password_verify",
            page_url="https://auth.openai.com/log-in/password?state=secret-query",
            device_id="fixture-device-id",
            log_fn=logs.append,
        )

    assert token
    assert page.goto_url == (
        "https://sentinel.openai.com/backend-api/sentinel/frame.html?sv=20260219f9f6"
    )
    assert page.goto_kwargs["wait_until"] == "load"
    assert playwright.browser.context is not None
    assert {cookie.get("url") for cookie in playwright.browser.context.cookies} == {
        "https://sentinel.openai.com/",
        "https://auth.openai.com/",
    }
    assert all("secret-query" not in line for line in logs)
    assert all("fixture-device-id" not in line for line in logs)


def test_sdk_wait_failure_is_logged_as_recoverable_http_pow_fallback():
    page = _FakePage(wait_error=RuntimeError("Timeout 15000ms exceeded"))
    playwright = _FakePlaywright(page)
    manager = _FakePlaywrightManager(playwright)
    sync_patch, headless_patch, display_patch = _browser_patches(manager)
    logs = []

    with sync_patch, headless_patch, display_patch:
        token = get_sentinel_token_via_browser(
            flow="password_verify",
            log_fn=logs.append,
        )

    assert token is None
    assert "Sentinel 浏览器通道未就绪，准备使用 HTTP PoW" in logs
    assert all("Timeout 15000ms exceeded" not in line for line in logs)
    assert playwright.browser.close_calls == 1
    assert playwright.stop_calls == 1


def test_task_stop_terminates_only_the_current_sentinel_driver():
    page = _FakePage(block_evaluate=True)
    playwright = _FakePlaywright(page)
    manager = _FakePlaywrightManager(playwright)
    control = RegisterTaskControl()
    attempt_id = control.start_attempt()
    outcome: list[BaseException | None] = []

    def run() -> None:
        try:
            with bind_task_attempt_context(control, attempt_id):
                get_sentinel_token_via_browser(flow="password_verify")
        except Exception as exc:
            outcome.append(exc)
        else:
            outcome.append(None)

    sync_patch, headless_patch, display_patch = _browser_patches(manager)
    worker = threading.Thread(target=run)
    try:
        with sync_patch, headless_patch, display_patch:
            worker.start()
            assert page.evaluate_started.wait(timeout=1)
            control.request_stop_once()
            assert playwright.driver.terminated.wait(timeout=0.5)
            worker.join(timeout=1)

        assert not worker.is_alive()
        assert len(outcome) == 1
        assert isinstance(outcome[0], StopTaskRequested)
        assert playwright.driver.terminate_calls == 1
        assert playwright.browser.close_calls == 1
        assert playwright.stop_calls == 1
    finally:
        page.release_evaluate.set()
        worker.join(timeout=1)
        control.finish_attempt(attempt_id)


def test_ordinary_browser_error_keeps_existing_fallback_behavior():
    page = _FakePage(evaluate_error=RuntimeError("ordinary browser failure"))
    playwright = _FakePlaywright(page)
    manager = _FakePlaywrightManager(playwright)
    sync_patch, headless_patch, display_patch = _browser_patches(manager)

    with sync_patch, headless_patch, display_patch:
        token = get_sentinel_token_via_browser(flow="password_verify")

    assert token is None
    assert playwright.driver.terminate_calls == 0
    assert playwright.browser.close_calls == 1
    assert playwright.stop_calls == 1


def test_stop_interrupt_does_not_reenter_the_task_store_log_lock():
    task_id = "sentinel-store-lock"
    page = _FakePage()
    playwright = _FakePlaywright(page)
    store = RegisterTaskStore()
    record = store.create(
        task_id,
        platform="chatgpt",
        total=1,
        source="manual",
    )
    attempt_id = record.control.start_attempt()
    result = []

    with bind_task_attempt_context(record.control, attempt_id):
        unregister = _register_current_attempt_driver_interrupt(
            playwright,
        )

    worker = threading.Thread(
        target=lambda: result.append(store.request_stop_if_active(task_id)),
        daemon=True,
    )
    try:
        worker.start()
        worker.join(timeout=0.5)

        assert not worker.is_alive()
        assert len(result) == 1
        assert playwright.driver.terminated.is_set()
    finally:
        unregister()
        record.control.finish_attempt(attempt_id)


def test_stopping_one_attempt_does_not_terminate_another_driver():
    first = _FakePlaywright(_FakePage())
    second = _FakePlaywright(_FakePage())
    first_control = RegisterTaskControl()
    second_control = RegisterTaskControl()
    first_attempt = first_control.start_attempt()
    second_attempt = second_control.start_attempt()

    with bind_task_attempt_context(first_control, first_attempt):
        unregister_first = _register_current_attempt_driver_interrupt(first)
    with bind_task_attempt_context(second_control, second_attempt):
        unregister_second = _register_current_attempt_driver_interrupt(second)

    try:
        first_control.request_stop_once()

        assert first.driver.terminated.is_set()
        assert first.driver.terminate_calls == 1
        assert not second.driver.terminated.is_set()
        assert second.driver.terminate_calls == 0
    finally:
        unregister_first()
        unregister_second()
        first_control.finish_attempt(first_attempt)
        second_control.finish_attempt(second_attempt)


def test_stop_during_browser_cleanup_still_interrupts_and_propagates():
    page = _FakePage()
    playwright = _FakePlaywright(page, block_browser_close=True)
    manager = _FakePlaywrightManager(playwright)
    control = RegisterTaskControl()
    attempt_id = control.start_attempt()
    outcome: list[BaseException | None] = []

    def run() -> None:
        try:
            with bind_task_attempt_context(control, attempt_id):
                get_sentinel_token_via_browser(flow="password_verify")
        except Exception as exc:
            outcome.append(exc)
        else:
            outcome.append(None)

    sync_patch, headless_patch, display_patch = _browser_patches(manager)
    worker = threading.Thread(target=run)
    try:
        with sync_patch, headless_patch, display_patch:
            worker.start()
            assert playwright.browser.close_started.wait(timeout=1)
            control.request_stop_once()
            assert playwright.driver.terminated.wait(timeout=0.5)
            playwright.browser.release_close.set()
            worker.join(timeout=1)

        assert not worker.is_alive()
        assert len(outcome) == 1
        assert isinstance(outcome[0], StopTaskRequested)
        assert playwright.driver.terminate_calls == 1
    finally:
        playwright.browser.release_close.set()
        worker.join(timeout=1)
        control.finish_attempt(attempt_id)
