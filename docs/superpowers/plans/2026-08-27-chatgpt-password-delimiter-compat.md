# ChatGPT Password Delimiter Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the account-management import accept `邮箱----密码` rows, preserve optional credential metadata, and prevent ChatGPT login jobs from running when the imported account has no usable MFA credential.

**Architecture:** Keep parsing at the account-import API boundary so all callers receive normalized `email`, `password`, and JSON metadata. Reuse the existing delimiter grammar used by the mail-import subsystem rather than adding a second parser. Add a preflight eligibility check to the ChatGPT relogin enqueue path, returning a per-account validation error before browser work when a password-only row cannot satisfy an MFA challenge.

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
    assert account.get_extra() == {}


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

### Task 2: Add ChatGPT MFA preflight for password-only imported rows

**Files:**
- Modify: `api/tasks.py:6452-6475`
- Modify: `services/chatgpt_relogin.py:240-325`
- Test: `tests/test_chatgpt_relogin_task.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_password_only_chatgpt_account_is_rejected_before_enqueue():
    account = AccountModel(
        platform="chatgpt",
        email="user@example.com",
        password="ChatGPT-password",
        extra_json="{}",
    )
    session.add(account)
    session.commit()

    with pytest.raises(HTTPException) as exc:
        create_chatgpt_relogin_task(
            ChatGPTReloginTaskRequest(account_ids=[account.id]),
            background_tasks=BackgroundTasks(),
        )

    assert exc.value.status_code == 400
    assert "MFA" in str(exc.value.detail)


def test_password_totp_chatgpt_account_remains_eligible():
    account = AccountModel(
        platform="chatgpt",
        email="user@example.com",
        password="ChatGPT-password",
        extra_json=json.dumps(
            {"account_type": "chatgpt_password_totp", "totp_secret": "BASE32_SECRET"}
        ),
    )
    session.add(account)
    session.commit()

    with mock.patch("api.tasks.enqueue_chatgpt_relogin_task", return_value="task-1"):
        result = create_chatgpt_relogin_task(
            ChatGPTReloginTaskRequest(account_ids=[account.id]),
            background_tasks=BackgroundTasks(),
        )

    assert result["task_id"] == "task-1"
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `pytest -q tests/test_chatgpt_relogin_task.py -k 'password_only or password_totp'`

Expected: The password-only test fails because the endpoint currently enqueues the browser task; the TOTP test provides the expected compatibility baseline.

- [ ] **Step 3: Implement the preflight check**

Add a pure helper that reads the saved ChatGPT account's `extra_json` and considers the account eligible when it has a managed `totp_secret`/`mfa_secret`/`totp`, a remote `totp_url`, or an existing mailbox context that can provide MFA. Call it only for explicitly supplied `account_ids`; preserve `all_eligible` behavior, which already filters through `list_relogin_eligible_account_ids`. Raise HTTP 400 with the account email and a concise missing-MFA message before creating a task when any selected row is password-only.

- [ ] **Step 4: Run the focused tests to verify they pass**

Run: `pytest -q tests/test_chatgpt_relogin_task.py -k 'password_only or password_totp'`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add api/tasks.py services/chatgpt_relogin.py tests/test_chatgpt_relogin_task.py
git commit -m "fix: preflight missing ChatGPT MFA credentials"
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

- [ ] **Step 3: Request code review against the previous commit**

Review the complete diff from `git diff HEAD~3..HEAD` for parser compatibility, credential leakage, API behavior, and deployment safety. Fix every critical or important finding before proceeding.

- [ ] **Step 4: Push the branch and main, then stage a hashed release artifact**

Create a tarball from the verified commit, calculate SHA-256, copy it to the deployment host, verify the hash and SQLite backup, and stage backend/frontend directories under a unique release name.

- [ ] **Step 5: Activate the release with rollback trap and verify health**

Atomically switch the `current` symlink and frontend directory, restart `any-auto-register.service`, verify systemd active state, loopback API status, public API status, public page status, frontend marker text, and database `PRAGMA quick_check=ok`. If any check fails, execute the rollback trap and report the actual failed check.

- [ ] **Step 6: Commit any final verification-only metadata and report evidence**

Report commit SHA, release path, service state, endpoint status, test counts, and the preserved database backup path.

