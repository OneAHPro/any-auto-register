"""Authenticated operating totals and per-instance sale-price configuration."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from threading import RLock
from time import monotonic
from weakref import WeakKeyDictionary
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator
from sqlmodel import Session, select
from core.db import Codex2APITargetModel, get_session
from core.operations_models import InstanceSalePriceModel
from services.operations_overview import build_operations_overview

router = APIRouter(prefix='/operations', tags=['operations'])
_CACHE = WeakKeyDictionary()
_LOCK = RLock()

class SalePriceRequest(BaseModel):
    price_cny_per_usd: Decimal

    @field_validator('price_cny_per_usd', mode='before')
    @classmethod
    def validate_price(cls, value):
        if isinstance(value, bool): raise ValueError('售价必须为非负金额')
        try: price = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError): raise ValueError('售价格式无效')
        if not price.is_finite() or price < 0 or price > 999999 or price.as_tuple().exponent < -4:
            raise ValueError('售价必须为非负金额，最多四位小数')
        return price

def _price_payload(row):
    stamp = row.updated_at
    if stamp.tzinfo is None: stamp = stamp.replace(tzinfo=timezone.utc)
    return {'target_id': row.target_id, 'price_cny_per_usd': format(Decimal(row.price_micros)/1_000_000,'.4f'), 'effective_at': stamp.isoformat()}

@router.get('/sale-prices')
def list_sale_prices(session: Session = Depends(get_session)):
    return {'items': [_price_payload(row) for row in session.exec(select(InstanceSalePriceModel)).all()]}

@router.put('/targets/{target_id}/sale-price')
def set_sale_price(target_id: int, body: SalePriceRequest, session: Session = Depends(get_session)):
    if session.get(Codex2APITargetModel, target_id) is None: raise HTTPException(404, '实例不存在')
    row = session.get(InstanceSalePriceModel, target_id) or InstanceSalePriceModel(target_id=target_id,price_micros=0)
    row.price_micros = int(body.price_cny_per_usd * 1_000_000)
    row.updated_at = datetime.now(timezone.utc)
    session.add(row); session.commit(); session.refresh(row)
    with _LOCK: _CACHE.pop(session.get_bind(), None)
    return _price_payload(row)

@router.get('/overview')
def overview(refresh: bool = Query(default=False), session: Session = Depends(get_session)):
    engine = session.get_bind()
    now = monotonic()
    with _LOCK:
        cached = _CACHE.get(engine)
        if cached and ((not refresh and cached[0] > now) or cached[2] >= now): return cached[1]
        result = build_operations_overview(engine, refresh=refresh)
        generated = monotonic()
        _CACHE[engine] = (generated+10, result, generated)
        return result
