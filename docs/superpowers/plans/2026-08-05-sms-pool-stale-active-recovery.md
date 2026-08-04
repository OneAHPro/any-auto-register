# SMS Pool Stale Active Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recover ownerless or terminal-owner SMS cards after a 30-minute active quarantine, preserve live-task cards, and show quarantine separately from actual use.

**Architecture:** Treat `updated_at` as immutable `active_since` while a row remains `active`, then recover strictly older rows inside the serialized SQLite write path. Startup performs an immediate interrupted/stale sweep, the scheduler repeats stale recovery every 600 seconds, and the API/UI expose active rows as **待回收** instead of folding them into **使用中**.

**Tech Stack:** Python 3, FastAPI, SQLModel/SQLAlchemy, SQLite, pytest/unittest, React 19, TypeScript, Ant Design, Vitest/Testing Library.

---

## File map

- `core/sms_pool.py`: timestamp invariant, stale recovery, and independent stats.
- `core/db.py`: startup recovery order.
- `core/scheduler.py`: 600-second recovery cadence and failure isolation.
- `api/sms_pool.py`: independent `reserved` and `active` filters.
- `frontend/src/pages/SmsPool.tsx`: **待回收** label, count, filter, and state time.
- `tests/test_sms_pool.py`: service, startup, and API contracts.
- `tests/test_scheduler.py`: cadence and retry contracts.
- `frontend/src/pages/SmsPool.test.tsx`: active-state presentation contract.

## Task 1: Preserve active age and recover stale rows

**Files:**
- Modify: `tests/test_sms_pool.py:227-416`
- Modify: `core/sms_pool.py:1-345`

- [ ] **Step 1: Write failing state-age tests**

Import `timedelta` and `TaskRunModel`. Add this helper and tests to
`SmsPoolServiceTests`:

```python
def _active_item(self, code, task_id, active_since):
    self.pool.import_text(code, default_base_url="https://sms.example.com/box")
    item = self.pool.reserve(task_id=task_id, count=1)[0]
    assert self.pool.mark_active(
        item_id=int(item.id), task_id=task_id,
        account_email=f"{code}@example.com",
    )
    with Session(self.engine) as session:
        row = session.get(SmsPoolItemModel, int(item.id))
        row.updated_at = active_since
        session.add(row)
        session.commit()
    return int(item.id)

def test_release_and_restart_preserve_existing_active_age(self):
    active_since = datetime(2026, 8, 5, tzinfo=timezone.utc)
    item_id = self._active_item("age-card", "task-finished", active_since)
    assert self.pool.release_task("task-finished") == 1
    assert self.pool.recover_interrupted() == 1
    with Session(self.engine) as session:
        row = session.get(SmsPoolItemModel, item_id)
        assert row.status == "active"
        assert row.reserved_task_id == ""
        assert row.updated_at == active_since

def test_stale_ownerless_active_releases_after_thirty_minutes(self):
    now = datetime(2026, 8, 5, 1, tzinfo=timezone.utc)
    item_id = self._active_item(
        "stale-card", "task-finished",
        now - timedelta(minutes=30, seconds=1),
    )
    self.pool.release_task("task-finished")
    assert self.pool.recover_stale_active(now=now) == 1
    with Session(self.engine) as session:
        row = session.get(SmsPoolItemModel, item_id)
        assert row.status == "unused"
        assert row.used_by_email == ""
        assert row.updated_at == now

def test_exact_boundary_and_running_owner_remain_active(self):
    now = datetime(2026, 8, 5, 1, tzinfo=timezone.utc)
    boundary_id = self._active_item(
        "boundary-card", "task-missing", now - timedelta(minutes=30)
    )
    running_id = self._active_item(
        "running-card", "task-running", now - timedelta(hours=2)
    )
    with Session(self.engine) as session:
        session.add(TaskRunModel(
            id="task-running", platform="chatgpt", status="running",
            created_at=now - timedelta(hours=2), updated_at=now,
        ))
        session.commit()
    assert self.pool.recover_stale_active(now=now) == 0
    with Session(self.engine) as session:
        assert session.get(SmsPoolItemModel, boundary_id).status == "active"
        assert session.get(SmsPoolItemModel, running_id).status == "active"

def test_terminal_and_missing_owners_release_when_stale(self):
    now = datetime(2026, 8, 5, 1, tzinfo=timezone.utc)
    terminal_id = self._active_item(
        "terminal-card", "task-stopped", now - timedelta(hours=2)
    )
    missing_id = self._active_item(
        "missing-card", "task-missing", now - timedelta(hours=2)
    )
    with Session(self.engine) as session:
        session.add(TaskRunModel(
            id="task-stopped", platform="chatgpt", status="stopped",
            created_at=now - timedelta(hours=2), updated_at=now,
        ))
        session.commit()
    assert self.pool.recover_stale_active(now=now) == 2
    with Session(self.engine) as session:
        assert session.get(SmsPoolItemModel, terminal_id).status == "unused"
        assert session.get(SmsPoolItemModel, missing_id).status == "unused"
```

