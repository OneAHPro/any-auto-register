# Post-MFA Phone OAuth Recovery Design

## Goal

Make existing-account login-and-phone tasks survive OpenAI session variation after managed MFA rotation without creating a LeadBee order until a fresh phone OAuth transaction is proven resumable.

## Root cause

The access-token login path currently prepares a phone OAuth transaction before managed MFA rotation. Rotation then deliberately discards that transaction and preserves only an authenticated browser snapshot. The phone worker later tries to reconstruct the transaction; if OpenAI returns a login page, a 403 page, or a web session without `accessToken`, the single recovery attempt ends the task before LeadBee is called.

This is not a LeadBee failure: failed runs have `provider_started=false`, no order, and no consumed exchange code.

## Chosen design

When managed MFA rotation is required, the initial web login will not prepare a disposable phone transaction. Immediately after rotation, while the authenticated `ChatGPTClient` session is still in memory, the registration engine will create fresh `OAuthClient` helpers and attempt to prepare a phone OAuth transaction from that exact session. A result is accepted only when the existing validator returns a resumable state such as `add_phone`.

The engine will make a bounded number of same-session attempts with short backoff. On success it stores the new resume context in memory and result metadata. On exhaustion it returns the already authenticated partial account with an explicit not-ready diagnostic; the task saves the Access Token and managed MFA but does not start LeadBee. The later phone recovery path remains a fallback for old persisted accounts, not the primary post-rotation path.

## Invariants

- LeadBee order creation remains downstream of a validated phone OAuth context.
- A stale pre-rotation PKCE transaction is never reused after MFA changes.
- Password, Access Token, TOTP, and recovery code remain durable even if phone OAuth preparation fails.
- No raw cookie, PKCE verifier, token, password, or recovery code is written to logs.
- Accounts that do not rotate MFA keep the current preparation path.
- Mandatory MFA enrollment produced during login is not rotated twice.

## Verification

- A regression test must first demonstrate that the current code clears the prepared context and never attempts same-session rebuilding.
- Tests cover first-attempt success, transient failure followed by success, and bounded exhaustion without LeadBee/provider start.
- Existing login, MFA, phone verification, retry-binding, and full test suites must pass.
- Production verification uses the existing partial accounts first; success requires phone verification, Refresh Token persistence, Codex2API upload, and a healthy automatic monitor cycle.
