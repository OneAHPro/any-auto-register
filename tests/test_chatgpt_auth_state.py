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
    ChatGPTMfaOperationConflict,
    commit_auth_projection,
    clear_chatgpt_auth_failure,
    ensure_chatgpt_auth_state,
    load_login_mfa_candidate,
    load_login_mfa_candidate_by_email,
    quarantine_legacy_staged_journals,
    reconcile_activated_chatgpt_mfa_rotation,
    promote_successful_chatgpt_account_auth,
    record_chatgpt_auth_failure,
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


def test_email_lookup_rejects_committed_mfa_from_reused_account_id(
    database_engine,
):
    account_id = 7
    with Session(database_engine) as session:
        original = _create_chatgpt_account(
            session,
            account_id=account_id,
            email="original@example.com",
        )
        state = ensure_chatgpt_auth_state(original.id, session=session)
        operation = stage_mfa_operation(
            original.id,
            original.email,
            "ORIGINAL-TOTP",
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
            original.id,
            expected_version=state.auth_version,
            active_operation_id=operation.operation_id,
            session=session,
        )
        session.commit()

    with Session(database_engine) as session:
        original = session.get(AccountModel, account_id)
        session.delete(original)
        session.commit()
        _create_chatgpt_account(
            session,
            account_id=account_id,
            email="replacement@example.com",
        )
        session.commit()

    with Session(database_engine) as session:
        candidate = load_login_mfa_candidate_by_email(
            "replacement@example.com",
            session=session,
        )

    assert candidate is None


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


def test_auth_failure_backoff_persists_and_success_clears(database_engine):
    with Session(database_engine) as session:
        _create_chatgpt_account(session)
        session.commit()
        first = record_chatgpt_auth_failure(
            7,
            failure_domain="email_backend",
            error_code="mailapi_timeout",
            session=session,
        )
        session.commit()
        assert first.failure_count == 1
        assert first.circuit_state == "open"
        assert first.next_retry_at is not None

        cleared = clear_chatgpt_auth_failure(7, session=session)
        session.commit()
        assert cleared.failure_count == 0
        assert cleared.circuit_state == "closed"
        assert cleared.next_retry_at is None
        assert cleared.failure_domain == ""
        assert cleared.error_code == ""


def test_successful_legacy_login_promotes_password_totp_to_canonical_state(
    database_engine,
):
    with Session(database_engine) as session:
        account = _create_chatgpt_account(session, password="PRIMARY-PASSWORD")
        account.set_extra(
            {
                "mailbox_login_context": {
                    "provider": "microsoft",
                    "email": "demo@example.com",
                    "extra": {
                        "account_type": "mailapi_url",
                        "mailapi_url": "https://mail.example.test/inbox",
                        "password": "PRIMARY-PASSWORD",
                        "totp_secret": "REMOTE-VERIFIED-TOTP",
                        "mfa_recovery_code": "REMOTE-RECOVERY",
                        "chatgpt_mfa_managed": True,
                    },
                }
            }
        )
        session.add(account)
        session.commit()

    with Session(database_engine) as session:
        promoted = promote_successful_chatgpt_account_auth(7, session=session)
        session.commit()
        candidate = load_login_mfa_candidate(7, session=session)

    assert promoted.primary_state.value == "confirmed"
    assert promoted.mfa_state.value == "active"
    assert candidate is not None
    assert candidate.totp_secret == "REMOTE-VERIFIED-TOTP"
    assert candidate.recovery_code == "REMOTE-RECOVERY"


