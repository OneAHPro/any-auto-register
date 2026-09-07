from datetime import datetime, timezone
import json
from decimal import Decimal

from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine

NOW = datetime(2026, 9, 7, 4, 0, tzinfo=timezone.utc)

def setup_world(monkeypatch):
    from services import operations_overview as module
    from core.db import AccountModel, AccountTargetBindingModel, Codex2APITargetModel, CodexInventorySnapshotModel
    from core.purchase_cost_models import PurchaseCostRecordModel
    from core.operations_models import InstanceSalePriceModel
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        for i, cost in ((1, 1000), (2, 2000)):
            s.add(AccountModel(id=i, platform='chatgpt', email=f'fixture{i}@example.test', password='PRIVATE_PASSWORD', identity_id=f'identity-{i}', purchase_cost_cents=cost, created_at=NOW))
            s.add(Codex2APITargetModel(id=i, name=f'node-{i}', base_url=f'https://node-{i}.example.test', admin_key_ref=f'fixture-ref-{i}', health_status='healthy', enabled=True))
            s.add(AccountTargetBindingModel(identity_id=f'identity-{i}', local_account_id=i, target_id=i, remote_account_id=i*11, enabled=True))
            s.add(CodexInventorySnapshotModel(target_id=i, remote_id=i*11, summary_json=json.dumps({'status':'active','enabled':True,'usage_percent_7d':20,'email':f'fixture{i}@example.test'}), fetched_at=NOW))
            s.add(PurchaseCostRecordModel(record_key=f'cost-{i}', account_id=i, cost_cents=cost, incurred_at=NOW if i==1 else datetime(2026,9,6,1,tzinfo=timezone.utc)))
            s.add(InstanceSalePriceModel(target_id=i, price_micros=220000 if i==1 else 260000, updated_at=NOW))
        s.add(PurchaseCostRecordModel(record_key='deleted-account', account_id=99, cost_cents=500, incurred_at=None))
        s.commit()
    details={(1,11):{'total_billed_usd':'100','today_date':'2026-09-07','today_billed_usd':'5','today_requests':12,'fetched_at':NOW.isoformat()}, (2,22):{'total_billed_usd':'200','today_date':'2026-09-07','today_billed_usd':'10','today_requests':20,'fetched_at':NOW.isoformat()}}
    monkeypatch.setattr(module,'fetch_account_usage_details',lambda *args,**kwargs:details)
    monkeypatch.setattr(module,'refresh_operations_inventory',lambda engine, target_ids, **kwargs:{target_id:True for target_id in target_ids})
    return module,engine,details

def test_global_finance_uses_all_nodes_and_actual_purchase_dates(monkeypatch):
    module,engine,_=setup_world(monkeypatch)
    data=module.build_operations_overview(engine,now=NOW)
    assert data['finance']['today_cost_cny']=='10.00'
    assert data['finance']['total_cost_cny']=='35.00'
    assert data['coverage']['undated_cost_cny']=='5.00'
    assert Decimal(data['finance']['today_billed_usd'])==15
    assert Decimal(data['finance']['total_billed_usd'])==300
    assert data['finance']['today_revenue_cny']=='3.70'
    assert data['finance']['total_revenue_cny']=='74.00'
    assert data['finance']['today_profit_cny'] is None
    assert data['coverage']['today_costs_complete'] is False
    assert data['finance']['total_profit_cny']=='39.00'
    assert data['finance']['remaining_cost_cny']=='0.00'
    assert data['account_status']['total']==2
    assert len(data['targets'])==2
    assert 'PRIVATE_PASSWORD' not in json.dumps(data)

