# ChatGPT Account Removal and Codex2API Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Represent confirmed deleted/deactivated ChatGPT accounts separately from ordinary re-login failures, keep them inside the inclusive re-login-failure alert metric, and optionally delete the matching Codex2API credential before local deletion.

**Architecture:** Add an independent normalized switch, shared mutation locks, a redacting Codex2API cleanup helper, and one remote-first/local-second account-removal service reused by automatic, single, and batch deletion. Automatic tasks freeze the switch and publish `relogin_failed_count` (R) plus `deleted_account_count` (D); the task-summary plan derives displayed `失败 = max(R-D, 0)`, while email alerting uses the unmodified inclusive R and identifies D as its deleted/deactivated subset.

**Tech Stack:** Python 3, FastAPI, SQLModel/SQLAlchemy, curl-cffi, pytest/unittest.mock, React 19, TypeScript, Ant Design, Vitest/Testing Library, SQLite, systemd.

---

## File map and boundary

- Create `services/chatgpt_account_coordination.py`: shared per-account and reentrant Codex2API remote-mutation locks.
- Create `services/chatgpt_account_removal.py`: remote-first/local-second deletion with optimistic local guard and structured results.
- Modify `platforms/chatgpt/codex2api_upload.py`: exact email/identity credential resolution, DELETE request, idempotency, and redacted status.
- Modify `api/config.py` and `frontend/src/pages/Settings.tsx`: independent `codex2api_delete_on_account_remove_enabled` switch, default off.
- Modify `api/accounts.py` and `frontend/src/pages/Accounts.tsx`: structured single deletion and independently committed partial batch results.
- Modify `services/chatgpt_relogin.py` and `api/tasks.py`: automatic cleanup delegation, frozen setting, removed outcome, R/D counters, history, and email arguments.
- Modify `services/external_sync.py`: serialize every Codex2API upload/replace
  with deletion and remove existing token-prefix debug output.
- Modify `core/task_runtime.py`: `AttemptOutcome.REMOVED`.
- Modify `services/chatgpt_auto_relogin_alerts.py`: show D as a subset while thresholding on inclusive R with `>=`.
- Modify `frontend/src/pages/TaskHistory.tsx`: render `removed` as `已删除`.
- Tests: `tests/test_chatgpt_account_coordination.py`, `tests/test_chatgpt_account_removal.py`, `tests/test_accounts_deletion.py`, existing backend tests, and corresponding frontend page tests.
- `frontend/src/pages/RunningTasks.tsx` and its tests belong to `docs/superpowers/plans/2026-08-05-task-runs-performance-retention.md`. That plan derives automatic summary `error_count=max(R-D,0)` and renders `已删除账号 D`; this plan produces R and D and does not edit those two files.

### Task 1: Add the independent deletion-link switch

**Files:**
- Modify: `tests/test_chatgpt_auto_relogin.py`
- Modify: `tests/test_codex2api_frontend_contract.py`
- Modify: `api/config.py`
- Modify: `frontend/src/pages/Settings.test.tsx`
- Modify: `frontend/src/pages/Settings.tsx`

- [ ] **Step 1: Write failing backend config tests**

Add the new key to `PUBLIC_KEYS`, assert its missing value resolves to `"0"` without a database write, and add:

~~~python
def test_codex2api_delete_link_normalizes_boolean_values(monkeypatch):
    from api import config as config_api

    store = FakeConfigStore()
    monkeypatch.setattr(config_api, "config_store", store)

    config_api.update_config(config_api.ConfigUpdate(data={
        "codex2api_delete_on_account_remove_enabled": "YES",
    }))
    config_api.update_config(config_api.ConfigUpdate(data={
        "codex2api_delete_on_account_remove_enabled": "unexpected",
    }))

    assert store.writes == [
        {"codex2api_delete_on_account_remove_enabled": "1"},
        {"codex2api_delete_on_account_remove_enabled": "0"},
    ]
~~~

Extend the static frontend contract:

~~~python
self.assertIn("codex2api_delete_on_account_remove_enabled", CONFIG_KEYS)
self.assertIn("title: '删除联动'", settings_source)
self.assertIn(
    "删除本地 ChatGPT 账号时，同步删除 Codex2API 认证",
    settings_source,
)
~~~

- [ ] **Step 2: Run tests and verify RED**

Run:

~~~bash
PYTHONPATH=. pytest tests/test_chatgpt_auto_relogin.py tests/test_codex2api_frontend_contract.py -q
~~~

Expected: FAIL because the key, fallback, normalization, and UI section are absent.

- [ ] **Step 3: Implement the backend contract**

Add `codex2api_delete_on_account_remove_enabled` next to the existing Codex2API keys in `CONFIG_KEYS`. In `get_config`:

~~~python
if not str(
    all_cfg.get("codex2api_delete_on_account_remove_enabled", "") or ""
).strip():
    all_cfg["codex2api_delete_on_account_remove_enabled"] = "0"
~~~

In `update_config` normalize it with the accepted truthy set:

~~~python
for bool_key in (
    "codex2api_delete_on_account_remove_enabled",
    "smtp_use_ssl",
    "smtp_force_auth_login",
):
    if bool_key in safe:
        enabled = str(safe.get(bool_key, "")).strip().lower()
        safe[bool_key] = (
            "1" if enabled in {"1", "true", "yes", "on"} else "0"
        )
