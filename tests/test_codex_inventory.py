import json
from datetime import datetime, timezone
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

from core import db
from services.codex_inventory import sync_inventory, read_inventory
from services.codex_inventory import materialize_inventory


def make_engine():
    e=create_engine('sqlite://', connect_args={'check_same_thread':False}, poolclass=StaticPool)
    db.init_account_pool_schema(e)
    return e

class Client:
    def __init__(self, rows=None, error=None): self.rows=rows or []; self.error=error
    def list_accounts(self):
        if self.error: raise self.error
        return self.rows


def test_database_preferred_client_requires_explicit_availability_for_empty_result():
    from services.codex_inventory import _DB_UNAVAILABLE, _DatabasePreferredClient

    class Adapter:
        def fetch_account_metadata(self):
            return []

    client = _DatabasePreferredClient(1, None, Adapter())
    assert client._database_rows() is _DB_UNAVAILABLE

    class AvailableAdapter(Adapter):
        last_status = {"available": True}

    available = _DatabasePreferredClient(1, None, AvailableAdapter())
    assert available._database_rows() == []


@pytest.mark.parametrize(
    ("value", "default", "expected"),
    [
        ("false", True, False),
        ("0", True, False),
        ("true", False, True),
        (None, True, True),
        ("unexpected", True, True),
    ],
)
def test_remote_boolean_fields_are_parsed_without_string_truthiness(value, default, expected):
    from services.codex2api_remote_accounts import remote_bool

    assert remote_bool(value, default) is expected


def test_database_preferred_client_rejects_null_account_payload_field():
    from services.codex_inventory import _DB_UNAVAILABLE, _DatabasePreferredClient

    class Adapter:
        last_status = {"available": True}

        def fetch_account_metadata(self):
            return {"accounts": None}

    client = _DatabasePreferredClient(1, None, Adapter())
    assert client._database_rows() is _DB_UNAVAILABLE


@pytest.mark.parametrize(
    "rows",
    [
        [{"target_id": 2, "remote_id": 7}],
        [{"target_id": 1.5, "remote_id": 7}],
        [{"target_id": 1, "remote_id": 7}, "malformed"],
    ],
)
def test_database_preferred_client_rejects_cross_target_or_malformed_rows(rows):
    from services.codex_inventory import _DB_UNAVAILABLE, _DatabasePreferredClient

    class Adapter:
        last_status = {"available": True}

        def fetch_account_metadata(self):
            return rows

    client = _DatabasePreferredClient(1, None, Adapter())
    assert client._database_rows() is _DB_UNAVAILABLE

def test_sync_persists_sanitized_target_scoped_snapshot():
    e=make_engine(); c=Client([{'id':7,'email':'A@EXAMPLE.com','status':'active','name':'A','usage_percent_7d':12,'updated_at':'2026-09-05T00:00:00Z','refresh_token':'SECRET','nested':{'password':'x'}}])
    result=sync_inventory(e,target_id=3,clients={3:c})
    assert result['targets'] == 1 and result['upserted'] == 1
    rows=read_inventory(e)
    assert len(rows)==1; row=rows[0]
    assert row['target_id']==3 and row['remote_id']==7 and row['email']=='A@EXAMPLE.com'
    assert 'refresh_token' not in row and 'password' not in row
    assert row['_inventory_missing'] is False

def test_successful_full_list_marks_absent_rows_missing_but_failure_keeps_them():
    e=make_engine(); c=Client([{'id':1,'email':'one@example.com'}]); sync_inventory(e,target_id=1,clients={1:c})
    c.rows=[]; sync_inventory(e,target_id=1,clients={1:c})
    row=read_inventory(e)[0]; assert row['_inventory_missing'] is True and row['_inventory_error']==''
    c.error=RuntimeError('offline'); result=sync_inventory(e,target_id=1,clients={1:c})
    assert result['errors']==1
    row=read_inventory(e)[0]; assert row['_inventory_missing'] is True and 'offline' in row['_inventory_error']


def test_duplicate_remote_ids_leave_prior_snapshot_intact_and_mark_target_stale():
    e = make_engine()
    client = Client([{'id': 7, 'email': 'first@example.com', 'status': 'active'}])
    sync_inventory(e, target_id=1, clients={1: client})
    before = read_inventory(e)[0]

    client.rows = [
        {'id': 7, 'email': 'first@example.com', 'status': 'active'},
        {'id': 7, 'email': 'different@example.com', 'status': 'active'},
    ]
    result = sync_inventory(e, target_id=1, clients={1: client})

    assert result['errors'] == 1
    assert result['stale'] == 1
    after = read_inventory(e)[0]
    assert after['email'] == before['email']
    assert after['_inventory_stale'] is True
    assert '重复远端账号 ID' in after['_inventory_error']


