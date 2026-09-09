import json
import time

import pytest

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

from core import db
from api.codex_import import CodexImportRequest, ImportFile, _ImportJob, _import_job, get_import_job, start_import, import_options


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
        # Mirror the rows accepted by the fake import endpoint.  The import
        # worker now requires a unique, positively identified remote row
        # before reporting success, so a fixture list must expose each
        # imported credential just like Codex2API does.
        rows = []
        for payload in [*self.imported, *self.agent_imported]:
            row = {
                "id": 7 + len(rows),
                "email": payload.get("email") or payload.get("name"),
                "status": "active",
                "enabled": True,
            }
            stable_id = payload.get("chatgpt_account_id") or payload.get("account_id")
            if stable_id:
                row["chatgpt_account_id"] = stable_id
            rows.append(row)
        return rows


def make_engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    db.init_account_pool_schema(engine)
    with Session(engine) as session:
        session.add(db.AccountPoolModel(id="PUBLIC_POOL", name="Public", enabled=True))
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


def test_import_job_quarantines_ambiguous_remote_match(monkeypatch):
    engine = make_engine()

    class AmbiguousClient(Client):
        def list_accounts(self):
            return [
                {"id": 7, "email": "ambiguous@example.com", "status": "active"},
                {"id": 8, "email": "ambiguous@example.com", "status": "active"},
            ]

    client = AmbiguousClient()
    monkeypatch.setattr("services.codex2api_target_client.get_target_client", lambda *_: client)
    request = CodexImportRequest(
        pool_id="PUBLIC_POOL",
        target_id=1,
        format="json",
        files=[ImportFile(
            name="ambiguous.json",
            content='{"refresh_token":"rt-ambiguous","email":"ambiguous@example.com"}',
        )],
    )
    job = _ImportJob(id="fixture-ambiguous-remote")

    _import_job(job, request, engine)

    assert job.status == "completed", job.error
    assert (job.total, job.processed, job.success, job.failed) == (1, 1, 0, 1)
    item = job.items[0]
    assert item["status"] == "needs_confirmation"
    assert item["needs_confirmation"] is True
    assert item["sync_status"] == "ambiguous"
    assert item["error_code"] == "remote_identity_ambiguous"
    assert item["diagnostic"]["candidate_count"] == 2
    with Session(engine) as session:
        account = session.exec(select(db.AccountModel)).one()
        assignment = session.exec(select(db.AccountAssignmentModel)).one()
        identity = session.get(db.AccountIdentityModel, account.identity_id)
    assert account.status == "invalid"
    assert assignment.state == "standby"
    assert identity is not None and identity.state == "ambiguous"


def test_import_job_rejects_non_positive_remote_id_and_quarantines_assignment(monkeypatch):
    engine = make_engine()

    class MissingIdClient(Client):
        def list_accounts(self):
            return [{
                "id": 1.5,
                "remote_id": 1.5,
                "email": "missing-id@example.com",
                "status": "active",
            }]

    client = MissingIdClient()
    monkeypatch.setattr("services.codex2api_target_client.get_target_client", lambda *_: client)
    request = CodexImportRequest(
        pool_id="PUBLIC_POOL",
        target_id=1,
        format="json",
        files=[ImportFile(
            name="missing-id.json",
            content='{"refresh_token":"rt-missing-id","email":"missing-id@example.com"}',
        )],
    )
    job = _ImportJob(id="fixture-missing-remote-id")

    _import_job(job, request, engine)

    assert job.status == "completed", job.error
    assert (job.total, job.processed, job.success, job.failed) == (1, 1, 0, 1)
    item = job.items[0]
    assert item["status"] == "failed"
    assert item["needs_confirmation"] is False
    assert item["sync_status"] == "failed"
    assert item["error_code"] == "remote_id_missing"
    assert item["diagnostic"]["remote_id"] == 0
    with Session(engine) as session:
        account = session.exec(select(db.AccountModel)).one()
        assignment = session.exec(select(db.AccountAssignmentModel)).one()
    assert account.status == "invalid"
    assert assignment.state == "standby"


