import json
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

from api.accounts import router
from api.chatgpt import router as chatgpt_router
from core import db
from core.base_platform import Account
from services.codex_inventory import materialize_inventory, sync_inventory


@pytest.fixture
def account_api():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db.init_account_pool_schema(engine)
    with Session(engine) as session:
        account = db.AccountModel(
            platform="chatgpt",
            email="cost@example.com",
            password="password",
            extra_json=json.dumps({"account_type": "chatgpt_password", "note": "keep"}),
        )
        session.add(account)
        session.commit()
        session.refresh(account)
        account_id = account.id

    def session_override():
        with Session(engine) as session:
            yield session

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.include_router(chatgpt_router, prefix="/api")
    app.dependency_overrides[db.get_session] = session_override
    with TestClient(app) as client:
        yield client, engine, account_id
    engine.dispose()


@pytest.mark.parametrize(
    ("amount", "expected"),
    [("12.34", "12.34"), (12.3, "12.30"), (0, "0.00"), ("999999999.99", "999999999.99")],
)
def test_patch_purchase_cost_persists_and_round_trips(account_api, amount, expected):
    client, engine, account_id = account_api

    response = client.patch(f"/api/accounts/{account_id}", json={"purchase_cost_cny": amount})

    assert response.status_code == 200
    extra = json.loads(response.json()["extra_json"])
    assert extra["purchase_cost_cny"] == expected
    assert extra["note"] == "keep"
    assert "purchase_cost_cents" not in response.json()
    with Session(engine) as session:
        stored = session.get(db.AccountModel, account_id)
        assert stored.purchase_cost_cents == int(Decimal(expected) * 100)
        assert "purchase_cost_cny" not in stored.get_extra()
    detail = client.get(f"/api/accounts/{account_id}").json()
    listing = client.get("/api/accounts?platform=chatgpt").json()
    assert json.loads(detail["extra_json"])["purchase_cost_cny"] == expected
    assert json.loads(listing["items"][0]["extra_json"])["purchase_cost_cny"] == expected


def test_patch_purchase_cost_can_be_changed_cleared_and_omitted(account_api):
    client, engine, account_id = account_api
    path = f"/api/accounts/{account_id}"
    client.patch(path, json={"purchase_cost_cny": "23.45"})
    changed = client.patch(path, json={"purchase_cost_cny": "10"})
    assert json.loads(changed.json()["extra_json"])["purchase_cost_cny"] == "10.00"

    retained = client.patch(path, json={"status": "subscribed"})
    assert json.loads(retained.json()["extra_json"])["purchase_cost_cny"] == "10.00"
    cleared = client.patch(path, json={"purchase_cost_cny": None})
    assert cleared.status_code == 200
    assert "purchase_cost_cny" not in json.loads(cleared.json()["extra_json"])
    assert json.loads(cleared.json()["extra_json"])["note"] == "keep"
    with Session(engine) as session:
        assert session.get(db.AccountModel, account_id).purchase_cost_cents is None


@pytest.mark.parametrize(
    "amount",
    [True, False, -1, "-0.01", "1.001", "999999999.991", 1000000000,
     "NaN", "Infinity", "-Infinity", "", "not-a-number", [], {}],
)
def test_patch_purchase_cost_rejects_invalid_values_without_mutation(account_api, amount):
    client, engine, account_id = account_api
    seeded = client.patch(f"/api/accounts/{account_id}", json={"purchase_cost_cny": "45.67"})
    assert seeded.status_code == 200

    response = client.patch(f"/api/accounts/{account_id}", json={"purchase_cost_cny": amount})

    assert response.status_code == 422
    with Session(engine) as session:
        assert session.get(db.AccountModel, account_id).purchase_cost_cents == 4567


@pytest.mark.parametrize("source", ["registration", "existing_account_web_login"])
@pytest.mark.parametrize("local_cost", ["12.34", "0.00", None])
@pytest.mark.parametrize("incoming_cost", ["98.76", None, "omitted"])
def test_credential_save_preserves_current_local_purchase_cost(
    account_api, monkeypatch, source, local_cost, incoming_cost,
):
    client, engine, account_id = account_api
    client.patch(f"/api/accounts/{account_id}", json={"purchase_cost_cny": local_cost})
    incoming_extra = {"chatgpt_token_source": source, "access_token": "new-token"}
    if incoming_cost != "omitted":
        incoming_extra["purchase_cost_cny"] = incoming_cost
    incoming = Account(
        platform="chatgpt", email="cost@example.com", password="new-password",
        token="new-token", extra=incoming_extra,
    )
    monkeypatch.setattr(db, "engine", engine)

    saved, created = db.save_account_with_creation_state(incoming)

    assert created is False
    assert saved.id == account_id
    assert saved.token == "new-token"
    displayed = json.loads(client.get(f"/api/accounts/{account_id}").json()["extra_json"])
    if local_cost is None:
        assert saved.purchase_cost_cents is None
        assert "purchase_cost_cny" not in displayed
    else:
        assert saved.purchase_cost_cents == int(Decimal(local_cost) * 100)
        assert displayed["purchase_cost_cny"] == local_cost


