from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from core.db import (
    AccountModel,
    ChatGPTAuthStateModel,
    ChatGPTMfaOperationModel,
    ChatGPTMfaRotationJournalModel,
    _create_database_engine,
)
from services.chatgpt_auth_state import (
    ChatGPTAuthVersionConflict,
    commit_auth_projection,
    ensure_chatgpt_auth_state,
    load_login_mfa_candidate,
    load_login_mfa_candidate_by_email,
    quarantine_legacy_staged_journals,
    stage_mfa_operation,
    transition_mfa_operation,
)


@pytest.fixture
def database_engine():
    test_engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(test_engine)
    return test_engine


def test_staged_operation_never_replaces_confirmed_totp(database_engine):
    with Session(database_engine) as session:
        state = ensure_chatgpt_auth_state(
            7,
            primary_confirmed=True,
            session=session,
        )
        active = stage_mfa_operation(
            7,
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
            7,
            expected_version=state.auth_version,
            active_operation_id=active.operation_id,
            session=session,
        )

        staged = stage_mfa_operation(
            7,
            "demo@example.com",
            "UNCONFIRMED-TOTP",
            base_auth_version=committed.auth_version,
            session=session,
        )
        candidate = load_login_mfa_candidate(7, session=session)

        assert candidate is not None
        assert candidate.generation == active.generation
        assert candidate.totp_secret == "CONFIRMED-TOTP"
        assert candidate.recovery_code == "CONFIRMED-RECOVERY"
        assert staged.status == "staged"


def test_old_operation_callback_cannot_activate_new_operation(database_engine):
    with Session(database_engine) as session:
        first = stage_mfa_operation(
            7,
            "demo@example.com",
            "FIRST-TOTP",
            session=session,
        )
        second = stage_mfa_operation(
            7,
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


def test_auth_commit_rejects_stale_version(database_engine):
    with Session(database_engine) as session:
        current = ensure_chatgpt_auth_state(7, session=session)
        initial_version = current.auth_version
        updated = commit_auth_projection(
            7,
            expected_version=initial_version,
            password="CURRENT-PASSWORD",
            session=session,
        )

        assert updated.auth_version == initial_version + 1
        with pytest.raises(ChatGPTAuthVersionConflict):
            commit_auth_projection(
                7,
                expected_version=initial_version,
                password="STALE-PASSWORD",
                session=session,
            )


def test_confirming_existing_primary_state_advances_auth_version(database_engine):
    with Session(database_engine) as session:
        initial = ensure_chatgpt_auth_state(7, session=session)
        confirmed = ensure_chatgpt_auth_state(
            7,
            primary_confirmed=True,
            session=session,
        )

        assert confirmed.primary_state == "confirmed"
        assert confirmed.auth_version == initial.auth_version + 1
        with pytest.raises(ChatGPTAuthVersionConflict):
            commit_auth_projection(
                7,
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
        initial = ensure_chatgpt_auth_state(7, session=session)
        commit_auth_projection(
            7,
            expected_version=initial.auth_version,
            password="CURRENT-PASSWORD",
            session=session,
        )

        with pytest.raises(ChatGPTAuthVersionConflict):
            stage_mfa_operation(
                7,
                "demo@example.com",
                "STALE-TOTP",
                base_auth_version=initial.auth_version,
                session=session,
            )

        assert session.exec(select(ChatGPTMfaOperationModel)).all() == []


def test_credential_revision_is_account_scoped_non_secret_metadata(database_engine):
    with Session(database_engine) as session:
        first = ensure_chatgpt_auth_state(7, session=session)
        second = ensure_chatgpt_auth_state(8, session=session)
        first_committed = commit_auth_projection(
            7,
            expected_version=first.auth_version,
            password="FIRST-PRIVATE-PASSWORD",
            session=session,
        )
        second_committed = commit_auth_projection(
            8,
            expected_version=second.auth_version,
            password="SECOND-PRIVATE-PASSWORD",
            session=session,
        )

        assert first_committed.credential_revision == (
            "chatgpt-auth:a7:v2:p1:m0:b0:t0"
        )
        assert second_committed.credential_revision == (
            "chatgpt-auth:a8:v2:p1:m0:b0:t0"
        )
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

        assert (
            load_login_mfa_candidate_by_email(
                "duplicate@example.com",
                session=session,
            )
            is None
        )


def test_auth_conflicts_and_logs_never_disclose_credentials(
    database_engine,
    caplog,
):
    password = "PASSWORD-MUST-STAY-PRIVATE"
    totp_secret = "TOTP-MUST-STAY-PRIVATE"
    recovery_code = "RECOVERY-MUST-STAY-PRIVATE"
    caplog.set_level(logging.DEBUG)

    with Session(database_engine) as session:
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