def test_nonempty_inventory_without_valid_remote_ids_is_not_treated_as_empty_success():
    e = make_engine()
    client = Client([{'id': 7, 'email': 'known@example.com', 'status': 'active'}])
    sync_inventory(e, target_id=1, clients={1: client})
    client.rows = [{'email': 'malformed@example.com', 'status': 'active'}]

    result = sync_inventory(e, target_id=1, clients={1: client})

    assert result['errors'] == 1
    row = read_inventory(e)[0]
    assert row['remote_id'] == 7
    assert row['_inventory_missing'] is False
    assert row['_inventory_stale'] is True


def test_inventory_rejects_explicit_cross_target_rows_and_filters_deleted_or_other_channels():
    e = make_engine()
    client = Client([{
        "id": 7,
        "target_id": 2,
        "email": "wrong-target@example.com",
        "status": "active",
    }])
    result = sync_inventory(e, target_id=1, clients={1: client})
    assert result["errors"] == 1
    assert read_inventory(e) == []

    client.rows = [
        {"id": 8, "email": "deleted@example.com", "status": "deleted"},
        {"id": 9, "email": "grok@example.com", "upstream_type": "grok", "status": "active"},
        {"id": 11, "email": "soft-deleted@example.com", "deleted_at": "2026-09-09T00:00:00Z", "status": "active"},
        {"id": 12, "email": "nested-grok@example.com", "credentials": {"upstream_type": "grok"}, "status": "active"},
        {"id": 10, "email": "codex@example.com", "platform": "chatgpt", "status": "active"},
    ]
    result = sync_inventory(e, target_id=1, clients={1: client})
    assert result["errors"] == 0
    rows = read_inventory(e)
    assert [(row["target_id"], row["remote_id"]) for row in rows] == [(1, 10)]


def test_inventory_rejects_fractional_remote_ids_without_truncation():
    e = make_engine()
    client = Client([{'id': 7, 'email': 'known@example.com', 'status': 'active'}])
    sync_inventory(e, target_id=1, clients={1: client})
    client.rows = [{'id': 1.5, 'email': 'wrong@example.com', 'status': 'active'}]

    result = sync_inventory(e, target_id=1, clients={1: client})

    assert result['errors'] == 1
    row = read_inventory(e)[0]
    assert row['remote_id'] == 7
    assert row['_inventory_stale'] is True


def test_stale_inventory_snapshot_is_not_materialized_back_into_active_pool():
    e = make_engine()
    with Session(e) as session:
        session.add_all([
            db.AccountModel(
                id=1, platform="chatgpt", email="stale@example.com", password="",
                identity_id="identity-stale",
            ),
            db.AccountIdentityModel(
                id="identity-stale", platform="chatgpt",
                canonical_email="stale@example.com",
            ),
            db.AccountTargetBindingModel(
                identity_id="identity-stale", local_account_id=1, target_id=1,
                remote_account_id=7, enabled=True, sync_status="synced",
            ),
            db.AccountAssignmentModel(
                identity_id="identity-stale", local_account_id=1,
                pool_id="PUBLIC_POOL", target_id=1, state="active",
            ),
            db.CodexInventorySnapshotModel(
                target_id=1, remote_id=7,
                summary_json='{"email":"stale@example.com","status":"active"}',
                error="target offline", missing=False,
            ),
        ])
        session.commit()

    assert materialize_inventory(e)["total"] == 0
    with Session(e) as session:
        binding = session.exec(select(db.AccountTargetBindingModel)).one()
        assignment = session.exec(select(db.AccountAssignmentModel)).one()
    assert binding.enabled is True
    assert assignment.state == "active"


def test_disabled_target_inventory_never_reactivates_binding_or_assignment():
    e = make_engine()
    with Session(e) as session:
        session.add(
            db.Codex2APITargetModel(
                id=1,
                name="disabled-target",
                base_url="https://disabled.example",
                admin_key_ref="disabled-key",
                enabled=False,
            )
        )
        session.commit()

    sync_inventory(
        e,
        target_id=1,
        clients={1: Client([{"id": 7, "email": "disabled@example.com", "status": "active"}])},
    )
    result = materialize_inventory(e)

    assert result["created"] == 1
    with Session(e) as session:
        binding = session.exec(select(db.AccountTargetBindingModel)).one()
        assignments = session.exec(select(db.AccountAssignmentModel)).all()
    assert binding.enabled is False
    assert binding.sync_status == "target_disabled"
    assert not [row for row in assignments if row.state in {"active", "draining"}]


