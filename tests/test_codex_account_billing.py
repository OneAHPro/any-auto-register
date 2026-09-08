from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Event, Lock
from types import SimpleNamespace

from sqlalchemy import create_engine


def test_read_cached_billing_is_nonblocking_and_only_returns_fresh_copies(monkeypatch):
    from services import codex_account_billing as module

    engine = create_engine("sqlite://")
    clock = [100.0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module, "get_target_client", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no remote reads")))
    module._remember(engine, (1, 7), module._summary(12.5))
    result = module.read_cached_account_billing_summaries(engine, [(1, 7), (2, 7)])
    assert result[(1, 7)]["billed_usd"] == 12.5
    assert (2, 7) not in result
    result[(1, 7)]["billed_usd"] = 99
    assert module.read_cached_account_billing_summaries(engine, [(1, 7)])[(1, 7)]["billed_usd"] == 12.5
    clock[0] = 160.0
    assert module.read_cached_account_billing_summaries(engine, [(1, 7)]) == {}


def test_billing_queries_only_requested_target_account_pairs(monkeypatch):
    from services import codex_account_billing as module

    engine = create_engine("sqlite://")
    resolved = []
    requested = []

    def get_client(target_id, database_engine=None):
        resolved.append((target_id, database_engine))

        def account_usage_all(remote_id):
            requested.append((target_id, remote_id))
            return {
                "account_id": remote_id,
                "email": "same@example.com",
                "total_account_billed": target_id * 100 + remote_id,
                "total_user_billed": 99999,
                "admin_key": "secret-value",
                "billed_7d": 555,
            }

        return SimpleNamespace(account_usage_all=account_usage_all)

    monkeypatch.setattr(module, "get_target_client", get_client)
    keys = [(1, 11), (1, 12), (2, 11), (1, 11)]

    summaries = module.fetch_account_billing_summaries(engine, keys)

    assert set(summaries) == {(1, 11), (1, 12), (2, 11)}
    assert sorted(requested) == [(1, 11), (1, 12), (2, 11)]
    assert sorted(resolved) == [(1, engine), (2, engine)]
    for (target_id, remote_id), billing in summaries.items():
        assert billing == {
            "scope": "all",
            "billed_usd": target_id * 100 + remote_id,
            "source": "codex2api",
            "status": "available",
            "fetched_at": billing["fetched_at"],
        }
        assert datetime.fromisoformat(billing["fetched_at"]).utcoffset().total_seconds() == 0


def test_billing_cache_expires_after_sixty_seconds_and_refresh_bypasses_it(monkeypatch):
    from services import codex_account_billing as module

    engine = create_engine("sqlite://")
    clock = [100.0]
    calls = []

    def usage(remote_id):
        calls.append(remote_id)
        return {"total_account_billed": len(calls)}

    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module, "get_target_client", lambda *args, **kwargs: SimpleNamespace(account_usage_all=usage))

    first = module.fetch_account_billing_summaries(engine, [(1, 7)])
    first[(1, 7)]["billed_usd"] = 98765
    clock[0] = 159.9
    cached = module.fetch_account_billing_summaries(engine, [(1, 7)])
    assert cached[(1, 7)]["billed_usd"] == 1
    assert calls == [7]

    clock[0] = 160.0
    expired = module.fetch_account_billing_summaries(engine, [(1, 7)])
    assert expired[(1, 7)]["billed_usd"] == 2

    refreshed = module.fetch_account_billing_summaries(engine, [(1, 7)], refresh=True)
    assert refreshed[(1, 7)]["billed_usd"] == 3
    assert calls == [7, 7, 7]


def test_billing_cache_is_scoped_to_database_engine(monkeypatch):
    from services import codex_account_billing as module

    engines = [create_engine("sqlite://"), create_engine("sqlite://")]
    resolved = []

    def get_client(target_id, database_engine=None):
        resolved.append(database_engine)
        return SimpleNamespace(account_usage_all=lambda remote_id: {
            "total_account_billed": 10 if database_engine is engines[0] else 20,
        })

    monkeypatch.setattr(module, "get_target_client", get_client)

    assert module.fetch_account_billing_summaries(engines[0], [(1, 7)])[(1, 7)]["billed_usd"] == 10
    assert module.fetch_account_billing_summaries(engines[1], [(1, 7)])[(1, 7)]["billed_usd"] == 20
    assert resolved == engines