def test_assigning_a_target_supersedes_another_target_binding_for_the_identity():
    from api.codex_import import _identity_and_assignment

    engine = make_engine()
    account = db.AccountModel(
        platform="chatgpt",
        email="move@example.com",
        password="",
        identity_id="identity-move",
    )
    new_target = db.Codex2APITargetModel(
        id=2,
        name="secondary",
        base_url="https://secondary",
        admin_key_ref="secondary-key",
        default_pool_id="PUBLIC_POOL",
        enabled=True,
    )
    with Session(engine) as session:
        session.add_all([
            account,
            new_target,
            db.AccountIdentityModel(
                id="identity-move",
                platform="chatgpt",
                canonical_email=account.email,
            ),
            db.AccountTargetBindingModel(
                identity_id="identity-move",
                local_account_id=0,
                target_id=2,
                remote_account_id=99,
                enabled=True,
                sync_status="synced",
            ),
        ])
        session.commit()
        session.refresh(account)
        pool = session.get(db.AccountPoolModel, "PUBLIC_POOL")
        target = session.get(db.Codex2APITargetModel, 1)
        _identity_and_assignment(session, account, pool, target)
        session.commit()

    with Session(engine) as session:
        old = session.exec(
            select(db.AccountTargetBindingModel).where(
                db.AccountTargetBindingModel.target_id == 2
            )
        ).one()
    assert old.enabled is False
    assert old.sync_status == "superseded"


def test_same_credential_can_be_synced_to_an_explicit_second_target(monkeypatch):
    engine = make_engine()
    with Session(engine) as session:
        session.add(db.Codex2APITargetModel(
            id=2, name="secondary", base_url="https://secondary",
            admin_key_ref="secondary-key", default_pool_id="PUBLIC_POOL", enabled=True,
        ))
        session.commit()

    clients = {1: Client(), 2: Client()}
    monkeypatch.setattr(
        "services.codex2api_target_client.get_target_client",
        lambda target_id, database_engine: clients[int(target_id)],
    )
    request_one = CodexImportRequest(
        pool_id="PUBLIC_POOL", target_id=1, format="json",
        files=[ImportFile(
            name="same.json",
            content='{"refresh_token":"rt-shared","email":"shared@example.com","chatgpt_account_id":"shared-id"}',
        )],
    )
    first = _ImportJob(id="fixture-multi-target-first")
    _import_job(first, request_one, engine)
    assert first.success == 1, first.error

    request_two = request_one.model_copy(update={"target_id": 2})
    second = _ImportJob(id="fixture-multi-target-second")
    _import_job(second, request_two, engine)

    assert second.status == "completed", second.error
    assert second.success == 1
    assert second.duplicate == 0
    assert len(clients[1].imported) == 1
    assert len(clients[2].imported) == 1
    with Session(engine) as session:
        account = session.exec(select(db.AccountModel)).one()
        bindings = session.exec(
            select(db.AccountTargetBindingModel).order_by(db.AccountTargetBindingModel.target_id)
        ).all()
        assignment = session.exec(select(db.AccountAssignmentModel)).one()
    assert account.id is not None
    assert len(bindings) == 2
    assert bindings[0].enabled is False and bindings[0].sync_status == "superseded"
    assert bindings[1].enabled is True and bindings[1].target_id == 2
    assert assignment.target_id == 2 and assignment.state == "active"


def test_import_replaces_a_dangling_or_cross_platform_identity_reference():
    from api.codex_import import _identity_and_assignment

    engine = make_engine()
    account = db.AccountModel(
        platform="chatgpt", email="dangling@example.com", password="",
        identity_id="missing-identity",
    )
    with Session(engine) as session:
        session.add(account)
        session.commit()
        session.refresh(account)
        pool = session.get(db.AccountPoolModel, "PUBLIC_POOL")
        target = session.get(db.Codex2APITargetModel, 1)
        _identity_and_assignment(session, account, pool, target)
        session.commit()
        assert account.identity_id != "missing-identity"
        assert session.get(db.AccountIdentityModel, account.identity_id) is not None


