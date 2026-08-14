# LeadBee Open API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace LeadBee browser-session card requests with signed Open API v1 orders for ChatGPT single-account and batch phone verification.

**Architecture:** Add a focused HMAC API client and an order-backed phone-provider adapter while retaining the existing provider interface and legacy adapter for rollback. Global write-only credentials live in the existing configuration store; API mode creates a non-secret client order reference per attempt and bypasses SMS-card reservations.

**Tech Stack:** Python 3, FastAPI, SQLModel configuration store, `curl_cffi`/Requests-compatible sessions, pytest/unittest, React, TypeScript, Ant Design, Vitest.

---

### Task 1: Signed LeadBee v1 client

**Files:**
- Create: `platforms/chatgpt/leadbee_open_api.py`
- Create: `tests/test_leadbee_open_api.py`

- [ ] **Step 1: Write failing canonical-signature tests**

Cover GET with an empty body/idempotency line and POST with the exact compact
JSON bytes sent on the wire. Use fixed timestamp and nonce providers:

```python
client = LeadBeeOpenAPIClient(
    api_key="ak_test",
    api_secret="secret_test",
    session=session,
    clock=lambda: 1_785_686_400,
    nonce_factory=lambda: "request_nonce_000001",
)
client.create_order(
    client_order_id="customer_order_0001",
    product_id="sms_verification_us",
    idempotency_key="order_20260803_customer_0001",
)
assert session.calls[0].body == (
    b'{"client_order_id":"customer_order_0001",'
    b'"product_id":"sms_verification_us","quantity":1}'
)
```

- [ ] **Step 2: Run the tests and confirm RED**

Run:

```bash
pytest -q tests/test_leadbee_open_api.py
```

Expected: import failure because `leadbee_open_api.py` does not exist.

- [ ] **Step 3: Implement byte-exact signing and envelope parsing**

Create these public units:

```python
LEADBEE_API_ORIGIN = "https://api.leadbee.cn"
LEADBEE_API_PREFIX = "/api/open/v1"

class LeadBeeAPIError(RuntimeError):
    def __init__(self, message: str, *, code: str = "", request_id: str = "",
                 retry_after: float | None = None, status_code: int = 0): ...

class LeadBeeOpenAPIClient:
    def list_products(self) -> dict[str, Any]: ...
    def get_balance(self) -> dict[str, Any]: ...
    def create_order(self, *, client_order_id: str, product_id: str,
                     idempotency_key: str) -> dict[str, Any]: ...
    def get_order(self, order_id: str) -> dict[str, Any]: ...
    def replace_order(self, order_id: str, *, idempotency_key: str) -> dict[str, Any]: ...
    def cancel_order(self, order_id: str, *, idempotency_key: str) -> dict[str, Any]: ...
```

Serialize once with `json.dumps(..., separators=(",", ":"),
ensure_ascii=False).encode("utf-8")`; send that value through `data=body`.
Use empty bytes for documented no-argument replace/cancel operations. Accept any
2xx response before checking `success is True` and `data` is a dictionary.

- [ ] **Step 4: Add error, retry metadata, and redaction tests**

Test malformed JSON, malformed envelopes, 401/403/404/409/429/503, arbitrary
2xx success codes, `Retry-After`, and transport errors containing the test key
or secret. Assert exception text contains neither credential nor signature.

- [ ] **Step 5: Run client tests GREEN**

```bash
pytest -q tests/test_leadbee_open_api.py
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add platforms/chatgpt/leadbee_open_api.py tests/test_leadbee_open_api.py
git commit -m "feat(leadbee): add signed Open API client"
```

### Task 2: Order-backed phone provider

**Files:**
- Modify: `platforms/chatgpt/phone_service.py`
- Modify: `tests/test_chatgpt_phone_flow.py`

- [ ] **Step 1: Write failing provider-factory and order-state tests**

Add tests proving `create_phone_service()` selects the Open API adapter when
`leadbee_api_enabled` is true and does not require a real exchange card. Cover:

```text
PROCESSING -> WAITING_CODE(phone) -> COMPLETED(code)
WAITING_CODE -> replace -> REPLACING -> WAITING_CODE(new phone)
WAITING_CODE -> cancel -> CANCELING -> CANCELED
EXPIRED -> terminal failure
UNKNOWN/MANUAL_REVIEW -> quarantined failure without a new order
```

Assert polling uses each response's `next_poll_after_seconds` and all write
retries preserve their idempotency key.

- [ ] **Step 2: Run focused tests RED**

```bash
pytest -q tests/test_chatgpt_phone_flow.py -k 'OpenAPI or open_api'
```

Expected: failures because the adapter and factory branch are absent.

- [ ] **Step 3: Implement `LeadBeeOpenAPIPhoneService`**

Keep the current provider protocol:

```python
class LeadBeeOpenAPIPhoneService:
    provider_name = "LeadBee API"
    supports_resend = False
    supports_blacklist = True
    requires_explicit_replacement = True
    supports_cancellation = True

    def acquire_phone(self, *, exclude_prefixes=None) -> PhoneEntry | None: ...
    def wait_for_code(self, entry: PhoneEntry, *, timeout=None) -> str | None: ...
    def request_replacement(self, phone: str, *, reason: str = "") -> bool: ...
    def cancel_active(self) -> bool: ...
```

Derive stable idempotency keys from the non-secret client order reference and
operation sequence. Validate E.164 phones and 4-8 digit codes but log only
masked phone hints and never codes.

- [ ] **Step 4: Update factory selection and legacy coverage**

Select Open API first when enabled and fully configured. Preserve the existing
`LeadBeePhoneService` branch when API mode is off and an exchange code exists.
Retain SMSToMe fallback.

- [ ] **Step 5: Run provider suite GREEN**

```bash
pytest -q tests/test_chatgpt_phone_flow.py
```

Expected: existing legacy and new Open API provider tests pass.

- [ ] **Step 6: Commit**

```bash
git add platforms/chatgpt/phone_service.py tests/test_chatgpt_phone_flow.py
git commit -m "feat(leadbee): drive phone verification with API orders"
```

### Task 3: Write-only configuration and connection test

**Files:**
- Modify: `api/config.py`
- Modify: `tests/test_chatgpt_auto_relogin.py`
- Create: `tests/test_leadbee_api_config.py`

- [ ] **Step 1: Write failing secret-preservation tests**

Verify:

```python
response = config_api.get_config()
assert response["leadbee_api_key"] == ""
assert response["leadbee_api_secret"] == ""

config_api.update_config(ConfigUpdate(data={
    "leadbee_api_key": "",
    "leadbee_api_secret": "",
}))
assert store.values["leadbee_api_secret"] == "stored-secret"
```

Also reject enabling API mode unless key, secret, and product ID exist in the
merged stored/update snapshot.

- [ ] **Step 2: Run configuration tests RED**

```bash
pytest -q tests/test_leadbee_api_config.py tests/test_chatgpt_auto_relogin.py
```

- [ ] **Step 3: Add config keys and masking**

Add `leadbee_api_enabled`, `leadbee_api_key`, `leadbee_api_secret`, and
`leadbee_api_product_id` to the allowlist. Normalize the enable flag, make key
and secret blank updates preserve stored values, and blank both in public GET
responses.

- [ ] **Step 4: Add `/config/leadbee/test`**

Merge unsaved overrides with stored values, call only `GET /products` and
`GET /balance`, sanitize the response, and return:

```json
{
  "ok": true,
  "product_ids": ["sms_verification_us"],
  "configured_product_available": true,
  "balance_available": "10.00",
  "currency": "CNY"
}
```

The parser must tolerate additional provider fields and must not return raw
provider payloads, credentials, signatures, phone values, or SMS codes.

- [ ] **Step 5: Run configuration tests GREEN**

