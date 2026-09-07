"""Global operating figures derived from purchase records and upstream account bills.

The account list's page size is never used as an accounting boundary. Durable
target/account snapshots survive account removal and movement between instances.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo
from sqlmodel import Session, select

from core.db import AccountModel, AccountTargetBindingModel, Codex2APITargetModel, CodexInventorySnapshotModel
from core.purchase_cost_models import PurchaseBatchModel, PurchaseCostRecordModel
from core.operations_models import InstanceSalePriceModel, OperationsBillingSnapshotModel
from services.codex_account_billing import fetch_account_usage_details
from services.operations_inventory import refresh_operations_inventory

BUSINESS_TZ = ZoneInfo('Asia/Shanghai')
MICRO = Decimal(1_000_000)

def _aware(value):
    if value is None:
        return None
    if isinstance(value, str):
        try: value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError: return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value

def _iso(value):
    value = _aware(value)
    return value.isoformat() if value else None

def _number(value):
    if value is None or isinstance(value, bool): return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and 0 <= result <= Decimal('9000000000000') else None
    except (InvalidOperation, ValueError, TypeError): return None

def _micros(value):
    number = _number(value)
    return int((number * MICRO).to_integral_value(rounding=ROUND_HALF_UP)) if number is not None else None

def _money(value, digits=2):
    if value is None: return None
    return format(Decimal(value).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP), f'.{digits}f')

def _json(value):
    try:
        result = json.loads(value or '{}')
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError): return {}

def _same_day(value, day):
    parsed = _aware(value)
    return bool(parsed and parsed.astimezone(BUSINESS_TZ).date() == day)

def _row_status(row):
    status = str(row.get('remote_status') or row.get('status') or '').lower()
    if row.get('_missing') or row.get('_error') or status in {'invalid', 'unauthorized', 'disabled', 'banned', 'deleted', 'error', 'expired'}:
        return 'abnormal'
    if status == 'rate_limited' or any((_number(row.get(f'usage_percent_{window}')) or 0) >= 100 for window in ('5h', '7d')):
        return 'limited'
    if status in {'active', 'ready', 'registered', 'valid', 'ok'} and row.get('enabled') is not False:
        return 'normal'
    return 'abnormal'

def _billing_trend(persisted, keys, prices, day, inventory_complete):
    series = {}
    all_history_complete = inventory_complete
    for key in keys:
        snapshot = persisted.get(key)
        rows = []
        try: rows = json.loads(snapshot.history_json) if snapshot else []
        except (ValueError, TypeError): pass
        days = {}
        valid = isinstance(rows, list)
        for row in rows if isinstance(rows, list) else []:
            try: row_day = date.fromisoformat(str(row.get('date', ''))).isoformat()
            except (ValueError, AttributeError): valid = False; continue
            amount = _number(row.get('account_billed'))
            if amount is None or row_day in days: valid = False; continue
            days[row_day] = (amount, row.get('requests'))
        total = Decimal(snapshot.total_billed_micros) / MICRO if snapshot and snapshot.total_billed_micros is not None else None
        full = valid and total is not None and abs(sum((item[0] for item in days.values()), Decimal(0)) - total) <= Decimal('0.00001')
        all_history_complete = all_history_complete and full
        series[key] = (snapshot, days, full)
    points = []
    for offset in range(6, -1, -1):
        stamp = (day-timedelta(days=offset)).isoformat()
        billed = Decimal(0)
        revenue = Decimal(0)
        requests = 0
        known = inventory_complete
        priced = True
        requests_known = True
        for key, (snapshot, days, full) in series.items():
            if stamp in days: amount, count = days[stamp]
            elif snapshot and stamp == snapshot.today_date and snapshot.today_billed_micros is not None:
                amount, count = Decimal(snapshot.today_billed_micros) / MICRO, snapshot.today_requests
            elif full: amount, count = Decimal(0), 0
            else: known = False; continue
            billed += amount
            if key[0] in prices: revenue += amount * Decimal(prices[key[0]].price_micros) / MICRO
            else: priced = False
            if isinstance(count, int) and not isinstance(count, bool) and count >= 0: requests += count
            else: requests_known = False
        points.append({'date': stamp, 'billed_usd': _money(billed,6) if known else None,
            'revenue_cny': _money(revenue) if known and priced else None, 'requests': requests if known and requests_known else None})
    return {'complete': bool(all_history_complete), 'points': points}

def build_operations_overview(database_engine, refresh=False, now=None):
    now = _aware(now or datetime.now(timezone.utc))
    day = now.astimezone(BUSINESS_TZ).date()
    with Session(database_engine) as session:
        targets = session.exec(select(Codex2APITargetModel).order_by(Codex2APITargetModel.id)).all()
    inventory_complete = refresh_operations_inventory(database_engine, [row.id for row in targets if row.enabled], refresh=refresh)
    with Session(database_engine) as session:
        accounts = session.exec(select(AccountModel).where(AccountModel.platform == 'chatgpt')).all()
        bindings = session.exec(select(AccountTargetBindingModel)).all()
        inventory = session.exec(select(CodexInventorySnapshotModel)).all()
        prices = {row.target_id: row for row in session.exec(select(InstanceSalePriceModel)).all()}
        costs = session.exec(select(PurchaseCostRecordModel)).all()
        batches = session.exec(select(PurchaseBatchModel).order_by(PurchaseBatchModel.created_at.desc()).limit(8)).all()
        persisted = {(row.target_id, row.remote_id): row for row in session.exec(select(OperationsBillingSnapshotModel)).all()}

    target_map = {row.id: row for row in targets}
    inventory_map = {(row.target_id, row.remote_id): row for row in inventory}
    account_ids = {row.id for row in accounts}
    bound_keys = {(row.target_id, row.remote_account_id) for row in bindings if row.local_account_id in account_ids}
    if any(key not in bound_keys and not row.missing and not row.error for key, row in inventory_map.items()):
        from services.codex_inventory import materialize_inventory
        materialize_inventory(database_engine)
        with Session(database_engine) as session:
            accounts = session.exec(select(AccountModel).where(AccountModel.platform == 'chatgpt')).all()
            bindings = session.exec(select(AccountTargetBindingModel)).all()
    keys = set(inventory_map) | set(persisted) | {(row.target_id, row.remote_account_id) for row in bindings if row.remote_account_id > 0}
    active_keys = [key for key in sorted(keys) if key[0] in target_map and target_map[key[0]].enabled]
    details = fetch_account_usage_details(database_engine, active_keys, refresh=refresh)
    freshness = {}
    with Session(database_engine) as session:
        for key in sorted(keys):
            detail = details.get(key)
            previous = persisted.get(key)
            if detail and _micros(detail.get('total_billed_usd')) is not None:
                amount = _micros(detail['total_billed_usd'])
                # A decreasing all-time counter is not silently treated as new income.
                if previous and previous.total_billed_micros is not None and amount < previous.total_billed_micros:
                    freshness[key] = 'stale'
                    continue
                row = session.get(OperationsBillingSnapshotModel, previous.id) if previous else OperationsBillingSnapshotModel(target_id=key[0], remote_id=key[1])
                row.total_billed_micros = amount
                row.today_date = str(detail.get('today_date') or '')
                row.today_billed_micros = _micros(detail.get('today_billed_usd'))
                requests = detail.get('today_requests')
                row.today_requests = requests if isinstance(requests, int) and not isinstance(requests, bool) and requests >= 0 else None
                row.captured_at = _aware(detail.get('fetched_at')) or now
                history = detail.get('history')
                if isinstance(history, list): row.history_json = json.dumps(history)
                session.add(row)
                session.flush()
                # Keep only detached values after this transaction.
                persisted[key] = OperationsBillingSnapshotModel(**row.model_dump())
                freshness[key] = 'stale' if detail.get('refresh_error') else 'available'
            else:
                freshness[key] = 'stale' if previous and previous.total_billed_micros is not None else 'unavailable'
        session.commit()

    # Use exactly the same visibility, missing-remote and auth-state rules as
    # the account cards. Incomplete local attempts must not inflate the pool.
    from api.accounts import _build_account_list
    with Session(database_engine) as session:
        population = _build_account_list(platform='chatgpt', include_live=True, session=session, summary_only=True)
    visible_ids = set(population['account_ids'])
    accounts = [account for account in accounts if account.id in visible_ids]
    account_status = population['summary']
    remote_count = population['remote_count']
    account_targets = {}
    for binding in bindings:
        if binding.local_account_id in visible_ids and binding.enabled:
            account_targets[binding.local_account_id] = binding.target_id

    known_cost = sum(row.cost_cents or 0 for row in costs)
    today_cost = sum(row.cost_cents or 0 for row in costs if _same_day(row.incurred_at, day))
    undated_cost = sum(row.cost_cents or 0 for row in costs if row.incurred_at is None)
    covered_ids = {row.account_id for row in costs if row.account_id is not None and row.cost_cents is not None}
    unknown_ids = {account.id for account in accounts if account.id not in covered_ids or account.purchase_cost_cents is None}
    unknown_ids.update(row.account_id for row in costs if row.account_id in visible_ids and row.cost_cents is None)
    unknown_cost_count = len(unknown_ids) + remote_count
    unknown_cost_records = sum(row.cost_cents is None and row.account_id not in visible_ids for row in costs)
    costs_complete = unknown_cost_count == 0 and unknown_cost_records == 0
    today_costs_complete = costs_complete and undated_cost == 0

    target_rows = []
    target_amounts = []
    attention = []
    target_ids = sorted(set(target_map) | {key[0] for key in keys})
    for target_id in target_ids:
        target = target_map.get(target_id)
        node_keys = [key for key in sorted(keys) if key[0] == target_id]
        inventory_known = inventory_complete.get(target_id, False) if target and target.enabled else bool(node_keys or (target and target.last_sync_at))
        node_records = [persisted[key] for key in node_keys if key in persisted]
        amounts = [row.total_billed_micros for row in node_records if row.total_billed_micros is not None]
        all_amount = Decimal(sum(amounts)) / MICRO if amounts or (not node_keys and inventory_known) else None
        today_valid = inventory_known and all(key in persisted and persisted[key].today_date == day.isoformat() and persisted[key].today_billed_micros is not None and freshness.get(key) == 'available' for key in node_keys)
        today_amount = Decimal(sum(row.today_billed_micros or 0 for row in node_records)) / MICRO if today_valid else None
        price = Decimal(prices[target_id].price_micros) / MICRO if target_id in prices else None
        target_amounts.append((today_amount, all_amount, price))
        statuses = [freshness.get(key, 'unavailable') for key in node_keys]
        status = 'available' if inventory_known and all(item == 'available' for item in statuses) else 'stale' if amounts else 'unavailable'
        node_inventory = [row for key, row in inventory_map.items() if key[0] == target_id and not row.missing]
        raw_rows = [{**_json(row.summary_json), '_error': row.error, '_missing': row.missing} for row in node_inventory]
        normal = sum(_row_status(row) == 'normal' for row in raw_rows)
        limited = sum(_row_status(row) == 'limited' for row in raw_rows)
        remaining = []
        estimated = Decimal(0)
        estimable = bool(raw_rows)
        for raw in raw_rows:
            percentages = [number for window in ('5h', '7d') if (number := _number(raw.get(f'usage_percent_{window}'))) is not None]
            if percentages: remaining.append(max(Decimal(0), Decimal(100) - max(percentages)))
            percent = _number(raw.get('usage_percent_7d'))
            detail = raw.get('usage_7d_detail') if isinstance(raw.get('usage_7d_detail'), dict) else {}
            billed = _number(raw.get('billed_7d'))
            if billed is None: billed = _number(detail.get('account_billed'))
            if percent is not None and percent > 0 and billed is not None:
                estimated += billed * max(Decimal(0), Decimal(100) - percent) / percent
            else: estimable = False
        error = None if status == 'available' else '实例库存尚未完整更新' if not inventory_known else '部分账号计费尚未更新，保留最近记录'
        total_accounts = len(node_inventory) or sum(value == target_id for value in account_targets.values())
        row = {'id': target_id, 'name': target.name if target else f'已移除实例 #{target_id}',
            'enabled': bool(target and target.enabled), 'health_status': target.health_status if target else 'unknown',
            'total_accounts': total_accounts, 'normal_accounts': normal, 'limited_accounts': limited, 'abnormal_accounts': max(0, len(raw_rows) - normal - limited),
            'remaining_percent_avg': float(sum(remaining)/len(remaining)) if remaining else None,
            'estimated_remaining_usd': _money(estimated, 6) if estimable else None,
            'today_billed_usd': _money(today_amount, 6), 'total_billed_usd': _money(all_amount, 6),
            'today_requests': sum(record.today_requests or 0 for record in node_records) if today_valid and all(record.today_requests is not None for record in node_records) else None,
            'price_cny_per_usd': _money(price, 4), 'today_revenue_cny': _money(today_amount * price) if today_amount is not None and price is not None else None,
            'total_revenue_cny': _money(all_amount * price) if all_amount is not None and price is not None else None,
            'captured_at': min((_iso(record.captured_at) for record in node_records if record.captured_at), default=None),
            'last_health_at': _iso(target.last_health_at) if target else None, 'billing_status': status, 'error': error}
        target_rows.append(row)
        if target and target.enabled:
            reason = '实例连接异常' if target.health_status not in {'healthy', 'recovering'} else '暂无正常可用账号' if normal == 0 else '平均剩余额度低于20%' if remaining and row['remaining_percent_avg'] < 20 else None
            if reason: attention.append({'target_id': target_id, 'name': target.name, 'reason': reason, 'severity': 'error' if normal == 0 else 'warning'})

    billing_complete = all(row['billing_status'] == 'available' for row in target_rows)
    today_complete = all(row['today_billed_usd'] is not None for row in target_rows)
    prices_complete = all(row['price_cny_per_usd'] is not None for row in target_rows)
    amounts = [_number(row['total_billed_usd']) for row in target_rows]
    total_billed = sum((amount for amount in amounts if amount is not None), Decimal(0)) if any(amount is not None for amount in amounts) or not target_rows else None
    today_billed = sum((_number(row['today_billed_usd']) for row in target_rows), Decimal(0)) if today_complete else None
    total_revenue = sum((amount * price for _, amount, price in target_amounts), Decimal(0)) if prices_complete and billing_complete else None
    today_revenue = sum((amount * price for amount, _, price in target_amounts), Decimal(0)) if prices_complete and today_complete else None
    total_cost_cny = Decimal(known_cost) / 100
    today_cost_cny = Decimal(today_cost) / 100
    recent_batches = []
    for batch in batches:
        records = [row for row in costs if row.batch_id == batch.id]
        batch_cost = None if any(row.cost_cents is None for row in records) else Decimal(sum(row.cost_cents or 0 for row in records)) / 100
        recent_batches.append({'id': batch.id, 'created_at': _iso(batch.created_at), 'source': batch.source, 'expected_count': batch.expected_count,
            'cost_cny': _money(batch_cost), 'linked_accounts': len({row.account_id for row in records if row.account_id is not None})})
    errors = [f"{row['name']}：{row['error']}" for row in target_rows if row['error']]
    return {'as_of': now.isoformat(), 'timezone': 'Asia/Shanghai', 'date': day.isoformat(), 'pricing_basis': 'current_configured_price',
        'coverage': {'targets_total': len(target_rows), 'targets_available': sum(row['billing_status']=='available' for row in target_rows),
            'billing_complete': billing_complete, 'today_complete': today_complete, 'prices_complete': prices_complete, 'costs_complete': costs_complete, 'today_costs_complete': today_costs_complete,
            'unknown_cost_accounts': unknown_cost_count, 'unknown_cost_records': unknown_cost_records,
            'undated_cost_cny': _money(Decimal(undated_cost)/100), 'errors': errors},
        'finance': {'today_cost_cny': _money(today_cost_cny), 'total_cost_cny': _money(total_cost_cny),
            'today_billed_usd': _money(today_billed,6), 'total_billed_usd': _money(total_billed,6),
            'today_revenue_cny': _money(today_revenue), 'total_revenue_cny': _money(total_revenue),
            'today_profit_cny': _money(today_revenue-today_cost_cny) if today_revenue is not None and today_costs_complete else None,
            'total_profit_cny': _money(total_revenue-total_cost_cny) if total_revenue is not None and costs_complete else None,
            'break_even_percent': float(total_revenue/total_cost_cny*100) if total_revenue is not None and costs_complete and total_cost_cny > 0 else None,
            'remaining_cost_cny': _money(max(Decimal(0),total_cost_cny-total_revenue)) if total_revenue is not None and costs_complete else None},
        'supply': {'today_purchased_accounts': sum(_same_day(row.incurred_at,day) for row in costs), 'total_purchased_accounts': len(costs),
            'today_added_accounts': sum(_same_day(account.created_at,day) for account in accounts)},
        'account_status': account_status, 'targets': target_rows, 'recent_batches': recent_batches, 'attention': attention,
        'trend': _billing_trend(persisted, keys, prices, day, all(inventory_complete.values()))}