def test_confirmed_rotation_journal_replaces_existing_canonical_mfa_generation(
    database_engine,
):
    with Session(database_engine) as session:
        account = _create_chatgpt_account(session, password="PRIMARY-PASSWORD")
        account.set_extra(
            {
                "mailbox_login_context": {
                    "provider": "microsoft",
                    "email": "demo@example.com",
                    "extra": {
                        "password": "PRIMARY-PASSWORD",
                        "totp_secret": "OLD-REMOTE-TOTP",
                        "mfa_recovery_code": "OLD-REMOTE-RECOVERY",
                        "chatgpt_mfa_managed": True,
                    },
                }
            }
        )
        session.add(account)
        session.commit()

        first = promote_successful_chatgpt_account_auth(7, session=session)
        session.commit()
        first_candidate = load_login_mfa_candidate(7, session=session)
        assert first_candidate is not None

        account = session.get(AccountModel, 7)
        account.set_extra(
            {
                "mailbox_login_context": {
                    "provider": "microsoft",
                    "email": "demo@example.com",
                    "extra": {
                        "password": "PRIMARY-PASSWORD",
                        "totp_secret": "NEW-REMOTE-TOTP",
                        "mfa_recovery_code": "NEW-REMOTE-RECOVERY",
                        "chatgpt_mfa_managed": True,
                        "mfa_rotated_at": "2026-08-24T13:50:37+00:00",
                    },
                }
            }
        )
        session.add(
            ChatGPTMfaRotationJournalModel(
                email="demo@example.com",
                totp_secret="NEW-REMOTE-TOTP",
                recovery_code="NEW-REMOTE-RECOVERY",
                status="activated",
                rotated_at="2026-08-24T13:50:37+00:00",
            )
        )
        session.add(account)
        session.commit()

        promoted = promote_successful_chatgpt_account_auth(7, session=session)
        session.commit()
        candidate = load_login_mfa_candidate(7, session=session)
        journal = session.exec(
            select(ChatGPTMfaRotationJournalModel).where(
                ChatGPTMfaRotationJournalModel.email == "demo@example.com"
            )
        ).first()

    assert promoted.auth_version == first.auth_version + 1
    assert candidate is not None
    assert candidate.generation != first_candidate.generation
    assert candidate.totp_secret == "NEW-REMOTE-TOTP"
    assert candidate.recovery_code == "NEW-REMOTE-RECOVERY"
    assert journal is None


def test_unconfirmed_rotation_journal_cannot_replace_existing_canonical_mfa(
    database_engine,
):
    with Session(database_engine) as session:
        account = _create_chatgpt_account(session, password="PRIMARY-PASSWORD")
        account.set_extra(
            {
                "mailbox_login_context": {
                    "email": "demo@example.com",
                    "extra": {
                        "password": "PRIMARY-PASSWORD",
                        "totp_secret": "OLD-REMOTE-TOTP",
                        "mfa_recovery_code": "OLD-REMOTE-RECOVERY",
                    },
                }
            }
        )
        session.add(account)
        session.commit()
        first = promote_successful_chatgpt_account_auth(7, session=session)
        session.commit()
        first_candidate = load_login_mfa_candidate(7, session=session)

        account = session.get(AccountModel, 7)
        account.set_extra(
            {
                "mailbox_login_context": {
                    "email": "demo@example.com",
                    "extra": {
                        "password": "PRIMARY-PASSWORD",
                        "totp_secret": "UNCONFIRMED-TOTP",
                        "mfa_recovery_code": "UNCONFIRMED-RECOVERY",
                    },
                }
            }
        )
        session.add(
            ChatGPTMfaRotationJournalModel(
                email="demo@example.com",
                totp_secret="UNCONFIRMED-TOTP",
                recovery_code="UNCONFIRMED-RECOVERY",
                status="staged",
            )
        )
        session.add(account)
        session.commit()

        with pytest.raises(ChatGPTMfaOperationConflict):
            promote_successful_chatgpt_account_auth(7, session=session)
        session.rollback()
        candidate = load_login_mfa_candidate(7, session=session)
        journal = session.exec(
            select(ChatGPTMfaRotationJournalModel).where(
                ChatGPTMfaRotationJournalModel.email == "demo@example.com"
            )
        ).one()

    assert candidate is not None
    assert first_candidate is not None
    assert candidate.generation == first_candidate.generation
    assert candidate.totp_secret == "OLD-REMOTE-TOTP"
    assert candidate.recovery_code == "OLD-REMOTE-RECOVERY"
    assert journal.status == "staged"


