# Codex2API Integration Design

**Date:** 2026-07-30

**Status:** Approved for implementation

**Upstream reference:** `james-6-23/codex2api` at commit `3ad94f75f32ad24fa7ebd069cc123cc7338767cd`

## Goal

Add Codex2API as an independent ChatGPT credential destination. A completed registration can automatically upload its credential to the one Codex2API instance configured by the user. Existing CPA and Sub2API integrations remain independently configurable.

The integration also provides per-account upload state, single-account upload, and batch upload from the ChatGPT accounts page.

## Scope

### Included

- A dedicated `Codex2API` item in the Settings left navigation.
- Persistent settings for:
  - automatic upload enablement;
  - Codex2API API URL;
  - Codex2API Admin Key.
- Official Codex2API management API integration using `X-Admin-Key`.
- Refresh Token upload when a Refresh Token is available.
- Access Token fallback when no Refresh Token is available.
- Automatic upload after successful ChatGPT registration.
- Single-account and batch manual upload actions.
- Independent upload status under `sync_statuses.codex2api`.
- ChatGPT account-list status presentation.

### Excluded

- Installing, starting, stopping, or updating Codex2API.
- Runtime endpoint negotiation for unofficial forks.
- Reusing CPA, CLIProxyAPI, contribution-server, or Sub2API addresses for Codex2API.
- Account-group assignment in Codex2API.
- Changing existing CPA, Sub2API, CodexProxy, CLIProxyAPI, or contribution upload contracts.

## Configuration

Add these configuration keys:

| Key | Type | Meaning |
| --- | --- | --- |
| `codex2api_enabled` | boolean-like persisted value | Enables registration-completion auto-upload |
| `codex2api_api_url` | string | The only permitted Codex2API target base URL |
| `codex2api_admin_key` | secret string | Value sent in the `X-Admin-Key` header |

The Settings page exposes these fields under a dedicated left-navigation item named `Codex2API`. The automatic-upload switch defaults to disabled unless an existing saved value explicitly enables it. Saving the page normalizes the switch to a boolean through the existing config-value helpers.

Manual uploads use the saved URL and Admin Key. The action UI does not accept per-request URL or key overrides, ensuring the module can only send credentials to the configured Codex2API instance.

## Upstream API Contract

The official Codex2API management API supports two JSON endpoints:

### Refresh Token path

```http
POST {codex2api_api_url}/api/admin/accounts
X-Admin-Key: {codex2api_admin_key}
Content-Type: application/json

{
  "name": "account@example.com",
  "refresh_token": "rt_..."
}
```

### Access Token fallback

```http
POST {codex2api_api_url}/api/admin/accounts/at
X-Admin-Key: {codex2api_admin_key}
Content-Type: application/json

{
  "name": "account@example.com",
  "access_token": "eyJ..."
}
```

The uploader selects exactly one endpoint per attempt:

1. Use `/api/admin/accounts` when a non-empty Refresh Token exists.
2. Otherwise use `/api/admin/accounts/at` when a non-empty Access Token exists.
3. Fail locally without making a request when neither credential exists.

An HTTP 200 response with `success > 0`, `updated > 0`, or `duplicate > 0` is successful. A duplicate is an idempotent success because the account is already present in the configured destination. A nominal HTTP 200 response that reports only failures is treated as a failed upload.

## Backend Components

### Codex2API uploader

Add `platforms/chatgpt/codex2api_upload.py` with one public operation:

```python
upload_to_codex2api(account) -> tuple[bool, str]
```

Responsibilities:

- read and normalize the saved Codex2API URL and Admin Key;
- select RT or AT using the precedence above;
- build the official JSON request;
- send it without using the account-registration proxy;
- parse success, update, duplicate, and failure counts;
- return a concise user-facing result without including credentials or the Admin Key.

The module remains independent from CPA token-file generation because Codex2API's official direct-management endpoints do not require CPA multipart auth files.

### Synchronization state

Extend `services/chatgpt_sync.py` with a `codex2api` sync name and the same state shape already used by CPA and Sub2API:

```json
{
  "uploaded": true,
  "uploaded_at": "2026-07-30T00:00:00+00:00",
  "last_attempt_ok": true,
  "last_attempt_at": "2026-07-30T00:00:00+00:00",
  "last_message": "上传成功（Refresh Token）"
}
```

Add helpers to record, persist, and update this state for both persisted `AccountModel` objects and duck-typed registration results.

### Automatic upload orchestration

Extend `services/external_sync.sync_account` with an independent Codex2API block.

- It runs only for ChatGPT accounts.
- It runs whenever `codex2api_enabled` is true. The uploader validates the URL and Admin Key so an enabled but incomplete configuration produces a visible failed-attempt state.
- It calls the dedicated uploader and persists the result as `sync_statuses.codex2api`.
- Its result is appended as `{"name": "Codex2API", ...}` so registration-task logs display it.
- It is evaluated independently from CPA and Sub2API switches.
- Contribution mode must not substitute its own URL or key for Codex2API. When Codex2API is enabled, its request always targets `codex2api_api_url` even if contribution mode is enabled.

