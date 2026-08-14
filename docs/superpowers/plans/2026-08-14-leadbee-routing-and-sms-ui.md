# LeadBee Routing and SMS UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provide API-first ChatGPT phone verification with safe SMS-card fallback, independent 50/5 provider queues, sanitized capacity reporting, and one consolidated SMS configuration page.

**Architecture:** Add a shared Decimal capacity parser and process-local capacity/rate coordinators. Batch workers choose API or reserve one pool card immediately before phone verification; only deterministic pre-order or non-ambiguous balance exhaustion can switch modes. Preserve legacy request shapes while new frontend requests use an explicit three-state mode.

**Tech Stack:** Python 3.11+, FastAPI, SQLModel/SQLite, pytest, React, TypeScript, Ant Design, Vitest, pnpm/Vite.

---

### Task 1: Parse and expose sanitized API capacity

**Files:**
- Create: `platforms/chatgpt/leadbee_capacity.py`
- Modify: `api/config.py`
- Modify: `tests/test_leadbee_api_config.py`

- [ ] **Step 1: Write failing Decimal capacity tests**

Cover nested product/balance payloads, price `1.300000`, available `35.70`,
reserved `0.00`, capacity `27`, missing/zero/negative price, malformed amounts,
and configured product absence. The desired public API is:

```python
snapshot = parse_leadbee_capacity(
    products_payload,
    balance_payload,
    product_id="sms_verification_us",
)
assert snapshot.public_dict() == {
    "configured_product_available": True,
    "balance_available": "35.70",
    "balance_reserved": "0.00",
    "unit_price": "1.30",
    "estimated_order_capacity": 27,
    "currency": "CNY",
}
```

- [ ] **Step 2: Run the focused tests RED**

```bash
PYTHONPATH="$PWD" /tmp/any-auto-register-py311/bin/python -m pytest -q \
  tests/test_leadbee_api_config.py -k 'unit_price or capacity or reserved'
```

- [ ] **Step 3: Implement the parser**

Create an immutable `LeadBeeCapacitySnapshot` with `Decimal | None` internal
amounts and a `public_dict()` method. Reuse the existing tolerant product ID
and balance traversal rules, but return only the six allowlisted public fields.
Normalize monetary output with fixed two-decimal strings and compute capacity
with `ROUND_FLOOR`.

- [ ] **Step 4: Use it in `/config/leadbee/test`**

Replace independent product/balance parsing with `parse_leadbee_capacity()`.
Keep all existing credential masking, fixed provider-error logging, and
write-free connection test behavior.

- [ ] **Step 5: Run config suites GREEN and commit**

```bash
PYTHONPATH="$PWD" /tmp/any-auto-register-py311/bin/python -m pytest -q \
  tests/test_leadbee_api_config.py tests/test_chatgpt_auto_relogin.py
git add platforms/chatgpt/leadbee_capacity.py api/config.py tests/test_leadbee_api_config.py
git commit -m "feat(leadbee): expose sanitized API capacity"
```

### Task 2: Add process-local capacity leases and API rate limits

**Files:**
- Create: `platforms/chatgpt/leadbee_runtime.py`
- Create: `tests/test_leadbee_runtime.py`
- Modify: `platforms/chatgpt/phone_service.py`
- Modify: `tests/test_chatgpt_phone_flow.py`

- [ ] **Step 1: Write failing capacity lease tests**

Use a fake API client, clock, and condition wait to prove:

```python
lease = coordinator.reserve(
    client_order_id="aar_" + "1" * 32,
    product_id="prod",
    products={"items": [{"id": "prod", "price": "1.30"}]},
    balance={"available": "2.60", "reserved": "0.00"},
)
```

Two leases succeed, the third raises `LeadBeeCapacityExhausted`, duplicate
client references return the existing lease, release restores capacity, commit
remains counted until the next balance snapshot, and quarantined leases expire
only after their bounded TTL.

- [ ] **Step 2: Write failing sliding-window tests**

Instantiate `LeadBeeApiRateLimiter(create_limit=2, request_limit=3,
window_seconds=60)` with fake time. Assert a third create and fourth total
request wait until the oldest timestamp expires, while deadline or cancellation
raises before recording a request.

- [ ] **Step 3: Run runtime tests RED**

```bash
PYTHONPATH="$PWD" /tmp/any-auto-register-py311/bin/python -m pytest -q \
  tests/test_leadbee_runtime.py
```

- [ ] **Step 4: Implement runtime primitives**

Expose:

```python
class LeadBeeCapacityExhausted(RuntimeError): ...
class LeadBeeCapacityLease:
    def commit(self) -> None: ...
    def release(self) -> None: ...
    def quarantine(self) -> None: ...
class LeadBeeCapacityCoordinator:
    def reserve_from_client(self, *, client, product_id, client_order_id,
                            deadline, checkpoint) -> LeadBeeCapacityLease: ...
class LeadBeeApiRateLimiter:
    def wait(self, *, create, deadline, checkpoint) -> None: ...
```

Production globals use 60 creates, 900 total requests, 60 seconds, a 60-second
product cache, and a short balance snapshot generation. All waits use monotonic
time and a cancellation checkpoint.

- [ ] **Step 5: Wire rate limiting and a minimum poll interval**

`LeadBeeOpenAPIPhoneService._write_with_retry()` calls the injected/default
limiter before every request and marks create calls separately. `_poll_delay()`
returns at least 4 seconds while preserving any larger local/provider delay.
No limiter diagnostics include request bodies, phones, codes, or credentials.

- [ ] **Step 6: Run runtime/provider tests GREEN and commit**

```bash
PYTHONPATH="$PWD" /tmp/any-auto-register-py311/bin/python -m pytest -q \
  tests/test_leadbee_runtime.py tests/test_chatgpt_phone_flow.py \
  tests/test_leadbee_open_api.py
git add platforms/chatgpt/leadbee_runtime.py platforms/chatgpt/phone_service.py \
  tests/test_leadbee_runtime.py tests/test_chatgpt_phone_flow.py
git commit -m "feat(leadbee): coordinate API capacity and request limits"
```

### Task 3: Split API and card provider queues

**Files:**
- Modify: `services/chatgpt_phone_verification.py`
- Modify: `api/tasks.py`
- Modify: `tests/test_chatgpt_phone_verification.py`
- Modify: `tests/test_sms_pool.py`
- Modify: `tests/test_chatgpt_login_with_phone.py`

- [ ] **Step 1: Write failing independent-slot tests**

Patch the provider runners with controlled events. Start 50 API flows and six
card flows. Assert API flows are not blocked by card slots, exactly five cards
enter concurrently, the sixth waits beyond the former 30-second boundary, and
then enters when a card slot is released.

- [ ] **Step 2: Write cancellation and cleanup tests**

Assert a card waiter exits promptly on stop/skip without invoking the provider,
and every success/error/cancel path releases exactly its own slot. API reused
sessions release the unused new slot.

- [ ] **Step 3: Run focused tests RED**

```bash
PYTHONPATH="$PWD" /tmp/any-auto-register-py311/bin/python -m pytest -q \
  tests/test_chatgpt_phone_verification.py tests/test_sms_pool.py \
  tests/test_chatgpt_login_with_phone.py -k 'slot or concurrency or queue'
```

- [ ] **Step 4: Implement separate provider locks**

Replace the shared lock with:

```python
leadbee_api_phone_flow_lock = threading.BoundedSemaphore(50)
leadbee_card_phone_flow_lock = threading.BoundedSemaphore(5)
```

Select by explicit mode in manager and task completion paths. API slot waits
remain cancellation-aware. Card waits have no independent 30-second deadline;
broker expiry/task stop remains the terminating condition. Start provider/card
deadlines only after slot acquisition.

- [ ] **Step 5: Run full related suites GREEN and commit**

```bash
PYTHONPATH="$PWD" /tmp/any-auto-register-py311/bin/python -m pytest -q \
  tests/test_chatgpt_phone_verification.py tests/test_sms_pool.py \
  tests/test_chatgpt_login_with_phone.py tests/test_chatgpt_phone_flow.py
git add services/chatgpt_phone_verification.py api/tasks.py \
  tests/test_chatgpt_phone_verification.py tests/test_sms_pool.py \
  tests/test_chatgpt_login_with_phone.py
git commit -m "feat(leadbee): split API and card verification queues"
```

### Task 4: Add explicit three-mode batch requests and safe card fallback

**Files:**
- Modify: `api/tasks.py`
- Modify: `services/chatgpt_phone_verification.py`
- Modify: `platforms/chatgpt/phone_service.py`
- Modify: `tests/test_chatgpt_login_with_phone.py`
- Modify: `tests/test_chatgpt_phone_flow.py`

- [ ] **Step 1: Write failing request normalization tests**

Cover `api_fallback_pool`, `pool`, and `none`. Assert explicit `pool` wins over
global API enablement, API fallback mode generates client references without
pre-reserving `count` cards, `none` removes every provider marker, and legacy
requests without the new field retain current behavior.

- [ ] **Step 2: Write failing fallback lifecycle tests**