def test_disabled_duplicate_target_does_not_displace_a_usable_binding():
    e = make_engine()
    with Session(e) as session:
        session.add_all([
            db.Codex2APITargetModel(
                id=1, name="usable", base_url="https://usable", admin_key_ref="usable-key", enabled=True,
            ),
            db.Codex2APITargetModel(
                id=2, name="disabled", base_url="https://disabled", admin_key_ref="disabled-key", enabled=False,
            ),
            db.AccountModel(
                id=1, platform="chatgpt", email="usable@example.com", password="",
                identity_id="usable-identity",
            ),
            db.AccountIdentityModel(
                id="usable-identity", platform="chatgpt", canonical_email="usable@example.com",
            ),
            db.AccountTargetBindingModel(
                identity_id="usable-identity", local_account_id=1, target_id=1,
                remote_account_id=11, enabled=True, sync_status="synced",
            ),
            db.AccountAssignmentModel(
                identity_id="usable-identity", local_account_id=1, pool_id="PUBLIC_POOL",
                target_id=1, state="active",
            ),
        ])
        session.commit()
    row = {"id": 22, "email": "usable@example.com", "chatgpt_account_id": "usable-id", "status": "active"}
    sync_inventory(e, target_id=2, clients={2: Client([row])})
    materialize_inventory(e)
    with Session(e) as session:
        bindings = session.exec(select(db.AccountTargetBindingModel).order_by(db.AccountTargetBindingModel.target_id)).all()
        assignment = session.exec(select(db.AccountAssignmentModel)).one()
    assert assignment.target_id == 1 and assignment.state == "active"
    assert bindings[0].target_id == 1 and bindings[0].enabled is True
    assert bindings[1].target_id == 2 and bindings[1].enabled is False


def test_older_remote_source_timestamp_cannot_replace_newer_inventory_projection():
    e = make_engine()
    client = Client([{
        "id": 7, "email": "new@example.com", "status": "active",
        "updated_at": "2026-09-09T02:00:00+00:00",
    }])
    sync_inventory(e, target_id=1, clients={1: client})
    client.rows = [{
        "id": 7, "email": "old@example.com", "status": "disabled",
        "updated_at": "2026-09-08T02:00:00+00:00",
    }]
    sync_inventory(e, target_id=1, clients={1: client})

    row = read_inventory(e)[0]
    assert row["email"] == "new@example.com"
    assert row["status"] == "active"

def test_missing_remote_only_rows_are_removed_from_active_scheduling():
    e = make_engine()
    client = Client([{'id': 9, 'email': 'gone@example.com', 'status': 'active'}])
    sync_inventory(e, target_id=1, clients={1: client})
    materialize_inventory(e)
    with Session(e) as session:
        binding = session.exec(select(db.AccountTargetBindingModel)).one()
        assignment = session.exec(select(db.AccountAssignmentModel)).one()
        assert binding.enabled is True
        assert assignment.state == 'active'
    client.rows = []
    sync_inventory(e, target_id=1, clients={1: client})
    with Session(e) as session:
        binding = session.exec(select(db.AccountTargetBindingModel)).one()
        assignment = session.exec(select(db.AccountAssignmentModel)).one()
    assert binding.enabled is False
    assert binding.sync_status == 'remote_missing'
    assert binding.remote_status == 'remote_missing'
    assert assignment.state == 'standby'
    assert assignment.assignment_version == 2


def test_missing_remote_row_does_not_quarantine_assignment_on_another_target():
    e = make_engine()
    with Session(e) as session:
        session.add_all([
            db.AccountTargetBindingModel(
                identity_id="identity-shared-target",
                local_account_id=1,
                target_id=1,
                remote_account_id=9,
                enabled=True,
                sync_status="synced",
            ),
            db.CodexInventorySnapshotModel(
                target_id=1,
                remote_id=9,
                summary_json='{"status":"active"}',
                missing=False,
            ),
            db.AccountTargetBindingModel(
                identity_id="identity-shared-target",
                local_account_id=1,
                target_id=2,
                remote_account_id=10,
                enabled=True,
                sync_status="synced",
            ),
            db.AccountAssignmentModel(
                identity_id="identity-shared-target",
                local_account_id=1,
                pool_id="PUBLIC_POOL",
                target_id=2,
                state="active",
            ),
        ])
        session.commit()

    sync_inventory(e, target_id=1, clients={1: Client([])})

    with Session(e) as session:
        assignment = session.exec(select(db.AccountAssignmentModel)).one()
    assert assignment.target_id == 2
    assert assignment.state == "active"


