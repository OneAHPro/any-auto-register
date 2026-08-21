from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import update
from sqlmodel import Session, SQLModel, create_engine, select

from core.db import (
    AccountModel,
    ChatGPTAuthStateModel,
    ChatGPTMfaOperationModel,
    ChatGPTMfaRotationJournalModel,
    _create_database_engine,
)
from services.chatgpt_auth_state import (
    ChatGPTAuthIdentityConflict,
    ChatGPTAuthVersionConflict,
    commit_auth_projection,
    ensure_chatgpt_auth_state,
    load_login_mfa_candidate,
    load_login_mfa_candidate_by_email,
    quarantine_legacy_staged_journals,
    resolve_chatgpt_auth_account_id,
    stage_mfa_operation,
    transition_mfa_operation,
)


@pytest.fixture
def database_engine():
    test_engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(test_engine)
    return test_engine


def _create_chatgpt_account(
    session,
    *,
    account_id=7,
    email="demo@example.com",
    password="ORIGINAL-PASSWORD",
    platform="chatgpt",
):
    account = AccountModel(
        id=account_id,
        platform=platform,
        email=email,
        password=password,
    )
    session.add(account)
    session.flush()
    return account


def test_staged_operation_never_replaces_confirmed_totp(database_engine):
    with Session(database_engine) as session:
        account = _create_chatgpt_account(session)
        state = ensure_chatgpt_auth_state(
            account.id,
            primary_confirmed=True,
            session=session,
        )
        active = stage_mfa_operation(
            account.id,
            "demo@example.com",
            "CONFIRMED-TOTP",
            base_auth_version=state.auth_version,
            session=session,
        )
        assert transition_mfa_operation(
            active.operation_id,
            expected_state="staged",
            new_state="activated_remote",
            expected_generation=active.generation,
            recovery_code="CONFIRMED-RECOVERY",
            session=session,
        )
        committed = commit_auth_projection(
            account.id,
            expected_version=state.auth_version,
            active_operation_id=active.operation_id,
            session=session,
        )

        staged = stage_mfa_operation(
            account.id,
            "demo@example.com",
            "UNCONFIRMED-TOTP",
            base_auth_version=committed.auth_version,
            session=session,
        )
        candidate = load_login_mfa_candidate(account.id, session=session)

        assert candidate is not None
        assert committed.mfa_state == "active"
        assert candidate.generation == active.generation
        assert candidate.totp_secret == "CONFIRMED-TOTP"
        assert candidate.recovery_code == "CONFIRMED-RECOVERY"
        assert staged.status == "staged"


def test_old_operation_callback_cannot_activate_new_operation(database_engine):
    with Session(database_engine) as session:
        account = _create_chatgpt_account(session)
        first = stage_mfa_operation(
            account.id,
            "demo@example.com",
            "FIRST-TOTP",
            session=session,
        )
        second = stage_mfa_operation(
            account.id,
            "demo@example.com",
            "SECOND-TOTP",
            session=session,
        )

        assert not transition_mfa_operation(
            first.operation_id,
            expected_state="staged",
            new_state="activated_remote",
            expected_generation=second.generation,
            session=session,
        )
        assert not transition_mfa_operation(
            second.operation_id,
            expected_state="activated_remote",
            new_state="committed",
            expected_generation=second.generation,
            session=session,
        )

        assert first.status == "staged"
        assert second.status == "staged"


def test_transition_api_cannot_bypass_projection_commit(database_engine):
    with Session(database_engine) as session:
        account = _create_chatgpt_account(session)
        state = ensure_chatgpt_auth_state(account.id, session=session)
        operation = stage_mfa_operation(
            account.id,
            account.email,
            "COMMIT-GUARD-TOTP",
            base_auth_version=state.auth_version,
            session=session,
        )
        assert transition_mfa_operation(
            operation.operation_id,
            expected_state="staged",
            new_state="activated_remote",
            expected_generation=operation.generation,
            session=session,
        )

        assert not transition_mfa_operation(
            operation.operation_id,
            expected_state="activated_remote",
            new_state="committed",
            expected_generation=operation.generation,
            session=session,
        )

        persisted_state = session.exec(
            select(ChatGPTAuthStateModel).where(
                ChatGPTAuthStateModel.account_id == account.id
            )
        ).one()
        persisted_operation = session.get(
            ChatGPTMfaOperationModel,
            operation.operation_id,
        )
        assert persisted_state.auth_version == state.auth_version
        assert persisted_state.mfa_state == "absent"
        assert persisted_operation.status == "activated_remote"