~~~

Do not consult `codex2api_enabled`.

- [ ] **Step 4: Write the failing Settings test**

In `Settings.test.tsx` load `"1"`, open the Codex2API tab, assert the named switch is checked, click it, save, and assert the PUT payload contains boolean `false`:

~~~tsx
const removalSwitch = await screen.findByRole('switch', {
  name: '删除本地 ChatGPT 账号时，同步删除 Codex2API 认证',
})
expect(removalSwitch.getAttribute('aria-checked')).toBe('true')
expect(screen.getByText(/远端删除失败时保留本地账号/)).toBeTruthy()
await user.click(removalSwitch)
await user.click(screen.getByRole('button', { name: /保存配置/ }))
// Parse the captured PUT body:
expect(payload.data.codex2api_delete_on_account_remove_enabled).toBe(false)
~~~

Also assert a missing value rehydrates as an unchecked switch.

- [ ] **Step 5: Run the frontend test and verify RED**

Run:

~~~bash
cd frontend && npm test -- --run src/pages/Settings.test.tsx
~~~

Expected: FAIL because the switch is not rendered.

- [ ] **Step 6: Add the separate UI section and round trip**

Append this section to the Codex2API tab:

~~~tsx
{
  title: '删除联动',
  desc: '控制本地账号删除时是否同步清理已经上传的 Codex2API 认证',
  fields: [{
    key: 'codex2api_delete_on_account_remove_enabled',
    label: '删除本地 ChatGPT 账号时，同步删除 Codex2API 认证',
    type: 'boolean',
  }],
},
~~~

Add help text:

~~~tsx
: field.key === 'codex2api_delete_on_account_remove_enabled'
  ? '自动清理、单个删除和批量删除均生效；远端删除失败时保留本地账号。'
~~~

Call `parseBooleanConfigValue` in load and save, and include the boolean in the post-save `form.setFieldsValue` block.

- [ ] **Step 7: Verify and commit**

Run:

~~~bash
PYTHONPATH=. pytest tests/test_chatgpt_auto_relogin.py tests/test_codex2api_frontend_contract.py -q
cd frontend && npm test -- --run src/pages/Settings.test.tsx
git add api/config.py tests/test_chatgpt_auto_relogin.py tests/test_codex2api_frontend_contract.py frontend/src/pages/Settings.tsx frontend/src/pages/Settings.test.tsx
git commit -m "feat: add Codex2API deletion-link setting"
~~~

Expected: all selected tests pass, then the commit succeeds.

### Task 2: Share account and Codex2API mutation locks

**Files:**
- Create: `services/chatgpt_account_coordination.py`
- Create: `tests/test_chatgpt_account_coordination.py`
- Modify: `services/chatgpt_relogin.py`
- Modify: `services/external_sync.py`
- Modify: `tests/test_external_sync_contribution_mode.py`

- [ ] **Step 1: Write failing lock tests**

~~~python
def test_same_account_lock_is_nonblocking_and_other_accounts_are_independent():
    with chatgpt_account_operation_lock(17, blocking=False) as first:
        assert first is True
        with chatgpt_account_operation_lock(17, blocking=False) as duplicate:
            assert duplicate is False
        with chatgpt_account_operation_lock(18, blocking=False) as other:
            assert other is True


def test_codex2api_mutation_lock_serializes_workers():
    entered = threading.Event()
    finished = threading.Event()

    def worker():
        with codex2api_account_mutation_lock():
            entered.set()
        finished.set()

    with codex2api_account_mutation_lock():
        thread = threading.Thread(target=worker)
        thread.start()
        assert entered.wait(0.05) is False
    assert finished.wait(1.0) is True
    thread.join(timeout=1.0)


def test_codex2api_mutation_lock_is_reentrant_in_one_thread():
    with codex2api_account_mutation_lock():
        with codex2api_account_mutation_lock():
            pass
~~~

- [ ] **Step 2: Run and verify RED**

Run `PYTHONPATH=. pytest tests/test_chatgpt_account_coordination.py -q`.

Expected: collection FAIL because the module does not exist.

- [ ] **Step 3: Create the complete lock module**

~~~python
"""Locks shared by ChatGPT account mutation workflows."""

from contextlib import contextmanager
import threading

_ACCOUNT_LOCKS_GUARD = threading.Lock()
_ACCOUNT_LOCKS: dict[int | str, threading.Lock] = {}
_CODEX2API_MUTATION_LOCK = threading.RLock()


@contextmanager
def chatgpt_account_operation_lock(account_id, *, blocking=False):
    try:
        key = int(account_id)
    except (TypeError, ValueError):
        key = str(account_id or "").strip()
    with _ACCOUNT_LOCKS_GUARD:
        lock = _ACCOUNT_LOCKS.setdefault(key, threading.Lock())
    acquired = lock.acquire(blocking=blocking)
    try:
        yield acquired
    finally:
        if acquired:
            lock.release()


@contextmanager
def codex2api_account_mutation_lock():
    with _CODEX2API_MUTATION_LOCK:
        yield
~~~

- [ ] **Step 4: Refactor re-login to use the shared account lock**

Remove the three private lock globals from `chatgpt_relogin.py`. Replace each manual acquire/finally-release block with:

~~~python
with chatgpt_account_operation_lock(account_id, blocking=False) as acquired:
    if not acquired:
        return {
            "ok": False,
            "relogin_ok": False,
            "stage": "relogin",
            "account_id": account_id,
            "email": "",
            "message": "重登失败: 该账号正在重登并同步，请等待当前任务完成",
        }
    return _relogin_chatgpt_account_locked(
        account_id,
        log_fn=log_fn,
        task_control=task_control,
        attempt_id=attempt_id,
    )
~~~

Use this complete corresponding branch in the refresh path:

~~~python
with chatgpt_account_operation_lock(account_id, blocking=False) as acquired:
    if not acquired:
        return {
            "ok": False,
            "relogin_ok": False,
            "refresh_ok": False,
            "refresh_state": "transient_error",
            "mode": "refresh_token",
            "stage": "refresh_deferred",
            "account_id": account_id,
            "email": "",
            "message": "认证维护失败: 该账号正在重登或刷新，请等待当前任务完成",
        }
    return _refresh_or_relogin_chatgpt_account_locked(
        account_id,
        log_fn=log_fn,
        task_control=task_control,
        attempt_id=attempt_id,
    )
~~~

Remove both private remote-sync wrappers. Calls remain:

~~~python
sync_result = sync_codex2api_account(
    account, force=True, replace_existing=True,
)
~~~

- [ ] **Step 5: Serialize every Codex2API upload and remove token debug output**

In `services/external_sync.py`, wrap only the actual upload call inside
`sync_codex2api_account` so registration uploads, refresh replacements, and
manual uploads share the same reentrant lock used by credential deletion:

~~~python
from services.chatgpt_account_coordination import (
    codex2api_account_mutation_lock,
)

with codex2api_account_mutation_lock():
    ok, msg = upload_to_codex2api(
        _build_chatgpt_upload_account(account),
        replace_existing=replace_existing,
    )
~~~

Delete the four custom-contribution debug `print` calls that expose extra key
names or the first 20 Refresh Token characters. Extend
`tests/test_external_sync_contribution_mode.py` with a token-shaped fixture,
capture stdout/stderr and logs, and assert the full token and its prefix are
absent while the existing request payload remains correct.

- [ ] **Step 6: Verify and commit**

Run:

~~~bash
PYTHONPATH=. pytest tests/test_chatgpt_account_coordination.py tests/test_chatgpt_relogin.py tests/test_external_sync_contribution_mode.py -q
git add services/chatgpt_account_coordination.py services/chatgpt_relogin.py services/external_sync.py tests/test_chatgpt_account_coordination.py tests/test_external_sync_contribution_mode.py
git commit -m "refactor: share ChatGPT account mutation locks"
~~~

Expected: lock tests and all re-login regressions pass.

### Task 3: Add the redacting Codex2API credential cleanup helper

**Files:**
- Modify: `tests/test_codex2api_upload.py`
- Modify: `platforms/chatgpt/codex2api_upload.py`

- [ ] **Step 1: Write failing resolution tests**

Add tests for these exact contracts:

~~~python
result = delete_codex2api_credential(
    email="demo@example.com",
    identity={"workspace_id": "workspace-1"},
)
assert result == {"status": "deleted", "remote_id": 7, "message": ""}
assert delete.call_args.args[0].endswith("/api/admin/accounts/7")
~~~

Fixtures must cover:

- two exact-email rows where only one stable identity matches;
- one identity-less legacy exact-email row;
- two remaining candidates => `ambiguous` and no DELETE;
- no candidate => `already_absent`;
- DELETE 404 => `already_absent` with known `remote_id`;
- missing URL/key => `config_missing` without network;
- list 401/403 => `unauthorized`;
- transport/timeout => `unavailable`;
- malformed JSON/list shape and other HTTP statuses => `failed`.

For every error case assert `len(message) <= 200` and that the configured Admin Key, access token, refresh token, and identity token are absent.

- [ ] **Step 2: Run and verify RED**

Run `PYTHONPATH=. pytest tests/test_codex2api_upload.py -q`.

Expected: FAIL because `delete_codex2api_credential` is absent.

- [ ] **Step 3: Extend stable identity matching**

Make `_identity_aliases` accept a mapping and add direct aliases plus JWT claims:

~~~python
claim_keys = (
    "chatgpt_account_id", "chatgpt_user_id",
    "workspace_id", "account_id", "user_id",
)
for key in claim_keys:
    add(payload.get(key))
for token_key in ("id_token", "access_token"):
    claims = _jwt_claims_no_verify(payload.get(token_key))
    for key in claim_keys:
        add(claims.get(key))
    auth = claims.get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        for key in claim_keys:
            add(auth.get(key))
~~~

Candidate selection must be:

~~~python
def _credential_cleanup_candidates(rows, *, email, local_aliases):
    candidates = []
    for row in _matching_remote_rows(rows, email=email):
        if _remote_row_id(row) <= 0:
            continue
        remote_aliases = _identity_aliases(row)
        if local_aliases and remote_aliases and local_aliases.isdisjoint(remote_aliases):
            continue
        candidates.append(row)
    return candidates
~~~

- [ ] **Step 4: Implement the public structured helper**