def test_billing_failure_is_isolated_and_cached_for_only_fifteen_seconds(monkeypatch):
    from services import codex_account_billing as module

    engine = create_engine("sqlite://")
    clock = [100.0]
    requested = []

    def get_client(target_id, database_engine=None):
        if target_id == 2:
            raise RuntimeError("secret-target-configuration")

        def usage(remote_id):
            requested.append(remote_id)
            if remote_id == 7 and requested.count(7) == 1:
                raise RuntimeError("secret-upstream-response")
            return {"total_account_billed": 0}

        return SimpleNamespace(account_usage_all=usage)

    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module, "get_target_client", get_client)
    keys = [(1, 7), (1, 8), (2, 9)]
    first = module.fetch_account_billing_summaries(engine, keys)

    for key in [(1, 7), (2, 9)]:
        assert first[key] == {
            "scope": "all", "billed_usd": None, "source": "codex2api",
            "status": "error", "fetched_at": first[key]["fetched_at"],
        }
    assert first[(1, 8)]["status"] == "available"
    assert first[(1, 8)]["billed_usd"] == 0

    clock[0] = 114.9
    assert module.fetch_account_billing_summaries(engine, keys) == first
    assert sorted(requested) == [7, 8]

    clock[0] = 115.0
    recovered = module.fetch_account_billing_summaries(engine, keys)
    assert recovered[(1, 7)]["status"] == "available"
    assert sorted(requested) == [7, 7, 8]


def test_failed_refresh_replaces_cached_total_with_unknown(monkeypatch):
    from services import codex_account_billing as module

    engine = create_engine("sqlite://")
    calls = []

    def usage(remote_id):
        calls.append(remote_id)
        if len(calls) > 1:
            raise RuntimeError("timeout")
        return {"total_account_billed": 10}

    monkeypatch.setattr(module, "get_target_client", lambda *args, **kwargs: SimpleNamespace(account_usage_all=usage))

    assert module.fetch_account_billing_summaries(engine, [(1, 7)])[(1, 7)]["billed_usd"] == 10
    failed = module.fetch_account_billing_summaries(engine, [(1, 7)], refresh=True)
    assert failed[(1, 7)]["billed_usd"] is None
    assert failed[(1, 7)]["status"] == "error"
    assert module.fetch_account_billing_summaries(engine, [(1, 7)]) == failed
    assert calls == [7, 7]


def test_billing_empty_page_does_not_resolve_any_target(monkeypatch):
    from services import codex_account_billing as module

    def unexpected_client(*args, **kwargs):
        raise AssertionError("empty page must not resolve a target")

    monkeypatch.setattr(module, "get_target_client", unexpected_client)
    assert module.fetch_account_billing_summaries(create_engine("sqlite://"), []) == {}


def test_billing_requests_have_at_most_four_concurrent_workers(monkeypatch):
    from services import codex_account_billing as module

    engine = create_engine("sqlite://")
    started_four = Event()
    release = Event()
    guard = Lock()
    active = 0
    maximum_active = 0
    calls = []

    def usage(remote_id):
        nonlocal active, maximum_active
        with guard:
            active += 1
            maximum_active = max(maximum_active, active)
            calls.append(remote_id)
            if active == 4:
                started_four.set()
        try:
            assert release.wait(timeout=3)
            return {"total_account_billed": remote_id}
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(module, "get_target_client", lambda *args, **kwargs: SimpleNamespace(account_usage_all=usage))
    with ThreadPoolExecutor(max_workers=2) as callers:
        first = callers.submit(module.fetch_account_billing_summaries, engine, [(1, i) for i in range(1, 7)])
        second = callers.submit(module.fetch_account_billing_summaries, engine, [(1, i) for i in range(7, 13)])
        try:
            assert started_four.wait(timeout=3)
        finally:
            release.set()
        results = {**first.result(timeout=3), **second.result(timeout=3)}

    assert maximum_active == 4
    assert sorted(calls) == list(range(1, 13))
    assert all(row["status"] == "available" for row in results.values())


