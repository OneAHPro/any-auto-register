# MailAPI Password Commit Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix password persistence after a Microsoft/MailAPI mailbox is consumed, restore account 1377 from its verified in-process mailbox context, deploy, and complete one real re-login.

**Architecture:** Treat a consumed Outlook mailbox row as a disabled durable credential checkpoint when a remote password reset succeeds. Preserve the original Microsoft backend when a persisted MailAPI context is forced through password reset, then restore only the verified mailbox/TOTP context for account 1377 and let the normal re-login flow generate and persist the real password and fresh tokens.

**Tech Stack:** Python 3.10, FastAPI services, SQLModel/SQLite, unittest/pytest, systemd release deployment.

---

### Task 1: Persist a reset password after the Outlook row is consumed

**Files:**
- Modify: `tests/test_outlook_mailbox_oauth.py`
- Modify: `core/base_mailbox.py:5535`

- [ ] **Step 1: Write the failing consumed-row test**

Add this test to `OutlookMailboxOAuthTests`:

```python
def test_claimed_mailapi_account_persists_reset_password_as_disabled(self):
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(test_engine)
    with Session(test_engine) as session:
        session.add(
            OutlookAccountModel(
                email="claimed-reset@example.com",
                password="",
                account_type="mailapi_url",
                mailapi_url="https://mail.example.test/claimed",
            )
        )
        session.commit()

    mailbox = OutlookMailbox()
    with mock.patch("core.db.engine", test_engine):
        claimed = mailbox.get_email_by_address("claimed-reset@example.com")
        self.assertTrue(
            mailbox.commit_password_reset(
                claimed,
                "Replacement-Password-2026!",
            )
        )

    with Session(test_engine) as session:
        saved = session.exec(
            select(OutlookAccountModel).where(
                OutlookAccountModel.email == "claimed-reset@example.com"
            )
        ).one()
    self.assertEqual(saved.password, "Replacement-Password-2026!")
    self.assertEqual(saved.account_type, "mailapi_url")
    self.assertEqual(saved.mailapi_url, "https://mail.example.test/claimed")
    self.assertFalse(saved.enabled)
    self.assertEqual(claimed.extra["password"], "Replacement-Password-2026!")
    self.assertFalse(claimed.extra["password_reset_required"])
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
python -m pytest tests/test_outlook_mailbox_oauth.py::OutlookMailboxOAuthTests::test_claimed_mailapi_account_persists_reset_password_as_disabled -q
```

Expected: FAIL because `commit_password_reset()` returns `False` after `get_email_by_address()` deletes the row.

- [ ] **Step 3: Implement the missing-row upsert**

Replace the missing-row return in `OutlookMailbox.commit_password_reset()` with a disabled insert and normalize the active account context:

```python
extra = dict(getattr(account, "extra", None) or {})
with self._lock:
    with Session(engine) as session:
        existing = session.exec(
            select(OutlookAccountModel).where(
                OutlookAccountModel.email == email
            )
        ).first()
        if existing is None:
            existing = OutlookAccountModel(
                email=email,
                password=password,
                client_id=str(extra.get("client_id") or ""),
                refresh_token=str(extra.get("refresh_token") or ""),
                account_type=self._normalize_account_type(
                    extra.get("account_type")
                ),
                mailapi_url=str(extra.get("mailapi_url") or ""),
                enabled=False,
                created_at=_utcnow(),
                updated_at=_utcnow(),
            )
        else:
            existing.password = password
            existing.updated_at = _utcnow()
        session.add(existing)
        session.commit()
if not isinstance(getattr(account, "extra", None), dict):
    account.extra = {}
account.extra["password"] = password
account.extra["password_reset_required"] = False
account.extra.pop("new_password", None)
return True
```

- [ ] **Step 4: Verify GREEN and failure requeue behavior**

Run:

```bash
python -m pytest tests/test_outlook_mailbox_oauth.py -q
```

Expected: all Outlook mailbox tests pass.

### Task 2: Keep Microsoft MailAPI reset contexts on the Outlook backend

