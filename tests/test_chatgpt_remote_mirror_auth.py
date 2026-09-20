"""Inventory mirrors must not become competing local login identities."""

import pytest
from sqlmodel import Session, SQLModel, create_engine

from core.db import AccountModel, ChatGPTMfaRotationJournalModel
from platforms.chatgpt.refresh_token_registration_engine import RefreshTokenRegistrationEngine
from services import chatgpt_relogin
from services.chatgpt_auth_state import (
    ChatGPTAuthIdentityConflict,
    commit_auth_projection,
    ensure_chatgpt_auth_state,
    load_login_mfa_candidate_by_email,
    reconcile_activated_chatgpt_mfa_rotation,
    resolve_chatgpt_auth_account_id,
    stage_mfa_operation,
    transition_mfa_operation,
)


@pytest.fixture
def mirror_accounts(monkeypatch):
    database = create_engine("sqlite://")
    SQLModel.metadata.create_all(database)
    monkeypatch.setattr(chatgpt_relogin, "engine", database)
    with Session(database) as session:
        local = AccountModel(id=1869, platform="chatgpt", email="mirror@example.com",
                             password="saved-password")
        local.set_extra({"mailbox_login_context": {
            "provider": "chatgpt_credentials", "email": local.email,
            "account_id": local.email,
            "extra": {"account_type": "chatgpt_password_totp",
                      "password": local.password, "totp_secret": "OLD-IMPORTED-TOTP"},
        }})
        remote = AccountModel(id=1876, platform="chatgpt", email=local.email,
                              password="", account_source="codex2api")
        remote.set_extra({"remote_only": True, "remote_target_id": 1,
                          "remote_id": 26431, "account_source": "codex2api"})
        session.add_all([local, remote])
        session.flush()
        state = ensure_chatgpt_auth_state(local.id, session=session)
        operation = stage_mfa_operation(local.id, local.email, "COMMITTED-TOTP",
                                        base_auth_version=state.auth_version, session=session)
        assert transition_mfa_operation(operation.operation_id, expected_state="staged",
                                        new_state="activated_remote",
                                        expected_generation=operation.generation, session=session)
        commit_auth_projection(local.id, expected_version=state.auth_version,
                               active_operation_id=operation.operation_id, session=session)
        session.commit()
    yield database
    database.dispose()


def test_remote_mirror_does_not_block_canonical_mfa_lookup(mirror_accounts):
    with Session(mirror_accounts) as session:
        candidate = load_login_mfa_candidate_by_email("MIRROR@example.com", session=session)
        assert candidate.account_id == 1869
        assert candidate.totp_secret == "COMMITTED-TOTP"


def test_saved_account_relogin_loads_committed_mfa_with_remote_mirror(mirror_accounts):
    saved = chatgpt_relogin._load_saved_account(1869)
    service = chatgpt_relogin._build_email_service(saved, {}, log_fn=None)
    engine = RefreshTokenRegistrationEngine(
        email_service=service, callback_logger=lambda _: None,
        extra_config={"chatgpt_local_account_id": 1869,
                      "_chatgpt_auth_engine": mirror_accounts},
    )
    assert engine._create_email(existing_account_login_only=True)
    assert engine.password == "saved-password"
    assert engine.totp_secret == "COMMITTED-TOTP"


def test_remote_mirror_does_not_block_activated_mfa_recovery(mirror_accounts):
    with Session(mirror_accounts) as session:
        journal = ChatGPTMfaRotationJournalModel(
            email="mirror@example.com", totp_secret="LATEST-ACTIVATED-TOTP",
            recovery_code="LATEST-RECOVERY", status="activated",
        )
        session.add(journal)
        session.flush()
        recovered = reconcile_activated_chatgpt_mfa_rotation(1869, session=session)
        assert recovered is not None
        candidate = load_login_mfa_candidate_by_email("mirror@example.com", session=session)
        assert candidate.totp_secret == "LATEST-ACTIVATED-TOTP"
        assert session.get(AccountModel, 1876).password == ""


@pytest.mark.parametrize("credential", ["password", "mailbox", "legacy_totp", "canonical_mfa"])
def test_remote_flag_never_hides_a_second_credential_owner(mirror_accounts, credential):
    with Session(mirror_accounts) as session:
        remote = session.get(AccountModel, 1876)
        extra = remote.get_extra()
        if credential == "password":
            remote.password = "second-password"
        elif credential == "mailbox":
            extra["mailbox_login_context"] = {"email": remote.email}
        elif credential == "legacy_totp":
            extra["totp_secret"] = "SECOND-TOTP"
        else:
            state = ensure_chatgpt_auth_state(remote.id, session=session)
            op = stage_mfa_operation(remote.id, remote.email, "SECOND-TOTP",
                                     base_auth_version=state.auth_version, session=session)
            assert transition_mfa_operation(op.operation_id, expected_state="staged",
                                            new_state="activated_remote",
                                            expected_generation=op.generation, session=session)
            commit_auth_projection(remote.id, expected_version=state.auth_version,
                                   active_operation_id=op.operation_id, session=session)
            session.refresh(remote)
            extra = remote.get_extra()
            extra.pop("mailbox_login_context", None)
        remote.set_extra(extra)
        session.add(remote)
        session.flush()
        with pytest.raises(ChatGPTAuthIdentityConflict):
            resolve_chatgpt_auth_account_id(remote.email, session=session)


def test_remote_mirrors_alone_do_not_supply_local_login_identity(mirror_accounts):
    with Session(mirror_accounts) as session:
        session.delete(session.get(AccountModel, 1869))
        session.flush()
        assert resolve_chatgpt_auth_account_id("mirror@example.com", session=session) is None
