# 订单前 OAuth 重试 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 LeadBee 下单前自动恢复一次失效的 ChatGPT 手机 OAuth 会话，并保证不会重复扣费或创建重复订单。

**Architecture:** 手机验证服务使用专用的订单前异常；manager 只在 `provider_started=False` 时重试一次。API 模式优先用已保存的密码/MFA或邮箱凭据重新登录并建立新的 OAuth 事务，凭据恢复不适用时再从认证浏览器快照生成新 PKCE。provider 锁和容量租约跨两次 runner 调用保持到最终结算。

**Tech Stack:** Python 3.11, threading, unittest/pytest, FastAPI task runtime。

---

### Task 1: Add failing regression tests

**Files:**
- Modify: `tests/test_chatgpt_phone_verification.py`

- [x] Test that a dedicated pre-provider OAuth exception is retried once and succeeds on the second runner call.
- [x] Test that a second pre-provider failure stops after two total calls and reports that no API order was created.
- [x] Test that retry state forces a fresh PKCE transaction instead of reusing the old prepared context.
- [x] Test that API retry prefers a fresh saved-account login context.
- [x] Test that provider-started failures are not retried and captured order diagnostics are not overwritten.
- [x] Run the focused tests before implementation and confirm the expected failures.

### Task 2: Implement bounded pre-order recovery

**Files:**
- Modify: `services/chatgpt_phone_verification.py`

- [x] Add `PhoneOAuthPreProviderError` and broker retry state.
- [x] Publish fixed, secret-free OAuth retry diagnostics and progress logs.
- [x] Retry the automatic runner once while keeping provider cleanup outside the retry loop.
- [x] Re-login from persisted account credentials in API mode, with browser-snapshot recovery as fallback.
- [x] Convert OAuth context/login failures into the dedicated exception only before provider start.
- [x] Publish a precise terminal message when the retry is exhausted before any order exists.

### Task 3: Verify and integrate

**Files:**
- Verify: `services/chatgpt_phone_verification.py`
- Verify: `tests/test_chatgpt_phone_verification.py`

- [x] Run focused phone, login, retry-binding, and re-login suites.
- [x] Run the complete pytest suite.
- [x] Run `git diff --check` and review the final diff.
- [ ] Commit and push the feature branch, fast-forward `main`, and push `main`.
- [ ] Deploy the exact `main` commit to `/www/any-auto-register` and verify service health.
