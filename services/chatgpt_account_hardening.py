"""Durable password and TOTP hardening for saved ChatGPT accounts."""

from __future__ import annotations

import json
import secrets
import sqlite3
import string
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from sqlmodel import Session, select

from core.db import (
    AccountModel,
    ChatGPTAttemptBindingModel,
    claim_chatgpt_account_hardening,
    engine,
    promote_chatgpt_mfa_secret,
    update_chatgpt_account_hardening,
)
from platforms.chatgpt.account_hardening import (
    ChatGPTMFAClient,
    MFAInventory,
    generate_totp,
    normalize_totp_secret,
)


PasswordResetCallback = Callable[[AccountModel, str], bool]
CandidateValidator = Callable[[AccountModel, str], bool]


@dataclass(frozen=True)
class ChatGPTAccountHardeningResult:
    account_id: int
    email: str
    status: str
    outcome: str
    changed: bool = False
    message: str = ""


def generate_account_password(length: int = 24) -> str:
    """Generate one strong, non-shared password without external services."""
    size = max(int(length or 24), 20)
    required = [
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.digits),
        secrets.choice("!@#$%^&*_-+="),
    ]
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*_-+="
    required.extend(secrets.choice(alphabet) for _ in range(size - len(required)))
    secrets.SystemRandom().shuffle(required)
    return "".join(required)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _candidate_from_mapping(mapping: Any) -> list[str]:
    if not isinstance(mapping, dict):
        return []
    values = []
    for key in (
        "mfa_pending_secret",
        "totp_secret",
        "mfa_secret",
        "totpSecret",
        "mfaSecret",
        "totp",
    ):
        value = _text(mapping.get(key))
        if value:
            values.append(value)
    return values