def test_missing_cost_is_not_a_free_account_or_a_profit_claim(monkeypatch):
    module,engine,_=setup_world(monkeypatch)
    from core.db import AccountModel
    from core.purchase_cost_models import PurchaseCostRecordModel
    from sqlmodel import select
    with Session(engine) as s:
        s.get(AccountModel,1).purchase_cost_cents=None
        s.exec(select(PurchaseCostRecordModel).where(PurchaseCostRecordModel.account_id==1)).one().cost_cents=None
        s.commit()
    data=module.build_operations_overview(engine,now=NOW)
    assert data['coverage']['costs_complete'] is False
    assert data['coverage']['unknown_cost_accounts']==1
    assert data['finance']['total_profit_cny'] is None
    assert data['finance']['break_even_percent'] is None

def test_missing_price_does_not_default_to_a_sale_rate(monkeypatch):
    module,engine,_=setup_world(monkeypatch)
    from core.operations_models import InstanceSalePriceModel
    with Session(engine) as s:
        s.delete(s.get(InstanceSalePriceModel,2)); s.commit()
    data=module.build_operations_overview(engine,now=NOW)
    assert data['coverage']['prices_complete'] is False
    assert data['finance']['total_revenue_cny'] is None
    assert data['targets'][1]['price_cny_per_usd'] is None

def test_upstream_day_mismatch_does_not_masquerade_as_today(monkeypatch):
    module,engine,details=setup_world(monkeypatch)
    details[(2,22)]['today_date']='2026-09-06'
    data=module.build_operations_overview(engine,now=NOW)
    assert data['coverage']['today_complete'] is False
    assert data['finance']['today_billed_usd'] is None
    assert Decimal(data['finance']['total_billed_usd'])==300

def test_transient_failure_retains_durable_billing_with_a_stale_marker(monkeypatch):
    module,engine,details=setup_world(monkeypatch)
    module.build_operations_overview(engine,now=NOW)
    details[(2,22)]=None
    data=module.build_operations_overview(engine,refresh=True,now=NOW)
    assert data['coverage']['billing_complete'] is False
    assert Decimal(data['finance']['total_billed_usd'])==300
    assert data['targets'][1]['billing_status']=='stale'
    assert data['finance']['total_profit_cny'] is None

def test_cost_day_boundary_uses_shanghai_calendar(monkeypatch):
    module,engine,_=setup_world(monkeypatch)
    from core.purchase_cost_models import PurchaseCostRecordModel
    with Session(engine) as s:
        s.add(PurchaseCostRecordModel(record_key='late-utc',cost_cents=250,incurred_at=datetime(2026,9,6,16,1,tzinfo=timezone.utc)))
        s.commit()
    data=module.build_operations_overview(engine,now=NOW)
    assert data['finance']['today_cost_cny']=='12.50'

def test_unknown_empty_inventory_is_not_reported_as_zero_billing(monkeypatch):
    module,engine,_=setup_world(monkeypatch)
    from core.db import Codex2APITargetModel
    with Session(engine) as s:
        s.add(Codex2APITargetModel(id=3,name='new-node',base_url='https://new.example.test',admin_key_ref='fixture-new'))
        s.commit()
    monkeypatch.setattr(module,'refresh_operations_inventory',lambda engine,target_ids,**kwargs:{target_id:target_id != 3 for target_id in target_ids})
    data=module.build_operations_overview(engine,now=NOW)
    assert data['targets'][2]['total_billed_usd'] is None
    assert data['targets'][2]['today_billed_usd'] is None
    assert data['targets'][2]['billing_status']=='unavailable'
    assert data['coverage']['billing_complete'] is False

def test_daily_profit_is_computed_when_purchase_dates_are_complete(monkeypatch):
    module,engine,_=setup_world(monkeypatch)
    from core.purchase_cost_models import PurchaseCostRecordModel
    from sqlmodel import select
    with Session(engine) as s:
        record=s.exec(select(PurchaseCostRecordModel).where(PurchaseCostRecordModel.record_key=='deleted-account')).one()
        record.incurred_at=datetime(2026,9,5,tzinfo=timezone.utc)
        s.commit()
    data=module.build_operations_overview(engine,now=NOW)
    assert data['coverage']['today_costs_complete'] is True
    assert data['finance']['today_profit_cny']=='-6.30'