def test_agent_identity_import_response_with_positive_id_is_confirmed():
    from api.codex_import import _remote_import_confirmed

    assert _remote_import_confirmed({"message": "created", "id": 123, "email": "agent@example.com"}) is True


def test_non_mapping_import_response_is_not_treated_as_success():
    from api.codex_import import _remote_import_confirmed

    assert _remote_import_confirmed("created") is False


def test_remote_match_rejects_duplicate_positive_ids_even_when_one_row_matches():
    from api.codex_import import _remote_match_details

    class ClientWithDuplicateIds:
        def list_accounts(self):
            return [
                {"id": 7, "email": "match@example.com", "account_id": "stable"},
                {"id": 7, "email": "other@example.com"},
            ]

    remote, state, diagnostic = _remote_match_details(
        ClientWithDuplicateIds(),
        {"email": "match@example.com", "account_id": "stable"},
    )

    assert remote is None
    assert state == "ambiguous"
    assert diagnostic["error_code"] == "duplicate_remote_ids"


def test_failed_import_can_retry_existing_quarantined_account_without_duplicate(monkeypatch):
    engine = make_engine()

    class RetryClient(Client):
        def __init__(self):
            super().__init__()
            self.ambiguous = True

        def list_accounts(self):
            if self.ambiguous:
                return [
                    {"id": 71, "email": "retry@example.com", "status": "active"},
                    {"id": 72, "email": "retry@example.com", "status": "active"},
                ]
            return [{"id": 73, "email": "retry@example.com", "status": "active"}]

    client = RetryClient()
    monkeypatch.setattr("services.codex2api_target_client.get_target_client", lambda *_: client)
    request = CodexImportRequest(
        pool_id="PUBLIC_POOL",
        target_id=1,
        format="json",
        files=[ImportFile(
            name="retry.json",
            content='{"refresh_token":"rt-retry","email":"retry@example.com"}',
        )],
    )
    first = _ImportJob(id="fixture-retry-first")
    _import_job(first, request, engine)
    assert first.failed == 1

    client.ambiguous = False
    second = _ImportJob(id="fixture-retry-second")
    _import_job(second, request, engine)

    assert second.status == "completed", second.error
    assert second.success == 1
    assert second.duplicate == 0
    with Session(engine) as session:
        accounts = session.exec(select(db.AccountModel)).all()
        bindings = session.exec(select(db.AccountTargetBindingModel)).all()
        identities = session.exec(select(db.AccountIdentityModel)).all()
    assert len(accounts) == 1
    assert accounts[0].status == "registered"
    assert len(bindings) == 1
    assert bindings[0].remote_account_id == 73
    assert bindings[0].enabled is True
    assert identities[0].state == "active"


def test_retrying_refresh_token_clears_an_omitted_stale_access_token():
    from api.codex_import import _replace_retry_credentials

    extra = {
        "refresh_token": "old-refresh",
        "access_token": "old-access",
        "id_token": "old-id",
        "purchase_note": "retain",
    }
    _replace_retry_credentials(extra, {"refresh_token": "new-refresh"})

    assert extra["refresh_token"] == "new-refresh"
    assert "access_token" not in extra
    assert "id_token" not in extra
    assert extra["purchase_note"] == "retain"


def test_imported_disabled_remote_account_is_not_left_schedulable(monkeypatch):
    engine = make_engine()

    class DisabledClient(Client):
        def list_accounts(self):
            return [{
                "id": 74,
                "email": "disabled@example.com",
                "status": "active",
                "enabled": False,
                "locked": True,
            }]

    client = DisabledClient()
    monkeypatch.setattr("services.codex2api_target_client.get_target_client", lambda *_: client)
    request = CodexImportRequest(
        pool_id="PUBLIC_POOL",
        target_id=1,
        format="json",
        files=[ImportFile(
            name="disabled.json",
            content='{"refresh_token":"rt-disabled","email":"disabled@example.com"}',
        )],
    )
    job = _ImportJob(id="fixture-disabled-remote")

    _import_job(job, request, engine)

    assert job.success == 1
    with Session(engine) as session:
        binding = session.exec(select(db.AccountTargetBindingModel)).one()
        assignment = session.exec(select(db.AccountAssignmentModel)).one()
    assert binding.enabled is False
    assert assignment.state == "standby"


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