Extend the existing explicit-quarantine test by patching `_utcnow()` to a fixed
instant and asserting a `reserved -> active` row receives that new timestamp.

- [ ] **Step 2: Run tests and verify RED**

```bash
PYTHONPATH=. .venv/bin/pytest tests/test_sms_pool.py \
  -k 'active_age or thirty_minutes or running_owner or missing_owners' -q
```

Expected: the old cleanup refreshes timestamps and `recover_stale_active` is missing.

- [ ] **Step 3: Preserve active age**

In `release_task()`, write `updated_at` only for `reserved -> active` or a
transition to `unused`:

```python
was_active = row.status == "active"
must_quarantine = was_active or int(row.id or 0) in quarantined_ids
row.status = "active" if must_quarantine else "unused"
row.reserved_task_id = ""
row.reserved_at = None
if must_quarantine:
    row.used_at = None
    if not was_active:
        row.updated_at = now
else:
    row.used_by_email = ""
    row.used_at = None
    row.updated_at = now
```

In `recover_interrupted()`, preserve `updated_at` for active rows and update it
only when a reserved row becomes unused.

- [ ] **Step 4: Implement strict stale recovery**

Import `timedelta` and `TaskRunModel`, define
`SMS_POOL_ACTIVE_STALE_SECONDS = 30 * 60`, and add:

```python
def recover_stale_active(
    self, *, now=None, stale_seconds=SMS_POOL_ACTIVE_STALE_SECONDS
):
    effective_now = now or _utcnow()
    if effective_now.tzinfo is None:
        effective_now = effective_now.replace(tzinfo=timezone.utc)
    else:
        effective_now = effective_now.astimezone(timezone.utc)
    cutoff = effective_now - timedelta(seconds=max(0.0, float(stale_seconds)))
    with self._lock, Session(self.engine) as session:
        self._begin_write(session)
        rows = session.exec(
            select(SmsPoolItemModel)
            .where(SmsPoolItemModel.status == "active")
            .where(SmsPoolItemModel.updated_at < cutoff)
        ).all()
        owners = {str(row.reserved_task_id or "").strip() for row in rows}
        owners.discard("")
        protected = set()
        if owners:
            protected = set(session.exec(
                select(TaskRunModel.id)
                .where(TaskRunModel.id.in_(owners))
                .where(TaskRunModel.status.in_(["pending", "running"]))
            ).all())
        recovered = 0
        for row in rows:
            if str(row.reserved_task_id or "").strip() in protected:
                continue
            row.status = "unused"
            row.reserved_task_id = ""
            row.reserved_at = None
            row.used_at = None
            row.used_by_email = ""
            row.updated_at = effective_now
            session.add(row)
            recovered += 1
        session.commit()
        return recovered
```

- [ ] **Step 5: Run the pool suite and commit**

```bash
PYTHONPATH=. .venv/bin/pytest tests/test_sms_pool.py -q
git add core/sms_pool.py tests/test_sms_pool.py
git commit -m "fix: recover stale SMS pool cards"
```

