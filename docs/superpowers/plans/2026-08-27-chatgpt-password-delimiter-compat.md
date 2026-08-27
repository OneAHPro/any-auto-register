# ChatGPT Password Delimiter Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make both account-management and mailbox-pool imports accept `邮箱----密码` rows, preserve optional credential metadata, and route iCloud password-only rows explicitly without mislabeling them as Google federation.

**Architecture:** Keep parsing at the account-import API boundary so all callers receive normalized `email`, `password`, and JSON metadata. Reuse the existing delimiter grammar used by the mail-import subsystem rather than adding a second parser. Add an explicit `chatgpt_password` pool account type for iCloud two-field rows, carrying the password through the existing ChatGPT login adapter; when the remote account requires TOTP and no factor is supplied, retain the existing precise missing-MFA result.

**Tech Stack:** FastAPI, SQLModel, Python `unittest`/pytest, React + TypeScript + Vite.

---

### Task 1: Lock down delimiter parsing at the account import API

**Files:**
- Modify: `api/accounts.py:90-235`
- Test: `tests/test_accounts_import.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_import_accepts_four_dash_email_password_rows():
    response = import_accounts(
        ImportRequest(
            platform="chatgpt",
            lines=["user@example.com----ChatGPT-password"],
        ),
        session=session,
    )

    assert response == {"created": 1}
    account = session.exec(select(AccountModel)).one()
    assert account.email == "user@example.com"
    assert account.password == "ChatGPT-password"
    assert account.get_extra() == {"account_type": "chatgpt_password"}


def test_import_preserves_json_metadata_after_dash_split():
    response = import_accounts(
        ImportRequest(
            platform="chatgpt",
            lines=[
                'user@example.com---ChatGPT-password---{"account_type":"chatgpt_password_totp","totp_secret":"BASE32_SECRET"}'
            ],
        ),
        session=session,
    )

    assert response == {"created": 1}
    account = session.exec(select(AccountModel)).one()
    assert account.email == "user@example.com"
    assert account.password == "ChatGPT-password"
    assert account.get_extra() == {
        "account_type": "chatgpt_password_totp",
        "totp_secret": "BASE32_SECRET",
    }


def test_import_keeps_passwords_containing_spaces_when_json_is_absent():
    response = import_accounts(
        ImportRequest(
            platform="chatgpt",
            lines=["user@example.com----password with spaces"],
        ),
        session=session,
    )

    assert response == {"created": 1}
    account = session.exec(select(AccountModel)).one()
    assert account.password == "password with spaces"
```

- [ ] **Step 2: Run the focused tests to verify the bug is reproduced**

Run: `pytest -q tests/test_accounts_import.py`

Expected: FAIL because `api.accounts.import_accounts` currently uses whitespace splitting and does not create a row from `邮箱----密码`.

- [ ] **Step 3: Implement the minimal parser change**

Import `split_mail_import_fields` from `core.mail_import_delimiters`, parse each non-empty line with it, use the first two normalized fields as email/password, and join any remaining fields with a single space before JSON decoding. Keep whitespace-only legacy rows working and continue skipping rows with fewer than two fields.

- [ ] **Step 4: Run the focused tests to verify the parser passes**

Run: `pytest -q tests/test_accounts_import.py`

Expected: PASS with 3 tests.

- [ ] **Step 5: Commit**

```bash
git add api/accounts.py tests/test_accounts_import.py
git commit -m "fix: accept dash-delimited account imports"
```

### Task 2: Preserve iCloud password-only credentials through the ChatGPT login adapter

