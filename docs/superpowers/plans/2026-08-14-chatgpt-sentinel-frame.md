# ChatGPT Sentinel Frame Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Load the Sentinel browser SDK from OpenAI's dedicated frame instead of waiting 15 seconds on Auth pages that return 403.

**Architecture:** Keep the existing Playwright lifecycle, task interruption, bounded JavaScript token call, and HTTP PoW fallback. Change only the SDK host page and device-cookie scope, then use neutral recoverable-channel logging.

**Tech Stack:** Python 3.11+, Playwright sync API, pytest.

---

### Task 1: Lock the browser helper to the official Sentinel frame

**Files:**
- Modify: `tests/test_chatgpt_sentinel_browser.py`
- Modify: `platforms/chatgpt/sentinel_browser.py`

- [ ] **Step 1: Write the failing frame and cookie tests**

Extend the fake page/context to capture `goto()` and `add_cookies()`, then add:

```python
def test_browser_loads_fixed_sentinel_frame_and_scopes_device_cookie_to_both_hosts():
    token = get_sentinel_token_via_browser(
        flow="password_verify",
        page_url="https://auth.openai.com/log-in/password?state=secret",
        device_id="fixture-device-id",
    )
    assert token
    assert page.goto_url == SENTINEL_FRAME_URL
    assert page.goto_kwargs["wait_until"] == "load"
    assert {cookie["url"] for cookie in context.cookies} == {
        "https://sentinel.openai.com/",
        "https://auth.openai.com/",
    }
```

Also assert logs do not contain the query-bearing `page_url`.

- [ ] **Step 2: Run the focused test and confirm RED**

Run:

```bash
PYTHONPATH="$PWD" /tmp/any-auto-register-py311/bin/python -m pytest -q \
  tests/test_chatgpt_sentinel_browser.py \
  -k 'fixed_sentinel_frame or scopes_device_cookie'
```

Expected: the fake page records the Auth URL and only an Auth-domain cookie.

- [ ] **Step 3: Implement the fixed frame**

Add:

```python
SENTINEL_FRAME_URL = (
    "https://sentinel.openai.com/backend-api/sentinel/frame.html"
    f"?sv={SENTINEL_VERSION}"
)
```

Navigate to `SENTINEL_FRAME_URL` with `wait_until="load"`. Add two URL-scoped
`oai-did` cookies, one for Sentinel and one for Auth. Keep `page_url` in the
public signature for compatibility but never log it or use it as the SDK host.

- [ ] **Step 4: Add recoverable failure logging coverage**

Make a fake `wait_for_function()` raise a timeout and assert the helper returns
`None`, closes browser/driver, and logs:

```text
Sentinel 浏览器通道未就绪，准备使用 HTTP PoW
```

The log must not contain token values, device ID, cookies, or the Auth URL.

- [ ] **Step 5: Run focused and OAuth compatibility tests GREEN**

```bash
PYTHONPATH="$PWD" /tmp/any-auto-register-py311/bin/python -m pytest -q \
  tests/test_chatgpt_sentinel_browser.py \
  tests/test_chatgpt_phone_flow.py \
  tests/test_chatgpt_relogin.py \
  tests/test_chatgpt_register.py
```

Expected: all selected tests pass.

- [ ] **Step 6: Run an optional no-credential Chrome smoke probe**

Use the installed Chrome executable to load `SENTINEL_FRAME_URL`, wait for
`SentinelSDK.token`, and request `password_verify`. Print only HTTP status,
boolean SDK readiness, boolean token presence, token key names, and elapsed
seconds.

- [ ] **Step 7: Commit**

```bash
git add platforms/chatgpt/sentinel_browser.py tests/test_chatgpt_sentinel_browser.py
git commit -m "fix(chatgpt): load Sentinel SDK from official frame"
```