def test_caller_owned_file_session_rollback_undoes_projection(tmp_path):
    database_engine = _create_database_engine(
        f"sqlite:///{tmp_path / 'caller-owned.db'}"
    )
    SQLModel.metadata.create_all(database_engine)
    with Session(database_engine) as setup:
        account = _create_chatgpt_account(setup)
        account_id = int(account.id)
        state = ensure_chatgpt_auth_state(account.id, session=setup)
        initial_version = int(state.auth_version)
        operation = stage_mfa_operation(
            account.id,
            account.email,
            "DURABLE-TOTP",
            base_auth_version=state.auth_version,
            session=setup,
        )
        assert transition_mfa_operation(
            operation.operation_id,
            expected_state="staged",
            new_state="activated_remote",
            expected_generation=operation.generation,
            session=setup,
        )
        setup.commit()

    with Session(database_engine) as caller_session:
        commit_auth_projection(
            account_id,
            expected_version=initial_version,
            password="DURABLE-PASSWORD",
            active_operation_id=operation.operation_id,
            session=caller_session,
        )
        caller_session.rollback()

    with Session(database_engine) as verify:
        saved_account = verify.get(AccountModel, account_id)
        saved_state = verify.exec(
            select(ChatGPTAuthStateModel).where(
                ChatGPTAuthStateModel.account_id == account_id
            )
        ).one()
        saved_operation = verify.get(
            ChatGPTMfaOperationModel,
            operation.operation_id,
        )
        assert saved_account.password == "ORIGINAL-PASSWORD"
        assert saved_state.auth_version == initial_version
        assert saved_state.mfa_state == "absent"
        assert saved_operation.status == "activated_remote"


def test_auth_commit_rejects_stale_version(database_engine):
    with Session(database_engine) as session:
        account = _create_chatgpt_account(session)
        current = ensure_chatgpt_auth_state(account.id, session=session)
        initial_version = current.auth_version
        updated = commit_auth_projection(
            account.id,
            expected_version=initial_version,
            password="CURRENT-PASSWORD",
            session=session,
        )

        assert updated.auth_version == initial_version + 1
        with pytest.raises(ChatGPTAuthVersionConflict):
            commit_auth_projection(
                account.id,
                expected_version=initial_version,
                password="STALE-PASSWORD",
                session=session,
            )


def test_confirming_existing_primary_state_advances_auth_version(database_engine):
    with Session(database_engine) as session:
        account = _create_chatgpt_account(session)
        initial = ensure_chatgpt_auth_state(account.id, session=session)
        confirmed = ensure_chatgpt_auth_state(
            account.id,
            primary_confirmed=True,
            session=session,
        )

        assert confirmed.primary_state == "confirmed"
        assert confirmed.auth_version == initial.auth_version + 1
        with pytest.raises(ChatGPTAuthVersionConflict):
            commit_auth_projection(
                account.id,
                expected_version=initial.auth_version,
                password="STALE-PASSWORD",
                session=session,
            )


def test_concurrent_first_ensure_is_idempotent(tmp_path):
    database_engine = _create_database_engine(
        f"sqlite:///{tmp_path / 'auth-state.db'}"
    )
    SQLModel.metadata.create_all(database_engine)
    selected = threading.Barrier(2)

    def ensure_from_independent_session():
        with Session(database_engine) as session:
            original_exec = session.exec
            first_query = True

            def synchronized_exec(statement, *args, **kwargs):
                nonlocal first_query
                result = original_exec(statement, *args, **kwargs)
                if first_query:
                    first_query = False
                    selected.wait(timeout=5)
                return result

            session.exec = synchronized_exec
            state = ensure_chatgpt_auth_state(7, session=session)
            session.commit()
            return state.auth_version

    with ThreadPoolExecutor(max_workers=2) as executor:
        versions = list(executor.map(
            lambda _index: ensure_from_independent_session(),
            range(2),
        ))

    with Session(database_engine) as session:
        rows = session.exec(select(ChatGPTAuthStateModel)).all()

    assert versions == [1, 1]
    assert len(rows) == 1