```bash
pytest -q tests/test_leadbee_api_config.py tests/test_chatgpt_auto_relogin.py
```

- [ ] **Step 6: Commit**

```bash
git add api/config.py tests/test_leadbee_api_config.py tests/test_chatgpt_auto_relogin.py
git commit -m "feat(leadbee): configure and test Open API credentials"
```

### Task 4: Single-account phone-verification API mode

**Files:**
- Modify: `api/chatgpt.py`
- Modify: `services/chatgpt_phone_verification.py`
- Modify: `tests/test_chatgpt_phone_verification.py`

- [ ] **Step 1: Write failing start tests**

Add `leadbee_api: bool = False` to the desired request. Test that API mode:

- succeeds without `leadbee_code` when stored configuration is complete;
- generates a unique `aar_<hex>` client reference;
- rejects mixed manual-phone/card/API modes;
- reports a configuration error before starting a broker when disabled or
  incomplete;
- never publishes credentials in the broker snapshot or errors.

- [ ] **Step 2: Run start tests RED**

```bash
pytest -q tests/test_chatgpt_phone_verification.py -k leadbee_api
```

- [ ] **Step 3: Implement explicit API start mode**

Keep the existing automatic broker provider name `leadbee` for compatibility,
but generate the client order reference server-side and pass it to
`run_leadbee_phone_oauth_flow`. Update visible copy from card-specific terms to
order/provider terms when API mode is active.

- [ ] **Step 4: Run the complete manager suite GREEN**

```bash
pytest -q tests/test_chatgpt_phone_verification.py
```

- [ ] **Step 5: Commit**

```bash
git add api/chatgpt.py services/chatgpt_phone_verification.py tests/test_chatgpt_phone_verification.py
git commit -m "feat(leadbee): start API phone verification without cards"
```

### Task 5: Batch login without card reservations

**Files:**
- Modify: `api/tasks.py`
- Modify: `tests/test_chatgpt_login_with_phone.py`
- Modify: `tests/test_register_task_controls.py`

- [ ] **Step 1: Write failing batch preparation tests**

With `leadbee_api_enabled=1`, assert a bind-phone request with N accounts:

- needs no `chatgpt_existing_account_leadbee_codes`;
- creates N unique non-secret client order references;
- does not call `sms_pool_service.reserve()` or check SMS-pool inventory;
- gives retry bindings fresh order references while retaining bound email and
  account ID;
- strips API key/secret from task/account persistence.

- [ ] **Step 2: Run batch tests RED**

```bash
pytest -q tests/test_chatgpt_login_with_phone.py tests/test_register_task_controls.py
```

- [ ] **Step 3: Implement API-aware request preparation**

Add a helper that reads the global API configuration and generates `aar_` IDs.
When enabled, force SMS-pool mode off, skip card-count validation, and pass the
generated references through the existing per-attempt completion path. Extend
the secret scrub set with API credential keys.

- [ ] **Step 4: Preserve legacy pool behavior**

Run and retain tests showing that API-disabled requests still require either
one exchange card per account or an SMS-pool reservation.

- [ ] **Step 5: Run batch suites GREEN**

```bash
pytest -q tests/test_chatgpt_login_with_phone.py tests/test_register_task_controls.py
```

- [ ] **Step 6: Commit**

```bash
git add api/tasks.py tests/test_chatgpt_login_with_phone.py tests/test_register_task_controls.py
git commit -m "feat(leadbee): use API orders in batch login"
```

### Task 6: Settings and login UI migration

**Files:**
- Modify: `frontend/src/pages/Settings.tsx`
- Modify: `frontend/src/pages/Settings.test.tsx`
- Modify: `frontend/src/components/ChatGPTPhoneVerificationModal.tsx`
- Modify: `frontend/src/components/ChatGPTPhoneVerificationModal.test.tsx`
- Modify: `frontend/src/components/ChatGPTExistingAccountLoginModal.tsx`
- Modify: `frontend/src/lib/chatgptStagedLogin.ts`
- Modify: `frontend/src/lib/chatgptStagedLogin.test.ts`

