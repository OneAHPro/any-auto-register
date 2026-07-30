# Codex2API Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Codex2API as an independently configured ChatGPT credential destination with automatic RT-first upload, AT fallback, manual single/batch upload, and persistent account status.

**Architecture:** A focused uploader calls the official Codex2API JSON management endpoints using only the saved Codex2API URL and Admin Key. Existing integration orchestration and generic platform actions invoke it, `services/chatgpt_sync.py` owns independent state, and the React pages reuse existing configuration and upload-status patterns.

**Tech Stack:** Python 3, FastAPI, SQLModel, `curl_cffi`, `unittest`, React 19, TypeScript, Ant Design, Vite, Docker Compose.

---

## File Map

- Create `platforms/chatgpt/codex2api_upload.py`: official management API client, RT/AT selection, response classification, redaction.
- Create `tests/test_codex2api_upload.py`: uploader unit tests.
- Modify `services/chatgpt_sync.py` and `tests/test_chatgpt_sync.py`: independent persisted upload state.
- Modify `services/external_sync.py` and `tests/test_external_sync_contribution_mode.py`: registration-completion orchestration.
- Modify `platforms/chatgpt/plugin.py`, `api/actions.py`, and their tests: manual single/batch action.
- Modify `api/config.py` and `frontend/src/pages/Settings.tsx`: dedicated configuration navigation.
- Modify `frontend/src/pages/Accounts.tsx`: status and batch control.
- Create `tests/test_codex2api_frontend_contract.py`: required frontend wiring contract.

## Task 1: Official Codex2API Uploader

**Files:**
- Create: `tests/test_codex2api_upload.py`
- Create: `platforms/chatgpt/codex2api_upload.py`

- [ ] **Step 1: Write failing uploader tests**

Create `tests/test_codex2api_upload.py` with a duck-typed account and mocked `cffi_requests.post`:

```python
import unittest
from types import SimpleNamespace
from unittest import mock

from platforms.chatgpt.codex2api_upload import upload_to_codex2api


class Response:
    status_code = 200
    text = ""

    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class Codex2APIUploadTests(unittest.TestCase):
    def account(self, refresh_token="rt-secret", access_token="at-secret"):
        return SimpleNamespace(
            email="demo@example.com",
            refresh_token=refresh_token,
            access_token=access_token,
        )

    def config(self, key, default=""):
        return {
            "codex2api_api_url": "http://codex2api.local:8080/",
            "codex2api_admin_key": "admin-secret",
        }.get(key, default)

    @mock.patch("platforms.chatgpt.codex2api_upload._get_config_value")
    @mock.patch("platforms.chatgpt.codex2api_upload.cffi_requests.post")
    def test_refresh_token_takes_precedence(self, post, get_config):
        get_config.side_effect = self.config
        post.return_value = Response({"success": 1, "failed": 0})
        ok, message = upload_to_codex2api(self.account())
        self.assertTrue(ok)
        self.assertIn("Refresh Token", message)
        self.assertEqual(post.call_args.args[0], "http://codex2api.local:8080/api/admin/accounts")
        self.assertEqual(post.call_args.kwargs["json"], {
            "name": "demo@example.com",
            "refresh_token": "rt-secret",
        })
        self.assertEqual(post.call_args.kwargs["headers"]["X-Admin-Key"], "admin-secret")

    @mock.patch("platforms.chatgpt.codex2api_upload._get_config_value")
    @mock.patch("platforms.chatgpt.codex2api_upload.cffi_requests.post")
    def test_access_token_is_fallback(self, post, get_config):
        get_config.side_effect = self.config
        post.return_value = Response({"updated": 1, "failed": 0})
        ok, message = upload_to_codex2api(self.account(refresh_token=""))
        self.assertTrue(ok)
        self.assertIn("Access Token", message)
        self.assertEqual(post.call_args.args[0], "http://codex2api.local:8080/api/admin/accounts/at")

    @mock.patch("platforms.chatgpt.codex2api_upload._get_config_value")
    @mock.patch("platforms.chatgpt.codex2api_upload.cffi_requests.post")
    def test_duplicate_is_success(self, post, get_config):
        get_config.side_effect = self.config
        post.return_value = Response({"duplicate": 1, "failed": 0})
        ok, message = upload_to_codex2api(self.account())
        self.assertTrue(ok)
        self.assertIn("已存在", message)

    @mock.patch("platforms.chatgpt.codex2api_upload._get_config_value", return_value="")
    @mock.patch("platforms.chatgpt.codex2api_upload.cffi_requests.post")
    def test_missing_config_does_not_send(self, post, _get_config):
        ok, message = upload_to_codex2api(self.account())
        self.assertFalse(ok)
        self.assertIn("API URL", message)
        post.assert_not_called()

    @mock.patch("platforms.chatgpt.codex2api_upload._get_config_value")
    @mock.patch("platforms.chatgpt.codex2api_upload.cffi_requests.post")
    def test_failed_count_is_failure(self, post, get_config):
        get_config.side_effect = self.config
        post.return_value = Response({"success": 0, "failed": 1, "message": "invalid"})
        ok, message = upload_to_codex2api(self.account())
        self.assertFalse(ok)
        self.assertEqual(message, "invalid")

    @mock.patch("platforms.chatgpt.codex2api_upload._get_config_value")
    @mock.patch("platforms.chatgpt.codex2api_upload.cffi_requests.post")
    def test_error_redacts_secrets(self, post, get_config):
        get_config.side_effect = self.config
        post.return_value = Response(
            {"error": "admin-secret rejected rt-secret and at-secret"},
            status_code=500,
        )
        ok, message = upload_to_codex2api(self.account())
        self.assertFalse(ok)
        self.assertNotIn("admin-secret", message)
        self.assertNotIn("rt-secret", message)
        self.assertNotIn("at-secret", message)
```

