import threading

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from core.db import AccountModel
from services.chatgpt_account_coordination import (
    chatgpt_account_email_operation_lock,
    chatgpt_account_operation_lock,
    codex2api_account_mutation_lock,
    validated_chatgpt_account_operation_lock,
)


def test_same_account_lock_is_nonblocking_and_other_accounts_are_independent():
    with chatgpt_account_operation_lock(17, blocking=False) as first:
        assert first is True
        with chatgpt_account_operation_lock("17", blocking=False) as duplicate:
            assert duplicate is False
        with chatgpt_account_operation_lock(18, blocking=False) as other:
            assert other is True


def test_same_account_email_lock_is_case_insensitive():
    with chatgpt_account_email_operation_lock(
        "Demo@Example.com",
        blocking=False,
    ) as first:
        assert first is True
        with chatgpt_account_email_operation_lock(
            "demo@example.COM",
            blocking=False,
        ) as duplicate:
            assert duplicate is False
        with chatgpt_account_email_operation_lock(
            "other@example.com",
            blocking=False,
        ) as other:
            assert other is True


def test_codex2api_mutation_lock_serializes_workers():
    entered = threading.Event()
    finished = threading.Event()

    def worker():
        with codex2api_account_mutation_lock():
            entered.set()
        finished.set()

    with codex2api_account_mutation_lock():
        thread = threading.Thread(target=worker)
        thread.start()
        assert entered.wait(0.05) is False
    assert finished.wait(1.0) is True
    thread.join(timeout=1.0)


def test_codex2api_mutation_lock_is_reentrant_in_one_thread():
    with codex2api_account_mutation_lock():
        with codex2api_account_mutation_lock():
            pass


def test_validated_account_lock_holds_identity_fence_and_rejects_deleted_row():
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(test_engine)
    with Session(test_engine) as session:
        account = AccountModel(
            platform="chatgpt",
            email="fenced@example.com",
            password="password",
        )
        session.add(account)
        session.commit()
        session.refresh(account)
        account_id = int(account.id)
        created_at = account.created_at

    with validated_chatgpt_account_operation_lock(
        account_id,
        email="fenced@example.com",
        created_at=created_at,
        database_engine=test_engine,
    ) as acquired:
        assert acquired is True
        with chatgpt_account_operation_lock(account_id) as duplicate:
            assert duplicate is False

    with Session(test_engine) as session:
        session.delete(session.get(AccountModel, account_id))
        session.commit()

    with validated_chatgpt_account_operation_lock(
        account_id,
        email="fenced@example.com",
        created_at=created_at,
        database_engine=test_engine,
    ) as acquired:
        assert acquired is False
