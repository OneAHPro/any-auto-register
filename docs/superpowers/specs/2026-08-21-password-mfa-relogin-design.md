# Password + MFA Relogin Design

## Goal

Accounts imported with only an email address and a MailAPI URL must use the
mailbox once to establish a ChatGPT password, then create or rotate a
project-managed TOTP factor. Later relogins must prefer the saved password and
TOTP instead of depending on the MailAPI URL.

## Decisions

- A remote Codex2API `error` row whose `error_message` explicitly contains a
  refresh-token invalidation marker is an authentication failure and enters
  full relogin immediately.
- A legacy `mailapi_url` record with a saved top-level ChatGPT password is
  promoted in memory to password login while retaining MailAPI as an OTP
  fallback.
- A legacy `mailapi_url` record without a ChatGPT password is sent through the
  existing forgot-password flow on its next full login. The generated password
  is committed to mailbox metadata and the ChatGPT account row before the new
  tokens are considered durable.
- Managed TOTP remains the second factor. MailAPI remains stored only as a
  recovery fallback and is not the preferred relogin route once both password
  and TOTP are present.

## Failure handling

- Temporary HTTP, model-support, quota, and rate-limit errors remain deferred.
- Only explicit credential markers such as `refresh_token_invalidated` bypass
  deferral.
- Password reset, TOTP activation, local persistence, and Codex2API replacement
  must all report their own stage. A partial result is never reported as a
  complete relogin.

## Verification

- Unit tests cover explicit 401 classification, legacy password promotion,
  password bootstrap, persistence, and unchanged handling of temporary errors.
- The focused relogin/health suites and the full ChatGPT task suites run before
  deployment.
- Production verification checks service health, deployed commit, account
  classification, and a live task/log observation without exposing secrets.