**Files:**
- Modify: `tests/test_chatgpt_relogin.py`
- Modify: `services/chatgpt_relogin.py:1280`

- [ ] **Step 1: Write the failing backend-routing test**

Add this test to the existing ChatGPT re-login test class:

```python
def test_microsoft_mailapi_password_bootstrap_keeps_outlook_backend(self):
    saved = {
        "email": "microsoft-reset@example.com",
        "password": "",
        "extra": {},
        "mailbox_context": {
            "provider": "microsoft",
            "email": "microsoft-reset@example.com",
            "account_id": "1377",
            "extra": {
                "account_type": "mailapi_url",
                "mailapi_url": "https://mail.example.test/messages",
                "totp_secret": "JBSWY3DPEHPK3PXP",
                "mfa_recovery_code": "RECOVERY-CODE",
                "chatgpt_mfa_managed": True,
            },
        },
    }
    mailbox = mock.Mock()
    mailbox.get_current_ids.return_value = set()

    with mock.patch(
        "services.chatgpt_relogin.create_mailbox",
        return_value=mailbox,
    ) as create_mailbox_mock:
        service = _build_email_service(
            saved,
            {},
            log_fn=None,
            force_password_reset=True,
        )

    self.assertEqual(create_mailbox_mock.call_args.args[0], "microsoft")
    email_info = service.create_email()
    self.assertEqual(
        email_info["account_type"],
        "chatgpt_password_reset_url_mail",
    )
    self.assertTrue(email_info["password_reset_required"])
    self.assertEqual(email_info["totp_secret"], "JBSWY3DPEHPK3PXP")
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
python -m pytest tests/test_chatgpt_relogin.py -k microsoft_mailapi_password_bootstrap_keeps_outlook_backend -q
```

Expected: FAIL because the current URL-reset branch calls `create_mailbox("applemail", ...)`.

- [ ] **Step 3: Select the backend from the persisted provider**

In the URL-reset branch of `_build_email_service()`, replace the hard-coded backend with:

```python
persisted_provider = _text(mailbox_context.get("provider")).lower()
mailbox_provider = (
    "microsoft"
    if persisted_provider in {"microsoft", "outlook"}
    else "applemail"
)
mailbox = create_mailbox(
    mailbox_provider,
    extra=mailbox_config,
    proxy=proxy,
)
```

Keep the existing AppleMail pool-file lookup unchanged when `credentials["pool_file"]` is present.

- [ ] **Step 4: Verify GREEN and run focused regression tests**

Run:

```bash
python -m pytest tests/test_chatgpt_relogin.py -k "microsoft_mailapi_password_bootstrap or passwordless_legacy_mailapi or used_reset_url" -q
```

Expected: the new Microsoft test and existing AppleMail URL tests all pass.

### Task 3: Run the full relevant suite and commit the implementation

**Files:**
- Verify: `core/base_mailbox.py`
- Verify: `services/chatgpt_relogin.py`
- Verify: `tests/test_outlook_mailbox_oauth.py`
- Verify: `tests/test_chatgpt_relogin.py`

- [ ] **Step 1: Run relevant suites**

Run:

```bash
python -m pytest \
  tests/test_outlook_mailbox_oauth.py \
  tests/test_chatgpt_relogin.py \
  tests/test_mail_imports_service.py -q
```

Expected: zero failures.

- [ ] **Step 2: Check the diff**

Run:

```bash
git diff --check
git diff --stat
```

Expected: no whitespace errors; changes are limited to the two implementation files and their tests.

- [ ] **Step 3: Commit**

Run:

```bash
git add core/base_mailbox.py services/chatgpt_relogin.py \
  tests/test_outlook_mailbox_oauth.py tests/test_chatgpt_relogin.py
git commit -m "fix: persist consumed mailapi password resets"
```

### Task 4: Deploy, restore account 1377, and perform one real re-login

**Files:**
- Deploy: `/www/any-auto-register/releases/mailapi-password-commit-$(date -u +%Y%m%d-%H%M%S)`
- Backup: `/www/any-auto-register/shared/backups/account_manager-before-1377-recovery-$(date -u +%Y%m%d-%H%M%S).db`
- Modify data: `/www/any-auto-register/shared/data/account_manager.db`