def test_billing_deadline_returns_unknown_and_cancels_queued_requests(monkeypatch):
    from services import codex_account_billing as module

    engine = create_engine("sqlite://")
    release = Event()
    four_started = Event()
    guard = Lock()
    called = []

    class RecordingExecutor(ThreadPoolExecutor):
        def __init__(self):
            super().__init__(max_workers=4)
            self.submitted = []

        def submit(self, *args, **kwargs):
            future = super().submit(*args, **kwargs)
            self.submitted.append(future)
            return future

    def usage(remote_id):
        with guard:
            called.append(remote_id)
            if len(called) == 4:
                four_started.set()
        assert release.wait(timeout=2)
        return {"total_account_billed": remote_id}

    monkeypatch.setattr(module, "_FETCH_DEADLINE_SECONDS", 0.05, raising=False)
    monkeypatch.setattr(module, "get_target_client", lambda *args, **kwargs: SimpleNamespace(account_usage_all=usage))
    with RecordingExecutor() as executor, ThreadPoolExecutor(max_workers=1) as callers:
        monkeypatch.setattr(module, "_EXECUTOR", executor)
        response = callers.submit(module.fetch_account_billing_summaries, engine, [(1, i) for i in range(1, 13)])
        try:
            assert four_started.wait(timeout=0.5)
            result = response.result(timeout=0.5)
            assert len(result) == 12
            assert all(row["status"] == "error" and row["billed_usd"] is None for row in result.values())
            assert sum(future.cancelled() for future in executor.submitted) == 8
            assert module.fetch_account_billing_summaries(engine, [(1, 12)])[(1, 12)]["status"] == "error"
            assert len(executor.submitted) == 12
        finally:
            release.set()

    assert sorted(called) == [1, 2, 3, 4]


def test_billing_overlapping_refresh_reuses_inflight_and_late_result_populates_cache(monkeypatch):
    from services import codex_account_billing as module

    engine = create_engine("sqlite://")
    release = Event()
    started = Event()
    called = []

    def usage(remote_id):
        called.append(remote_id)
        started.set()
        assert release.wait(timeout=2)
        return {"total_account_billed": 42.125}

    monkeypatch.setattr(module, "_FETCH_DEADLINE_SECONDS", 0.05, raising=False)
    monkeypatch.setattr(module, "get_target_client", lambda *args, **kwargs: SimpleNamespace(account_usage_all=usage))
    with ThreadPoolExecutor(max_workers=4) as executor, ThreadPoolExecutor(max_workers=2) as callers:
        monkeypatch.setattr(module, "_EXECUTOR", executor)
        first = callers.submit(module.fetch_account_billing_summaries, engine, [(1, 7)])
        try:
            assert started.wait(timeout=0.5)
            refreshed = callers.submit(module.fetch_account_billing_summaries, engine, [(1, 7)], refresh=True)
            assert first.result(timeout=0.5)[(1, 7)]["status"] == "error"
            assert refreshed.result(timeout=0.5)[(1, 7)]["status"] == "error"
            assert called == [7]
            assert module.fetch_account_billing_summaries(engine, [(1, 7)])[(1, 7)]["billed_usd"] is None
        finally:
            release.set()

    # Executor shutdown joins both the request and its completion callback.
    late = module.fetch_account_billing_summaries(engine, [(1, 7)])
    assert late[(1, 7)]["status"] == "available"
    assert late[(1, 7)]["billed_usd"] == 42.125
    assert called == [7]


def test_billing_deadline_preserves_completed_accounts_and_isolates_slow_ones(monkeypatch):
    from services import codex_account_billing as module

    engine = create_engine("sqlite://")
    release = Event()

    def usage(remote_id):
        if remote_id == 8:
            assert release.wait(timeout=2)
        return {"total_account_billed": remote_id}

    monkeypatch.setattr(module, "_FETCH_DEADLINE_SECONDS", 0.05, raising=False)
    monkeypatch.setattr(module, "get_target_client", lambda *args, **kwargs: SimpleNamespace(account_usage_all=usage))
    with ThreadPoolExecutor(max_workers=4) as executor, ThreadPoolExecutor(max_workers=1) as callers:
        monkeypatch.setattr(module, "_EXECUTOR", executor)
        pending = callers.submit(module.fetch_account_billing_summaries, engine, [(1, 7), (1, 8)])
        try:
            result = pending.result(timeout=0.5)
            assert result[(1, 7)]["billed_usd"] == 7
            assert result[(1, 7)]["status"] == "available"
            assert result[(1, 8)]["billed_usd"] is None
            assert result[(1, 8)]["status"] == "error"
        finally:
            release.set()