- [ ] **Step 1: Write failing frontend tests**

Test write-only key/secret fields, product ID, enabled switch, connection-test
request, API single-account start body `{leadbee_api: true}`, and a batch
payload that omits card codes and SMS-pool mode when API is enabled.

- [ ] **Step 2: Run frontend tests RED**

```bash
cd frontend && pnpm test --run src/pages/Settings.test.tsx src/components/ChatGPTPhoneVerificationModal.test.tsx src/lib/chatgptStagedLogin.test.ts
```

- [ ] **Step 3: Add the LeadBee API settings section**

Render enabled, write-only API key, write-only secret, product ID, and a
"测试 LeadBee API" button. Empty credentials preserve stored values. Show only
sanitized success/error text.

- [ ] **Step 4: Migrate phone-login inputs**

When API mode is enabled, show `LeadBee API 自动接码`, remove the exchange-code
textarea and SMS-pool availability gate, and emit the new API flags. Preserve
manual phone and legacy card UI when API mode is off.

- [ ] **Step 5: Run frontend tests and build GREEN**

```bash
cd frontend && pnpm test --run src/pages/Settings.test.tsx src/components/ChatGPTPhoneVerificationModal.test.tsx src/lib/chatgptStagedLogin.test.ts
cd frontend && pnpm build
```

- [ ] **Step 6: Commit**

```bash
git add frontend/src/pages/Settings.tsx frontend/src/pages/Settings.test.tsx frontend/src/components/ChatGPTPhoneVerificationModal.tsx frontend/src/components/ChatGPTPhoneVerificationModal.test.tsx frontend/src/components/ChatGPTExistingAccountLoginModal.tsx frontend/src/lib/chatgptStagedLogin.ts frontend/src/lib/chatgptStagedLogin.test.ts
git commit -m "feat(leadbee): expose Open API login controls"
```

### Task 7: Integration verification and deployment

**Files:**
- Modify only if failures expose implementation defects.

- [ ] **Step 1: Run focused backend regression**

```bash
pytest -q tests/test_leadbee_open_api.py tests/test_leadbee_api_config.py tests/test_chatgpt_phone_flow.py tests/test_chatgpt_phone_verification.py tests/test_chatgpt_login_with_phone.py tests/test_register_task_controls.py
```

- [ ] **Step 2: Run full backend and frontend regression**

```bash
pytest -q
cd frontend && pnpm test --run
cd frontend && pnpm build
```

- [ ] **Step 3: Run read-only credential validation**

Store credentials only in the runtime configuration, then call the connection
test. Confirm signed `GET /products` and `GET /balance` succeed, record only
sanitized product IDs/balance metadata, and choose an actually authorized
product ID. Do not create an order in this step.

- [ ] **Step 4: Review secret exposure and diff**

```bash
git diff --check
git status --short
rg -n "ak_live_|API_SECRET_VALUE" --hidden --glob '!*.db' .
```

Expected: no real credential appears in tracked or untracked project files.

- [ ] **Step 5: Controlled provider compatibility check**

After read-only validation, create one order, verify number polling, and either
complete or cancel it. If OpenAI rejects the number, use that same order to
verify `/replace`. Confirm the final state is `COMPLETED`, `CANCELED`, or
provider-confirmed `EXPIRED`; never leave `UNKNOWN/MANUAL_REVIEW` and create a
second order.

- [ ] **Step 6: Deploy through the existing atomic release workflow**

Build the frontend into the release, back up and verify SQLite, switch the
release symlink, restart the service, and verify service state, HTTP 200, error
logs, config masking, and read-only LeadBee connection test.

- [ ] **Step 7: Push the verified branch**

```bash
git push origin HEAD:main
```

Expected: the deployed commit and `origin/main` resolve to the same SHA.
