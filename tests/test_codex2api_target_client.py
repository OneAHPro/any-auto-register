from dataclasses import dataclass
from types import SimpleNamespace

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
    assert row.admin_key_ref == "codex2api_target_1_admin_key"
    with Session(engine) as session:
        assert session.get(db.Codex2APITargetModel, row.id) is not None


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
        get_extra=lambda: {"refresh_token": "refresh-token"},
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