def test_remote_managed_account_cost_survives_inventory_refresh(account_api):
    client, engine, _ = account_api

    class InventoryClient:
        def list_accounts(self):
            return [{
                "id": 7, "email": "remote@example.com", "status": "active",
                "purchase_cost_cny": "999.99",
            }]

    sync_inventory(engine, target_id=1, clients={1: InventoryClient()})
    materialize_inventory(engine)
    with Session(engine) as session:
        remote = session.exec(
            select(db.AccountModel).where(db.AccountModel.email == "remote@example.com")
        ).one()
        remote_id = remote.id
        assert remote_id > 0
        assert remote.get_extra()["remote_only"] is True

    response = client.patch(f"/api/accounts/{remote_id}", json={"purchase_cost_cny": "8.90"})

    assert response.status_code == 200
    assert json.loads(response.json()["extra_json"])["purchase_cost_cny"] == "8.90"
    sync_inventory(engine, target_id=1, clients={1: InventoryClient()})
    materialize_inventory(engine)
    with Session(engine) as session:
        saved = session.get(db.AccountModel, remote_id)
        assert saved.purchase_cost_cents == 890
        assert saved.get_extra()["remote_only"] is True
    detail = client.get(f"/api/accounts/{remote_id}").json()
    assert json.loads(detail["extra_json"])["purchase_cost_cny"] == "8.90"


def test_relogin_token_refresh_preserves_purchase_cost(account_api, monkeypatch):
    from services import chatgpt_relogin

    client, engine, account_id = account_api
    client.patch(f"/api/accounts/{account_id}", json={"purchase_cost_cny": "19.99"})
    with Session(engine) as session:
        account = session.get(db.AccountModel, account_id)
        created_at = account.created_at
    monkeypatch.setattr(chatgpt_relogin, "engine", engine)

    chatgpt_relogin._persist_fresh_tokens(
        account_id,
        {"access_token": "fresh-access", "refresh_token": "fresh-refresh"},
        expected_email="cost@example.com",
        expected_created_at=created_at,
    )

    with Session(engine) as session:
        saved = session.get(db.AccountModel, account_id)
        assert saved.token == "fresh-access"
        assert saved.purchase_cost_cents == 1999


def test_purchase_cost_migration_is_additive_and_idempotent():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE accounts (id INTEGER PRIMARY KEY, platform TEXT, email TEXT, extra_json TEXT)"
        )
        connection.exec_driver_sql(
            "INSERT INTO accounts (id, platform, email, extra_json) VALUES (?, ?, ?, ?)",
            (1, "chatgpt", "legacy@example.com", '{"purchase_cost_cny":"25.00"}'),
        )

    db.init_account_pool_schema(engine)

    with engine.begin() as connection:
        columns = {row[1]: row for row in connection.exec_driver_sql("PRAGMA table_info('accounts')")}
        assert "purchase_cost_cents" in columns
        assert columns["purchase_cost_cents"][2] == "INTEGER"
        row = connection.exec_driver_sql("SELECT purchase_cost_cents, extra_json FROM accounts WHERE id = 1").one()
        assert row == (None, '{"purchase_cost_cny":"25.00"}')
        connection.exec_driver_sql("UPDATE accounts SET purchase_cost_cents = 1234 WHERE id = 1")
    db.init_account_pool_schema(engine)
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT purchase_cost_cents FROM accounts WHERE id = 1").scalar() == 1234
    engine.dispose()


def test_response_ignores_stale_extra_cost_after_clear(account_api):
    client, engine, account_id = account_api
    path = f"/api/accounts/{account_id}"
    client.patch(path, json={"purchase_cost_cny": "20.00"})
    with Session(engine) as session:
        account = session.get(db.AccountModel, account_id)
        extra = account.get_extra()
        extra["purchase_cost_cny"] = "50.00"
        account.set_extra(extra)
        session.add(account)
        session.commit()
    assert json.loads(client.get(path).json()["extra_json"])["purchase_cost_cny"] == "20.00"

    client.patch(path, json={"purchase_cost_cny": None})

    assert "purchase_cost_cny" not in json.loads(client.get(path).json()["extra_json"])
    listing = client.get("/api/accounts?platform=chatgpt").json()
    assert "purchase_cost_cny" not in json.loads(listing["items"][0]["extra_json"])


@pytest.mark.parametrize("concurrent_cost", ["56.78", None])
def test_concurrent_token_refresh_does_not_overwrite_purchase_cost(
    account_api, monkeypatch, concurrent_cost,
):
    from platforms.chatgpt.token_refresh import TokenRefreshManager, TokenRefreshResult

    client, engine, account_id = account_api
    path = f"/api/accounts/{account_id}"
    client.patch(path, json={"purchase_cost_cny": "12.34"})

    def refresh_after_cost_patch(_manager, _account):
        changed = client.patch(path, json={"purchase_cost_cny": concurrent_cost})
        assert changed.status_code == 200
        return TokenRefreshResult(success=True, access_token="new-access", refresh_token="new-refresh")

    monkeypatch.setattr(TokenRefreshManager, "refresh_account", refresh_after_cost_patch)

    response = client.post(f"/api/chatgpt/{account_id}/refresh-token")

    assert response.status_code == 200
    extra = json.loads(client.get(path).json()["extra_json"])
    if concurrent_cost is None:
        assert "purchase_cost_cny" not in extra
    else:
        assert extra["purchase_cost_cny"] == concurrent_cost
    with Session(engine) as session:
        saved = session.get(db.AccountModel, account_id)
        assert saved.token == "new-access"
        assert saved.purchase_cost_cents == (None if concurrent_cost is None else 5678)