- [ ] **Step 2: Run the tests to verify the module is missing**

Run: `python -m unittest tests.test_codex2api_upload -v`

Expected: import failure for `platforms.chatgpt.codex2api_upload`.

- [ ] **Step 3: Implement the minimal uploader**

Create `platforms/chatgpt/codex2api_upload.py` with the complete request and response path:

```python
from __future__ import annotations

import logging
from typing import Any

from curl_cffi import requests as cffi_requests

logger = logging.getLogger(__name__)


def _get_config_value(key: str) -> str:
    try:
        from core.config_store import config_store
        return str(config_store.get(key, "") or "").strip()
    except Exception:
        return ""


def _redact(value: Any, secrets: list[str]) -> str:
    text = str(value or "").strip()
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def _response_detail(response, secrets: list[str]) -> str:
    try:
        payload = response.json()
    except Exception:
        return _redact(str(getattr(response, "text", ""))[:200], secrets)
    if isinstance(payload, dict):
        return _redact(
            payload.get("message") or payload.get("msg") or payload.get("error") or "",
            secrets,
        )
    return _redact(payload, secrets)

def upload_to_codex2api(account) -> tuple[bool, str]:
    api_url = _get_config_value("codex2api_api_url").rstrip("/")
    admin_key = _get_config_value("codex2api_admin_key")
    refresh_token = str(getattr(account, "refresh_token", "") or "").strip()
    access_token = str(getattr(account, "access_token", "") or "").strip()
    email = str(getattr(account, "email", "") or "codex-account").strip()

    if not api_url:
        return False, "Codex2API API URL 未配置"
    if not admin_key:
        return False, "Codex2API Admin Key 未配置"
    if refresh_token:
        url = f"{api_url}/api/admin/accounts"
        payload = {"name": email, "refresh_token": refresh_token}
        credential_label = "Refresh Token"
    elif access_token:
        url = f"{api_url}/api/admin/accounts/at"
        payload = {"name": email, "access_token": access_token}
        credential_label = "Access Token"
    else:
        return False, "账号缺少 Refresh Token 和 Access Token"

    secrets = [admin_key, refresh_token, access_token]
    try:
        response = cffi_requests.post(
            url,
            headers={"X-Admin-Key": admin_key, "Content-Type": "application/json"},
            json=payload,
            proxies=None,
            verify=False,
            timeout=30,
            impersonate="chrome110",
        )
    except Exception as exc:
        detail = _redact(exc, secrets)
        logger.error("Codex2API upload failed: %s", detail)
        return False, f"Codex2API 上传异常: {detail}"

    detail = _response_detail(response, secrets)
    if response.status_code not in (200, 201):
        if response.status_code in (401, 403):
            return False, "Codex2API Admin Key 无效或无权限"
        if response.status_code == 404:
            return False, f"Codex2API 管理接口不存在: {api_url}"
        suffix = f": {detail}" if detail else ""
        return False, f"Codex2API 上传失败: HTTP {response.status_code}{suffix}"

    try:
        data = response.json()
    except Exception:
        return False, "Codex2API 返回了无法解析的响应"
    if not isinstance(data, dict):
        return False, "Codex2API 返回了无法识别的响应"
    success = int(data.get("success") or 0)
    updated = int(data.get("updated") or 0)
    duplicate = int(data.get("duplicate") or 0)
    failed = int(data.get("failed") or 0)
    if success > 0:
        return True, f"上传成功（{credential_label}）"
    if updated > 0:
        return True, f"远端账号已更新（{credential_label}）"
    if duplicate > 0:
        return True, f"远端账号已存在（{credential_label}）"
    detail = _redact(data.get("message") or data.get("msg") or data.get("error"), secrets)
    if failed > 0 or detail:
        return False, detail or "Codex2API 拒绝了账号"
    return False, "Codex2API 未确认账号已导入"
```