def test_billing_repeated_refresh_eventually_fetches_accounts_cancelled_in_earlier_rounds(monkeypatch):
    from services import codex_account_billing as module

    engine = create_engine("sqlite://")
    requested = []
    clock = [100.0]
    keys = [(1, remote_id) for remote_id in range(1, 13)]
    monkeypatch.setattr(module, "_FETCH_DEADLINE_SECONDS", 0.05)
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])

    for _ in range(3):
        release = Event()
        four_started = Event()
        round_requests = []
        guard = Lock()

        def usage(remote_id):
            with guard:
                requested.append(remote_id)
                round_requests.append(remote_id)
                if len(round_requests) == 4:
                    four_started.set()
            assert release.wait(timeout=2)
            return {"total_account_billed": remote_id}

        monkeypatch.setattr(module, "get_target_client", lambda *args, **kwargs: SimpleNamespace(account_usage_all=usage))
        with ThreadPoolExecutor(max_workers=4) as executor, ThreadPoolExecutor(max_workers=1) as callers:
            monkeypatch.setattr(module, "_EXECUTOR", executor)
            response = callers.submit(module.fetch_account_billing_summaries, engine, keys, refresh=True)
            try:
                assert four_started.wait(timeout=0.5)
                result = response.result(timeout=0.5)
                assert len(result) == 12
                assert all(row["status"] == "error" for row in result.values())
            finally:
                # Join running callbacks before the next forced page refresh.
                release.set()
        # Cache expiry must retain the previous cancellation priority.
        clock[0] += 61

    assert sorted(requested) == list(range(1, 13))


def test_usage_details_reuse_each_fresh_key_while_fetching_missing_keys(monkeypatch):
    from services import codex_account_billing as module

    engine = create_engine("sqlite://")
    clock = [100.0]
    calls = []

    def usage(remote_id):
        calls.append(remote_id)
        if remote_id == 9:
            raise RuntimeError("private-upstream-error")
        return {"total_account_billed": remote_id, "today": {"date": "2026-09-07", "account_billed": 0, "requests": 0}}

    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module, "get_target_client", lambda *args, **kwargs: SimpleNamespace(account_usage_all=usage))
    first = module.fetch_account_usage_details(engine, [(1, 7)])
    clock[0] = 130.0
    second = module.fetch_account_usage_details(engine, [(1, 7), (1, 8), (1, 9)])
    assert second[(1, 7)] == first[(1, 7)]
    assert second[(1, 8)]["total_billed_usd"] == "8"
    assert second[(1, 9)] is None
    assert calls.count(7) == 1
    assert calls.count(8) == 1

    clock[0] = 159.9
    module.fetch_account_usage_details(engine, [(1, 7), (1, 8)])
    assert calls.count(7) == 1
    clock[0] = 160.0
    module.fetch_account_usage_details(engine, [(1, 7), (1, 8)])
    assert calls.count(7) == 2
    assert calls.count(8) == 1


def test_usage_details_manual_refresh_keeps_fresh_fallback_and_marks_failed_reads(monkeypatch):
    from services import codex_account_billing as module

    engine = create_engine("sqlite://")
    clock = [100.0]
    fail = [False]
    calls = []

    def usage(remote_id):
        calls.append(remote_id)
        if fail[0]:
            raise RuntimeError("private-response")
        return {"total_account_billed": 12.5}

    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module, "get_target_client", lambda *args, **kwargs: SimpleNamespace(account_usage_all=usage))
    original = module.fetch_account_usage_details(engine, [(1, 7)])[(1, 7)]
    fail[0] = True
    clock[0] = 120.0
    refreshed = module.fetch_account_usage_details(engine, [(1, 7), (1, 9)], refresh=True)
    assert refreshed[(1, 7)] == {**original, "refresh_error": True}
    assert refreshed[(1, 9)] is None
    assert calls == [7, 7, 9]
    # A failed forced read neither renews the original timestamp nor poisons it.
    cached = module.fetch_account_usage_details(engine, [(1, 7)])[(1, 7)]
    assert cached == original
    clock[0] = 160.0
    assert module.fetch_account_usage_details(engine, [(1, 7)])[(1, 7)] is None