def test_missing_remote_does_not_rewrite_a_superseded_historical_binding():
    e = make_engine()
    with Session(e) as session:
        session.add_all([
            db.AccountTargetBindingModel(
                identity_id="identity-history", local_account_id=1,
                target_id=1, remote_account_id=9, enabled=False,
                sync_status="superseded", remote_status="superseded",
            ),
            db.CodexInventorySnapshotModel(
                target_id=1, remote_id=9, summary_json='{"status":"active"}',
                missing=False,
            ),
        ])
        session.commit()

    sync_inventory(e, target_id=1, clients={1: Client([])})

    with Session(e) as session:
        binding = session.exec(select(db.AccountTargetBindingModel)).one()
    assert binding.sync_status == "superseded"
    assert binding.remote_status == "superseded"


def test_materializing_an_identity_on_a_new_target_supersedes_old_binding():
    e = make_engine()
    account = db.AccountModel(
        id=1,
        platform="chatgpt",
        email="move-inventory@example.com",
        password="password",
        identity_id="identity-inventory-move",
        extra_json='{"workspace_id":"workspace-inventory-move"}',
    )
    with Session(e) as session:
        session.add_all([
            account,
            db.Codex2APITargetModel(
                id=1, name="old", base_url="https://old", admin_key_ref="old-key",
                default_pool_id="PUBLIC_POOL", enabled=True,
            ),
            db.Codex2APITargetModel(
                id=2, name="new", base_url="https://new", admin_key_ref="new-key",
                default_pool_id="ENTERPRISE_POOL", enabled=True,
            ),
            db.AccountIdentityModel(
                id="identity-inventory-move", platform="chatgpt",
                canonical_email=account.email, current_account_id=0,
            ),
            db.AccountTargetBindingModel(
                identity_id="identity-inventory-move", local_account_id=1,
                target_id=1, remote_account_id=11, remote_email=account.email,
                sync_status="synced", enabled=True,
            ),
            db.AccountAssignmentModel(
                identity_id="identity-inventory-move", local_account_id=1,
                pool_id="PUBLIC_POOL", target_id=1, state="active",
            ),
            db.CodexInventorySnapshotModel(
                target_id=2, remote_id=22,
                summary_json='{"email":"move-inventory@example.com","account_id":"workspace-inventory-move","status":"active","enabled":true}',
                missing=False,
            ),
        ])
        session.commit()

    materialize_inventory(e)

    with Session(e) as session:
        bindings = session.exec(
            select(db.AccountTargetBindingModel).order_by(db.AccountTargetBindingModel.target_id)
        ).all()
        assignment = session.exec(select(db.AccountAssignmentModel)).one()
    old, new = bindings
    assert old.target_id == 1 and old.enabled is False and old.sync_status == "superseded"
    assert new.target_id == 2 and new.enabled is True
    assert assignment.target_id == 2 and assignment.pool_id == "ENTERPRISE_POOL" and assignment.state == "active"


def test_materialize_preserves_ambiguous_identity_out_of_active_scheduling():
    e = make_engine()
    with Session(e) as session:
        session.add_all([
            db.AccountModel(
                id=1, platform="chatgpt", email="ambiguous-materialize@example.com",
                password="", identity_id="identity-ambiguous-materialize",
            ),
            db.Codex2APITargetModel(
                id=1, name="target", base_url="https://target", admin_key_ref="key",
                default_pool_id="PUBLIC_POOL", enabled=True,
            ),
            db.AccountIdentityModel(
                id="identity-ambiguous-materialize", platform="chatgpt",
                canonical_email="ambiguous-materialize@example.com",
                current_account_id=1, state="ambiguous",
            ),
            db.AccountTargetBindingModel(
                identity_id="identity-ambiguous-materialize", local_account_id=1,
                target_id=1, remote_account_id=77, enabled=False,
                sync_status="ambiguous",
            ),
            db.AccountAssignmentModel(
                identity_id="identity-ambiguous-materialize", local_account_id=1,
                pool_id="PUBLIC_POOL", target_id=1, state="standby",
            ),
            db.CodexInventorySnapshotModel(
                target_id=1, remote_id=77,
                summary_json='{"email":"ambiguous-materialize@example.com","status":"active","enabled":true}',
                missing=False,
            ),
        ])
        session.commit()

    materialize_inventory(e)

    with Session(e) as session:
        identity = session.get(db.AccountIdentityModel, "identity-ambiguous-materialize")
        binding = session.exec(select(db.AccountTargetBindingModel)).one()
        assignment = session.exec(select(db.AccountAssignmentModel)).one()
    assert identity.state == "ambiguous"
    assert binding.enabled is False
    assert assignment.state == "standby"
    assert assignment.assignment_version == 2