- [ ] **Step 4: Run uploader tests**

Run: `python -m unittest tests.test_codex2api_upload -v`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add platforms/chatgpt/codex2api_upload.py tests/test_codex2api_upload.py
git commit -m "feat: add codex2api credential uploader"
```

## Task 2: Independent Sync State

**Files:**
- Modify: `services/chatgpt_sync.py`
- Modify: `tests/test_chatgpt_sync.py`

- [ ] **Step 1: Write the failing state transition test**

```python
def test_codex2api_state_preserves_success_after_failed_retry(self):
    extra = {}
    record_codex2api_sync_result(extra, True, "uploaded")
    state = record_codex2api_sync_result(extra, False, "timeout")
    self.assertTrue(state["uploaded"])
    self.assertFalse(state["last_attempt_ok"])
    self.assertEqual(get_codex2api_sync_state(extra), state)
```

- [ ] **Step 2: Verify the new helpers are missing**

Run: `python -m unittest tests.test_chatgpt_sync -v`

Expected: import failure for Codex2API state helpers.

- [ ] **Step 3: Add state helpers beside CPA/Sub2API helpers**

```python
CODEX2API_SYNC_NAME = "codex2api"

def get_codex2api_sync_state(extra_or_account: Any) -> dict[str, Any]:
    return _get_sync_state(extra_or_account, CODEX2API_SYNC_NAME)

def record_codex2api_sync_result(extra: dict[str, Any], ok: bool, msg: str) -> dict[str, Any]:
    return _record_sync_result(extra, CODEX2API_SYNC_NAME, ok, msg)

