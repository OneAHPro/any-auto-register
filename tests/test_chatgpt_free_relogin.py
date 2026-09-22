from types import SimpleNamespace
from unittest.mock import Mock
from datetime import datetime, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from core.db import AccountModel, ChatGPTAuthStateModel
from services import chatgpt_relogin as relogin


@pytest.fixture
def account_setup(monkeypatch):
    database = create_engine("sqlite://")
    SQLModel.metadata.create_all(database)
    monkeypatch.setattr(relogin, "engine", database)
    monkeypatch.setattr("core.db.engine", database)
    with Session(database) as session:
        account = AccountModel(platform="chatgpt", email="free@example.com",
                               password="saved-password", token="old-access")
        account.set_extra({"refresh_token": "old-refresh", "mailbox_login_context": {
            "provider": "chatgpt_credentials", "email": account.email,
            "extra": {"account_type": "chatgpt_password_totp",
                      "password": account.password, "totp_secret": "JBSWY3DPEHPK3PXP"},
        }})
        session.add(account)
        session.commit()
        session.refresh(account)
        account_id = account.id
    monkeypatch.setattr(relogin.config_store, "get_all", lambda: {
        "chatgpt_subscription_gate_enabled": False,
        "codex2api_delete_on_account_remove_enabled": "0",
    })
    remote_delete = Mock(return_value={"status": "deleted", "remote_id": 11})
    monkeypatch.setattr("services.chatgpt_account_removal.delete_codex2api_credential", remote_delete)
    sync = Mock(return_value={"ok": True, "msg": "ok"})
    monkeypatch.setattr(relogin, "sync_codex2api_account", sync)
    yield database, account_id, remote_delete, sync
    database.dispose()


def free_result():
    return SimpleNamespace(success=False, skipped=True, error_code="free_plan",
                           error_message="检测到 Free 套餐", metadata={"subscription_plan": "free"})


def test_saved_free_login_deletes_before_sync_and_overrides_disabled_gate(account_setup, monkeypatch):
    database, account_id, remote_delete, sync = account_setup
    adapter = Mock()
    adapter.run.return_value = free_result()
    monkeypatch.setattr(relogin, "build_chatgpt_registration_mode_adapter", lambda _: adapter)
    result = relogin.relogin_chatgpt_account(account_id)
    assert adapter.run.call_args.args[0].extra_config["chatgpt_subscription_gate_enabled"] is True
    assert result["account_removed"] is True
    assert result["removal_reason"] == "free_plan"
    remote_delete.assert_called_once()
    sync.assert_not_called()
    with Session(database) as session:
        assert session.get(AccountModel, account_id) is None
        assert not session.exec(select(ChatGPTAuthStateModel)).all()


def test_free_cleanup_failure_keeps_account_and_never_syncs(account_setup, monkeypatch):
    database, account_id, remote_delete, sync = account_setup
    remote_delete.return_value = {"status": "failed", "message": "remote unavailable"}
    adapter = Mock()
    adapter.run.return_value = free_result()
    monkeypatch.setattr(relogin, "build_chatgpt_registration_mode_adapter", lambda _: adapter)
    result = relogin.relogin_chatgpt_account(account_id)
    assert result["stage"] == "account_remove_failed"
    assert result["account_removed"] is False
    sync.assert_not_called()
    with Session(database) as session:
        assert session.get(AccountModel, account_id).token == "old-access"


@pytest.mark.parametrize("plan", ["free", "plus", "pro", "team", "unknown"])
def test_refresh_checks_plan_before_external_sync(account_setup, monkeypatch, plan):
    database, account_id, remote_delete, sync = account_setup
    manager = Mock()
    manager.refresh_by_oauth_token.return_value = SimpleNamespace(
        success=True, state="valid", access_token="fresh-access", refresh_token="fresh-refresh",
        http_status=200, error_code="",
    )
    monkeypatch.setattr(relogin, "TokenRefreshManager", lambda **_: manager)
    probe = Mock(return_value={"plan": plan, "http_status": 503 if plan == "unknown" else 200})
    monkeypatch.setattr("platforms.chatgpt.status_probe.probe_chatgpt_subscription", probe)
    result = relogin.refresh_or_relogin_chatgpt_account(account_id)
    probe.assert_called_once_with("fresh-access", proxy=None)
    with Session(database) as session:
        saved = session.get(AccountModel, account_id)
        if plan == "free":
            assert saved is None
            assert result["account_removed"] is True
            assert result["removal_reason"] == "free_plan"
            remote_delete.assert_called_once()
            sync.assert_not_called()
        else:
            assert saved.get_extra()["refresh_token"] == "fresh-refresh"
            remote_delete.assert_not_called()
            if plan == "unknown":
                assert result["ok"] is False
                sync.assert_not_called()
            else:
                assert result["ok"] is True
                sync.assert_called_once()