def test_materialize_reactivates_existing_standby_assignment_when_account_returns():
    e = make_engine()
    client = Client([{'id': 9, 'email': 'returned@example.com', 'status': 'active'}])
    sync_inventory(e, target_id=1, clients={1: client})
    materialize_inventory(e)
    with Session(e) as session:
        assignment_id = session.exec(select(db.AccountAssignmentModel)).one().id

    client.rows = []
    sync_inventory(e, target_id=1, clients={1: client})
    client.rows = [{'id': 9, 'email': 'returned@example.com', 'status': 'active'}]
    sync_inventory(e, target_id=1, clients={1: client})
    materialize_inventory(e)
    materialize_inventory(e)

    with Session(e) as session:
        assignment = session.exec(select(db.AccountAssignmentModel)).one()
        assert assignment.id == assignment_id
        assert assignment.state == 'active'
        assert session.exec(select(db.AccountModel)).one().email == 'returned@example.com'

def test_snapshots_are_isolated_by_target_and_idempotent():
    e=make_engine(); c1=Client([{'id':4,'email':'same@example.com'}]); c2=Client([{'id':4,'email':'same@example.com','plan_type':'pro'}])
    sync_inventory(e,target_id=1,clients={1:c1}); sync_inventory(e,target_id=2,clients={2:c2}); sync_inventory(e,target_id=2,clients={2:c2})
    rows=sorted(read_inventory(e), key=lambda x:x['target_id']); assert [(r['target_id'],r['remote_id']) for r in rows]==[(1,4),(2,4)]
    assert len(rows)==2 and rows[1]['plan_type']=='pro'


def test_materialize_inventory_creates_local_rows_for_local_only_account_listing():
    e = make_engine()
    c = Client([{
        'id': 7, 'email': 'local@example.com', 'status': 'active', 'enabled': True,
        'plan_type': 'pro', 'usage_percent_7d': 20, 'billed_7d': 12.5,
        'usage_7d_detail': {'requests': 44},
    }])
    sync_inventory(e, target_id=1, clients={1: c})
    result = materialize_inventory(e)
    assert result['created'] == 1
    with Session(e) as session:
        account = session.exec(select(db.AccountModel)).one()
    assert account.email == 'local@example.com'
    assert account.get_extra()['remote_only'] is True

def test_materialize_inventory_reuses_local_credentials_by_stable_chatgpt_id_without_email():
    e = make_engine()
    with Session(e) as session:
        session.add(db.AccountModel(
            platform='chatgpt',
            email='local-credential@example.com',
            password='p',
            user_id='acct-x',
            extra_json=json.dumps({
                'account_type': 'chatgpt_password',
                'chatgpt_local': {'account_id': 'acct-x'},
            }),
        ))
        session.commit()
    sync_inventory(e, target_id=1, clients={1: Client([{
        'id': 7,
        'email': '',
        'name': 'managed-account-7',
        'chatgpt_account_id': 'acct-x',
        'status': 'active',
    }])})
    result = materialize_inventory(e)
    assert result['created'] == 0
    with Session(e) as session:
        accounts = session.exec(select(db.AccountModel)).all()
    assert len(accounts) == 1
    assert not accounts[0].get_extra().get('remote_only')
    assert accounts[0].get_extra()['codex_remote_snapshot']['remote_id'] == 7


def test_materialize_reuses_remote_only_account_across_targets_by_strong_alias():
    e = make_engine()
    row = {
        'id': 7,
        'email': 'shared@example.com',
        'chatgpt_account_id': 'acct-shared',
        'effective_workspace_id': 'workspace-shared',
        'status': 'active',
    }
    sync_inventory(e, target_id=1, clients={1: Client([row])})
    sync_inventory(e, target_id=2, clients={2: Client([row])})
    materialize_inventory(e)
    with Session(e) as session:
        accounts = session.exec(select(db.AccountModel)).all()
        bindings = session.exec(select(db.AccountTargetBindingModel)).all()
    assert len(accounts) == 1
    assert {(int(binding.target_id), int(binding.remote_account_id)) for binding in bindings} == {(1, 7), (2, 7)}


