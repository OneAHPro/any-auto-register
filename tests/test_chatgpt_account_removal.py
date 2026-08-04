from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from core.db import AccountModel
from services.chatgpt_account_coordination import chatgpt_account_operation_lock
from services.chatgpt_account_removal import remove_account


@pytest.fixture
def database_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def _add_account(
    engine,
    *,
    platform="chatgpt",
    email="demo@example.com",
    token="at-column-secret",
    extra=None,
):
    account = AccountModel(
        platform=platform,
        email=email,
        password="password-secret",
        token=token,
        user_id="stored-user-1",
    )
    account.set_extra(
        dict(extra)
        if extra is not None
        else {
            "access_token": "at-extra-secret",
            "refresh_token": "rt-extra-secret",
            "id_token": "id-extra-secret",
            "workspace_id": "workspace-1",
        }
    )
    with Session(engine) as session:
        session.add(account)
        session.commit()
        session.refresh(account)
        return int(account.id), account.created_at, account.updated_at


def _exists(engine, account_id):
    with Session(engine) as session:
        return session.get(AccountModel, account_id) is not None


def test_disabled_chatgpt_cleanup_is_local_only(database_engine):
    account_id, _created, _updated = _add_account(database_engine)

    with mock.patch(
        "services.chatgpt_account_removal.delete_codex2api_credential"
    ) as remote:
        result = remove_account(
            account_id,
            database_engine=database_engine,
            codex2api_delete_on_account_remove_enabled=False,
        )

    assert result == {
        "ok": True,
        "account_id": account_id,
        "status": "deleted",
        "local_deleted": True,
        "codex2api": {"enabled": False, "status": "skipped_disabled"},
        "error_code": "",
        "message": "账号已删除",
    }
    assert not _exists(database_engine, account_id)
    remote.assert_not_called()


def test_non_chatgpt_account_is_always_local_only(database_engine):
    account_id, _created, _updated = _add_account(
        database_engine,
        platform="qwen",
    )

    with mock.patch(
        "services.chatgpt_account_removal.delete_codex2api_credential"
    ) as remote:
        result = remove_account(
            account_id,
            database_engine=database_engine,
            codex2api_delete_on_account_remove_enabled=True,
        )

    assert result["ok"] is True
    assert result["codex2api"] == {
        "enabled": False,
        "status": "not_applicable",
    }
    remote.assert_not_called()


def test_enabled_at_only_account_deletes_remote_while_local_row_exists(
    database_engine,
):
    account_id, _created, _updated = _add_account(
        database_engine,
        token="at-only-column-secret",
        extra={"workspaceId": "workspace-camel-1"},
    )

    def remote_delete(*, email, identity):
        assert _exists(database_engine, account_id)
        assert email == "demo@example.com"
        assert identity["workspace_id"] == "workspace-camel-1"
        assert identity["access_token"] == "at-only-column-secret"
        return {"status": "deleted", "remote_id": 71, "message": ""}

    with mock.patch(
        "services.chatgpt_account_removal.delete_codex2api_credential",
        side_effect=remote_delete,
    ) as remote:
        result = remove_account(
            account_id,
            database_engine=database_engine,
            codex2api_delete_on_account_remove_enabled=True,
        )

    assert result["ok"] is True
    assert result["codex2api"] == {
        "enabled": True,
        "status": "deleted",
        "remote_id": 71,
    }
    assert not _exists(database_engine, account_id)
    remote.assert_called_once()


@pytest.mark.parametrize("remote_status", ["deleted", "already_absent"])
def test_remote_success_states_allow_local_deletion(database_engine, remote_status):
    account_id, _created, _updated = _add_account(database_engine)
    with mock.patch(
        "services.chatgpt_account_removal.delete_codex2api_credential",
        return_value={"status": remote_status, "remote_id": 91, "message": ""},
    ):
        result = remove_account(
            account_id,
            database_engine=database_engine,
            codex2api_delete_on_account_remove_enabled=True,
        )

    assert result["ok"] is True
    assert result["local_deleted"] is True
    assert result["codex2api"]["status"] == remote_status