**Files:**
- Modify: `services/chatgpt_relogin.py:240-325`
- Modify: `core/applemail_pool.py:292-470`
- Modify: `core/base_mailbox.py:659-1060`
- Modify: `platforms/chatgpt/plugin.py:155-620`
- Modify: `platforms/chatgpt/refresh_token_registration_engine.py:580-690`
- Modify: `services/mail_imports/auto_detection.py:260-280`
- Modify: `services/mail_imports/providers.py:90-115`
- Modify: `services/mail_imports/schemas.py:10-25`
- Test: `tests/test_icloud_mailbox.py`, `tests/test_chatgpt_relogin.py`, `tests/test_chatgpt_existing_account_login.py`, `tests/test_chatgpt_plugin.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_icloud_two_field_pool_row_is_classified_as_chatgpt_password():
    records = parse_applemail_pool_content("user@icloud.com----ChatGPT-password")
    assert records[0]["account_type"] == "chatgpt_password"


def test_saved_chatgpt_password_context_builds_without_mailbox_receiver():
    saved = {
        "email": "user@icloud.com",
        "password": "ChatGPT-password",
        "mailbox_context": {
            "provider": "chatgpt_credentials",
            "email": "user@icloud.com",
            "extra": {
                "account_type": "chatgpt_password",
                "password": "ChatGPT-password",
            },
        },
    }
    service = _build_email_service(saved, {}, log_fn=None)
    assert service.create_email()["account_type"] == "chatgpt_password"
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `pytest -q tests/test_chatgpt_relogin_task.py -k 'password_only or password_totp'`

Expected: The focused tests fail because two-field iCloud rows are currently labeled as Google federation and the saved-account adapter does not preserve a direct ChatGPT password type.

- [ ] **Step 3: Implement the preflight check**

Classify two-field iCloud rows as `chatgpt_password`, add that account type to the mailbox strategy and snapshot schema, and carry it through `GenericEmailService`, `_load_saved_account`, `_build_email_service`, and the registration engine. Keep the existing missing-TOTP error when OpenAI returns an MFA challenge without a supplied factor; no factor is fabricated.

- [ ] **Step 4: Run the focused tests to verify they pass**

Run: `PYTHONPATH=. ./.venv/bin/pytest -q tests/test_icloud_mailbox.py tests/test_chatgpt_relogin.py tests/test_chatgpt_existing_account_login.py tests/test_chatgpt_plugin.py -k 'chatgpt_password or direct_password'`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add core/applemail_pool.py core/base_mailbox.py services/chatgpt_relogin.py platforms/chatgpt/plugin.py platforms/chatgpt/refresh_token_registration_engine.py services/mail_imports/providers.py services/mail_imports/schemas.py services/mail_imports/auto_detection.py tests/test_icloud_mailbox.py tests/test_chatgpt_relogin.py tests/test_chatgpt_existing_account_login.py tests/test_chatgpt_plugin.py
git commit -m "fix: preserve iCloud password-only ChatGPT imports"
```

### Task 3: Update the account import UI and regression coverage

**Files:**
- Modify: `frontend/src/pages/Accounts.tsx:1059-1077,2140-2157`
- Test: `frontend/src/pages/Accounts.test.tsx` (create if missing)

- [ ] **Step 1: Write the failing UI test**

```tsx
it('documents dash-delimited email and password imports', async () => {
  render(<Accounts platform="chatgpt" />)
  await user.click(screen.getByRole('button', { name: '导入' }))
  expect(screen.getByText(/email----password/)).toBeTruthy()
})
```

- [ ] **Step 2: Run the focused UI test to verify it fails**

Run: `pnpm --dir frontend vitest run src/pages/Accounts.test.tsx -t 'dash-delimited'`

Expected: FAIL because the modal currently documents only whitespace-delimited fields.

- [ ] **Step 3: Implement the UI compatibility copy and normalization**

Update the helper text to show `邮箱----密码` and optional JSON metadata. Normalize textarea line endings with `split(/\\r?\\n/)`, trim blank lines, and leave delimiter content untouched for the backend parser. If the response contains a `skipped` count or errors, show the created count plus the skipped count instead of claiming every submitted line was imported.

- [ ] **Step 4: Run the focused UI test and production typecheck/build**

Run: `pnpm --dir frontend vitest run src/pages/Accounts.test.tsx -t 'dash-delimited'`

Expected: PASS.

Run: `pnpm --dir frontend build`

Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/Accounts.tsx frontend/src/pages/Accounts.test.tsx
git commit -m "fix: document dash-delimited account imports"
```

### Task 4: Full verification, review, and deployment

**Files:**
- Modify: none unless verification finds an issue

- [ ] **Step 1: Run the complete backend regression suite**

Run: `pytest -q`

Expected: all tests pass with no unexpected failures.

- [ ] **Step 2: Run the complete frontend regression suite and build**

Run: `pnpm --dir frontend vitest run`

Expected: all frontend tests pass.

Run: `pnpm --dir frontend build`

Expected: exit 0.

- [ ] **Step 3: Request code review against the implementation commits**

Review the complete implementation diff from the plan commit through `HEAD` for parser compatibility, credential leakage, API behavior, and deployment safety. Fix every critical or important finding before proceeding.

- [ ] **Step 4: Push the branch and main, then stage a hashed release artifact**

Create a tarball from the verified commit, calculate SHA-256, copy it to the deployment host, verify the hash and SQLite backup, and stage backend/frontend directories under a unique release name.

- [ ] **Step 5: Activate the release with rollback trap and verify health**

Atomically switch the `current` symlink and frontend directory, restart `any-auto-register.service`, verify systemd active state, loopback API status, public API status, public page status, frontend marker text, and database `PRAGMA quick_check=ok`. If any check fails, execute the rollback trap and report the actual failed check.

- [ ] **Step 6: Commit any final verification-only metadata and report evidence**

Report commit SHA, release path, service state, endpoint status, test counts, and the preserved database backup path.
