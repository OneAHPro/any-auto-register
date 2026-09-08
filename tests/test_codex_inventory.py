import json
from datetime import datetime, timezone
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
