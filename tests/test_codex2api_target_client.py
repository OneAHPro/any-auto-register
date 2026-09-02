from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from unittest import mock

import pytest


@dataclass
class FakeResponse:
    payload: object = None
    status_code: int = 200
    text: str = ""

    def json(self):
        if isinstance(self.payload, BaseException):
            raise self.payload
        return self.payload


def test_client_lists_accounts_with_target_credentials(monkeypatch):
    from services import codex2api_target_client as module

    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse({"accounts": []})

    monkeypatch.setattr(module.cffi_requests, "get", fake_get)
    target = module.TargetConfig(
        id=2,
        name="node-b",
        base_url="https://node-b",
        admin_key="admin-secret",
    )

    assert module.Codex2APITargetClient(target).list_accounts() == []
    assert calls[0][0] == "https://node-b/api/admin/accounts?channel=codex"
    assert calls[0][1]["headers"]["X-Admin-Key"] == "admin-secret"


def test_client_sets_enabled_with_json_payload(monkeypatch):
    from services import codex2api_target_client as module

    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse({"message": "ok"})

    monkeypatch.setattr(module.cffi_requests, "post", fake_post)
    target = module.TargetConfig(
        id=1,
        name="node-a",
        base_url="https://node-a/",
        admin_key="secret",
    )

    result = module.Codex2APITargetClient(target).set_enabled(55, False)

    assert result == {"message": "ok"}
    assert calls[0][0] == "https://node-a/api/admin/accounts/55/enable"
    assert calls[0][1]["json"] == {"enabled": False}


def test_drain_requires_explicit_account_row_and_active_request_count(monkeypatch):
    from services import codex2api_target_client as module

    target = module.TargetConfig(
        id=1,
        name="node-a",
        base_url="https://node-a",
        admin_key="secret",
    )
    client = module.Codex2APITargetClient(target)
    monkeypatch.setattr(client, "list_accounts", lambda: [])
    with pytest.raises(module.Codex2APITargetError):
        client.wait_for_zero_active_requests(7, timeout_seconds=0)

    monkeypatch.setattr(client, "list_accounts", lambda: [{"id": 7}])
    with pytest.raises(module.Codex2APITargetError):
        client.wait_for_zero_active_requests(7, timeout_seconds=0)


def test_structured_target_rejects_missing_or_invalid_stable_id():
    from services.codex2api_target_client import load_target_configs

    with pytest.raises(ValueError):
        load_target_configs(
            {"codex2api_targets": [{"name": "node", "base_url": "https://node", "admin_key": "key"}]}
        )
    with pytest.raises(ValueError):
        load_target_configs(
            {"codex2api_targets": [{"id": 0, "name": "node", "base_url": "https://node", "admin_key": "key"}]}
        )


def test_client_parses_final_sse_complete_event(monkeypatch):
    from services import codex2api_target_client as module

    response = FakeResponse(
        payload=ValueError("not json"),
        text=(
            'data: {"type":"progress","current":1}\n\n'
            'data: {"type":"complete","success":1,"failed":0}\n\n'
        ),
    )
    monkeypatch.setattr(module.cffi_requests, "post", lambda *args, **kwargs: response)
    target = module.TargetConfig(
        id=2,
        name="node-b",
        base_url="https://node-b",
        admin_key="secret",
    )

    result = module.Codex2APITargetClient(target).import_full_json(
        {"email": "a@example.com", "refresh_token": "rt", "access_token": "at"}
    )

    assert result["success"] == 1
    assert result["failed"] == 0


def test_legacy_config_is_materialized_as_default_target():
    from services.codex2api_target_client import load_target_configs

    targets = load_target_configs(
        {
            "codex2api_api_url": "https://legacy/",
            "codex2api_admin_key": "legacy-secret",
        }
    )

    assert len(targets) == 1
    assert targets[0].name == "default"
    assert targets[0].base_url == "https://legacy"
    assert targets[0].admin_key == "legacy-secret"


def test_structured_target_config_takes_precedence_over_legacy_values():
    from services.codex2api_target_client import load_target_configs

    targets = load_target_configs(
        {
            "codex2api_api_url": "https://legacy",
            "codex2api_admin_key": "legacy-secret",
            "codex2api_targets": [
                {
                    "id": 2,
                    "name": "node-b",
                    "base_url": "https://node-b",
                    "admin_key": "new-secret",
                }
            ],
        }
    )

    assert [(target.id, target.name) for target in targets] == [(2, "node-b")]