For one API-first account, simulate capacity exhaustion before manager start.
Assert exactly one pool card is reserved, binding is updated before activation,
legacy provider receives that card, and finalization follows restored/consumed/
unusable rules. With 25 workers and enough cards, assert five provider calls at
a time and all 25 eventually run. With ten cards, only ten fallback attempts
run and the other fifteen fail with the fixed generic message.

- [ ] **Step 3: Write ambiguity and durability tests**

Assert transport, 5xx, malformed 2xx, idempotency conflict, unknown/manual
review, or at-risk order never calls `sms_pool_service.reserve`. Explicit
allowlisted balance rejection may fall back only when no order ID exists and
create ambiguity is false. Any initial or fallback binding persistence failure
prevents provider/card activation.

- [ ] **Step 4: Run tests RED**

```bash
PYTHONPATH="$PWD" /tmp/any-auto-register-py311/bin/python -m pytest -q \
  tests/test_chatgpt_login_with_phone.py tests/test_chatgpt_phone_flow.py \
  -k 'sms_mode or fallback or capacity or ambiguous'
```

- [ ] **Step 5: Implement mode normalization and preflight**

Add `CHATGPT_SMS_MODE_KEY` and normalize only the three public values. In
`api_fallback_pool`, acquire a capacity lease immediately before starting the
phone manager. On `LeadBeeCapacityExhausted`, atomically reserve one card,
persist its binding, and run legacy mode. Release a pending lease on every exit;
the provider commits it after a successful create and quarantines ambiguous
creates.

- [ ] **Step 6: Implement deterministic provider rejection classification**

Allowlist only balance codes such as `NO_BALANCE`, `INSUFFICIENT_BALANCE`,
`BALANCE_INSUFFICIENT`, and `INSUFFICIENT_FUNDS`. Publish the coarse internal
code `LEADBEE_API_CAPACITY_EXHAUSTED`; continue collapsing every other API code
to `LEADBEE_API_ERROR`. Preserve the prepared OAuth context for one safe card
retry and never restart mailbox OTP.

- [ ] **Step 7: Implement dynamic pool settlement**

Treat dynamically reserved fallback items exactly like primary pool items for
mark-active, restored, consumed, unusable, quarantine, replacement-card, stop,
retry binding, and final task release. Public snapshots contain only `LeadBee
API` or masked card hints.

- [ ] **Step 8: Run related suites GREEN and commit**

```bash
PYTHONPATH="$PWD" /tmp/any-auto-register-py311/bin/python -m pytest -q \
  tests/test_chatgpt_login_with_phone.py tests/test_sms_pool.py \
  tests/test_chatgpt_phone_verification.py tests/test_chatgpt_phone_flow.py \
  tests/test_leadbee_open_api.py tests/test_leadbee_runtime.py
git add api/tasks.py services/chatgpt_phone_verification.py \
  platforms/chatgpt/phone_service.py tests/test_chatgpt_login_with_phone.py \
  tests/test_chatgpt_phone_flow.py
git commit -m "feat(leadbee): fall back safely to SMS cards"
```

### Task 5: Move LeadBee settings into the SMS page

**Files:**
- Create: `frontend/src/components/LeadBeeApiSettingsCard.tsx`
- Create: `frontend/src/components/LeadBeeApiSettingsCard.test.tsx`
- Modify: `frontend/src/pages/SmsPool.tsx`
- Modify: `frontend/src/pages/SmsPool.test.tsx`
- Modify: `frontend/src/pages/Settings.tsx`
- Modify: `frontend/src/pages/Settings.test.tsx`
- Modify: `frontend/src/App.tsx`

- [ ] **Step 1: Write failing navigation and ownership tests**

Assert the menu/page title is `SMS接码`, `/sms-pool` is unchanged, the SMS page
contains the API settings fields, and Settings does not render or submit any
LeadBee field.

- [ ] **Step 2: Write failing settings-card tests**

Port the existing write-only credential, blank-preservation, enable validation,
unsaved connection test, stale response, delayed hydration, save reset, and
unmount tests to the new component. Add balance/reserved/unit-price/capacity
rendering and assert raw provider metadata never renders.

- [ ] **Step 3: Run frontend tests RED**

```bash
cd frontend && pnpm test --run \
  src/components/LeadBeeApiSettingsCard.test.tsx \
  src/pages/SmsPool.test.tsx src/pages/Settings.test.tsx
```

- [ ] **Step 4: Extract the independent settings card**

The component owns a four-field Ant Form, loads `/config`, saves only the four
allowlisted keys through `PUT /config`, tests through `POST
/config/leadbee/test`, and uses generation/snapshot guards for asynchronous
results. Credentials are never populated from GET responses.

- [ ] **Step 5: Integrate and remove the old Settings section**