Use the existing GET `/api/admin/accounts?channel=codex` protocol and shared mutation lock. The public signature is `delete_codex2api_credential(*, email: str, identity: Mapping[str, Any] | None = None) -> dict[str, Any]`; every return contains exactly `status`, `remote_id`, and a bounded redacted `message`.

Implement these terminal branches:

~~~python
if not candidates:
    return _cleanup_result("already_absent")
if len(candidates) != 1:
    return _cleanup_result(
        "ambiguous",
        message="Codex2API 对应认证不唯一，已停止删除",
    )
remote_id = _remote_row_id(candidates[0])
response = cffi_requests.delete(
    api_url + "/api/admin/accounts/" + str(remote_id),
    **_admin_read_kwargs(admin_key, accept="application/json", timeout=15),
)
if response.status_code in (200, 201, 204):
    return _cleanup_result("deleted", remote_id=remote_id)
if response.status_code == 404:
    return _cleanup_result("already_absent", remote_id=remote_id)
~~~

Map authorization, transport, and other failures to the statuses tested above. Never return/log a candidate row or upstream response body. Keep upload replacement behavior unchanged by adapting its private delete wrapper separately.

- [ ] **Step 5: Verify and commit**

Run:

~~~bash
PYTHONPATH=. pytest tests/test_codex2api_upload.py tests/test_actions_codex2api.py tests/test_external_sync_contribution_mode.py -q
git add platforms/chatgpt/codex2api_upload.py tests/test_codex2api_upload.py
git commit -m "feat: delete matched Codex2API credentials"
~~~

Expected: cleanup and existing upload tests pass.

### Task 4: Build the ordered account-removal service

**Files:**
- Create: `services/chatgpt_account_removal.py`
- Create: `tests/test_chatgpt_account_removal.py`

- [ ] **Step 1: Write failing service tests**

Use an in-memory SQLModel engine and assert:

~~~python
result = remove_account(
    account_id,
    database_engine=engine,
    codex2api_delete_on_account_remove_enabled=False,
)
assert result["ok"] is True
assert result["local_deleted"] is True
assert result["codex2api"]["status"] == "skipped_disabled"
remote.assert_not_called()
~~~

Also test:

- non-ChatGPT rows are always `not_applicable` and local-only;
- enabled legacy/imported ChatGPT rows with only an Access Token still attempt
  exact remote cleanup;
- enabled ChatGPT deletion calls remote while the local row still exists, then deletes locally;
- `deleted` and `already_absent` remote states allow local deletion;
- `ambiguous/config_missing/unauthorized/unavailable/failed` preserve the local row;
- a local compare-delete conflict after remote success reports `local_delete_conflict` and remains retryable;
- the retry sees remote `already_absent` and completes local deletion;
- task stop checkpoints run before remote mutation and again before local deletion;
- a busy account lock returns `account_busy` without remote mutation.

- [ ] **Step 2: Run and verify RED**

Run `PYTHONPATH=. pytest tests/test_chatgpt_account_removal.py -q`.

Expected: collection FAIL because the service is absent.

- [ ] **Step 3: Define snapshot, identity, and result contracts**

~~~python
@dataclass(frozen=True)
class AccountSnapshot:
    account_id: int
    platform: str
    email: str
    user_id: str
    created_at: datetime
    updated_at: datetime
    extra: dict[str, Any]


def _identity_payload(snapshot):
    extra = snapshot.extra
    return {
        "workspace_id": extra.get("workspace_id") or extra.get("workspaceId"),
        "chatgpt_account_id": (
            extra.get("chatgpt_account_id") or extra.get("chatgptAccountId")
        ),
        "account_id": extra.get("account_id") or extra.get("accountId"),
        "chatgpt_user_id": (
            extra.get("chatgpt_user_id") or extra.get("chatgptUserId")
        ),
        "user_id": (
            extra.get("user_id") or extra.get("userId") or snapshot.user_id
        ),
        "id_token": extra.get("id_token") or extra.get("idToken"),
        "access_token": extra.get("access_token") or extra.get("accessToken"),
    }


~~~

Every result contains `ok, account_id, status, local_deleted, codex2api, error_code, message`. The nested Codex2API object contains only `enabled, status` and `remote_id` when known.

- [ ] **Step 4: Implement optimistic local deletion**

~~~python
result = session.exec(
    delete(AccountModel)
    .where(AccountModel.id == snapshot.account_id)
    .where(AccountModel.platform == snapshot.platform)
    .where(func.lower(AccountModel.email) == snapshot.email.lower())
    .where(AccountModel.created_at == snapshot.created_at)
    .where(AccountModel.updated_at == snapshot.updated_at)
)
if int(getattr(result, "rowcount", 0) or 0) == 1:
    session.commit()
    return "deleted"
session.rollback()
current = session.get(AccountModel, snapshot.account_id)
return "already_absent" if current is None else "conflict"
~~~

- [ ] **Step 5: Implement remote-first orchestration**

The public signature is `remove_account(account_id: int, *, database_engine=None, codex2api_delete_on_account_remove_enabled: bool | None = None, expected_created_at: datetime | None = None, expected_updated_at: datetime | None = None, already_locked: bool = False, task_control=None, attempt_id: int | None = None) -> dict[str, Any]`.

Inside the acquired account lock:

~~~python
if snapshot.platform != "chatgpt":
    remote = {"enabled": False, "status": "not_applicable"}