To preserve the existing contribution-mode semantics for the older destinations, the Codex2API block executes before the current contribution-mode early return. CPA, CodexProxy, and Sub2API retain their existing contribution-mode behavior.

### Manual actions

Add an `upload_codex2api` ChatGPT platform action with no editable parameters. It calls the same uploader used by automatic upload.

The existing generic action API provides:

- single-account execution;
- batch execution for selected account IDs;
- batch execution for the current filtered account range.

Extend the action-result persistence hook so both single and batch execution update `sync_statuses.codex2api`.

## Frontend Design

### Settings

Add a top-level Settings tab with:

- title: `Codex2API`;
- section title: `管理面板`;
- description: `注册完成后自动上传到配置的 Codex2API 管理后台`;
- fields: enable switch, API URL, and secret Admin Key.

The existing Settings layout, cards, password visibility control, and save button are reused without introducing a new visual system.

### ChatGPT accounts

Normalize `sync_statuses.codex2api` alongside CPA, Sub2API, and CLIProxyAPI.

Display a `Codex2API` tag in the local-status panel using the existing upload-state presentation:

- no state: `未上传`;
- a failed first attempt: `失败`;
- any successful upload or duplicate: `已上传`. A later failed retry keeps the historical `已上传` state while the tooltip exposes the latest failure, matching existing CPA/Sub2API semantics.

The tag tooltip includes successful time, latest attempt time, and the sanitized result message.

Add a batch upload entry for either selected rows or the current filtered range. The existing per-account action menu exposes `上传 Codex2API` through the platform action metadata.

## Data Flows

### Registration-completion auto-upload

1. ChatGPT registration succeeds and the account is persisted.
2. The existing background integration thread calls `sync_account`.
3. The Codex2API block checks its independent enable flag and configuration.
4. The uploader chooses RT or AT and sends one official management request.
5. The result is persisted under `sync_statuses.codex2api`.
6. The registration task receives a `[Codex2API]` success or failure log entry.
7. Failures do not roll back or change the successful local registration.

### Manual single or batch upload

1. The user invokes `upload_codex2api` for one account, selected accounts, or the filtered range.
2. The generic action API converts each database account into the platform account shape.
3. The platform action calls the same uploader with saved configuration.
4. The action persistence hook updates each account's Codex2API state.
5. The frontend reloads account data and displays aggregate success/failure feedback.

## Error Handling

- Disabled auto-upload: skip silently and create no state entry.
- Enabled but missing URL or Admin Key: return a clear configuration error and persist a failed attempt.
- Missing RT and AT: do not issue a network request; persist a failed attempt.
- HTTP 401/403: report that the Admin Key was rejected without echoing it.
- HTTP 404: report that the official management endpoint was not found and include the sanitized target base URL.
- Other non-2xx responses: prefer upstream `message`, `msg`, or `error`; otherwise include the HTTP status and a bounded response excerpt.
- HTTP 200 with only reported failures: treat as failed.
- HTTP 200 with duplicate/update/success count: treat as successful.
- Timeout, TLS, DNS, and connection errors: return a concise upload exception message.
- Credential tokens and Admin Key must never appear in logs, exception messages, persisted status messages, or frontend notifications.

## Testing Strategy

### Uploader unit tests

- RT uses `/api/admin/accounts`, `X-Admin-Key`, and the expected JSON body.
- AT is used only when RT is absent.
- RT takes precedence when both tokens exist.
- No request occurs when both tokens are absent.
- Missing URL and missing Admin Key fail before any request.
- New, updated, and duplicate HTTP 200 responses are successful.
- HTTP 200 containing only failures is unsuccessful.
- Authentication, endpoint, other HTTP, malformed JSON, and network failures produce sanitized messages.

### Orchestration and state tests

- Enabled and fully configured Codex2API uploads and persists state.
- Disabled Codex2API does not upload even when configuration remains saved.
- Codex2API uses only its own configured target.
- Codex2API still runs before the existing contribution-mode early return.
- CPA and Sub2API behavior remains unchanged.
- Successful and failed manual actions update only `sync_statuses.codex2api`.
- A previous success remains recorded while a later failure updates the latest-attempt fields, matching existing upload-state semantics.

### Frontend verification

- TypeScript build succeeds.
- The dedicated Settings navigation item renders all three fields and persists the boolean switch.
- ChatGPT account rows show the three Codex2API states correctly.
- Single and batch actions call `upload_codex2api` and refresh displayed state.
- Existing CPA, Sub2API, CLIProxyAPI, and registration views continue to render.

## Acceptance Criteria

1. The Settings page has an independent `Codex2API` left-navigation item.
2. A configured and enabled integration auto-uploads a newly registered ChatGPT account to the configured URL only.
3. RT is preferred and AT is used only as fallback.
4. CPA and Sub2API remain independently configurable and functional.
5. Single and batch manual upload use the saved Codex2API target without URL/key overrides.
6. Duplicate imports are displayed as successful/idempotent uploads.
7. Account rows expose independent Codex2API upload state and failure detail.
8. No secret is emitted in logs or user-facing errors.
9. Backend tests and the frontend production build pass.