def test_confirmed_rotation_promotion_and_journal_consumption_roll_back_together(
    database_engine,
):
    with Session(database_engine) as session:
        account = _create_chatgpt_account(session, password="PRIMARY-PASSWORD")
        account.set_extra(
            {
                "mailbox_login_context": {
                    "email": "demo@example.com",
                    "extra": {
                        "password": "PRIMARY-PASSWORD",
                        "totp_secret": "OLD-REMOTE-TOTP",
                        "mfa_recovery_code": "OLD-REMOTE-RECOVERY",
                    },
                }
            }
        )
        session.add(account)
        session.commit()
        promote_successful_chatgpt_account_auth(7, session=session)
        session.commit()

        account = session.get(AccountModel, 7)
        account.set_extra(
            {
                "mailbox_login_context": {
                    "email": "demo@example.com",
                    "extra": {
                        "password": "PRIMARY-PASSWORD",
                        "totp_secret": "NEW-REMOTE-TOTP",
                        "mfa_recovery_code": "NEW-REMOTE-RECOVERY",
                    },
                }
            }
        )
        session.add(account)
        session.add(
            ChatGPTMfaRotationJournalModel(
                email="demo@example.com",
                totp_secret="NEW-REMOTE-TOTP",
                recovery_code="NEW-REMOTE-RECOVERY",
                status="activated",
            )
        )
        session.commit()

        promote_successful_chatgpt_account_auth(7, session=session)
        in_transaction = load_login_mfa_candidate(7, session=session)
        assert in_transaction is not None
        assert in_transaction.totp_secret == "NEW-REMOTE-TOTP"
        assert session.exec(
            select(ChatGPTMfaRotationJournalModel)
        ).first() is None
        session.rollback()

    with Session(database_engine) as session:
        candidate = load_login_mfa_candidate(7, session=session)
        journal = session.exec(select(ChatGPTMfaRotationJournalModel)).one()

    assert candidate is not None
    assert candidate.totp_secret == "OLD-REMOTE-TOTP"
    assert journal.status == "activated"
    assert journal.totp_secret == "NEW-REMOTE-TOTP"


def test_matching_confirmed_journal_is_consumed_idempotently(database_engine):
    with Session(database_engine) as session:
        account = _create_chatgpt_account(session, password="PRIMARY-PASSWORD")
        account.set_extra(
            {
                "mailbox_login_context": {
                    "email": "demo@example.com",
                    "extra": {
                        "password": "PRIMARY-PASSWORD",
                        "totp_secret": "REMOTE-TOTP",
                        "mfa_recovery_code": "REMOTE-RECOVERY",
                    },
                }
            }
        )
        session.add(account)
        session.commit()
        first = promote_successful_chatgpt_account_auth(7, session=session)
        session.commit()
        session.add(
            ChatGPTMfaRotationJournalModel(
                email="demo@example.com",
                totp_secret="REMOTE-TOTP",
                recovery_code="REMOTE-RECOVERY",
                status="activated",
            )
        )
        session.commit()

        replayed = promote_successful_chatgpt_account_auth(7, session=session)
        session.commit()
        journal = session.exec(select(ChatGPTMfaRotationJournalModel)).first()
        candidate = load_login_mfa_candidate(7, session=session)

    assert replayed.auth_version == first.auth_version
    assert journal is None
    assert candidate is not None
    assert candidate.totp_secret == "REMOTE-TOTP"


def test_replayed_journal_preserves_consumed_recovery_code_state(database_engine):
    with Session(database_engine) as session:
        account = _create_chatgpt_account(session, password="PRIMARY-PASSWORD")
        account.set_extra(
            {
                "mailbox_login_context": {
                    "email": "demo@example.com",
                    "extra": {
                        "password": "PRIMARY-PASSWORD",
                        "totp_secret": "REMOTE-TOTP",
                        "mfa_recovery_code": "REMOTE-RECOVERY",
                    },
                }
            }
        )
        session.add(account)
        session.commit()
        first = promote_successful_chatgpt_account_auth(7, session=session)
        session.commit()
        active = session.exec(
            select(ChatGPTMfaOperationModel).where(
                ChatGPTMfaOperationModel.generation
                == first.active_mfa_generation
            )
        ).one()
        active.recovery_code_state = "consumed"
        session.add(active)
        session.add(
            ChatGPTMfaRotationJournalModel(
                email="demo@example.com",
                totp_secret="REMOTE-TOTP",
                recovery_code="REMOTE-RECOVERY",
                status="activated",
            )
        )
        session.commit()

        replayed = reconcile_activated_chatgpt_mfa_rotation(
            7,
            session=session,
        )
        session.commit()
        operations = session.exec(
            select(ChatGPTMfaOperationModel).where(
                ChatGPTMfaOperationModel.account_id == 7
            )
        ).all()
        journal = session.exec(select(ChatGPTMfaRotationJournalModel)).first()

    assert replayed is not None
    assert replayed.auth_version == first.auth_version
    assert len(operations) == 1
    assert operations[0].recovery_code_state == "consumed"
    assert journal is None


