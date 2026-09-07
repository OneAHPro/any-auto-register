import json
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import BackgroundTasks
from pydantic import ValidationError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from api import tasks
from api.accounts import AccountUpdate, update_account
from api.codex_import import CodexImportRequest, ImportFile, _ImportJob, _import_job
from core import db
from core.purchase_cost_models import PurchaseBatchModel, PurchaseCostRecordModel
from core.task_runtime import RegisterTaskStore


@pytest.fixture
def engine(monkeypatch):
    value = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(value)
    monkeypatch.setattr(db, 'engine', value)
    monkeypatch.setattr(tasks, 'engine', value)
    monkeypatch.setattr(tasks, '_task_store', RegisterTaskStore())
    monkeypatch.setattr('core.config_store.config_store.get_all', lambda: {})
    with Session(value) as session:
        session.add(db.AccountPoolModel(id='PUBLIC_POOL', name='Public', enabled=True))
        session.commit()
    yield value
    value.dispose()


def login_request(**kwargs):
    return tasks.RegisterTaskRequest(platform='chatgpt', count=3, extra={'chatgpt_existing_account_login_only': True}, **kwargs)


def import_request(**kwargs):
    return CodexImportRequest(format='json', files=[ImportFile(name='accounts.json', content=json.dumps([
        {'email': 'one@example.com', 'refresh_token': 'fixture-rt-one'},
        {'email': 'two@example.com', 'refresh_token': 'fixture-rt-two'},
    ]))], **kwargs)


@pytest.mark.parametrize('builder', [login_request, import_request])
@pytest.mark.parametrize('value', [-1, True, '1.001', 'NaN', 'Infinity', '', [], {}])
def test_purchase_input_rejects_invalid_cost(builder, value):
    with pytest.raises(ValidationError):
        builder(purchase_cost_cny=value, purchase_batch_key=str(uuid4()))


@pytest.mark.parametrize('builder', [login_request, import_request])
def test_purchase_input_requires_uuid_for_amount_and_retains_decimal(builder):
    with pytest.raises(ValidationError):
        builder(purchase_cost_cny='10.01')
    with pytest.raises(ValidationError):
        builder(purchase_cost_cny='10.01', purchase_batch_key='bad-key')
    request = builder(purchase_cost_cny='10.01', purchase_batch_key=str(uuid4()))
    assert request.purchase_cost_cny == Decimal('10.01')
    assert builder().purchase_cost_cny is None


def test_registration_and_other_platforms_reject_purchase_cost():
    for args in [dict(platform='chatgpt'), dict(platform='grok', extra={'chatgpt_existing_account_login_only': True})]:
        with pytest.raises(ValidationError):
            tasks.RegisterTaskRequest(**args, purchase_cost_cny='10', purchase_batch_key=str(uuid4()))


def test_import_records_full_batch_before_remote_failure_and_retries_do_not_double_cost(engine, monkeypatch):
    class FailingClient:
        def import_refresh_token(self, payload):
            raise RuntimeError('fixture remote unavailable')
    monkeypatch.setattr('services.codex2api_target_client.get_target_client', lambda *_: FailingClient())
    with Session(engine) as session:
        session.add(db.Codex2APITargetModel(id=1, name='fixture', base_url='https://fixture.invalid', admin_key_ref='fixture-key', default_pool_id='PUBLIC_POOL', enabled=True))
        session.commit()
    request = import_request(target_id=1, purchase_cost_cny='10.01', purchase_batch_key=str(uuid4()))
    job = _ImportJob(id='failure')
    _import_job(job, request, engine)
    assert job.failed == 2
    with Session(engine) as session:
        records = session.exec(select(PurchaseCostRecordModel).order_by(PurchaseCostRecordModel.id)).all()
        assert [record.cost_cents for record in records] == [501, 500]
        assert all(record.account_id is not None for record in records)
        assert all(record.incurred_at is not None for record in records)
        assert [account.purchase_cost_cents for account in session.exec(select(db.AccountModel).order_by(db.AccountModel.id)).all()] == [501, 500]
    _import_job(_ImportJob(id='repeat'), request, engine)
    with Session(engine) as session:
        assert len(session.exec(select(PurchaseBatchModel)).all()) == 1
        assert sum(row.cost_cents for row in session.exec(select(PurchaseCostRecordModel)).all()) == 1001


def test_login_enqueues_preallocated_cost_once_and_failed_slots_remain_unbound(engine):
    request = login_request(purchase_cost_cny='1.01', purchase_batch_key=str(uuid4()))
    background = BackgroundTasks()
    first = tasks._enqueue_prepared_register_task(request, background_tasks=background)
    again = tasks._enqueue_prepared_register_task(request, background_tasks=background)
    assert first == again
    assert len(background.tasks) == 1
    with Session(engine) as session:
        rows = session.exec(select(PurchaseCostRecordModel)).all()
        assert [row.cost_cents for row in rows] == [34, 34, 33]
        assert all(row.account_id is None for row in rows)
        snapshot = session.get(db.TaskRunModel, first)
        assert json.loads(snapshot.meta_json)['purchase_batch_id'] == rows[0].batch_id