def test_materialize_prefers_exact_email_when_stable_id_is_shared():
    e = make_engine()
    shared_id = 'shared-chatgpt-account-id'
    with Session(e) as session:
        session.add(db.AccountModel(
            platform='chatgpt', email='generic-account@example.com', password='p',
            extra_json=json.dumps({'account_type': 'chatgpt_password', 'chatgpt_account_id': shared_id}),
        ))
        session.add(db.AccountModel(
            platform='chatgpt', email='exact@example.com', password='p',
            extra_json=json.dumps({'account_type': 'chatgpt_password', 'chatgpt_account_id': shared_id}),
        ))
        session.commit()
    sync_inventory(e, target_id=1, clients={1: Client([{
        'id': 8, 'email': 'exact@example.com', 'chatgpt_account_id': shared_id, 'status': 'active',
    }])})
    materialize_inventory(e)
    with Session(e) as session:
        exact = session.exec(select(db.AccountModel).where(db.AccountModel.email == 'exact@example.com')).one()
        generic = session.exec(select(db.AccountModel).where(db.AccountModel.email == 'generic-account@example.com')).one()
    assert exact.get_extra()['codex_remote_snapshot']['remote_id'] == 8
    assert 'codex_remote_snapshot' not in generic.get_extra()


def test_materialize_exact_email_reuses_credential_when_remote_has_no_stable_alias():
    e = make_engine()
    with Session(e) as session:
        session.add(db.AccountModel(
            platform="chatgpt", email="same@example.com", password="p",
            user_id="local-stable-id",
        ))
        session.commit()
    sync_inventory(e, target_id=1, clients={1: Client([{
        "id": 31, "email": "same@example.com", "status": "active",
    }])})
    result = materialize_inventory(e)
    assert result["created"] == 0
    with Session(e) as session:
        accounts = session.exec(select(db.AccountModel)).all()
    assert len(accounts) == 1


def test_materialize_does_not_attach_cross_email_reused_stable_id_to_credentials():
    e = make_engine()
    with Session(e) as session:
        session.add(db.AccountModel(
            platform="chatgpt", email="wrong@example.com", password="p",
            user_id="reused-stable-id",
        ))
        session.commit()
    sync_inventory(e, target_id=1, clients={1: Client([{
        "id": 32,
        "email": "right@example.com",
        "chatgpt_account_id": "reused-stable-id",
        "status": "active",
    }])})
    materialize_inventory(e)
    with Session(e) as session:
        accounts = session.exec(select(db.AccountModel).order_by(db.AccountModel.id)).all()
    assert len(accounts) == 2
    assert accounts[0].email == "wrong@example.com"
    assert accounts[1].email == "right@example.com"
    assert accounts[0].get_extra().get("codex_remote_snapshot") is None


def test_materialize_repairs_stale_binding_to_exact_email_account():
    e = make_engine()
    shared_id = 'shared-chatgpt-account-id'
    with Session(e) as session:
        stale = db.AccountModel(
            platform='chatgpt', email='generic-account@example.com', password='p',
            extra_json=json.dumps({'account_type': 'chatgpt_password', 'chatgpt_account_id': shared_id}),
        )
        exact = db.AccountModel(
            platform='chatgpt', email='exact@example.com', password='p',
            extra_json=json.dumps({'account_type': 'chatgpt_password', 'chatgpt_account_id': shared_id}),
        )
        session.add(stale); session.add(exact); session.flush()
        exact_id = int(exact.id or 0)
        session.add(db.AccountTargetBindingModel(
            identity_id='stale-identity', local_account_id=int(stale.id or 0),
            target_id=1, remote_account_id=8, remote_email='old@example.com',
        ))
        session.commit()
    sync_inventory(e, target_id=1, clients={1: Client([{
        'id': 8, 'email': 'exact@example.com', 'chatgpt_account_id': shared_id, 'status': 'active',
    }])})
    materialize_inventory(e)
    with Session(e) as session:
        binding = session.exec(select(db.AccountTargetBindingModel)).one()
    assert binding.local_account_id == exact_id
    assert binding.remote_account_id == 8