def test_stale_base_auth_version_does_not_create_operation(database_engine):
    with Session(database_engine) as session:
        account = _create_chatgpt_account(session)
        initial = ensure_chatgpt_auth_state(account.id, session=session)
        commit_auth_projection(
            account.id,
            expected_version=initial.auth_version,
            password="CURRENT-PASSWORD",
            session=session,
        )

        with pytest.raises(ChatGPTAuthVersionConflict):
            stage_mfa_operation(
                account.id,
                "demo@example.com",
                "STALE-TOTP",
                base_auth_version=initial.auth_version,
                session=session,
            )

        assert session.exec(select(ChatGPTMfaOperationModel)).all() == []


def test_credential_revision_is_account_scoped_non_secret_metadata(database_engine):
    with Session(database_engine) as session:
        first_account = _create_chatgpt_account(session, account_id=7)
        second_account = _create_chatgpt_account(
            session,
            account_id=8,
            email="second@example.com",
        )
        first = ensure_chatgpt_auth_state(first_account.id, session=session)
        second = ensure_chatgpt_auth_state(second_account.id, session=session)
        first_committed = commit_auth_projection(
            first_account.id,
            expected_version=first.auth_version,
            password="FIRST-PRIVATE-PASSWORD",
            session=session,
        )
        second_committed = commit_auth_projection(
            second_account.id,
            expected_version=second.auth_version,
            password="SECOND-PRIVATE-PASSWORD",
            session=session,
        )

        assert len(first_committed.credential_revision) == 64
        assert len(second_committed.credential_revision) == 64
        int(first_committed.credential_revision, 16)
        int(second_committed.credential_revision, 16)
        assert first_committed.credential_revision != second_committed.credential_revision
        assert "PRIVATE-PASSWORD" not in first_committed.credential_revision


def test_email_lookup_rejects_duplicate_local_accounts(database_engine):
    with Session(database_engine) as session:
        first = AccountModel(
            platform="chatgpt",
            email="duplicate@example.com",
            password="FIRST-PASSWORD",
        )
        second = AccountModel(
            platform="chatgpt",
            email="duplicate@example.com",
            password="SECOND-PASSWORD",
        )
        session.add(first)
        session.add(second)
        session.commit()
        session.refresh(first)

        state = ensure_chatgpt_auth_state(first.id, session=session)
        operation = stage_mfa_operation(
            first.id,
            first.email,
            "FIRST-TOTP",
            base_auth_version=state.auth_version,
            session=session,
        )
        assert transition_mfa_operation(
            operation.operation_id,
            expected_state="staged",
            new_state="activated_remote",
            expected_generation=operation.generation,
            session=session,
        )
        commit_auth_projection(
            first.id,
            expected_version=state.auth_version,
            active_operation_id=operation.operation_id,
            session=session,
        )

        with pytest.raises(ChatGPTAuthIdentityConflict):
            load_login_mfa_candidate_by_email(
                "duplicate@example.com",
                session=session,
            )


def test_identity_resolver_distinguishes_zero_one_and_duplicate_accounts(
    database_engine,
):
    with Session(database_engine) as session:
        assert resolve_chatgpt_auth_account_id(
            "missing@example.com",
            session=session,
        ) is None
        account = _create_chatgpt_account(
            session,
            email="unique@example.com",
        )
        assert resolve_chatgpt_auth_account_id(
            "UNIQUE@example.com",
            session=session,
        ) == account.id
        _create_chatgpt_account(
            session,
            account_id=8,
            email="unique@example.com",
        )
        with pytest.raises(ChatGPTAuthIdentityConflict):
            resolve_chatgpt_auth_account_id(
                "unique@example.com",
                session=session,
            )


def test_commit_requires_chatgpt_account_and_matching_operation_identity(
    database_engine,
):
    cases = (
        (None, "missing@example.com", "missing@example.com"),
        ("outlook", "other@example.com", "other@example.com"),
        ("chatgpt", "real@example.com", "different@example.com"),
    )
    for platform, account_email, operation_email in cases:
        with Session(database_engine) as session:
            account = None
            account_id = 100 + cases.index(
                (platform, account_email, operation_email)
            )
            if platform is not None:
                account = _create_chatgpt_account(
                    session,
                    account_id=account_id,
                    email=account_email,
                    platform=platform,
                )
            state = ensure_chatgpt_auth_state(
                account_id,
                session=session,
            )
            operation = stage_mfa_operation(
                account_id,
                operation_email,
                "IDENTITY-GUARD-TOTP",
                base_auth_version=state.auth_version,
                session=session,
            )
            assert transition_mfa_operation(
                operation.operation_id,
                expected_state="staged",
                new_state="activated_remote",
                expected_generation=operation.generation,
                session=session,
            )
            with pytest.raises(ChatGPTAuthIdentityConflict):
                commit_auth_projection(
                    account_id,
                    expected_version=state.auth_version,
                    password="MUST-NOT-COMMIT",
                    active_operation_id=operation.operation_id,
                    session=session,
                )
            persisted_state = session.exec(
                select(ChatGPTAuthStateModel).where(
                    ChatGPTAuthStateModel.account_id == account_id
                )
            ).one()
            persisted_operation = session.get(
                ChatGPTMfaOperationModel,
                operation.operation_id,
            )
            assert persisted_state.auth_version == state.auth_version
            assert persisted_state.mfa_state == "absent"
            assert persisted_operation.status == "activated_remote"