@pytest.mark.parametrize("format", ["auto", "json"])
@pytest.mark.parametrize("shape", ["array", "accounts", "mixed_accounts"])
def test_one_json_file_imports_every_account_and_skips_only_actual_duplicate(monkeypatch, format, shape):
    engine = make_engine()
    client = Client()
    monkeypatch.setattr("services.codex2api_target_client.get_target_client", lambda *_: client)
    entries = [
        {"email": f"account-{index}@example.com", "refresh_token": f"fixture-rt-{index}",
         "chatgpt_account_id": f"fixture-account-{index}"}
        for index in range(3)
    ]
    entries.append(dict(entries[0]))
    if shape == "mixed_accounts":
        entries[0] = {"name": "First account", "credentials": entries[0]}
    payload = entries if shape == "array" else {"accounts": entries}
    job = _ImportJob(id=f"fixture-{format}-{shape}")
    request = CodexImportRequest(target_id=1, format=format, files=[
        ImportFile(name="accounts.json", content=json.dumps(payload)),
    ])

    _import_job(job, request, engine)

    assert job.status == "completed", job.error
    assert (job.total, job.processed, job.success, job.duplicate, job.failed) == (4, 4, 3, 1, 0)
    assert [item["status"] for item in job.items] == ["success", "success", "success", "duplicate"]
    assert all(item["file"] == "accounts.json" for item in job.items)
    assert [row["email"] for row in client.imported] == [f"account-{index}@example.com" for index in range(3)]
    with Session(engine) as session:
        assert len(session.exec(select(db.AccountModel)).all()) == 3
        assert len(session.exec(select(db.AccountAssignmentModel)).all()) == 3


def test_import_does_not_case_fold_distinct_token_credentials(monkeypatch):
    engine = make_engine()
    client = Client()
    monkeypatch.setattr("services.codex2api_target_client.get_target_client", lambda *_: client)
    job = _ImportJob(id="fixture-case-sensitive-credentials")
    request = CodexImportRequest(target_id=1, format="json", files=[ImportFile(
        name="accounts.json", content=json.dumps([
            {"email": "upper@example.com", "refresh_token": "fixture-RT"},
            {"email": "lower@example.com", "refresh_token": "fixture-rt"},
        ]),
    )])

    _import_job(job, request, engine)

    assert job.status == "completed", job.error
    assert (job.total, job.processed, job.success, job.duplicate) == (2, 2, 2, 0)
    assert len(client.imported) == 2


def test_import_does_not_overwrite_a_distinct_refresh_token_sharing_email(monkeypatch):
    engine = make_engine()
    client = Client()
    monkeypatch.setattr("services.codex2api_target_client.get_target_client", lambda *_: client)
    first = _ImportJob(id="fixture-shared-email-first")
    _import_job(
        first,
        CodexImportRequest(
            target_id=1, format="json",
            files=[ImportFile(name="one.json", content='{"email":"same@example.com","refresh_token":"rt-one"}')],
        ),
        engine,
    )
    second = _ImportJob(id="fixture-shared-email-second")
    _import_job(
        second,
        CodexImportRequest(
            target_id=1, format="json",
            files=[ImportFile(name="two.json", content='{"email":"same@example.com","refresh_token":"rt-two"}')],
        ),
        engine,
    )

    assert first.success == 1
    # The provider exposes two rows with the same email and no stable alias;
    # the second item is correctly held for confirmation, but it must still
    # remain a separate local credential rather than overwrite the first.
    assert second.failed == 1
    with Session(engine) as session:
        accounts = session.exec(select(db.AccountModel).order_by(db.AccountModel.id)).all()
    assert len(accounts) == 2
    assert {json.loads(account.extra_json)["refresh_token"] for account in accounts} == {
        "rt-one", "rt-two"
    }