Expected: existing immediate-quarantine and provider-settlement tests also pass.

## Task 2: Run startup and 600-second recovery

**Files:**
- Modify: `tests/test_sms_pool.py`
- Modify: `tests/test_scheduler.py:1-230`
- Modify: `core/db.py:315-340`
- Modify: `core/scheduler.py:1-130`

- [ ] **Step 1: Write failing lifecycle tests**

```python
def test_init_db_runs_interrupted_before_stale_recovery():
    pool = mock.Mock()
    with (
        mock.patch("core.db.SQLModel.metadata.create_all"),
        mock.patch("core.db._migrate_outlook_accounts_schema"),
        mock.patch("core.db._recover_chatgpt_attempt_bindings"),
        mock.patch("core.sms_pool.SmsPoolService", return_value=pool),
    ):
        init_db()
    assert pool.method_calls == [
        mock.call.recover_interrupted(), mock.call.recover_stale_active(),
    ]
```

Add scheduler tests that set `_last_sms_pool_recovery_at = 0.0`: one calls
`run_once(wall_now, 599.0)` then `run_once(wall_now, 600.0)` and asserts exactly
one `recover_stale_sms_pool(now=wall_now)` call plus a marker of `600.0`; the
other raises `RuntimeError` and asserts the marker remains `0.0` while automatic
re-login still ticks.

- [ ] **Step 2: Run tests and verify RED**

```bash
PYTHONPATH=. .venv/bin/pytest tests/test_sms_pool.py::test_init_db_runs_interrupted_before_stale_recovery tests/test_scheduler.py -q
```

Expected: startup and scheduler recovery entry points are missing.

- [ ] **Step 3: Implement startup order**

Change the end of `init_db()`:

```python
from core.sms_pool import SmsPoolService
pool = SmsPoolService(engine)
pool.recover_interrupted()
pool.recover_stale_active()
```

- [ ] **Step 4: Implement scheduler cadence and isolation**

Add a lazy `recover_stale_sms_pool(now=...)` wrapper, interval `600`, and last
marker state. Initialize the marker to `time.monotonic()` in `start()` because
startup recovery already ran. Add this independent `run_once()` block:

```python
if (
    monotonic_now - self._last_sms_pool_recovery_at
    >= self._sms_pool_recovery_interval_seconds
):
    try:
        recovered = recover_stale_sms_pool(now=wall_now)
    except Exception as e:
        print(f"[Scheduler] SMS 接码池回收错误: {type(e).__name__}")
    else:
        self._last_sms_pool_recovery_at = monotonic_now
        if recovered:
            print(f"[Scheduler] 已回收过期 SMS 接码池卡密: {recovered} 张")
```

- [ ] **Step 5: Run lifecycle suites and commit**

```bash
PYTHONPATH=. .venv/bin/pytest tests/test_sms_pool.py tests/test_scheduler.py -q
git add core/db.py core/scheduler.py tests/test_sms_pool.py tests/test_scheduler.py
git commit -m "fix: schedule SMS pool stale recovery"
```

## Task 3: Separate active quarantine in API and UI

**Files:**
- Modify: `tests/test_sms_pool.py:207-225`
- Modify: `api/sms_pool.py:35-67`
- Modify: `frontend/src/pages/SmsPool.test.tsx:33-163`
- Modify: `frontend/src/pages/SmsPool.tsx:27-270`

- [ ] **Step 1: Write the failing API test**

Create two reserved rows, mark one active, then assert stats equal:

```python
{"total": 2, "unused": 0, "reserved": 1, "active": 1, "used": 0}
```

Also call `list_sms_pool_items("reserved", 1, 50)` and
`list_sms_pool_items("active", 1, 50)` and assert each returns only its exact
state.

- [ ] **Step 2: Run the API test and verify RED**

```bash
PYTHONPATH=. .venv/bin/pytest tests/test_sms_pool.py -k 'counts_and_filters_reserved' -q
```

Expected: active is rejected and included in reserved stats.