@pytest.mark.parametrize(
    ("remote_status", "error_code"),
    [
        ("ambiguous", "remote_ambiguous"),
        ("config_missing", "codex2api_delete_failed"),
        ("unauthorized", "codex2api_delete_failed"),
        ("unavailable", "codex2api_delete_failed"),
        ("failed", "codex2api_delete_failed"),
    ],
)
def test_remote_failure_preserves_local_account(
    database_engine,
    remote_status,
    error_code,
):
    account_id, _created, _updated = _add_account(database_engine)
    with mock.patch(
        "services.chatgpt_account_removal.delete_codex2api_credential",
        return_value={
            "status": remote_status,
            "remote_id": None,
            "message": "远端认证删除未完成 at-extra-secret",
        },
    ):
        result = remove_account(
            account_id,
            database_engine=database_engine,
            codex2api_delete_on_account_remove_enabled=True,
        )

    assert result["ok"] is False
    assert result["status"] == "remote_failed"
    assert result["error_code"] == error_code
    assert result["local_deleted"] is False
    assert _exists(database_engine, account_id)
    assert "at-extra-secret" not in result["message"]
    assert len(result["message"]) <= 200


def test_local_cas_conflict_after_remote_success_is_retryable(database_engine):
    account_id, _created, _updated = _add_account(database_engine)

    def delete_then_change_local(**_kwargs):
        with Session(database_engine) as session:
            account = session.get(AccountModel, account_id)
            account.updated_at = datetime.now(timezone.utc) + timedelta(seconds=1)
            session.add(account)
            session.commit()
        return {"status": "deleted", "remote_id": 101, "message": ""}

    with mock.patch(
        "services.chatgpt_account_removal.delete_codex2api_credential",
        side_effect=delete_then_change_local,
    ):
        conflicted = remove_account(
            account_id,
            database_engine=database_engine,
            codex2api_delete_on_account_remove_enabled=True,
        )

    assert conflicted["status"] == "local_delete_conflict"
    assert conflicted["error_code"] == "local_delete_conflict"
    assert _exists(database_engine, account_id)

    with mock.patch(
        "services.chatgpt_account_removal.delete_codex2api_credential",
        return_value={"status": "already_absent", "remote_id": 101, "message": ""},
    ):
        retried = remove_account(
            account_id,
            database_engine=database_engine,
            codex2api_delete_on_account_remove_enabled=True,
        )

    assert retried["ok"] is True
    assert retried["local_deleted"] is True
    assert not _exists(database_engine, account_id)


def test_task_checkpoints_run_before_remote_and_local_delete(database_engine):
    account_id, _created, _updated = _add_account(database_engine)
    checkpoints = []

    class Control:
        def checkpoint(self, *, attempt_id=None):
            checkpoints.append(attempt_id)

    def remote_delete(**_kwargs):
        assert checkpoints == [8]
        return {"status": "deleted", "remote_id": 111, "message": ""}

    with mock.patch(
        "services.chatgpt_account_removal.delete_codex2api_credential",
        side_effect=remote_delete,
    ):
        result = remove_account(
            account_id,
            database_engine=database_engine,
            codex2api_delete_on_account_remove_enabled=True,
            task_control=Control(),
            attempt_id=8,
        )

    assert result["ok"] is True
    assert checkpoints == [8, 8]


def test_busy_account_returns_without_remote_mutation(database_engine):
    account_id, _created, _updated = _add_account(database_engine)
    with chatgpt_account_operation_lock(account_id) as acquired:
        assert acquired is True
        with mock.patch(
            "services.chatgpt_account_removal.delete_codex2api_credential"
        ) as remote:
            result = remove_account(
                account_id,
                database_engine=database_engine,
                codex2api_delete_on_account_remove_enabled=True,
            )

    assert result["ok"] is False
    assert result["status"] == "busy"
    assert result["error_code"] == "account_busy"
    assert _exists(database_engine, account_id)
    remote.assert_not_called()


def test_already_locked_skips_reacquiring_nonreentrant_account_lock(database_engine):
    account_id, _created, _updated = _add_account(database_engine)
    with chatgpt_account_operation_lock(account_id) as acquired:
        assert acquired is True
        result = remove_account(
            account_id,
            database_engine=database_engine,
            codex2api_delete_on_account_remove_enabled=False,
            already_locked=True,
        )

    assert result["ok"] is True
    assert not _exists(database_engine, account_id)


def test_expected_timestamp_mismatch_fails_before_remote(database_engine):
    account_id, created_at, updated_at = _add_account(database_engine)
    with mock.patch(
        "services.chatgpt_account_removal.delete_codex2api_credential"
    ) as remote:
        result = remove_account(
            account_id,
            database_engine=database_engine,
            codex2api_delete_on_account_remove_enabled=True,
            expected_created_at=created_at,
            expected_updated_at=updated_at + timedelta(seconds=1),
        )

    assert result["status"] == "local_delete_conflict"
    assert _exists(database_engine, account_id)
    remote.assert_not_called()


def test_initially_missing_account_is_not_found(database_engine):
    result = remove_account(999, database_engine=database_engine)

    assert result["ok"] is False
    assert result["status"] == "not_found"
    assert result["error_code"] == "not_found"