Render the card above pool statistics/import/list. Remove the old section and
LeadBee-specific submit normalization from Settings. Rename visible navigation
and heading copy while retaining the route.

- [ ] **Step 6: Run tests/build GREEN and commit**

```bash
cd frontend && pnpm test --run \
  src/components/LeadBeeApiSettingsCard.test.tsx \
  src/pages/SmsPool.test.tsx src/pages/Settings.test.tsx src/App.test.tsx
pnpm build
git add frontend/src/components/LeadBeeApiSettingsCard.tsx \
  frontend/src/components/LeadBeeApiSettingsCard.test.tsx \
  frontend/src/pages/SmsPool.tsx frontend/src/pages/SmsPool.test.tsx \
  frontend/src/pages/Settings.tsx frontend/src/pages/Settings.test.tsx \
  frontend/src/App.tsx
git commit -m "feat(leadbee): consolidate SMS configuration"
```

### Task 6: Add three-mode login UI and API capacity summary

**Files:**
- Modify: `frontend/src/components/ChatGPTExistingAccountLoginModal.tsx`
- Modify: `frontend/src/components/ChatGPTExistingAccountLoginModal.test.tsx`
- Modify: `frontend/src/lib/chatgptStagedLogin.ts`
- Modify: `frontend/src/lib/chatgptStagedLogin.test.ts`

- [ ] **Step 1: Write failing payload tests**

Assert `api_fallback_pool`, `pool`, and `none` produce the explicit mode and
omit incompatible legacy flags, raw cards, credentials, client references, and
provider metadata. Legacy helper inputs remain supported only for existing
callers.

- [ ] **Step 2: Write failing modal tests**

With API enabled, assert `API优先` is selected, balance/price/capacity and card
count are visible, direct-card textarea is absent, all three modes can be
selected, explicit pool works while global API is on, and concurrency is capped
at 50. Status failure shows neutral copy and does not leak configuration.

- [ ] **Step 3: Run tests RED**

```bash
cd frontend && pnpm test --run \
  src/lib/chatgptStagedLogin.test.ts \
  src/components/ChatGPTExistingAccountLoginModal.test.tsx
```

- [ ] **Step 4: Implement the payload enum and modal controls**

Use an Ant Design `Radio.Group` with values `api_fallback_pool`, `pool`, and
`none`. Fetch `/config`, `/config/leadbee/test`, and `/sms-pool/stats` without
ever storing credentials. Default to API-first when configured, otherwise pool
when cards exist, otherwise none. Submit the explicit mode and keep count /
concurrency mutually valid with a maximum of 50.

- [ ] **Step 5: Run frontend tests/build GREEN and commit**

```bash
cd frontend && pnpm test --run \
  src/lib/chatgptStagedLogin.test.ts \
  src/components/ChatGPTExistingAccountLoginModal.test.tsx \
  src/pages/SmsPool.test.tsx
pnpm build
git add frontend/src/components/ChatGPTExistingAccountLoginModal.tsx \
  frontend/src/components/ChatGPTExistingAccountLoginModal.test.tsx \
  frontend/src/lib/chatgptStagedLogin.ts frontend/src/lib/chatgptStagedLogin.test.ts
git commit -m "feat(leadbee): choose API or SMS card routing"
```

### Task 7: Full verification and deployment readiness

**Files:**
- Verify all changed files
- Update: `static/` using the repository's existing frontend build-copy flow

- [ ] **Step 1: Run complete backend verification**

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" \
  /tmp/any-auto-register-py311/bin/python -m pytest -q
PYTHONDONTWRITEBYTECODE=1 /tmp/any-auto-register-py311/bin/python -m compileall -q \
  api core services platforms tests
```

- [ ] **Step 2: Run complete frontend verification**

```bash
cd frontend
pnpm test --run
pnpm build
```

- [ ] **Step 3: Run static and secret checks**

Run `git diff --check`, available Ruff checks on changed Python ranges, confirm
only intended files changed, and scan changed production files for API key,
secret, signature, full card, phone, OTP, private-key, and common token patterns.

- [ ] **Step 4: Build deployable static assets and commit**

Copy the verified frontend build into the repository's tracked `static/`
directory using the existing build script/process, verify the copied asset
contains `SMS接码` and the new mode labels, then commit only generated assets:

```bash
git add static
git commit -m "build: publish LeadBee routing interface"
```

- [ ] **Step 5: Prepare reversible production deployment**

Require no pending/running tasks, create and verify an online SQLite backup,
publish the exact verified commit as a new release, atomically switch
`/www/any-auto-register/current`, restart only `any-auto-register.service`, and
verify service status plus loopback/public health. Retain the prior release for
immediate symlink rollback.