def test_sensitive_client_errors_are_redacted(monkeypatch):
    from services import codex2api_target_client as module

    def failed_get(*args, **kwargs):
        raise RuntimeError("request failed with admin-secret")

    monkeypatch.setattr(module.cffi_requests, "get", failed_get)
    target = module.TargetConfig(
        id=1,
        name="node-a",
        base_url="https://node-a",
        admin_key="admin-secret",
    )

    with pytest.raises(module.Codex2APITargetError) as exc_info:
        module.Codex2APITargetClient(target).health()

    assert "admin-secret" not in str(exc_info.value)


def test_target_model_resolves_a_secret_reference_without_exposing_it():
    from services.codex2api_target_client import target_config_from_model

    model = SimpleNamespace(
        id=3,
        name="node-c",
        target_type="enterprise",
        server_label="us-c",
        base_url="https://node-c/",
        admin_key_ref="secret-ref-c",
        default_pool_id="ENTERPRISE_C_POOL",
        enabled=True,
    )

    target = target_config_from_model(model, {"secret-ref-c": "admin-secret"})

    assert target.id == 3
    assert target.base_url == "https://node-c"
    assert target.admin_key == "admin-secret"
    assert target.default_pool_id == "ENTERPRISE_C_POOL"


def test_default_target_can_resolve_legacy_admin_key_fallback():
    from services.codex2api_target_client import target_config_from_model

    model = SimpleNamespace(
        id=1,
        name="default",
        target_type="public",
        server_label="",
        base_url="https://legacy",
        admin_key_ref="codex2api_target_1_admin_key",
        default_pool_id="PUBLIC_POOL",
        enabled=True,
    )

    target = target_config_from_model(
        model,
        {"codex2api_admin_key": "legacy-secret"},
    )

    assert target.admin_key == "legacy-secret"


def test_ensure_default_target_materializes_legacy_configuration():
    from sqlalchemy.pool import StaticPool
    from sqlmodel import Session, create_engine

    from core import db
    from services.codex2api_target_client import ensure_default_target

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db.init_account_pool_schema(engine)

    row = ensure_default_target(
        engine,
        {
            "codex2api_api_url": "https://legacy",
            "codex2api_admin_key": "legacy-secret",
        },
    )

    assert row.name == "default"
    assert row.base_url == "https://legacy"
    assert row.admin_key_ref == "codex2api_admin_key"
    with Session(engine) as session:
        assert session.get(db.Codex2APITargetModel, row.id) is not None


def test_ensure_configured_targets_materializes_all_structured_targets(monkeypatch):
    from sqlalchemy.pool import StaticPool
    from sqlmodel import Session, create_engine, select

    from core import db
    from services.codex2api_target_client import ensure_configured_targets, load_db_target_configs
    from cryptography.fernet import Fernet

    monkeypatch.setenv("ACCOUNT_MANAGER_SECRET_KEY", Fernet.generate_key().decode())

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db.init_account_pool_schema(engine)
    config = {
        "codex2api_targets": [
            {"id": 1, "name": "node-a", "base_url": "https://a", "admin_key": "key-a"},
            {"id": 2, "name": "node-b", "base_url": "https://b", "admin_key": "key-b"},
        ]
    }

    rows = ensure_configured_targets(engine, config)

    assert [row.name for row in rows] == ["node-a", "node-b"]
    with mock.patch.object(module := __import__("services.codex2api_target_client", fromlist=["_config_snapshot"]), "_config_snapshot", return_value={
        "codex2api_target_1_admin_key": "key-a",
        "codex2api_target_2_admin_key": "key-b",
    }):
        configs = load_db_target_configs(engine)
    assert [(item.name, item.admin_key) for item in configs] == [("node-a", "key-a"), ("node-b", "key-b")]


def test_target_secret_is_persisted_under_reference_without_returning_from_model(monkeypatch):
    from sqlalchemy.pool import StaticPool
    from sqlmodel import Session, create_engine
    from cryptography.fernet import Fernet

    from core import db
    from core.config_store import ConfigItem
    from services.codex2api_target_client import ensure_configured_targets

    monkeypatch.setenv("ACCOUNT_MANAGER_SECRET_KEY", Fernet.generate_key().decode())

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db.init_account_pool_schema(engine)
    rows = ensure_configured_targets(
        engine,
        {"codex2api_targets": [{"id": 2, "name": "node-b", "base_url": "https://b", "admin_key": "secret-b"}]},
    )

    assert rows[0].admin_key_ref == "codex2api_target_2_admin_key"
    assert not hasattr(rows[0], "admin_key")
    with Session(engine) as session:
        stored = session.get(ConfigItem, rows[0].admin_key_ref).value
    assert "secret-b" not in stored
    assert stored.startswith("fernet:v1:")


