# Unified OAuth Recovery Design

## Goal

Make ChatGPT manual login, automatic re-login, and post-MFA phone verification recover from expired OpenAI authorization sessions without reusing stale OAuth transactions or starting a phone provider before a resumable transaction is proven.

## Evidence and root cause

Production release `3b3709a` still reports `authorize_continue -> HTTP 409` with OpenAI's `sign-in session is no longer valid` response. The OAuth retry loop recreates the HTTP session and device id, but its PKCE verifier, OAuth state, and authorize parameters are created outside the retry loop, so each attempt submits the same expired transaction. The Web login path returns the same 409 immediately from its state machine. After managed MFA rotation, phone OAuth preparation retries by cloning the same authenticated browser session; once that session is invalidated, all attempts remain on `log_in` or reach a 403 page.

## Design

1. `OAuthClient.login_and_get_tokens` creates a fresh PKCE verifier, OAuth state, authorize parameters, and authorize URL for every non-prepared entry attempt. A 409/session-invalid response is classified as recoverable entry failure and causes a full session, fingerprint, and transaction restart.
2. `ChatGPTClient.login_existing_account_and_get_session` treats a transient `authorize_continue` failure like a failed bootstrap attempt: it discards the helper/session and restarts from the ChatGPT home entry, bounded by the existing six-attempt limit.
3. The post-MFA phone path first performs the existing bounded same-session rebuild. If that cannot produce a valid `add_phone`-compatible context, it performs one fresh credential login using the newly managed TOTP/recovery code and prepares phone OAuth in that new authenticated session. The fresh fallback never starts LeadBee and is accepted only after the resume-context validator passes.
4. Automatic re-login defaults to three concurrent accounts. The setting remains configurable but is bounded to three for the automatic OAuth entry path, reducing simultaneous Sentinel and authorize requests while keeping account work parallel.
5. The provider-start gate remains strict: no LeadBee/SMS reservation occurs without a version-2 resume context containing PKCE verifier, OAuth state, and flow state.

## Invariants

- A retry never reuses PKCE verifier, OAuth state, authorize parameters, or a browser session after a session-invalid response.
- A stale pre-MFA-rotation phone transaction is never consumed after rotation.
- Password, Access Token, Refresh Token, managed TOTP, and recovery code remain persisted when phone OAuth preparation is deferred.
- No provider order, SMS reservation, or exchange-code consumption occurs before a validated phone OAuth context exists.
- Diagnostics contain only stage, attempt, page type, HTTP status, and recovery status; no credentials, cookies, tokens, or PKCE secrets.

## Verification

- RED/GREEN regression tests cover fresh PKCE generation per retry, Web 409 restart, post-MFA fresh-login fallback, and provider-start gating.
- Focused OAuth, phone, retry, and automation tests pass before the full suite.
- Full test, compile, and diff checks pass before commit.
- Production receives a SQLite backup and immutable release. The same 20-account manifest is checked before and after deployment without printing secrets.