elif not enabled:
    remote = {"enabled": False, "status": "skipped_disabled"}
else:
    checkpoint(task_control, attempt_id)
    remote_result = delete_codex2api_credential(
        email=snapshot.email,
        identity=_identity_payload(snapshot),
    )
    if remote_result["status"] not in {"deleted", "already_absent"}:
        return {
            "ok": False,
            "account_id": snapshot.account_id,
            "status": "remote_failed",
            "local_deleted": False,
            "codex2api": {
                "enabled": True,
                "status": remote_result["status"],
                **(
                    {"remote_id": remote_result["remote_id"]}
                    if remote_result.get("remote_id") is not None else {}
                ),
            },
            "error_code": (
                "remote_ambiguous"
                if remote_result["status"] == "ambiguous"
                else "codex2api_delete_failed"
            ),
            "message": str(
                remote_result.get("message")
                or "Codex2API 认证删除未完成"
            )[:200],
        }

checkpoint(task_control, attempt_id)
local_status = _delete_local_snapshot(database_engine, snapshot)
~~~

Map `ambiguous` to `remote_ambiguous`, other remote failures to `codex2api_delete_failed`, and local compare failure to `local_delete_conflict`. Missing initial row is `not_found`. Bound messages to 200 characters and never include credentials.

- [ ] **Step 6: Verify and commit**

Run:

~~~bash
PYTHONPATH=. pytest tests/test_chatgpt_account_removal.py tests/test_chatgpt_account_coordination.py -q
git add services/chatgpt_account_removal.py tests/test_chatgpt_account_removal.py
git commit -m "feat: coordinate remote-first account removal"
~~~

Expected: all service tests pass.

### Task 5: Route single and batch API/UI deletion through the service

**Files:**
- Modify: `api/accounts.py`
- Create: `tests/test_accounts_deletion.py`
- Modify: `frontend/src/pages/Accounts.tsx`
- Modify: `frontend/src/pages/Accounts.test.tsx`

- [ ] **Step 1: Write failing API tests**

Single success must expose:

~~~python
assert result == {
    "ok": True,
    "account_id": 17,
    "status": "deleted",
    "local_deleted": True,
    "codex2api": {"enabled": True, "status": "deleted", "remote_id": 71},
    "error_code": "",
    "message": "账号已删除",
}
~~~

Assert status mapping: missing 404; busy/local conflict 409; remote failure 502; local database failure 500.

For `ids=[1,2,1,3]` mock one success, one remote failure, and one missing row. Assert one call per unique ID and:

~~~python
assert response["total_requested"] == 4
assert response["total_unique"] == 3
assert response["deleted"] == 1
assert response["failed"] == 1
assert response["not_found"] == [3]
assert response["remote_deleted"] == 1
assert len(response["items"]) == 3
~~~

- [ ] **Step 2: Run and verify RED**

Run `PYTHONPATH=. pytest tests/test_accounts_deletion.py -q`.

Expected: FAIL because routes still report unconditional local success.

- [ ] **Step 3: Implement route contracts**

Single route:

~~~python
result = remove_account(account_id, database_engine=session.get_bind())
if result["ok"]:
    return result
status_code = (
    404 if result["status"] == "not_found"
    else 409 if result["status"] in {"busy", "local_delete_conflict"}
    else 502 if result["status"] == "remote_failed"
    else 500
)
return JSONResponse(
    status_code=status_code,
    content={**result, "detail": result["message"] or "删除失败"},
)
~~~

Batch route deduplicates with `list(dict.fromkeys(body.ids))`, calls `remove_account` once per ID, and derives `deleted, not_found, failed, remote_deleted, remote_already_absent, remote_skipped, items`. Each service call owns its commit; do not roll back prior successes.

- [ ] **Step 4: Write failing frontend partial-result tests**

Mock a result with one deleted and one `remote_ambiguous` failure. Select both rows, confirm deletion, then assert warning text and `已选 1 个`. Add all-success and all-failed cases.

~~~tsx
expect(await screen.findByText(
  /部分完成：删除 1 个，失败 1 个/,
)).toBeTruthy()
expect(screen.getByText('已选 1 个')).toBeTruthy()
~~~

- [ ] **Step 5: Run frontend test and verify RED**

Run `cd frontend && npm test -- --run src/pages/Accounts.test.tsx`.

Expected: FAIL because selection is always cleared.

- [ ] **Step 6: Implement UI response handling**

~~~tsx
const completedIds = new Set(
  result.items
    .filter((item) => item.ok || item.status === 'not_found')
    .map((item) => Number(item.account_id)),
)
setSelectedRowKeys((current) =>
  current.filter((key) => !completedIds.has(Number(key))),
)
if (result.failed === 0) {
  message.success('批量删除完成：删除 ' + result.deleted + ' 个')
} else if (result.deleted === 0) {
  message.error('批量删除失败：失败 ' + result.failed + ' 个')
} else {
  message.warning(
    '批量删除部分完成：删除 ' + result.deleted +
    ' 个，失败 ' + result.failed + ' 个',
  )
}
await load()
~~~

Single deletion may say `本地账号与 Codex2API 认证已删除` when remote status is `deleted`.

- [ ] **Step 7: Verify and commit**

Run:

~~~bash
PYTHONPATH=. pytest tests/test_accounts_deletion.py tests/test_accounts_visibility.py tests/test_accounts_api_sanitization.py -q
cd frontend && npm test -- --run src/pages/Accounts.test.tsx
git add api/accounts.py tests/test_accounts_deletion.py frontend/src/pages/Accounts.tsx frontend/src/pages/Accounts.test.tsx
git commit -m "feat: report partial account deletion results"
~~~

Expected: backend and frontend deletion tests pass.

### Task 6: Freeze the switch and delegate automatic deactivation cleanup

**Files:**
- Modify: `tests/test_chatgpt_relogin.py`
- Modify: `tests/test_chatgpt_relogin_task.py`
- Modify: `services/chatgpt_relogin.py`
- Modify: `api/tasks.py`

- [ ] **Step 1: Write failing delegation tests**

Patch `remove_account` and raise `ChatGPTAccountDeactivatedError` from login. Assert:

~~~python
result = relogin_chatgpt_account(
    self.account_id,
    codex2api_delete_on_account_remove_enabled=True,
)
assert result["account_removed"] is True
assert result["stage"] == "account_removed"
assert remove.call_args.kwargs["already_locked"] is True
assert remove.call_args.kwargs[
    "codex2api_delete_on_account_remove_enabled"
] is True
~~~

Return `remote_failed` from the removal mock and assert `account_removed=False`, `stage=account_remove_failed`, the stable error code, and the local row remains.

- [ ] **Step 2: Write the failing frozen-setting test**

Create an automatic task while `config_store.get` returns `"1"`, change the store to `"0"` before running it, then assert the re-login call still receives boolean true:

~~~python
assert snapshot["meta"][
    "codex2api_delete_on_account_remove_enabled"
] is True
assert relogin.call_args.kwargs[
    "codex2api_delete_on_account_remove_enabled"
] is True
~~~

- [ ] **Step 3: Run and verify RED**

Run `PYTHONPATH=. pytest tests/test_chatgpt_relogin.py tests/test_chatgpt_relogin_task.py -q`.

Expected: FAIL because cleanup is still local-only and the task does not freeze/pass the switch.

- [ ] **Step 4: Delegate the deactivation branch**

Add the setting argument through `relogin_chatgpt_account`, `_relogin_chatgpt_account_locked`, and `_remove_deactivated_local_account`. The latter calls:

~~~python
removal = remove_account(
    account_id,
    database_engine=engine,
    expected_created_at=created_at,
    expected_updated_at=updated_at,
    already_locked=True,
    task_control=task_control,
    attempt_id=attempt_id,
    codex2api_delete_on_account_remove_enabled=(
        codex2api_delete_on_account_remove_enabled
    ),
)
~~~

Only `status in {"deleted","already_absent"}` becomes `account_removed=True, stage=account_removed`. All remote/local conflicts become `account_removed=False, stage=account_remove_failed` and retain the stable error code. Remove the obsolete local-only delete helper.

- [ ] **Step 5: Freeze and pass the setting**

At task creation:

~~~python
task_meta["deleted_account_count"] = 0
if automation:
    task_meta["codex2api_delete_on_account_remove_enabled"] = _is_truthy(
        config_store.get(
            "codex2api_delete_on_account_remove_enabled", "0"
        )
    )
~~~

Read it once from task metadata and pass it to every automatic `relogin_chatgpt_account` call. A manual re-login passes `None` so the service resolves current config.

- [ ] **Step 6: Verify and commit**

Run:

~~~bash
PYTHONPATH=. pytest tests/test_chatgpt_relogin.py tests/test_chatgpt_relogin_task.py -q
git add services/chatgpt_relogin.py api/tasks.py tests/test_chatgpt_relogin.py tests/test_chatgpt_relogin_task.py
git commit -m "feat: apply linked cleanup to deactivated accounts"
~~~

Expected: automatic cleanup delegation, preservation, and freeze tests pass.

### Task 7: Add REMOVED and the inclusive R/D task contract

**Files:**
- Modify: `tests/test_task_runtime.py`
- Modify: `core/task_runtime.py`
- Modify: `tests/test_chatgpt_relogin_task.py`
- Modify: `api/tasks.py`
- Modify: `core/db.py`

- [ ] **Step 1: Write the failing runtime test**

~~~python
def test_attempt_result_has_independent_removed_outcome():
    result = AttemptResult.removed("账号已被删除或停用")
    assert result.outcome == AttemptOutcome.REMOVED
    assert result.message == "账号已被删除或停用"
~~~

- [ ] **Step 2: Run and verify RED**

Run `PYTHONPATH=. pytest tests/test_task_runtime.py::test_attempt_result_has_independent_removed_outcome -q`.

Expected: FAIL because the outcome is absent.

- [ ] **Step 3: Add the outcome**

