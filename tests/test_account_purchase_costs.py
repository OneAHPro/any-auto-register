from datetime import datetime, timezone
from importlib import import_module, util

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from core.db import AccountModel


@pytest.fixture
def ledger():
    assert util.find_spec('core.purchase_cost_models'), 'purchase cost models are missing'
    assert util.find_spec('services.account_purchase_costs'), 'purchase cost service is missing'
    models = import_module('core.purchase_cost_models')
    service = import_module('services.account_purchase_costs')
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    yield engine, models, service
    engine.dispose()


def account(session, name='one', cost=None):
    row = AccountModel(platform='chatgpt', email=f'{name}@example.com', password='', identity_id=f'identity-{name}', purchase_cost_cents=cost)
    session.add(row)
    session.flush()
    return row


def records(session, models):
    return session.exec(select(models.PurchaseCostRecordModel).order_by(models.PurchaseCostRecordModel.id)).all()


def test_batch_preallocates_every_slot_with_exact_total_and_keeps_failed_slots(ledger):
    engine, models, costs = ledger
    with Session(engine) as session:
        batch = costs.create_purchase_batch(session, total_cost_cents=101, expected_count=3, source='task_login', request_key='request-one')
        assert batch.expected_count == 3
        rows = records(session, models)
        assert [row.cost_cents for row in rows] == [34, 34, 33]
        assert all(row.incurred_at is not None for row in rows)
        saved = account(session)
        costs.bind_purchase_slot(session, batch.id, 1, saved)
        costs.bind_purchase_slot(session, batch.id, 1, saved)
        session.commit()
        rows = records(session, models)
        assert len(rows) == 3
        assert sum(row.cost_cents for row in rows) == 101
        assert [row.account_id for row in rows] == [None, saved.id, None]
        assert saved.purchase_cost_cents == 34
        assert rows[1].identity_id == saved.identity_id
        other = account(session, 'other')
        with pytest.raises(ValueError):
            costs.bind_purchase_slot(session, batch.id, 1, other)
        with pytest.raises(ValueError):
            costs.bind_purchase_slot(session, batch.id, 3, saved)


def test_batch_request_key_is_idempotent_and_conflicting_reuse_is_rejected(ledger):
    engine, models, costs = ledger
    with Session(engine) as session:
        kwargs = dict(total_cost_cents=100, expected_count=3, source='json_import', request_key='repeat')
        first = costs.create_purchase_batch(session, **kwargs)
        session.commit()
        again = costs.create_purchase_batch(session, **kwargs)
        assert again.id == first.id
        assert len(records(session, models)) == 3
        for changed in [dict(total_cost_cents=101), dict(expected_count=2), dict(source='task_login')]:
            with pytest.raises(ValueError):
                costs.create_purchase_batch(session, **dict(kwargs, **changed))
        assert len(records(session, models)) == 3


def test_batch_and_slots_are_rolled_back_together(ledger):
    engine, models, costs = ledger
    with Session(engine) as session:
        costs.create_purchase_batch(session, total_cost_cents=99, expected_count=2, source='json_import', request_key='rollback')
        session.rollback()
    with Session(engine) as session:
        assert records(session, models) == []
        assert session.exec(select(models.PurchaseBatchModel)).all() == []


@pytest.mark.parametrize('amount,count', [(-1, 1), (1, 0), (True, 1), (1.2, 1), (10, True)])
def test_service_rejects_invalid_amounts_and_counts_before_writing(ledger, amount, count):
    engine, models, costs = ledger
    with Session(engine) as session:
        with pytest.raises(ValueError):
            costs.create_purchase_batch(session, total_cost_cents=amount, expected_count=count, source='json_import', request_key='bad')
        assert records(session, models) == []


def test_legacy_costs_migrate_once_with_unknown_purchase_date(ledger):
    engine, models, costs = ledger
    with Session(engine) as session:
        existing = account(session, cost=1234)
        existing.created_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        account(session, 'unknown')
        costs.ensure_legacy_purchase_costs(session)
        costs.ensure_legacy_purchase_costs(session)
        session.commit()
        rows = records(session, models)
        assert len(rows) == 1
        assert rows[0].account_id == existing.id
        assert rows[0].cost_cents == 1234
        assert rows[0].incurred_at is None
        costs.update_account_purchase_cost(session, existing, 999)
        session.commit()
        assert records(session, models)[0].incurred_at is None
        assert sum(row.cost_cents for row in records(session, models)) == 999


def test_account_delete_preserves_purchase_record_and_cost(ledger):
    engine, models, costs = ledger
    with Session(engine) as session:
        saved = account(session)
        batch = costs.create_purchase_batch(session, total_cost_cents=99, expected_count=1, source='task_login', request_key='delete')
        costs.bind_purchase_slot(session, batch.id, 0, saved)
        session.commit()
        session.delete(saved)
        session.commit()
        rows = records(session, models)
        assert len(rows) == 1
        assert rows[0].cost_cents == 99
        assert rows[0].identity_id == 'identity-one'