def test_configured_target_id_is_the_database_target_id(monkeypatch):
    from sqlalchemy.pool import StaticPool
    from sqlmodel import create_engine
    from cryptography.fernet import Fernet

    from core import db
    from services.codex2api_target_client import ensure_configured_targets, get_target_client

    monkeypatch.setenv("ACCOUNT_MANAGER_SECRET_KEY", Fernet.generate_key().decode())

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db.init_account_pool_schema(engine)
    rows = ensure_configured_targets(
        engine,
        {"codex2api_targets": [{"id": 2, "name": "node-b", "base_url": "https://b", "admin_key": "secret-b"}]},
    )

    assert rows[0].id == 2
    assert get_target_client(2, engine).target.name == "node-b"


def test_client_redacts_credentials_echoed_by_error_response(monkeypatch):
    from services import codex2api_target_client as module

    response = FakeResponse(
        {"error": "refresh-secret access-secret session-secret"},
        status_code=500,
    )
    monkeypatch.setattr(module.cffi_requests, "post", lambda *args, **kwargs: response)
    target = module.TargetConfig(
        id=1,
        name="node-a",
        base_url="https://node-a",
        admin_key="admin-secret",
    )

    with pytest.raises(module.Codex2APITargetError) as exc_info:
        module.Codex2APITargetClient(target)._request(
            "POST",
            "/api/admin/accounts",
            json_body={
                "refresh_token": "refresh-secret",
                "access_token": "access-secret",
                "session_token": "session-secret",
            },
        )

    message = str(exc_info.value)
    assert "refresh-secret" not in message
    assert "access-secret" not in message
    assert "session-secret" not in message


def test_client_scrubs_credential_fields_from_success_payload(monkeypatch):
    from services import codex2api_target_client as module

    monkeypatch.setattr(
        module.cffi_requests,
        "get",
        lambda *args, **kwargs: FakeResponse(
            {
                "accounts": [
                    {
                        "id": 7,
                        "email": "a@example.com",
                        "status": "active",
                        "token": "access-secret",
                        "credentials": {"refresh_token": "refresh-secret"},
                        "allowed_api_key_ids": [1, 2],
                    }
                ]
            }
        ),
    )
    target = module.TargetConfig(
        id=1,
        name="node-a",
        base_url="https://node-a",
        admin_key="secret",
    )

    rows = module.Codex2APITargetClient(target).list_accounts()

    serialized = __import__("json").dumps(rows)
    assert "access-secret" not in serialized
    assert "refresh-secret" not in serialized
    assert rows[0]["allowed_api_key_ids"] == [1, 2]


def test_account_test_treats_usage_limit_as_authenticated(monkeypatch):
    from services import codex2api_target_client as module

    monkeypatch.setattr(
        module.cffi_requests,
        "get",
        lambda *args, **kwargs: FakeResponse(
            {"error": {"type": "usage_limit_reached"}},
            status_code=429,
        ),
    )
    target = module.TargetConfig(
        id=1,
        name="node-a",
        base_url="https://node-a",
        admin_key="secret",
    )

    result = module.Codex2APITargetClient(target).test_account(7)

    assert result["success"] is True
    assert result["usage_limited"] is True


def test_account_test_keeps_auth_failure_precedence_over_usage_text(monkeypatch):
    from services import codex2api_target_client as module

    monkeypatch.setattr(
        module.cffi_requests,
        "get",
        lambda *args, **kwargs: FakeResponse(
            payload=ValueError("not json"),
            status_code=429,
            text="token_invalidated; usage limit reached",
        ),
    )
    target = module.TargetConfig(
        id=1,
        name="node-a",
        base_url="https://node-a",
        admin_key="secret",
    )

    result = module.Codex2APITargetClient(target).test_account(7)

    assert result["success"] is False
    assert result["auth_failed"] is True


def test_account_test_rejects_unclassified_rate_limit_response(monkeypatch):
    from services import codex2api_target_client as module

    monkeypatch.setattr(
        module.cffi_requests,
        "get",
        lambda *args, **kwargs: FakeResponse(
            {"error": "temporary upstream issue"},
            status_code=429,
        ),
    )
    target = module.TargetConfig(
        id=1,
        name="node-a",
        base_url="https://node-a",
        admin_key="secret",
    )

    result = module.Codex2APITargetClient(target).test_account(7)

    assert result["success"] is False
    assert result["verified"] is False