def test_unknown_login_does_not_delete_or_sync(account_setup, monkeypatch):
    database, account_id, remote_delete, sync = account_setup
    adapter = Mock()
    adapter.run.return_value = SimpleNamespace(
        success=False, skipped=False, error_code="subscription_probe_failed",
        error_message="订阅套餐检测失败", metadata={"subscription_plan": "unknown"},
    )
    monkeypatch.setattr(relogin, "build_chatgpt_registration_mode_adapter", lambda _: adapter)
    result = relogin.relogin_chatgpt_account(account_id)
    assert result["ok"] is False
    remote_delete.assert_not_called()
    sync.assert_not_called()
    with Session(database) as session:
        assert session.get(AccountModel, account_id) is not None


def test_real_login_engine_free_result_reaches_removal(account_setup, monkeypatch):
    database, account_id, remote_delete, sync = account_setup
    from platforms.chatgpt.refresh_token_registration_engine import RefreshTokenRegistrationEngine

    oauth = Mock()
    oauth.config = {}
    oauth.login_and_get_tokens.return_value = {
        "access_token": "free-access", "refresh_token": "free-refresh", "account_id": "fixture",
    }
    monkeypatch.setattr(RefreshTokenRegistrationEngine, "_build_oauth_client", lambda _: oauth)
    probe = Mock(return_value={"plan": "free", "http_status": 200})
    monkeypatch.setattr("platforms.chatgpt.refresh_token_registration_engine.probe_chatgpt_subscription", probe)
    result = relogin.relogin_chatgpt_account(account_id)
    assert result["removal_reason"] == "free_plan"
    assert result["account_removed"] is True
    probe.assert_called_once_with("free-access", proxy=None)
    sync.assert_not_called()
    remote_delete.assert_called_once()
    with Session(database) as session:
        assert session.get(AccountModel, account_id) is None


def test_import_free_cleanup_selects_local_owner_with_inventory_mirror(account_setup):
    from api.tasks import _load_unique_chatgpt_account_identity

    database, account_id, _, _ = account_setup
    with Session(database) as session:
        mirror = AccountModel(platform="chatgpt", email="free@example.com", password="")
        mirror.set_extra({"remote_only": True})
        session.add(mirror)
        session.commit()
    identity = _load_unique_chatgpt_account_identity(
        "free@example.com", database_engine=database, prefer_local_credentials=True,
    )
    assert identity.id == account_id


def test_free_removal_after_login_persists_a_new_primary_password(account_setup, monkeypatch):
    database, account_id, remote_delete, sync = account_setup
    adapter = Mock()
    with Session(database) as session:
        account = session.get(AccountModel, account_id)
        extra = account.get_extra()
        extra["mailbox_login_context"]["extra"].update(
            account_type="chatgpt_password_url_otp", mail_api_url="https://mail.example.test/message",
        )
        account.set_extra(extra)
        session.add(account)
        session.commit()

    def login(context):
        # The password-reset callback persists its result before the plan gate.
        assert context.email_service.commit_password_reset("new-primary-password")
        return free_result()

    adapter.run.side_effect = login
    monkeypatch.setattr(relogin, "build_chatgpt_registration_mode_adapter", lambda _: adapter)
    result = relogin.relogin_chatgpt_account(account_id)
    assert result["account_removed"] is True
    sync.assert_not_called()
    remote_delete.assert_called_once()


@pytest.mark.parametrize("identity_change", ["created_at", "email", "updated_at"])
def test_free_removal_rejects_changed_account_identity(account_setup, monkeypatch, identity_change):
    database, account_id, remote_delete, sync = account_setup
    adapter = Mock()

    def login(_context):
        with Session(database) as session:
            account = session.get(AccountModel, account_id)
            setattr(account, identity_change, datetime.now(timezone.utc)
                    if identity_change != "email" else "replacement@example.com")
            session.add(account)
            session.commit()
        return free_result()

    adapter.run.side_effect = login
    monkeypatch.setattr(relogin, "build_chatgpt_registration_mode_adapter", lambda _: adapter)
    result = relogin.relogin_chatgpt_account(account_id)
    assert result["account_removed"] is False
    remote_delete.assert_not_called()
    sync.assert_not_called()
    with Session(database) as session:
        assert session.get(AccountModel, account_id) is not None