def test_patch_cost_updates_ledger_and_keeps_unknown_legacy_date(engine):
    with Session(engine) as session:
        account = db.AccountModel(platform='chatgpt', email='old@example.com', password='', identity_id='identity-old', purchase_cost_cents=1234, created_at=datetime(2020, 1, 1))
        session.add(account)
        session.commit()
        update_account(account.id, AccountUpdate(purchase_cost_cny='23.45'), session)
        record = session.exec(select(PurchaseCostRecordModel)).one()
        assert record.cost_cents == 2345
        assert record.incurred_at is None
        update_account(account.id, AccountUpdate(purchase_cost_cny=None), session)
        session.refresh(record)
        assert record.cost_cents is None
        assert record.incurred_at is None


def test_startup_migrates_legacy_expenses_once(engine, monkeypatch):
    monkeypatch.setattr('services.account_identity.reconcile_existing_accounts', lambda *_: None)
    monkeypatch.setattr('services.codex2api_target_client.ensure_default_target', lambda *_: None)
    monkeypatch.setattr('services.pool_scheduler.ensure_default_pools', lambda *_: None)
    with Session(engine) as session:
        session.add(db.AccountModel(platform='chatgpt', email='legacy@example.com', password='', purchase_cost_cents=777))
        session.commit()
    db.init_db()
    db.init_db()
    with Session(engine) as session:
        rows = session.exec(select(PurchaseCostRecordModel)).all()
        assert len(rows) == 1
        assert rows[0].cost_cents == 777
        assert rows[0].incurred_at is None


def test_saved_login_and_retry_bind_to_original_purchase_slots(engine):
    binder = getattr(tasks, '_bind_saved_login_purchase', None)
    assert binder is not None, 'login persistence does not bind purchase slots'
    request = login_request(purchase_cost_cny='1.01', purchase_batch_key=str(uuid4()))
    task_id = tasks._enqueue_prepared_register_task(request, background_tasks=BackgroundTasks())
    with Session(engine) as session:
        first = db.AccountModel(platform='chatgpt', email='first@example.com', password='', identity_id='first')
        recovered = db.AccountModel(platform='chatgpt', email='retry@example.com', password='', identity_id='recovered')
        session.add_all([first, recovered])
        session.flush()
        original = db.ChatGPTAttemptBindingModel(task_id=task_id, attempt_index=2, leadbee_code='fixture-code', email=recovered.email)
        session.add(original)
        session.commit()
        session.refresh(first)
        session.refresh(recovered)
        session.refresh(original)
        retry_id = original.id
        first_id, recovered_id = first.id, recovered.id
    binder(task_id, request, 1, first)
    retry = tasks.RegisterTaskRequest(platform='chatgpt', extra={
        'chatgpt_existing_account_login_only': True,
        tasks.CHATGPT_RETRY_BINDINGS_KEY: [{'id': retry_id, 'email': recovered.email, 'leadbee_code': 'fixture-code'}],
    })
    binder('retry-task', retry, 0, recovered)
    binder('retry-task', retry, 0, recovered)
    with Session(engine) as session:
        rows = session.exec(select(PurchaseCostRecordModel).order_by(PurchaseCostRecordModel.id)).all()
        assert [row.account_id for row in rows] == [None, first_id, recovered_id]
        assert [row.cost_cents for row in rows] == [34, 34, 33]
        assert len(session.exec(select(PurchaseBatchModel)).all()) == 1


def test_json_duplicate_submit_reuses_the_queued_job_and_conflicts_are_visible(engine, monkeypatch):
    from api import codex_import
    from fastapi import HTTPException

    queued = []
    monkeypatch.setattr(codex_import._EXECUTOR, 'submit', lambda *args: queued.append(args))
    request = import_request(purchase_cost_cny='10.01', purchase_batch_key=str(uuid4()))
    with Session(engine) as session:
        first = codex_import.start_import(request, session)
        second = codex_import.start_import(request, session)
        assert first['job_id'] == second['job_id']
        assert len(queued) == 1
        changed = request.model_copy(update={'purchase_cost_cny': Decimal('12.00')})
        with pytest.raises(HTTPException) as exc:
            codex_import.start_import(changed, session)
        assert exc.value.status_code == 409


def test_other_platform_costs_remain_local_and_never_enter_chatgpt_ledger(engine):
    from services.account_purchase_costs import ensure_legacy_purchase_costs

    with Session(engine) as session:
        chatgpt = db.AccountModel(platform='chatgpt', email='chatgpt@example.com', password='', identity_id='chatgpt', purchase_cost_cents=111)
        grok = db.AccountModel(platform='grok', email='grok@example.com', password='', identity_id='grok', purchase_cost_cents=222)
        kiro = db.AccountModel(platform='kiro', email='kiro@example.com', password='', identity_id='kiro', purchase_cost_cents=333)
        session.add_all([chatgpt, grok, kiro])
        session.commit()
        ensure_legacy_purchase_costs(session)
        session.commit()
        rows = session.exec(select(PurchaseCostRecordModel)).all()
        assert [(row.identity_id, row.cost_cents) for row in rows] == [('chatgpt', 111)]
        update_account(grok.id, AccountUpdate(purchase_cost_cny='4.44'), session)
        update_account(kiro.id, AccountUpdate(purchase_cost_cny=None), session)
        update_account(chatgpt.id, AccountUpdate(purchase_cost_cny='5.55'), session)
        session.refresh(grok)
        session.refresh(kiro)
        assert grok.purchase_cost_cents == 444
        assert kiro.purchase_cost_cents is None
        rows = session.exec(select(PurchaseCostRecordModel)).all()
        assert [(row.identity_id, row.cost_cents) for row in rows] == [('chatgpt', 555)]