def test_usage_details_sanitize_daily_account_bills_and_return_independent_snapshots(monkeypatch):
    import json
    from services import codex_account_billing as module

    engine = create_engine("sqlite://")
    payload = {
        "total_account_billed": "12.2500", "total_user_billed": 999999,
        "email": "private-email", "token": "private-token",
        "today": {"date": "2026-09-07", "account_billed": "1.2500", "user_billed": 9999, "requests": 3, "secret": "private-today"},
        "history": [
            {"date": "2026-09-06", "account_billed": 0, "user_billed": 88, "requests": 0, "secret": "private-history"},
            {"date": "2026-02-30", "account_billed": 10, "requests": 3},
            {"date": "2026-09-05", "account_billed": "NaN", "requests": True},
            {"date": "2026-09-04", "user_billed": 88, "requests": -1},
            "private-row",
        ],
    }
    monkeypatch.setattr(module, "get_target_client", lambda *args, **kwargs: SimpleNamespace(account_usage_all=lambda _: payload))
    result = module.fetch_account_usage_details(engine, [(1, 7)])[(1, 7)]
    assert result == {
        "total_billed_usd": "12.2500", "today_date": "2026-09-07", "today_billed_usd": "1.2500", "today_requests": 3,
        "fetched_at": result["fetched_at"],
        "history": [
            {"date": "2026-09-06", "account_billed": "0", "requests": 0},
            {"date": "2026-09-05", "account_billed": None, "requests": None},
            {"date": "2026-09-04", "account_billed": None, "requests": None},
        ],
    }
    assert "private" not in json.dumps(result)
    assert "user_billed" not in json.dumps(result)
    result["history"][0]["account_billed"] = "100000"
    cached = module.fetch_account_usage_details(engine, [(1, 7)])[(1, 7)]
    assert cached["history"][0]["account_billed"] == "0"


def test_usage_details_progressively_cover_accounts_across_multiple_deadlines(monkeypatch):
    from services import codex_account_billing as module

    engine = create_engine("sqlite://")
    clock = [100.0]
    keys = [(1, remote_id) for remote_id in range(1, 13)]
    requested = []
    monkeypatch.setattr(module, "_FETCH_DEADLINE_SECONDS", 0.05)
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])

    for round_index in range(3):
        release = Event()
        four_started = Event()
        round_requests = []
        guard = Lock()

        def usage(remote_id):
            with guard:
                requested.append(remote_id)
                round_requests.append(remote_id)
                if len(round_requests) == 4:
                    four_started.set()
            assert release.wait(timeout=2)
            return {"total_account_billed": remote_id, "today": {"date": "2026-09-07", "account_billed": 0, "requests": 0}}

        monkeypatch.setattr(module, "get_target_client", lambda *args, **kwargs: SimpleNamespace(account_usage_all=usage))
        with ThreadPoolExecutor(max_workers=4) as executor, ThreadPoolExecutor(max_workers=1) as callers:
            monkeypatch.setattr(module, "_EXECUTOR", executor)
            response = callers.submit(module.fetch_account_usage_details, engine, keys)
            try:
                assert four_started.wait(timeout=0.5)
                result = response.result(timeout=0.5)
                assert len(result) == 12
                assert sum(value is not None for value in result.values()) == round_index * 4
            finally:
                release.set()
        clock[0] += 15

    result = module.fetch_account_usage_details(engine, keys)
    assert all(value is not None for value in result.values())
    assert sorted(requested) == list(range(1, 13))
    assert all(value["today_date"] == "2026-09-07" for value in result.values())