def test_login_candidate_hides_unavailable_recovery_codes(database_engine):
    with Session(database_engine) as session:
        account = _create_chatgpt_account(session)
        state = ensure_chatgpt_auth_state(account.id, session=session)
        operation = stage_mfa_operation(
            account.id,
            account.email,
            "RECOVERY-STATE-TOTP",
            base_auth_version=state.auth_version,
            session=session,
        )
        assert transition_mfa_operation(
            operation.operation_id,
            expected_state="staged",
            new_state="activated_remote",
            expected_generation=operation.generation,
            recovery_code="RECOVERY-STATE-CODE",
            session=session,
        )
        commit_auth_projection(
            account.id,
            expected_version=state.auth_version,
            active_operation_id=operation.operation_id,
            session=session,
        )
        for recovery_state in ("available", "reserved", "consumed", "unknown"):
            session.exec(
                update(ChatGPTMfaOperationModel)
                .where(
                    ChatGPTMfaOperationModel.operation_id
                    == operation.operation_id
                )
                .values(recovery_code_state=recovery_state)
            )
            session.flush()
            candidate = load_login_mfa_candidate(account.id, session=session)
            assert candidate is not None
            if recovery_state == "available":
                assert candidate.recovery_code == "RECOVERY-STATE-CODE"
            else:
                assert candidate.recovery_code == ""


def test_auth_conflicts_and_logs_never_disclose_credentials(
    database_engine,
    caplog,
):
    password = "PASSWORD-MUST-STAY-PRIVATE"
    totp_secret = "TOTP-MUST-STAY-PRIVATE"
    recovery_code = "RECOVERY-MUST-STAY-PRIVATE"
    caplog.set_level(logging.DEBUG)

    with Session(database_engine) as session:
        _create_chatgpt_account(session, account_id=7)
        current = ensure_chatgpt_auth_state(7, session=session)
        operation = stage_mfa_operation(
            7,
            "demo@example.com",
            totp_secret,
            session=session,
        )
        assert transition_mfa_operation(
            operation.operation_id,
            expected_state="staged",
            new_state="activated_remote",
            expected_generation=operation.generation,
            recovery_code=recovery_code,
            session=session,
        )
        commit_auth_projection(
            7,
            expected_version=current.auth_version,
            password=password,
            active_operation_id=operation.operation_id,
            session=session,
        )

        with pytest.raises(ChatGPTAuthVersionConflict) as captured:
            commit_auth_projection(
                7,
                expected_version=current.auth_version,
                password=password,
                active_operation_id=operation.operation_id,
                session=session,
            )

    emitted = f"{captured.value}\n{caplog.text}"
    assert password not in emitted
    assert totp_secret not in emitted
    assert recovery_code not in emitted


def test_quarantine_legacy_staged_journals_preserves_migration_data_without_logging(
    database_engine,
    caplog,
):
    totp_secret = "LEGACY-TOTP-MUST-STAY-PRIVATE"
    recovery_code = "LEGACY-RECOVERY-MUST-STAY-PRIVATE"
    caplog.set_level(logging.DEBUG)

    with Session(database_engine) as session:
        legacy = ChatGPTMfaRotationJournalModel(
            email="legacy@example.com",
            totp_secret=totp_secret,
            recovery_code=recovery_code,
            status="staged",
        )
        session.add(legacy)
        session.commit()

        assert quarantine_legacy_staged_journals(session=session) == 1
        session.refresh(legacy)

        assert legacy.status == "quarantined"
        assert legacy.totp_secret == totp_secret
        assert legacy.recovery_code == recovery_code

    assert totp_secret not in caplog.text
    assert recovery_code not in caplog.text