def update_account_model_codex2api_sync(
    account: AccountModel,
    ok: bool,
    msg: str,
    session: Session | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    extra = account.get_extra()
    state = record_codex2api_sync_result(extra, ok, msg)
    account.set_extra(extra)
    account.updated_at = _utcnow()
    if session is not None:
        session.add(account)
        if commit:
            session.commit()
            session.refresh(account)
    return state

def persist_codex2api_sync_result(account: Any, ok: bool, msg: str) -> None:
    if isinstance(account, AccountModel) and account.id is not None:
        with Session(engine) as session:
            row = session.get(AccountModel, account.id)
            if row:
                update_account_model_codex2api_sync(
                    row,
                    ok,
                    msg,
                    session=session,
                    commit=True,
                )
                return
    extra = getattr(account, "extra", None)
    if isinstance(extra, dict):
        record_codex2api_sync_result(extra, ok, msg)
```

- [ ] **Step 4: Run and commit**

Run: `python -m unittest tests.test_chatgpt_sync -v`

```bash
git add services/chatgpt_sync.py tests/test_chatgpt_sync.py
git commit -m "feat: track codex2api upload status"
```

## Task 3: Automatic Upload Orchestration

**Files:**
- Modify: `services/external_sync.py`
- Modify: `tests/test_external_sync_contribution_mode.py`

- [ ] **Step 1: Write failing tests for enabled, disabled, and contribution mode**

```python
def test_codex2api_enabled_uploads_and_persists(self):
    account = DummyAccount(extra={"refresh_token": "rt-local"})
    cfg = {
        "contribution_enabled": "0",
        "codex2api_enabled": "1",
        "codex2api_api_url": "http://codex2api.local:8080",
        "codex2api_admin_key": "admin-key",
    }
    with mock.patch("core.config_store.config_store.get", side_effect=_config_getter(cfg)):
        with mock.patch(
            "platforms.chatgpt.codex2api_upload.upload_to_codex2api",
            return_value=(True, "ok"),
        ) as upload:
            with mock.patch("services.external_sync.persist_codex2api_sync_result") as persist:
                result = sync_account(account)
    self.assertEqual(result, [{"name": "Codex2API", "ok": True, "msg": "ok"}])
    upload.assert_called_once()
    persist.assert_called_once_with(account, True, "ok")

def test_codex2api_disabled_keeps_configuration_without_upload(self):
    account = DummyAccount()
    cfg = {
        "contribution_enabled": "0",
        "codex2api_enabled": "0",
        "codex2api_api_url": "http://codex2api.local:8080",
        "codex2api_admin_key": "admin-key",
    }
    with mock.patch("core.config_store.config_store.get", side_effect=_config_getter(cfg)):
        with mock.patch("platforms.chatgpt.codex2api_upload.upload_to_codex2api") as upload:
            result = sync_account(account)
    self.assertEqual(result, [])
    upload.assert_not_called()

def test_codex2api_runs_before_contribution_early_return(self):
    account = DummyAccount(extra={"refresh_token": "rt-local"})
    cfg = {
        "contribution_enabled": "1",
        "contribution_server_url": "http://contribution.local:7317",
        "contribution_key": "public-key",
        "codex2api_enabled": "1",
        "codex2api_api_url": "http://codex2api.local:8080",
        "codex2api_admin_key": "admin-key",
    }
    with mock.patch("core.config_store.config_store.get", side_effect=_config_getter(cfg)):
        with mock.patch(
            "platforms.chatgpt.codex2api_upload.upload_to_codex2api",
            return_value=(True, "codex-ok"),
        ):
            with mock.patch("services.external_sync.persist_codex2api_sync_result"):
                with mock.patch(
                    "services.external_sync.upload_chatgpt_account_to_cpa",
                    return_value=(True, "contribution-ok"),
                ):
                    with mock.patch("services.external_sync.persist_cpa_sync_result"):
                        result = sync_account(account)
    self.assertEqual([item["name"] for item in result], ["Codex2API", "Contribution"])
```

- [ ] **Step 2: Run tests to verify Codex2API orchestration is absent**

Run: `python -m unittest tests.test_external_sync_contribution_mode -v`

- [ ] **Step 3: Add the independent block before contribution handling**

```python
codex2api_enabled = _is_config_enabled(
    config_store.get("codex2api_enabled", "0"),
    default=False,
)
if codex2api_enabled:
    from platforms.chatgpt.codex2api_upload import upload_to_codex2api
    ok, msg = upload_to_codex2api(upload_account)
    persist_codex2api_sync_result(account, ok, msg)
    results.append({"name": "Codex2API", "ok": ok, "msg": msg})
```

Import `persist_codex2api_sync_result` from `services.chatgpt_sync`. Do not change existing CPA/Sub2API/contribution branches.

- [ ] **Step 4: Run and commit**

Run: `python -m unittest tests.test_external_sync_contribution_mode -v`

```bash
git add services/external_sync.py tests/test_external_sync_contribution_mode.py
git commit -m "feat: auto-upload chatgpt accounts to codex2api"
```

## Task 4: Parameterless Manual Action and Persistence

**Files:**
- Modify: `platforms/chatgpt/plugin.py`
- Modify: `api/actions.py`
- Modify: `tests/test_chatgpt_plugin.py`
- Create: `tests/test_actions_codex2api.py`

- [ ] **Step 1: Write failing action tests**

Assert the action metadata is exactly:

```python
{"id": "upload_codex2api", "label": "上传 Codex2API", "params": []}
```

Call `execute_action` with malicious `api_url` and `api_key` params and assert `upload_to_codex2api(a)` receives only the built account. In `tests/test_actions_codex2api.py`, call `_apply_action_result` and assert:

```python
update_account_model_codex2api_sync(
    account,
    True,
    "uploaded",
    session=session,
    commit=False,
)
```

- [ ] **Step 2: Verify action tests fail**

Run: `python -m unittest tests.test_chatgpt_plugin tests.test_actions_codex2api -v`

- [ ] **Step 3: Implement metadata, execution, and result persistence**

```python
if action_id == "upload_codex2api":
    from platforms.chatgpt.codex2api_upload import upload_to_codex2api
    ok, msg = upload_to_codex2api(a)
    return {"ok": ok, "data": msg}
```

In `_apply_action_result`, mirror the `upload_sub2api` branch using `update_account_model_codex2api_sync`. The generic batch endpoint then works without a new API route.

- [ ] **Step 4: Run and commit**

Run: `python -m unittest tests.test_chatgpt_plugin tests.test_actions_codex2api -v`

```bash
git add platforms/chatgpt/plugin.py api/actions.py tests/test_chatgpt_plugin.py tests/test_actions_codex2api.py
git commit -m "feat: add codex2api account actions"
```

## Task 5: Dedicated Settings Navigation

**Files:**
- Modify: `api/config.py`
- Modify: `frontend/src/pages/Settings.tsx`
- Create: `tests/test_codex2api_frontend_contract.py`

- [ ] **Step 1: Write a failing source contract**

The test imports `CONFIG_KEYS` and reads `Settings.tsx`, asserting the presence of:

```python
expected_keys = {
    "codex2api_enabled",
    "codex2api_api_url",
    "codex2api_admin_key",
}
self.assertTrue(expected_keys.issubset(CONFIG_KEYS))
self.assertIn("key: 'codex2api'", settings_source)
self.assertIn("label: 'Codex2API'", settings_source)
self.assertIn("values.codex2api_enabled = parseBooleanConfigValue", settings_source)
```

- [ ] **Step 2: Verify it fails**

Run: `python -m unittest tests.test_codex2api_frontend_contract -v`

- [ ] **Step 3: Add config keys and the Settings tab**

Add the three keys to `CONFIG_KEYS`. Insert this top-level item after ChatGPT:

```tsx
{
  key: 'codex2api',
  label: 'Codex2API',
  icon: <ApiOutlined />,
  sections: [{
    title: '管理面板',
    desc: '注册完成后自动上传到配置的 Codex2API 管理后台',
    fields: [
      { key: 'codex2api_enabled', label: '启用自动上传', type: 'boolean' },
      { key: 'codex2api_api_url', label: 'API URL', placeholder: 'http://127.0.0.1:8080' },
      { key: 'codex2api_admin_key', label: 'Admin Key', secret: true },
    ],
  }],
},
```

Normalize `data.codex2api_enabled` on load, `values.codex2api_enabled` before save, and include it in `form.setFieldsValue` after save.

- [ ] **Step 4: Run and commit**

Run:

```bash
python -m unittest tests.test_codex2api_frontend_contract -v
cd frontend && npm run build
```

```bash
git add api/config.py frontend/src/pages/Settings.tsx tests/test_codex2api_frontend_contract.py
git commit -m "feat: add codex2api settings page"
```

## Task 6: Account Status and Batch Upload UI

**Files:**
- Modify: `frontend/src/pages/Accounts.tsx`
- Modify: `tests/test_codex2api_frontend_contract.py`

- [ ] **Step 1: Add a failing account-page contract**

Assert `Accounts.tsx` contains `syncStatuses.codex2api`, `codex2apiSync`, `uploadSyncTitle('Codex2API'`, `/upload_codex2api/batch`, and `导入 Codex2API`.

- [ ] **Step 2: Verify it fails**

Run: `python -m unittest tests.test_codex2api_frontend_contract -v`

- [ ] **Step 3: Normalize and render status**

```tsx
const codex2apiSync = syncStatuses.codex2api && typeof syncStatuses.codex2api === 'object'
  ? syncStatuses.codex2api
  : {}
```

Return it from `normalizeAccount`, derive `uploadSyncMeta`, and render:

```tsx
<Tag color={codex2apiMeta.color} title={uploadSyncTitle('Codex2API', codex2apiSync)}>
  Codex2API {codex2apiMeta.label}
</Tag>
```

- [ ] **Step 4: Add selected-or-filtered batch upload**

Add `codex2apiUploadLoading`, then mirror the existing generic batch action flow with:

```tsx
await apiFetch(`/actions/${currentPlatform}/upload_codex2api/batch`, {
  method: 'POST',
  body: JSON.stringify(body),
})
```

The body contains `account_ids` when rows are selected; otherwise it contains `all_filtered: true` plus current email/status filters. Add a confirmed toolbar button labeled `导入 Codex2API` and reuse `showBatchActionResult`.

- [ ] **Step 5: Run and commit**

Run:

```bash
python -m unittest tests.test_codex2api_frontend_contract -v
cd frontend && npm run build
```

```bash
git add frontend/src/pages/Accounts.tsx tests/test_codex2api_frontend_contract.py
git commit -m "feat: show and batch upload codex2api accounts"
```

## Task 7: Full Verification and Local Runtime Refresh

- [ ] **Step 1: Run focused backend tests**

```bash
python -m unittest \
  tests.test_codex2api_upload \
  tests.test_chatgpt_sync \
  tests.test_external_sync_contribution_mode \
  tests.test_chatgpt_plugin \
  tests.test_actions_codex2api \
  tests.test_codex2api_frontend_contract -v
```

Expected: all pass.

- [ ] **Step 2: Run full backend and frontend gates**

```bash
python -m unittest discover -s tests -v
cd frontend && npm run build && npm run lint
```

Expected: all pass; any unrelated pre-existing failure is reported by exact test or lint rule.

- [ ] **Step 3: Rebuild the local app**

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build app
```

Expected: `any-auto-register` is recreated and `http://127.0.0.1:18080` is healthy.

- [ ] **Step 4: Verify live UI without saving or uploading credentials**

Check `/settings` for the independent Codex2API menu and its three fields. Check `/accounts/chatgpt` for the state tag, batch button, and per-account action. Do not click save or upload during verification.

- [ ] **Step 5: Review ownership and diff**

```bash
git status --short
git log --oneline -8
git diff --check
```

Expected: unrelated pre-existing changes remain intact and unstaged.
