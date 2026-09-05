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

def test_malformed_empty_response_does_not_mark_existing_rows_missing():
    e=make_engine(); c=Client([{'id':8,'email':'keep@example.com'}]); sync_inventory(e,target_id=1,clients={1:c})
    c.rows=None
    # Client implementation turns None into [] so use a dedicated malformed client.
    class Bad:
        def list_accounts(self): return None
    sync_inventory(e,target_id=1,clients={1:Bad()})
    row=read_inventory(e)[0]; assert row['_inventory_missing'] is False and row['_inventory_error']