- [ ] **Step 3: Split service stats and list filters**

Return independent `reserved` and `active` counts from `stats()`. In
`api/sms_pool.py`, accept `{"unused", "reserved", "active", "used"}` and use
`SmsPoolItemModel.status == normalized_status` for list and count queries.
Update older combined-count assertions.

- [ ] **Step 4: Write failing frontend assertions**

Return `{ total: 4, unused: 1, reserved: 1, active: 1, used: 1 }` from the stats
fixture. Add an active row with no `reserved_at`, an `updated_at` of
`2026-08-01T00:20:00Z`, and assert:

```typescript
expect(screen.getByText('使用中 1')).toBeTruthy()
expect(screen.getByText('待回收 1')).toBeTruthy()
const row = screen.getByText('bei-sms-FULL-SECRET-0004').closest('tr')
expect(within(row as HTMLTableRowElement).getByText('待回收')).toBeTruthy()
expect(within(row as HTMLTableRowElement).getByText(
  new Date('2026-08-01T00:20:00Z').toLocaleString('zh-CN'),
)).toBeTruthy()
```

- [ ] **Step 5: Run the component test and verify RED**

```bash
cd frontend
npm test -- src/pages/SmsPool.test.tsx
```

Expected: active still renders as **使用中**, lacks its count, and shows the
import time.

- [ ] **Step 6: Implement active presentation**

Add `updated_at?: string` to `SmsPoolItem`, `active: number` to stats, initialize
it to zero, and apply:

```tsx
if (status === 'active') return <Tag color="warning">待回收</Tag>
if (status === 'reserved') return <Tag color="processing">使用中</Tag>

render: (_, item) => formatTime(
  item.used_at || item.reserved_at || item.updated_at || item.created_at,
),
```

Render `<Tag color="warning">待回收 {stats.active}</Tag>` and add the
`{ value: 'active', label: '待回收' }` filter option.

- [ ] **Step 7: Run tests/build and commit**

```bash
cd ..
PYTHONPATH=. .venv/bin/pytest tests/test_sms_pool.py -q
cd frontend
npm test -- src/pages/SmsPool.test.tsx
npm run build
cd ..
git add core/sms_pool.py api/sms_pool.py tests/test_sms_pool.py \
  frontend/src/pages/SmsPool.tsx frontend/src/pages/SmsPool.test.tsx
git commit -m "fix: distinguish stale SMS pool cards"
```

## Task 4: Review and full verification

**Files:**
- Verify: every file changed in Tasks 1-3
- Reference: `docs/superpowers/specs/2026-08-05-sms-pool-stale-active-recovery-design.md`

- [ ] **Step 1: Run cross-layer regression**

```bash
PYTHONPATH=. .venv/bin/pytest tests/test_sms_pool.py tests/test_scheduler.py \
  tests/test_register_task_controls.py tests/test_chatgpt_phone_flow.py \
  tests/test_chatgpt_phone_verification.py tests/test_chatgpt_login_with_phone.py -q
cd frontend
npm test -- src/pages/SmsPool.test.tsx src/components/ChatGPTExistingAccountLoginModal.test.tsx
npm run lint
npm run build
```

- [ ] **Step 2: Inspect invariants and request review**

Run `git diff --check`, inspect the feature diff, and confirm: active age is not
refreshed by release/startup; the boundary is `< cutoff`; pending/running owners
are protected; failures keep the scheduler retry marker; active is independent
in API/UI; recovery logs contain aggregate counts only; no migration exists.
Then use `superpowers:requesting-code-review`, resolve findings, and rerun Step 1.

- [ ] **Step 3: Complete production preflight and verification**

Before the atomic release switch, run `PRAGMA quick_check`, create a consistent
SQLite backup through the backup API, confirm no ChatGPT registration task is
pending/running, and record only aggregate SMS counts. After startup, verify no
active row older than 30 minutes has an empty/missing/terminal owner, live-owner
rows remain protected, the UI splits **使用中**/**待回收**, and automatic re-login
settings plus public service health are unchanged.