def test_materialize_retargets_stale_binding_identity_and_quarantines_old_assignment():
    """An exact credential match must not inherit a stale identity key."""
    e = make_engine()
    with Session(e) as session:
        stale = db.AccountModel(
            id=1,
            platform="chatgpt",
            email="generic-account@example.com",
            password="p",
            identity_id="stale-identity",
            extra_json=json.dumps({"chatgpt_account_id": "shared-id"}),
        )
        exact = db.AccountModel(
            id=2,
            platform="chatgpt",
            email="exact-account@example.com",
            password="p",
            identity_id="exact-identity",
            extra_json=json.dumps({"chatgpt_account_id": "shared-id"}),
        )
        session.add_all([
            stale,
            exact,
            db.AccountIdentityModel(
                id="stale-identity",
                platform="chatgpt",
                canonical_email=stale.email,
                current_account_id=stale.id,
            ),
            db.AccountIdentityModel(
                id="exact-identity",
                platform="chatgpt",
                canonical_email=exact.email,
                current_account_id=exact.id,
            ),
            db.AccountTargetBindingModel(
                identity_id="stale-identity",
                local_account_id=stale.id,
                target_id=1,
                remote_account_id=8,
                remote_email="old@example.com",
                enabled=True,
                sync_status="synced",
            ),
            db.AccountAssignmentModel(
                identity_id="stale-identity",
                local_account_id=stale.id,
                pool_id="PUBLIC_POOL",
                target_id=1,
                state="active",
                assignment_version=1,
            ),
            db.CodexInventorySnapshotModel(
                target_id=1,
                remote_id=8,
                summary_json=json.dumps({
                    "id": 8,
                    "email": exact.email,
                    "chatgpt_account_id": "shared-id",
                    "status": "active",
                    "enabled": True,
                }),
                missing=False,
            ),
        ])
        session.commit()

    materialize_inventory(e)

    with Session(e) as session:
        binding = session.exec(select(db.AccountTargetBindingModel)).one()
        assignments = session.exec(
            select(db.AccountAssignmentModel).order_by(db.AccountAssignmentModel.identity_id)
        ).all()

    assert binding.identity_id == "exact-identity"
    assert binding.local_account_id == 2
    old_assignment = next(row for row in assignments if row.identity_id == "stale-identity")
    new_assignment = next(row for row in assignments if row.identity_id == "exact-identity")
    assert old_assignment.state == "standby"
    assert old_assignment.assignment_version == 2
    assert new_assignment.state == "active"
    assert new_assignment.target_id == 1


def test_materialize_transfers_remote_row_from_duplicate_remote_only_account():
    e = make_engine()
    stable_id = 'current-stable-id'
    with Session(e) as session:
        credential = db.AccountModel(
            platform='chatgpt', email='same@example.com', password='p',
            extra_json=json.dumps({'chatgpt_account_id': stable_id}),
        )
        remote_only = db.AccountModel(
            platform='chatgpt', email='same@example.com', password='',
            extra_json=json.dumps({'remote_only': True, 'remote_id': 7}),
        )
        session.add(credential); session.add(remote_only); session.flush()
        credential_id = int(credential.id or 0); remote_only_id = int(remote_only.id or 0)
        session.add(db.AccountTargetBindingModel(
            identity_id='remote-only', local_account_id=remote_only_id,
            target_id=1, remote_account_id=7, remote_email='same@example.com',
        ))
        session.commit()
    sync_inventory(e, target_id=1, clients={1: Client([{
        'id': 7, 'email': 'same@example.com', 'chatgpt_account_id': stable_id, 'status': 'active',
    }])})
    materialize_inventory(e)
    with Session(e) as session:
        bindings = session.exec(select(db.AccountTargetBindingModel)).all()
    assert len(bindings) == 1
    assert bindings[0].local_account_id == credential_id
    assert bindings[0].remote_account_id == 7

def test_materialize_inventory_reuses_identity_binding_when_remote_id_rotates():
    e = make_engine()
    with Session(e) as session:
        account = db.AccountModel(
            platform='chatgpt',
            email='rotated@example.com',
            password='p',
            identity_id='identity-rotated',
            extra_json='{}',
        )
        session.add(account)
        session.flush()
        session.add(db.AccountTargetBindingModel(
            identity_id='identity-rotated',
            local_account_id=int(account.id or 0),
            target_id=1,
            remote_account_id=4,
            remote_email='rotated@example.com',
            sync_status='remote_missing',
            enabled=False,
        ))
        session.commit()
    sync_inventory(e, target_id=1, clients={1: Client([{
        'id': 8,
        'email': 'rotated@example.com',
        'chatgpt_account_id': 'identity-rotated',
        'status': 'active',
    }])})
    result = materialize_inventory(e)
    assert result['created'] == 0
    with Session(e) as session:
        bindings = session.exec(select(db.AccountTargetBindingModel)).all()
    assert len(bindings) == 1
    assert bindings[0].remote_account_id == 8
    assert bindings[0].enabled is True