~~~python
class AttemptOutcome(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    STOPPED = "stopped"
    REMOVED = "removed"


@classmethod
def removed(cls, message: str = "") -> "AttemptResult":
    return cls(AttemptOutcome.REMOVED, message)
~~~

- [ ] **Step 4: Write the exact 3 + 17 task test**

Create 20 confirmed-auth-failure fixtures. IDs 1-3 return ordinary full-login failures; IDs 4-20 return `account_removed=True`. Assert:

~~~python
assert snapshot["registered"] == 20
assert len(snapshot["errors"]) == 3
assert snapshot["meta"]["deleted_account_count"] == 17
assert snapshot["meta"]["relogin_failed_count"] == 20
alert_sender.assert_called_once_with(
    task_id=task_id,
    total_accounts=20,
    successful_accounts=0,
    invalid_rt_count=20,
    relogin_failed_count=20,
    deleted_account_count=17,
)
~~~

Update the single removed test to assert no generic error and `TaskLog.status == "removed"`.

- [ ] **Step 5: Run and verify RED**

Run `PYTHONPATH=. pytest tests/test_task_runtime.py tests/test_chatgpt_relogin_task.py -q`.

Expected: FAIL because removed attempts still enter errors and R excludes D.

- [ ] **Step 6: Make R inclusive**

Replace the current exclusion logic with:

~~~python
full_relogin_failed = (
    str(result.get("mode") or "").strip().lower() == "full_login"
    and not bool(result.get("relogin_ok"))
)
~~~

Do not exclude `account_removed` or `stage=account_removed`. Thus R includes ordinary full-login failures, successful deleted/deactivated cleanup, and cleanup failures that followed a failed full login.

- [ ] **Step 7: Return and aggregate REMOVED**

Before the generic failure path:

~~~python
if account_removed:
    _log(task_id, "[REMOVE] 账号已被删除或停用，本地记录已移除: " + account_label)
    _save_task_log(
        "chatgpt", email, "removed",
        detail={
            "mode": result_mode,
            "account_id": account_id,
            "stage": "account_removed",
            "account_removed": True,
        },
    )
    return AttemptResult.removed(detail_message)
~~~

In aggregation:

~~~python
elif outcome.outcome == AttemptOutcome.REMOVED:
    deleted_account_count += 1
    processed += 1
    _task_store.update_meta(
        task_id,
        deleted_account_count=deleted_account_count,
    )
~~~

Only the generic failure branch appends to `errors`. Persist final R and D and pass both to the alert. Update the `TaskLog.status` comment to include `removed`.

The paired task-summary plan must derive automatic displayed failure count as:

~~~python
error_count = max(relogin_failed_count - deleted_account_count, 0)
~~~

For R=20 and D=17 the card therefore renders `失败 3`, `重登失败 20`, and `已删除账号 17`.

- [ ] **Step 8: Verify and commit**

Run:

~~~bash
PYTHONPATH=. pytest tests/test_task_runtime.py tests/test_chatgpt_relogin_task.py -q
git add core/task_runtime.py core/db.py api/tasks.py tests/test_task_runtime.py tests/test_chatgpt_relogin_task.py
git commit -m "feat: track deleted accounts apart from task errors"
~~~

Expected: the exact 3/17/20 contract passes.

### Task 8: Show the deleted subset in alert email and history

**Files:**
- Modify: `tests/test_chatgpt_auto_relogin_alerts.py`
- Modify: `services/chatgpt_auto_relogin_alerts.py`
- Create: `frontend/src/pages/TaskHistory.test.tsx`
- Modify: `frontend/src/pages/TaskHistory.tsx`

- [ ] **Step 1: Write failing alert tests**

At threshold 20 pass D=17 and assert the four headline metrics remain unchanged plus:

~~~python
assert "其中已删除或停用账号：17" in plain
assert "其中已删除或停用账号" in html
assert ">17<" in html
~~~

Add a below-threshold case R=19/D=19 that opens no SMTP connection. Add a normalization case D>R and assert the displayed subset is clamped to R.

- [ ] **Step 2: Run and verify RED**

Run `PYTHONPATH=. pytest tests/test_chatgpt_auto_relogin_alerts.py -q`.

Expected: FAIL because the alert has no D argument.

- [ ] **Step 3: Implement the subset without changing threshold math**

Add `deleted_account_count: int = 0` to the public sender and builder:

~~~python
failed_count = _non_negative_int(relogin_failed_count)
deleted_count = min(
    _non_negative_int(deleted_account_count),
    failed_count,
)
if failed_count < threshold:
    return {
        "sent": False,
        "reason": "below_threshold",
        "threshold": threshold,
    }
~~~

Keep the four cells as total, success, auth failure, re-login failure. Add `其中已删除或停用账号：D` immediately after the R line in plain text and one detail row below the four-cell HTML table. Explain D is a subset of R. Never use D in the trigger decision.

- [ ] **Step 4: Write the failing Task History test**

Mock one item with `status: 'removed'` and assert `已删除` appears while `失败` does not.

~~~tsx
expect(await screen.findByText('已删除')).toBeTruthy()
expect(screen.queryByText('失败')).toBeNull()
~~~

- [ ] **Step 5: Run and verify RED**

Run `cd frontend && npm test -- --run src/pages/TaskHistory.test.tsx`.

Expected: FAIL because all non-success states render as failures.

- [ ] **Step 6: Implement explicit history presentation**

~~~tsx
const STATUS_PRESENTATION = {
  success: { color: 'success', label: '成功' },
  failed: { color: 'error', label: '失败' },
  skipped: { color: 'default', label: '已跳过' },
  removed: { color: 'warning', label: '已删除' },
} as const
~~~

Render unknown legacy values with a neutral tag.

- [ ] **Step 7: Verify and commit**

Run:

~~~bash
PYTHONPATH=. pytest tests/test_chatgpt_auto_relogin_alerts.py tests/test_chatgpt_relogin_task.py -q
cd frontend && npm test -- --run src/pages/TaskHistory.test.tsx
git add services/chatgpt_auto_relogin_alerts.py tests/test_chatgpt_auto_relogin_alerts.py frontend/src/pages/TaskHistory.tsx frontend/src/pages/TaskHistory.test.tsx
git commit -m "feat: show deleted subset in relogin alerts"
~~~

Expected: email and history tests pass.

### Task 9: Integrate with task summary and run the complete quality gate

**Files:**
- Verify: `docs/superpowers/plans/2026-08-05-task-runs-performance-retention.md`
- Verify: all files changed in Tasks 1-8

- [ ] **Step 1: Verify the cross-plan contract**

Confirm `/api/tasks/summary` whitelists R and D and derives automatic `error_count=max(R-D,0)`. `RunningTasks` must render summary `error_count`, R, and D independently. Non-automatic tasks continue using raw generic error count.

- [ ] **Step 2: Run focused backend suites**

~~~bash
PYTHONPATH=. pytest \
  tests/test_chatgpt_auto_relogin.py \
  tests/test_codex2api_frontend_contract.py \
  tests/test_chatgpt_account_coordination.py \
  tests/test_codex2api_upload.py \
  tests/test_chatgpt_account_removal.py \
  tests/test_accounts_deletion.py \
  tests/test_task_runtime.py \
  tests/test_chatgpt_relogin.py \
  tests/test_chatgpt_relogin_task.py \
  tests/test_chatgpt_auto_relogin_alerts.py -q
~~~

Expected: zero failures.

- [ ] **Step 3: Run focused frontend suites**

~~~bash
cd frontend && npm test -- --run \
  src/pages/Settings.test.tsx \
  src/pages/Accounts.test.tsx \
  src/pages/TaskHistory.test.tsx \
  src/pages/RunningTasks.test.tsx
~~~

Expected: zero failures.

- [ ] **Step 4: Run the full gate**

~~~bash
PYTHONPATH=. pytest -q
cd frontend && npm test -- --run && npm run lint && npm run build
git diff --check
~~~

Expected: all tests pass; lint/build exit 0; no whitespace errors.

- [ ] **Step 5: Inspect credential redaction and commit integration-only edits**

Inspect the final diff and confirm no Admin Key, token, remote row, or upstream body is logged/returned. If integration required edits, commit them:

~~~bash
git add api/tasks.py frontend/src/pages/RunningTasks.tsx frontend/src/pages/RunningTasks.test.tsx
git commit -m "test: verify deleted account summary contract"
~~~

If the tree is already clean, skip the empty commit.

### Task 10: Deploy and enable production cleanup

**Files:**
- Verify: `/www/any-auto-register/shared/data/account_manager.db`
- Verify: `any-auto-register.service`

- [ ] **Step 1: Confirm a clean release and idle production**

Run `git rev-parse HEAD` and `git status --short` locally. Then:

~~~bash
ssh -i /Users/xuann/.ssh/id_ed25519_103_144_241_126 -p 55222 root@103.144.241.126 \
  'systemctl is-active any-auto-register.service'
~~~

Query `task_runs` read-only and require zero `pending/running` rows before cutover.

- [ ] **Step 2: Create a consistent verified SQLite backup**

Use Python's `sqlite3.Connection.backup` from the production database to a timestamped `account_manager.db.before-account-removal-*.bak` file, then require `pragma quick_check = ok`.

- [ ] **Step 3: Deploy the combined release**

Follow `docs/superpowers/plans/2026-08-03-no-downtime-server-deployment-plan.md`: stage a SHA-named release, build assets, atomically switch the current release, restart the service, and retain the previous release. Require active service, port 18081, and a successful public request to `https://accounts.anhepro.com`.

- [ ] **Step 4: Enable only the new switch**

Run this idempotent upsert:

~~~python
with sqlite3.connect(
    "/www/any-auto-register/shared/data/account_manager.db"
) as db:
    db.execute(
        """insert into configs(key, value) values(?, ?)
           on conflict(key) do update set value=excluded.value""",
        ("codex2api_delete_on_account_remove_enabled", "1"),
    )
    db.commit()
~~~

Read it back and require `"1"`. Do not change automatic re-login enablement, interval, concurrency, alert threshold, or upload switch.

- [ ] **Step 5: Verify with a guaranteed no-match fixture**

Inside the active release:

~~~python
result = delete_codex2api_credential(
    email="codex-cleanup-no-match@invalid.example",
    identity={"workspace_id": "cleanup-no-match-fixture"},
)
assert result["status"] == "already_absent"
assert result["remote_id"] is None
~~~

This list-only no-match check must issue no DELETE. Do not delete a live operator account for deployment verification.

- [ ] **Step 6: Record evidence**

Record release SHA, backup path, service health, setting value `1`, synthetic status `already_absent`, test totals, and this exact user-facing contract:

~~~text
自动任务：失败 = max(重登失败 - 已删除账号, 0)
告警：重登失败包含已删除/停用账号，并在 >= 配置阈值时发送
邮件：明确标出重登失败中已删除/停用账号的子集数量
~~~