- [ ] **Step 1: Push the implementation commit**

Run:

```bash
git push origin HEAD:main
```

Expected: `origin/main` advances to the tested implementation commit.

- [ ] **Step 2: Back up the live SQLite database before touching the old process**

Use `sqlite3.Connection.backup()` while the service is live, save the backup under `shared/backups`, set mode `0600`, and require `PRAGMA quick_check` to return `ok` for both live and backup databases. Store the exact backup path in the deployment log.

- [ ] **Step 3: Recover, validate, and persist the in-process mailbox context before restart**

Scan readable mappings of the current main Python process for the exact target email, URL signature, `mailbox_login_context`, `totp_secret`, and `mfa_recovery_code`. Parse the containing JSON object and require:

```python
assert context["email"] == "m82e48a8ff7f3f71b8358@o6f4.my"
assert "7c592c4c37981d020842f63163be9fc6" in context["extra"]["mailapi_url"]
assert len(context["extra"]["totp_secret"]) == 32
assert hashlib.sha256(
    context["extra"]["totp_secret"].encode()
).hexdigest().startswith("85fc155fba1e59f1")
assert len(context["extra"]["mfa_recovery_code"]) == 30
assert hashlib.sha256(
    context["extra"]["mfa_recovery_code"].encode()
).hexdigest().startswith("dc542f8067f2a0c5")
```

Do not print the recovered values. In the same Python process, insert a single `AccountModel` with ID 1377 only if that ID and email are absent:

```python
account = AccountModel(
    id=1377,
    platform="chatgpt",
    email=context["email"],
    password="",
    user_id="",
    token="",
    status="registered",
)
account.set_extra({"mailbox_login_context": context})
session.add(account)
session.commit()
```

Validate `is_saved_chatgpt_account_relogin_eligible(1377)` is `True` and never print secret fields.

- [ ] **Step 4: Create a release and atomically switch production**

Create the release name once, stream the tested Git tree to it, set ownership, switch the symlink, and restart:

```bash
release_name="mailapi-password-commit-$(date -u +%Y%m%d-%H%M%S)"
release_dir="/www/any-auto-register/releases/$release_name"
mkdir -p "$release_dir"
tar -xf /tmp/any-auto-register-release.tar -C "$release_dir"
chown -R any-auto-register:any-auto-register "$release_dir"
ln -sfn "$release_dir" /www/any-auto-register/current.next
mv -Tf /www/any-auto-register/current.next /www/any-auto-register/current
systemctl restart any-auto-register
systemctl is-active any-auto-register
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18081/
```

The local side creates `/tmp/any-auto-register-release.tar` with `git archive --format=tar HEAD` and transfers it before these commands. Expected health output: `active` and `200`.

- [ ] **Step 5: Execute exactly one real re-login**

With automatic re-login temporarily disabled and its previous setting recorded, call:

```python
from services.chatgpt_relogin import relogin_chatgpt_account

result = relogin_chatgpt_account(
    1377,
    log_fn=lambda message: print(message),
    rotate_mfa=False,
)
assert result.get("ok") is True, result
```

Restore the previous automatic re-login setting after the call, even on failure. Do not retry the call in the same deployment run.

- [ ] **Step 6: Verify final state**

Require all of the following:

```python
assert account.password
assert len(account.password) >= 12
assert account.token
assert account.status == "registered"
assert context_extra["totp_secret"]
assert context_extra["mfa_recovery_code"]
assert outlook_row.enabled is False
assert outlook_row.password == account.password
```

Also require live and backup `PRAGMA quick_check = ok`, service `active`, application HTTP `200`, and `NRestarts = 0` after the deployment restart settles.

- [ ] **Step 7: Remove the temporary feature branch after main is verified**

Switch the worktree to detached `origin/main`, delete `codex/fix-mailapi-password-commit` locally and remotely if it exists, and confirm no temporary recovery file contains raw TOTP or recovery-code material.