class ChatGPTAccountHardeningService:
    """One idempotent state machine shared by login hooks and batch tasks."""

    def __init__(
        self,
        *,
        database_engine=None,
        mfa_client_factory=None,
        password_reset_callback: PasswordResetCallback | None = None,
        candidate_validator: CandidateValidator | None = None,
        backup_paths: Iterable[str] = (),
    ) -> None:
        self._engine = database_engine or engine
        self._mfa_client_factory = mfa_client_factory or ChatGPTMFAClient
        self._password_reset_callback = password_reset_callback
        self._candidate_validator = candidate_validator
        self._backup_paths = tuple(
            str(path) for path in (backup_paths or ()) if str(path or "").strip()
        )

    def _load_account(self, account_id: int) -> AccountModel:
        with Session(self._engine) as session:
            account = session.get(AccountModel, int(account_id))
            if account is None or _text(account.platform).lower() != "chatgpt":
                raise RuntimeError("ChatGPT account does not exist")
            return AccountModel(**account.model_dump())

    @staticmethod
    def classify_account(
        account: AccountModel,
        inventory: MFAInventory | None = None,
        *,
        has_candidate: bool | None = None,
        supports_safe_replacement: bool = False,
    ) -> str:
        extra = dict(account.get_extra() or {})
        if (
            _text(account.password)
            and _text(extra.get("totp_secret"))
            and _text(extra.get("mfa_hardening_status")) == "ready"
        ):
            return "ready"
        if not _text(account.password):
            return "needs_password"
        if _text(extra.get("mfa_pending_secret")):
            return "recoverable_mfa"
        if inventory is None or not inventory.has_totp:
            return "needs_mfa"
        if has_candidate:
            return "recoverable_mfa"
        if supports_safe_replacement:
            return "replacement_candidate"
        return "missing_mfa_material"

    def _make_client(
        self,
        account: AccountModel,
        *,
        access_token: str = "",
        account_identity: str = "",
    ):
        extra = dict(account.get_extra() or {})
        token = _text(
            access_token
            or extra.get("access_token")
            or extra.get("accessToken")
            or account.token
        )
        if not token:
            raise RuntimeError("ChatGPT access token is missing")
        remote_account_id = _text(
            account_identity
            or extra.get("account_id")
            or extra.get("chatgpt_account_id")
            or account.user_id
        )
        return self._mfa_client_factory(
            access_token=token,
            account_id=remote_account_id,
            proxy=_text(extra.get("proxy_used")),
        )

    def _pool_candidates(self, account: AccountModel) -> list[str]:
        extra = dict(account.get_extra() or {})
        mailbox_context = extra.get("mailbox_login_context")
        context_extra = (
            dict(mailbox_context.get("extra") or {})
            if isinstance(mailbox_context, dict)
            else {}
        )
        pool_file = _text(context_extra.get("pool_file"))
        if not pool_file:
            return []
        try:
            from core.applemail_pool import load_applemail_pool_records
            from core.config_store import config_store

            config = dict(config_store.get_all() or {})
            _path, records = load_applemail_pool_records(
                pool_file=pool_file,
                pool_dir=_text(config.get("applemail_pool_dir")) or "mail",
            )
        except Exception:
            return []
        email = _text(account.email).lower()
        values: list[str] = []
        for record in records:
            if _text(record.get("email")).lower() == email:
                values.extend(_candidate_from_mapping(record))
        return values

    def _binding_candidates(self, account: AccountModel) -> list[str]:
        email = _text(account.email).lower()
        values: list[str] = []
        try:
            with Session(self._engine) as session:
                rows = session.exec(
                    select(ChatGPTAttemptBindingModel).where(
                        ChatGPTAttemptBindingModel.email == account.email
                    )
                ).all()
        except Exception:
            return []
        for row in rows:
            try:
                context = json.loads(row.mailbox_context_json or "{}")
            except Exception:
                continue
            if _text(context.get("email") or email).lower() != email:
                continue
            values.extend(_candidate_from_mapping(context))
            values.extend(_candidate_from_mapping(context.get("extra")))
        return values

    def _backup_candidates(self, account: AccountModel) -> list[str]:
        email = _text(account.email).lower()
        values: list[str] = []
        for raw_path in self._backup_paths:
            path = Path(raw_path).expanduser()
            if not path.is_file():
                continue
            connection = None
            try:
                connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                rows = connection.execute(
                    "SELECT extra_json FROM accounts "
                    "WHERE lower(email) = ? AND lower(platform) = 'chatgpt'",
                    (email,),
                ).fetchall()
                for (raw_extra,) in rows:
                    try:
                        backup_extra = json.loads(raw_extra or "{}")
                    except Exception:
                        continue
                    values.extend(_candidate_from_mapping(backup_extra))
                    mailbox = backup_extra.get("mailbox_login_context")
                    if isinstance(mailbox, dict):
                        values.extend(_candidate_from_mapping(mailbox))
                        values.extend(_candidate_from_mapping(mailbox.get("extra")))
            except Exception:
                continue
            finally:
                if connection is not None:
                    connection.close()
        return values

    def discover_candidate_secrets(self, account: AccountModel) -> list[str]:
        extra = dict(account.get_extra() or {})
        mailbox = extra.get("mailbox_login_context")
        raw_values = _candidate_from_mapping(extra)
        if isinstance(mailbox, dict):
            raw_values.extend(_candidate_from_mapping(mailbox))
            raw_values.extend(_candidate_from_mapping(mailbox.get("extra")))
        raw_values.extend(self._pool_candidates(account))
        raw_values.extend(self._binding_candidates(account))
        raw_values.extend(self._backup_candidates(account))
        normalized: list[str] = []
        for value in raw_values:
            try:
                candidate = normalize_totp_secret(value)
            except ValueError:
                continue
            if candidate not in normalized:
                normalized.append(candidate)
        return normalized

    @staticmethod
    def _result(
        account: AccountModel,
        status: str,
        outcome: str,
        *,
        changed: bool = False,
        message: str = "",
    ) -> ChatGPTAccountHardeningResult:
        return ChatGPTAccountHardeningResult(
            account_id=int(account.id or 0),
            email=_text(account.email),
            status=status,
            outcome=outcome,
            changed=changed,
            message=_text(message),
        )

    def _release(
        self,
        account: AccountModel,
        *,
        owner: str,
        status: str,
        error: Exception | None = None,
    ) -> AccountModel:
        updates = {"mfa_hardening_status": status}
        if error is not None:
            updates["mfa_hardening_error"] = type(error).__name__
        updated = update_chatgpt_account_hardening(
            int(account.id or 0),
            expected_email=account.email,
            expected_created_at=account.created_at,
            expected_updated_at=account.updated_at,
            owner=owner,
            extra_updates=updates,
            release_owner=True,
            database_engine=self._engine,
        )
        return updated or account

    def _default_candidate_validator(
        self,
        account: AccountModel,
        candidate: str,
    ) -> bool:
        if not _text(account.password):
            return False
        try:
            from services.chatgpt_relogin import _login_with_saved_credentials

            extra = dict(account.get_extra() or {})
            mailbox_context = {
                "provider": "chatgpt_credentials",
                "email": account.email,
                "account_id": account.email,
                "extra": {
                    "provider": "chatgpt_credentials",
                    "account_type": "chatgpt_password_totp",
                    "password": account.password,
                    "totp_secret": candidate,
                },
            }
            tokens = _login_with_saved_credentials(
                {
                    "id": int(account.id or 0),
                    "email": account.email,
                    "created_at": account.created_at,
                    "updated_at": account.updated_at,
                    "password": account.password,
                    "user_id": account.user_id,
                    "extra": extra,
                    "mailbox_context": mailbox_context,
                }
            )
            return bool(_text(tokens.get("access_token")))
        except Exception:
            return False

    def harden_authenticated_account(
        self,
        account_id: int,
        *,
        access_token: str = "",
        account_identity: str = "",
        dry_run: bool = False,
        owner: str = "",
    ) -> ChatGPTAccountHardeningResult:
        return self.harden_saved_account(
            account_id,
            access_token=access_token,
            account_identity=account_identity,
            dry_run=dry_run,
            owner=owner,
        )

    def harden_saved_account(
        self,
        account_id: int,
        *,
        access_token: str = "",
        account_identity: str = "",
        dry_run: bool = False,
        owner: str = "",
    ) -> ChatGPTAccountHardeningResult:
        account = self._load_account(account_id)
        local_state = self.classify_account(account)
        if local_state == "ready":
            return self._result(account, "ready", "ready_before")
        if dry_run and local_state == "needs_password":
            return self._result(account, "needs_password", "pending_password")

        if dry_run:
            inventory = self._make_client(
                account,
                access_token=access_token,
                account_identity=account_identity,
            ).get_inventory()
            candidates = self.discover_candidate_secrets(account)
            state = self.classify_account(
                account,
                inventory,
                has_candidate=bool(candidates),
            )
            outcome = {
                "recoverable_mfa": "recovered_secret",
                "missing_mfa_material": "missing_mfa_material",
                "needs_mfa": "hardened",
            }.get(state, "ready_before")
            return self._result(account, state, outcome)

        claim_owner = _text(owner) or f"hardening-{uuid.uuid4()}"
        claimed = claim_chatgpt_account_hardening(
            int(account.id or 0),
            expected_email=account.email,
            expected_created_at=account.created_at,
            expected_updated_at=account.updated_at,
            owner=claim_owner,
            database_engine=self._engine,
        )
        if claimed is None:
            return self._result(account, "busy", "failed", message="account changed")
        account = claimed

        if not _text(account.password):
            new_password = generate_account_password()
            reset_callback = self._password_reset_callback
            try:
                reset_ok = bool(
                    reset_callback(account, new_password)
                    if callable(reset_callback)
                    else False
                )
            except Exception as exc:
                account = self._release(
                    account,
                    owner=claim_owner,
                    status="pending_password",
                    error=exc,
                )
                return self._result(
                    account,
                    "pending_password",
                    "pending_password",
                    message=type(exc).__name__,
                )
            if not reset_ok:
                account = self._release(
                    account,
                    owner=claim_owner,
                    status="pending_password",
                )
                return self._result(
                    account,
                    "pending_password",
                    "pending_password",
                )
            updated = update_chatgpt_account_hardening(
                int(account.id or 0),
                expected_email=account.email,
                expected_created_at=account.created_at,
                expected_updated_at=account.updated_at,
                owner=claim_owner,
                extra_updates={"mfa_hardening_status": "needs_mfa"},
                password=new_password,
                database_engine=self._engine,
            )
            if updated is None:
                return self._result(account, "busy", "failed", message="account changed")
            account = updated

        try:
            client = self._make_client(
                account,
                access_token=access_token,
                account_identity=account_identity,
            )
            inventory = client.get_inventory()
            extra = dict(account.get_extra() or {})
            pending_secret = _text(extra.get("mfa_pending_secret"))
            if inventory.has_totp:
                candidates = self.discover_candidate_secrets(account)
                verified = ""
                if pending_secret:
                    try:
                        verified = normalize_totp_secret(pending_secret)
                    except ValueError:
                        verified = ""
                validator = self._candidate_validator or self._default_candidate_validator
                if not verified:
                    for candidate in candidates:
                        if validator(account, candidate):
                            verified = candidate
                            break
                if not verified:
                    account = self._release(
                        account,
                        owner=claim_owner,
                        status="missing_mfa_material",
                    )
                    return self._result(
                        account,
                        "missing_mfa_material",
                        "missing_mfa_material",
                    )
                promoted = promote_chatgpt_mfa_secret(
                    int(account.id or 0),
                    expected_email=account.email,
                    expected_created_at=account.created_at,
                    expected_updated_at=account.updated_at,
                    owner=claim_owner,
                    secret=verified,
                    database_engine=self._engine,
                )
                if promoted is None:
                    raise RuntimeError("account changed during MFA promotion")
                return self._result(
                    promoted,
                    "ready",
                    "recovered_secret",
                    changed=True,
                )

            pending_session = _text(extra.get("mfa_hardening_session_id"))
            if pending_secret and pending_session:
                enrollment_secret = normalize_totp_secret(pending_secret)
                session_id = pending_session
            else:
                enrollment = client.start_totp_enrollment()
                enrollment_secret = enrollment.secret
                session_id = enrollment.session_id
                staged = update_chatgpt_account_hardening(
                    int(account.id or 0),
                    expected_email=account.email,
                    expected_created_at=account.created_at,
                    expected_updated_at=account.updated_at,
                    owner=claim_owner,
                    extra_updates={
                        "mfa_hardening_status": "confirming",
                        "mfa_pending_secret": enrollment_secret,
                        "mfa_hardening_session_id": session_id,
                    },
                    database_engine=self._engine,
                )
                if staged is None:
                    raise RuntimeError("account changed while staging MFA")
                account = staged
            client.activate_totp_enrollment(
                session_id,
                generate_totp(enrollment_secret),
            )
            promoted = promote_chatgpt_mfa_secret(
                int(account.id or 0),
                expected_email=account.email,
                expected_created_at=account.created_at,
                expected_updated_at=account.updated_at,
                owner=claim_owner,
                secret=enrollment_secret,
                database_engine=self._engine,
            )
            if promoted is None:
                raise RuntimeError("account changed during MFA promotion")
            return self._result(
                promoted,
                "ready",
                "hardened",
                changed=True,
            )
        except Exception as exc:
            current_extra = dict(account.get_extra() or {})
            failure_status = (
                "confirming"
                if _text(current_extra.get("mfa_pending_secret"))
                else "failed"
            )
            account = self._release(
                account,
                owner=claim_owner,
                status=failure_status,
                error=exc,
            )
            return self._result(
                account,
                failure_status,
                "failed",
                message=type(exc).__name__,
            )
