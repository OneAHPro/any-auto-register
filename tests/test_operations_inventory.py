from concurrent.futures import ThreadPoolExecutor
from threading import Event
from sqlalchemy import create_engine


def test_empty_successful_inventory_is_distinct_from_failed_inventory(monkeypatch):
    from services import operations_inventory as module
    engine=create_engine('sqlite://')
    calls=[]
    def sync(database_engine,target_id=None,refresh=False):
        calls.append(target_id)
        return {'targets':1,'errors':int(target_id==2),'upserted':0}
    monkeypatch.setattr(module,'sync_inventory',sync)
    assert module.refresh_operations_inventory(engine,[1,2])=={1:True,2:False}
    assert module.refresh_operations_inventory(engine,[1,2])=={1:True,2:False}
    assert sorted(calls)==[1,2]


def test_overlapping_overviews_share_inventory_work(monkeypatch):
    from services import operations_inventory as module
    engine=create_engine('sqlite://')
    entered=Event()
    release=Event()
    calls=[]
    def sync(database_engine,target_id=None,refresh=False):
        calls.append(target_id)
        entered.set()
        assert release.wait(2)
        return {'targets':1,'errors':0,'upserted':3}
    monkeypatch.setattr(module,'sync_inventory',sync)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(module.refresh_operations_inventory,engine,[1])
        assert entered.wait(1)
        second=pool.submit(module.refresh_operations_inventory,engine,[1])
        release.set()
        assert first.result(timeout=3)=={1:True}
        assert second.result(timeout=3)=={1:True}
    assert calls==[1]