def test_weekly_trend_sums_real_daily_account_billing_per_instance(monkeypatch):
    module,engine,details=setup_world(monkeypatch)
    details[(1,11)]['history']=[{'date':'2026-09-06','account_billed':'95','requests':100},{'date':'2026-09-07','account_billed':'5','requests':12}]
    details[(2,22)]['history']=[{'date':'2026-09-06','account_billed':'190','requests':200},{'date':'2026-09-07','account_billed':'10','requests':20}]
    data=module.build_operations_overview(engine,now=NOW)
    assert data['trend']['complete'] is True
    assert len(data['trend']['points'])==7
    assert Decimal(data['trend']['points'][-2]['billed_usd'])==285
    assert data['trend']['points'][-2]['requests']==300
    assert Decimal(data['trend']['points'][-1]['billed_usd'])==15
    assert data['trend']['points'][-1]['revenue_cny']=='3.70'

def test_absent_history_is_unknown_rather_than_a_zero_trend(monkeypatch):
    module,engine,_=setup_world(monkeypatch)
    data=module.build_operations_overview(engine,now=NOW)
    assert data['trend']['complete'] is False
    assert data['trend']['points'][0]['billed_usd'] is None
    assert Decimal(data['trend']['points'][-1]['billed_usd'])==15

def test_global_revenue_rounds_after_combining_instance_values(monkeypatch):
    module,engine,details=setup_world(monkeypatch)
    from core.operations_models import InstanceSalePriceModel
    for detail in details.values():
        detail['total_billed_usd']='0.01'
        detail['today_billed_usd']='0'
    with Session(engine) as s:
        for target_id in (1,2): s.get(InstanceSalePriceModel,target_id).price_micros=250000
        s.commit()
    data=module.build_operations_overview(engine,now=NOW)
    assert data['finance']['total_revenue_cny']=='0.01'

def test_sale_price_validation_is_exact_and_rejects_boolean_negative_and_excess_precision():
    from api.operations import SalePriceRequest
    import pytest
    assert SalePriceRequest(price_cny_per_usd='0.2200').price_cny_per_usd==Decimal('0.2200')
    for value in (True,False,'-0.1','NaN','Infinity','0.12345'):
        with pytest.raises(ValueError): SalePriceRequest(price_cny_per_usd=value)

def test_http_price_save_refreshes_real_overview_and_preserves_other_instances(monkeypatch):
    module,engine,_=setup_world(monkeypatch)
    from api import operations
    from core.db import get_session
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app=FastAPI()
    app.include_router(operations.router,prefix='/api')
    def session_dependency():
        with Session(engine) as session: yield session
    app.dependency_overrides[get_session]=session_dependency
    monkeypatch.setattr(operations,'build_operations_overview',lambda database_engine,refresh=False:module.build_operations_overview(database_engine,refresh=refresh,now=NOW))
    client=TestClient(app)
    assert client.get('/api/operations/overview').json()['finance']['total_revenue_cny']=='74.00'
    response=client.put('/api/operations/targets/1/sale-price',json={'price_cny_per_usd':'0.2500'})
    assert response.status_code==200
    assert response.json()['price_cny_per_usd']=='0.2500'
    prices=client.get('/api/operations/sale-prices').json()['items']
    assert {row['target_id']:row['price_cny_per_usd'] for row in prices}=={1:'0.2500',2:'0.2600'}
    assert client.get('/api/operations/overview').json()['finance']['total_revenue_cny']=='77.00'
    assert client.put('/api/operations/targets/1/sale-price',json={'price_cny_per_usd':True}).status_code==422
    assert client.put('/api/operations/targets/999/sale-price',json={'price_cny_per_usd':'0.22'}).status_code==404