def test_activated_rotation_journal_is_not_consumed_for_duplicate_account_identity(
    database_engine,
):
    with Session(database_engine) as session:
        _create_chatgpt_account(session, account_id=7, email="duplicate@example.com")
        _create_chatgpt_account(session, account_id=8, email="duplicate@example.com")
        session.add(
            ChatGPTMfaRotationJournalModel(
                email="duplicate@example.com",
                totp_secret="REMOTE-TOTP",
                recovery_code="REMOTE-RECOVERY",
                status="activated",
            )
        )
        session.commit()

        with pytest.raises(ChatGPTAuthIdentityConflict):
            reconcile_activated_chatgpt_mfa_rotation(7, session=session)
        session.rollback()
        journal = session.exec(select(ChatGPTMfaRotationJournalModel)).one()
        operations = session.exec(select(ChatGPTMfaOperationModel)).all()

    assert journal.status == "activated"
    assert operations == []


def test_confirmed_rotation_with_empty_recovery_code_replaces_canonical_mfa(
    database_engine,
):
    with Session(database_engine) as session:
        account = _create_chatgpt_account(session, password="PRIMARY-PASSWORD")
        account.set_extra(
            {
                "mailbox_login_context": {
                    "email": "demo@example.com",
                    "extra": {
                        "password": "PRIMARY-PASSWORD",
                        "totp_secret": "OLD-TOTP",
                        "mfa_recovery_code": "OLD-RECOVERY",
                    },
                }
            }
        )
        session.add(account)
        session.commit()
        promote_successful_chatgpt_account_auth(7, session=session)
        session.commit()

        account = session.get(AccountModel, 7)
        account.set_extra(
            {
                "mailbox_login_context": {
                    "email": "demo@example.com",
                    "extra": {
                        "password": "PRIMARY-PASSWORD",
                        "totp_secret": "INHOUSE-TOTP",
                        "mfa_recovery_code": "",
                    },
                }
            }
        )
        session.add(account)
        session.add(
            ChatGPTMfaRotationJournalModel(
                email="demo@example.com",
                totp_secret="INHOUSE-TOTP",
                recovery_code="",
                status="activated",
            )
        )
        session.commit()

        promote_successful_chatgpt_account_auth(7, session=session)
        session.commit()
        candidate = load_login_mfa_candidate(7, session=session)

    assert candidate is not None
    assert candidate.totp_secret == "INHOUSE-TOTP"
    assert candidate.recovery_code == ""


def test_first_login_promotes_activated_journal_created_before_account_row(
    database_engine,
):
    with Session(database_engine) as session:
        session.add(
            ChatGPTMfaRotationJournalModel(
                email="first-login@example.com",
                totp_secret="FIRST-LOGIN-TOTP",
                recovery_code="FIRST-LOGIN-RECOVERY",
                status="activated",
            )
        )
        session.commit()
        account = _create_chatgpt_account(
            session,
            account_id=7,
            email="first-login@example.com",
            password="FIRST-LOGIN-PASSWORD",
        )
        account.set_extra(
            {
                "mailbox_login_context": {
                    "email": "first-login@example.com",
                    "extra": {
                        "password": "FIRST-LOGIN-PASSWORD",
                        "totp_secret": "FIRST-LOGIN-TOTP",
                        "mfa_recovery_code": "FIRST-LOGIN-RECOVERY",
                    },
                }
            }
        )
        session.add(account)
        session.commit()

        promoted = promote_successful_chatgpt_account_auth(7, session=session)
        session.commit()
        candidate = load_login_mfa_candidate(7, session=session)
        journal = session.exec(select(ChatGPTMfaRotationJournalModel)).first()

    assert promoted.mfa_state.value == "active"
    assert candidate is not None
    assert candidate.totp_secret == "FIRST-LOGIN-TOTP"
    assert journal is None


def test_activated_journal_without_totp_is_preserved_and_rejected(database_engine):
    with Session(database_engine) as session:
        _create_chatgpt_account(session)
        session.add(
            ChatGPTMfaRotationJournalModel(
                email="demo@example.com",
                totp_secret="",
                recovery_code="RECOVERY-MUST-NOT-BE-CONSUMED",
                status="activated",
            )
        )
        session.commit()

        with pytest.raises(ChatGPTMfaOperationConflict):
            reconcile_activated_chatgpt_mfa_rotation(7, session=session)
        session.rollback()
        journal = session.exec(select(ChatGPTMfaRotationJournalModel)).one()

    assert journal.status == "activated"
    assert journal.recovery_code == "RECOVERY-MUST-NOT-BE-CONSUMED"