def test_materialize_keeps_bound_account_when_remote_email_changes_but_identity_matches():
    e = make_engine()
    with Session(e) as session:
        account = db.AccountModel(
            platform="chatgpt",
            email="old@example.com",
            password="p",
            identity_id="identity-email-rotation",
            extra_json='{"workspace_id":"workspace-email-rotation"}',
        )
        session.add(account)
        session.commit()
        session.refresh(account)
        session.add_all([
            db.AccountIdentityModel(
                id="identity-email-rotation",
                platform="chatgpt",
                canonical_email=account.email,
                current_account_id=account.id,
            ),
            db.AccountTargetBindingModel(
                identity_id="identity-email-rotation",
                local_account_id=account.id,
                target_id=1,
                remote_account_id=7,
                remote_email=account.email,
                enabled=True,
                sync_status="synced",
            ),
            db.CodexInventorySnapshotModel(
                target_id=1,
                remote_id=7,
                summary_json=json.dumps({
                    "id": 7,
                    "email": "new@example.com",
                    "workspace_id": "workspace-email-rotation",
                    "status": "active",
                    "enabled": True,
                }),
            ),
        ])
        session.commit()

    materialize_inventory(e)

    with Session(e) as session:
        accounts = session.exec(select(db.AccountModel)).all()
        binding = session.exec(select(db.AccountTargetBindingModel)).one()
    assert len(accounts) == 1
    assert accounts[0].email == "old@example.com"
    assert binding.local_account_id == accounts[0].id
    assert binding.remote_email == "new@example.com"

def test_materialize_inventory_keeps_duplicate_remote_emails_as_separate_accounts():
    e = make_engine()
    sync_inventory(e, target_id=1, clients={1: Client([
        {'id': 11, 'email': 'shared@example.com', 'status': 'active'},
        {'id': 12, 'email': 'shared@example.com', 'status': 'rate_limited'},
    ])})
    result = materialize_inventory(e)
    assert result['created'] == 2
    with Session(e) as session:
        accounts = session.exec(select(db.AccountModel)).all()
        bindings = session.exec(select(db.AccountTargetBindingModel)).all()
    assert len(accounts) == 2
    assert {a.get_extra()['remote_id'] for a in accounts} == {11, 12}
    assert {int(b.remote_account_id) for b in bindings} == {11, 12}

def test_malformed_empty_response_does_not_mark_existing_rows_missing():
    e=make_engine(); c=Client([{'id':8,'email':'keep@example.com'}]); sync_inventory(e,target_id=1,clients={1:c})
    c.rows=None
    # Client implementation turns None into [] so use a dedicated malformed client.
    class Bad:
        def list_accounts(self): return None
    sync_inventory(e,target_id=1,clients={1:Bad()})
    row=read_inventory(e)[0]; assert row['_inventory_missing'] is False and row['_inventory_error']


def test_sync_prefers_database_metadata_and_falls_back_to_http_when_database_unavailable(monkeypatch):
    import services.codex_inventory as inventory_module
    import services.codex2api_target_client as target_client_module

    e = make_engine()
    with Session(e) as session:
        session.add(db.Codex2APITargetModel(
            id=1,
            name="db-target",
            base_url="https://codex2api.example",
            admin_key_ref="target-key",
            enabled=True,
        ))
        session.commit()

    class DBReader:
        def __init__(self):
            self.available = True
            self.calls = 0
            self.last_status = {"available": True, "status": "available"}

        def fetch_account_metadata(self):
            self.calls += 1
            if not self.available:
                self.last_status = {"available": False, "status": "query_error"}
                return []
            return [{
                "remote_id": 77,
                "email": "db@example.com",
                "account_id": "stable-db-id",
                "quota": {"7d_used_percent": 12},
                "status": "active",
            }]

    class HTTPClient:
        def __init__(self):
            self.calls = 0

        def list_accounts(self):
            self.calls += 1
            return [{
                "id": 88,
                "email": "http@example.com",
                "account_id": "stable-http-id",
                "status": "active",
            }]

    reader = DBReader()
    http = HTTPClient()
    monkeypatch.setattr(inventory_module, "get_codex2api_db_adapter", lambda _target_id: reader)
    monkeypatch.setattr(target_client_module, "get_target_client", lambda _target_id, _engine: http)

    first = inventory_module.sync_inventory(e, target_id=1)
    assert first["upserted"] == 1
    assert reader.calls == 1
    assert http.calls == 0
    assert read_inventory(e)[0]["remote_id"] == 77

    reader.available = False
    second = inventory_module.sync_inventory(e, target_id=1)
    assert second["upserted"] == 1
    assert reader.calls == 2
    assert http.calls == 1
    rows = read_inventory(e)
    assert any(row["remote_id"] == 88 and not row["_inventory_missing"] for row in rows)