def test_capabilities_include_restore_from_supported_upstream_contract(monkeypatch):
    from services import codex2api_target_client as module

    monkeypatch.setattr(
        module.cffi_requests,
        "get",
        lambda *args, **kwargs: FakeResponse({"settings": {}}),
    )
    target = module.TargetConfig(
        id=1,
        name="node-a",
        base_url="https://node-a",
        admin_key="secret",
    )

    capabilities = module.Codex2APITargetClient(target).capabilities()

    assert capabilities["restore"] is True


def test_quota_reader_accepts_an_explicit_target_client():
    from services import chatgpt_codex2api_health as health

    target = SimpleNamespace(
        list_accounts=lambda: [
            {
                "id": 9,
                "email": "a@example.com",
                "status": "active",
                "usage_percent_7d": 10,
                "billed_7d": 20,
            }
        ]
    )

    rows = health.fetch_codex2api_quota_accounts(client=target)

    assert rows[0]["remote_id"] == 9
    assert rows[0]["billed_7d"] == 20


def test_sync_accepts_an_explicit_target_client(monkeypatch):
    from services import external_sync

    account = SimpleNamespace(
        email="a@example.com",
        token="access-token",
        user_id="workspace-1",
        get_extra=lambda: {
            "refresh_token": "refresh-token",
            "workspace_id": "workspace-1",
            "account_id": "different-account-id",
        },
    )
    calls = []

    class FakeTarget:
        def import_full_json(self, payload):
            calls.append(payload)
            return {"success": 1, "failed": 0}

    result = external_sync.sync_codex2api_account(
        account,
        target=SimpleNamespace(id=2),
        client=FakeTarget(),
    )

    assert result == {"name": "Codex2API", "ok": True, "msg": "目标账号已导入"}
    assert calls[0]["email"] == "a@example.com"
    assert calls[0]["refresh_token"] == "refresh-token"
    assert calls[0]["account_id"] == "workspace-1"


def test_explicit_target_sync_holds_shared_mutation_lock(monkeypatch):
    from services import external_sync

    events = []

    @contextmanager
    def tracked_lock():
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    class FakeTarget:
        def import_full_json(self, payload):
            events.append("import")
            return {"success": 1, "failed": 0}

    monkeypatch.setattr(external_sync, "codex2api_account_mutation_lock", tracked_lock)
    account = SimpleNamespace(
        email="a@example.com",
        token="access-token",
        user_id="workspace-1",
        get_extra=lambda: {"refresh_token": "refresh-token"},
    )

    result = external_sync.sync_codex2api_account(
        account,
        target=SimpleNamespace(id=2),
        client=FakeTarget(),
    )

    assert result["ok"] is True
    assert events == ["enter", "import", "exit"]


def test_explicit_target_sync_persists_structured_binding():
    from sqlalchemy.pool import StaticPool
    from sqlmodel import Session, create_engine, select

    from core import db
    from services.account_identity import ensure_identity
    from services import external_sync

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db.init_account_pool_schema(engine)
    account = db.AccountModel(
        platform="chatgpt",
        email="a@example.com",
        password="password",
        token="access-token",
        extra_json='{"refresh_token":"refresh-token","workspace_id":"workspace-1"}',
    )
    with Session(engine) as session:
        session.add(account)
        session.commit()
        session.refresh(account)
    identity = ensure_identity(
        engine,
        account_id=account.id,
        platform="chatgpt",
        email=account.email,
        workspace_id="workspace-1",
    )
    with Session(engine) as session:
        account = session.get(db.AccountModel, account.id)

    class FakeTarget:
        def import_full_json(self, payload):
            return {"success": 1, "failed": 0}

        def list_accounts(self):
            return [{"id": 77, "email": "a@example.com", "status": "active", "enabled": True}]

    result = external_sync.sync_codex2api_account(
        account,
        target=SimpleNamespace(id=2),
        client=FakeTarget(),
        database_engine=engine,
    )

    assert result["ok"] is True
    with Session(engine) as session:
        binding = session.exec(select(db.AccountTargetBindingModel)).one()
    assert binding.identity_id == identity.identity_id
    assert binding.target_id == 2
    assert binding.remote_account_id == 77
    assert binding.sync_status == "synced"