def test_manual_cost_updates_preserve_date_and_clear_means_unknown(ledger):
    engine, models, costs = ledger
    with Session(engine) as session:
        saved = account(session)
        costs.update_account_purchase_cost(session, saved, 500)
        session.flush()
        date = records(session, models)[0].incurred_at
        assert date is not None
        costs.update_account_purchase_cost(session, saved, 0)
        assert records(session, models)[0].cost_cents == 0
        costs.update_account_purchase_cost(session, saved, None)
        session.commit()
        assert saved.purchase_cost_cents is None
        assert records(session, models)[0].cost_cents is None
        assert records(session, models)[0].incurred_at == date
        costs.ensure_legacy_purchase_costs(session)
        assert len(records(session, models)) == 1


@pytest.mark.parametrize('initial,corrected,expected', [([100, 200], 101, [34, 67]), ([0, 0], 3, [2, 1])])
def test_multi_purchase_correction_is_proportional_and_preserves_each_date(ledger, initial, corrected, expected):
    engine, models, costs = ledger
    with Session(engine) as session:
        saved = account(session)
        for index, amount in enumerate(initial):
            batch = costs.create_purchase_batch(session, total_cost_cents=amount, expected_count=1, source='task_login', request_key=f'purchase-{index}', incurred_at=datetime(2025, 1, index + 1))
            costs.bind_purchase_slot(session, batch.id, 0, saved)
        dates = [row.incurred_at for row in records(session, models)]
        costs.update_account_purchase_cost(session, saved, corrected)
        session.commit()
        rows = records(session, models)
        assert [row.cost_cents for row in rows] == expected
        assert [row.incurred_at for row in rows] == dates
        assert saved.purchase_cost_cents == corrected
        costs.ensure_legacy_purchase_costs(session)
        assert len(records(session, models)) == 2


def test_stale_concurrent_slot_binding_cannot_reassign_a_purchase(ledger):
    engine, models, costs = ledger
    with Session(engine) as session:
        first = account(session, 'winner')
        second = account(session, 'late')
        batch = costs.create_purchase_batch(session, total_cost_cents=100, expected_count=1, source='task_login', request_key='race')
        session.commit()
        first_id, second_id, batch_id = first.id, second.id, batch.id
    with Session(engine) as first_session, Session(engine) as second_session:
        stale = records(second_session, models)[0]
        assert stale.account_id is None
        costs.bind_purchase_slot(first_session, batch_id, 0, first_session.get(AccountModel, first_id))
        first_session.commit()
        with pytest.raises(ValueError):
            costs.bind_purchase_slot(second_session, batch_id, 0, second_session.get(AccountModel, second_id))
        second_session.rollback()
    with Session(engine) as session:
        assert records(session, models)[0].account_id == first_id


def test_deletion_detaches_cost_owner_before_sqlite_account_id_reuse(ledger):
    from core.db import cleanup_chatgpt_account_dependents

    engine, models, costs = ledger
    with Session(engine) as session:
        first = account(session, 'deleted')
        costs.update_account_purchase_cost(session, first, 123)
        session.commit()
        old_id = first.id
        session.delete(first)
        session.flush()
        cleanup_chatgpt_account_dependents(session, old_id)
        session.commit()
        assert records(session, models)[0].account_id is None
        replacement = account(session, 'replacement')
        assert replacement.id == old_id
        costs.update_account_purchase_cost(session, replacement, 456)
        session.commit()
        rows = records(session, models)
        assert len(rows) == 2
        assert [row.cost_cents for row in rows] == [123, 456]
        assert rows[0].identity_id == 'identity-deleted'
        assert rows[1].identity_id == 'identity-replacement'


def test_unknown_historical_weights_are_not_treated_as_free_purchases(ledger):
    engine, models, costs = ledger
    with Session(engine) as session:
        saved = account(session)
        first = costs.create_purchase_batch(session, total_cost_cents=100, expected_count=1, source='task_login', request_key='unknown-old')
        costs.bind_purchase_slot(session, first.id, 0, saved)
        costs.update_account_purchase_cost(session, saved, None)
        second = costs.create_purchase_batch(session, total_cost_cents=200, expected_count=1, source='task_login', request_key='known-new')
        costs.bind_purchase_slot(session, second.id, 0, saved)
        assert [row.cost_cents for row in records(session, models)] == [None, 200]
        costs.update_account_purchase_cost(session, saved, 101)
        assert [row.cost_cents for row in records(session, models)] == [51, 50]
