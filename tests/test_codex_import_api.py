import time

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

from core import db
from api.codex_import import CodexImportRequest, ImportFile, get_import_job, start_import, import_options


class Client:
    def __init__(self):
        self.imported = []
        self.agent_imported = []

    def import_refresh_token(self, payload):
        self.imported.append(dict(payload))
        return {"success": 1}

    def import_access_token(self, payload):
        self.imported.append(dict(payload))
        return {"success": 1}

    def import_full_json(self, payload):
        self.imported.append(dict(payload))
        return {"success": 1}

    def import_agent_identity(self, payload):
        self.agent_imported.append(dict(payload))
        return {"success": 1}

    def list_accounts(self):
        return [{"id": 7, "email": "one@example.com", "status": "active", "enabled": True}]


def make_engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    db.init_account_pool_schema(engine)
    with Session(engine) as session:
        session.add(db.Codex2APITargetModel(id=1, name="default", base_url="https://node", admin_key_ref="key", default_pool_id="PUBLIC_POOL", enabled=True))
        session.commit()
    return engine


def test_import_options_exposes_default_public_pool_and_target():
    engine = make_engine()
    with Session(engine) as session:
        result = import_options(session)
    assert result["default_pool_id"] == "PUBLIC_POOL"
    assert any(pool["id"] == "PUBLIC_POOL" for pool in result["pools"])
    assert result["pools"][0]["targets"][0]["id"] == 1


def test_import_job_creates_local_account_and_syncs_refresh_token(monkeypatch):
    engine = make_engine()
    client = Client()
    monkeypatch.setattr("services.codex2api_target_client.get_target_client", lambda target_id, database_engine: client)
    with Session(engine) as session:
        started = start_import(
            CodexImportRequest(pool_id="PUBLIC_POOL", target_id=1, format="json", files=[ImportFile(name="account.json", content='{"refresh_token":"rt","email":"one@example.com","chatgpt_account_id":"acct-1"}')]),
            session,
        )
    deadline = time.time() + 3
    while time.time() < deadline:
        job = get_import_job(started["job_id"])
        if job["status"] in {"completed", "failed"}:
            break
        time.sleep(0.01)
    assert job["status"] == "completed"
    assert job["success"] == 1
    assert client.imported[0]["refresh_token"] == "rt"
    with Session(engine) as session:
        account = session.exec(select(db.AccountModel)).one()
        assignment = session.exec(select(db.AccountAssignmentModel)).one()
    assert account.email == "one@example.com"
    assert assignment.pool_id == "PUBLIC_POOL"
    assert assignment.target_id == 1


def test_explicit_target_must_belong_to_selected_pool():
    engine = make_engine()
    with Session(engine) as session:
        session.add(db.AccountPoolModel(id="OTHER_POOL", name="Other", enabled=True))
        session.commit()
        try:
            start_import(
                CodexImportRequest(pool_id="OTHER_POOL", target_id=1, format="txt",
                                   files=[ImportFile(name="tokens.txt", content="rt")]),
                session,
            )
        except Exception as exc:
            assert getattr(exc, "status_code", None) == 409
        else:
            raise AssertionError("target outside selected pool should be rejected")


def test_import_job_routes_agent_identity_to_dedicated_importer(monkeypatch):
    engine = make_engine()
    client = Client()
    monkeypatch.setattr("services.codex2api_target_client.get_target_client", lambda target_id, database_engine: client)
    content = '{"agent_identity":{"agent_runtime_id":"runtime-1","agent_private_key":"private-1","email":"agent@example.com"}}'
    with Session(engine) as session:
        started = start_import(
            CodexImportRequest(pool_id="PUBLIC_POOL", target_id=1, format="json", files=[ImportFile(name="agent.json", content=content)]),
            session,
        )
    deadline = time.time() + 3
    while time.time() < deadline:
        job = get_import_job(started["job_id"])
        if job["status"] in {"completed", "failed"}:
            break
        time.sleep(0.01)
    assert job["status"] == "completed"
    assert len(client.agent_imported) == 1
    assert client.agent_imported[0]["agent_runtime_id"] == "runtime-1"
