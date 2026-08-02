# LeadBee Phone Retry Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent an OpenAI add-phone rejection from being overwritten by a false LeadBee replacement-state error, while restoring an activated exchange code whenever the automatic phone flow exits unsuccessfully.

**Architecture:** Keep LeadBee's replacement-state guard as defense in depth, but make the OAuth retry loop honor the provider contract: a provider that requires an explicit replacement may only be queried again after `replace-number` succeeds. Forward the bounded original add-phone failure to the progress broker and release any non-terminal LeadBee card in a `finally` path.

**Tech Stack:** Python 3.11, `unittest`, `unittest.mock`, pytest, Docker.

---

### Task 1: Reproduce the classification and state-machine failures

**Files:**
- Modify: `tests/test_chatgpt_phone_flow.py`

- [ ] **Step 1: Add a marker-precedence regression test**

```python
def test_explicit_similar_phone_rejection_wins_over_rate_limit_marker(self):
    detail = "Too many phone numbers similar to yours were detected"
    self.assertTrue(OAuthClient._should_blacklist_phone_failure(detail))
```

- [ ] **Step 2: Add a real LeadBee failure-path regression test**

```python
def test_unhandled_send_failure_preserves_original_error_and_restores_code(self):
    phone = "+12025550123"
    detail = "add-phone/send 失败: 429 - too many verification requests"
    broker = InteractivePhoneVerificationBroker(account_id=17, provider="leadbee")
    session = _LeadBeeSession(
        {"ok": True, "card": _leadbee_card(status="number_ready", phone=phone)},
        {"ok": True, "card": _leadbee_card(status="canceled", is_terminal=True)},
    )
    service = LeadBeePhoneService(
        {"leadbee_code": "bei-sms-RESTORE-CODE", "chatgpt_phone_progress_broker": broker},
        session=session,
    )
    client = OAuthClient({"chatgpt_phone_progress_broker": broker}, verbose=False)
    with mock.patch(
        "platforms.chatgpt.oauth_client.create_phone_service", return_value=service
    ), mock.patch.object(
        client, "_send_phone_number", return_value=(False, None, detail)
    ) as send_phone:
        state = client._handle_add_phone_verification(
            "device-id", "Mozilla/5.0", None, None, FlowState(page_type="add_phone")
        )

    self.assertIsNone(state)
    send_phone.assert_called_once()
    self.assertIn(detail, client.last_error)
    self.assertNotIn("LeadBee 换号未成功", client.last_error)
    self.assertIn(detail, "\n".join(broker.snapshot()["logs"]))
    self.assertEqual(
        [call[0] for call in session.calls],
        [
            "https://sms.leadbee.cn/smsbox/api/activate",
            "https://sms.leadbee.cn/smsbox/api/cancel",
        ],
    )
```

- [ ] **Step 3: Run the two tests and verify RED**

Run:

```bash
docker run --rm -v "$PWD:/app" -w /app --entrypoint pytest any-auto-register-local-app \
  tests/test_chatgpt_phone_flow.py::OAuthPhoneBlacklistTests::test_explicit_similar_phone_rejection_wins_over_rate_limit_marker \
  tests/test_chatgpt_phone_flow.py::LeadBeeOAuthFlowTests::test_unhandled_send_failure_preserves_original_error_and_restores_code -q
```

Expected: both tests fail against the old implementation: marker precedence returns `False`, and the flow reports `LeadBee 换号未成功` without calling `/api/cancel`.

### Task 2: Enforce the LeadBee retry contract and cleanup

**Files:**
- Modify: `platforms/chatgpt/phone_service.py`
- Modify: `platforms/chatgpt/oauth_client.py`
- Test: `tests/test_chatgpt_phone_flow.py`

- [ ] **Step 1: Declare provider lifecycle capabilities**

```python
class LeadBeePhoneService:
    requires_explicit_replacement = True
    supports_cancellation = True

    def cancel_active(self) -> bool:
        return self._cancel_active_card()
```

Leave SMSToMe on the default non-explicit/no-cancellation behavior.

- [ ] **Step 2: Prioritize explicit phone rejection markers**

Evaluate `blacklist_markers` before `non_blacklist_markers`:

```python
if any(marker in combined for marker in blacklist_markers):
    return True
if any(marker in combined for marker in non_blacklist_markers):
    return False
return False
```

- [ ] **Step 3: Gate every retry for explicit-replacement providers**

For send failures and unexpected post-send states, continue only when `_blacklist_phone_if_needed(...)` returned `True`:

```python
requires_explicit_replacement = (
    getattr(phone_service, "requires_explicit_replacement", False) is True
)
replacement_scheduled = self._blacklist_phone_if_needed(
    phone_service, entry, last_failure
)
if requires_explicit_replacement and not replacement_scheduled:
    break
continue
```

For WhatsApp, OTP-delivery, and OTP-validation failures, stop the LeadBee loop when `requires_explicit_replacement` is true. Preserve the current multi-number behavior for SMSToMe.

- [ ] **Step 4: Preserve diagnostics and release the code**

Forward the bounded failure via `_phone_service_log(last_failure[:500])`. Wrap the provider loop in this lifecycle guard:

```python
phone_flow_succeeded = False
try:
    # existing provider loop
    phone_flow_succeeded = True
    return validated_state
finally:
    if (
        not phone_flow_succeeded
        and getattr(phone_service, "supports_cancellation", False) is True
    ):
        phone_service.cancel_active()
```

Successful flows are not cancelled; terminal cards also remain protected by `cancel_active()`.

- [ ] **Step 5: Run the focused tests and verify GREEN**

Run the command from Task 1 plus the existing LeadBee replacement and cancellation tests. Expected: all pass.

### Task 3: Regression, build, deployment, and live smoke test

**Files:**
- Verify: `tests/test_chatgpt_phone_flow.py`
- Verify: `tests/test_chatgpt_phone_verification.py`
- Verify: backend and frontend suites

- [ ] **Step 1: Run focused backend suites**

```bash
docker run --rm -v "$PWD:/app" -w /app --entrypoint pytest any-auto-register-local-app \
  tests/test_chatgpt_phone_flow.py tests/test_chatgpt_phone_verification.py -q
```

- [ ] **Step 2: Run full backend tests**

```bash
docker run --rm -v "$PWD:/app" -w /app --entrypoint pytest any-auto-register-local-app tests -q
```

- [ ] **Step 3: Run frontend tests and production build**

Use the repository's installed npm scripts from `frontend/package.json`; both test and build commands must exit zero.

- [ ] **Step 4: Rebuild and restart only the local app container**

Use the existing Compose configuration, confirm the health/API endpoints, and do not start the 33-account recovery batch yet.

- [ ] **Step 5: Run one recovery pair as a smoke test**

Use the first entry from the generated 17–49 recovery files. Confirm either successful replacement and token upload or a preserved original OpenAI rejection plus automatic LeadBee cancellation. Only then recommend concurrency 3 with a 2-second start interval.
