from __future__ import annotations

import pytest

from platforms.chatgpt.leadbee_runtime import (
    LeadBeeApiRateLimiter,
    LeadBeeCapacityCoordinator,
    LeadBeeCapacityExhausted,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        delay = float(seconds)
        self.sleeps.append(delay)
        self.now += delay


def _products():
    return {
        "items": [
            {
                "id": "prod",
                "status": "ACTIVE",
                "price": "1.30",
            }
        ]
    }


def _balance(available: str = "2.60"):
    return {"available_balance": available, "reserved": "0.00"}


def test_capacity_leases_are_idempotent_and_prevent_snapshot_oversell():
    coordinator = LeadBeeCapacityCoordinator()

    first = coordinator.reserve(
        client_order_id="aar_" + "1" * 32,
        product_id="prod",
        products=_products(),
        balance=_balance(),
    )
    duplicate = coordinator.reserve(
        client_order_id="aar_" + "1" * 32,
        product_id="prod",
        products=_products(),
        balance=_balance(),
    )
    second = coordinator.reserve(
        client_order_id="aar_" + "2" * 32,
        product_id="prod",
        products=_products(),
        balance=_balance(),
    )

    assert duplicate is first
    assert second is not first
    with pytest.raises(LeadBeeCapacityExhausted):
        coordinator.reserve(
            client_order_id="aar_" + "3" * 32,
            product_id="prod",
            products=_products(),
            balance=_balance(),
        )


def test_release_restores_capacity_and_commit_counts_until_refresh():
    coordinator = LeadBeeCapacityCoordinator()
    first = coordinator.reserve(
        client_order_id="aar_" + "1" * 32,
        product_id="prod",
        products=_products(),
        balance=_balance("1.30"),
    )

    first.release()
    second = coordinator.reserve(
        client_order_id="aar_" + "2" * 32,
        product_id="prod",
        products=_products(),
        balance=_balance("1.30"),
    )
    second.commit()

    with pytest.raises(LeadBeeCapacityExhausted):
        coordinator.reserve(
            client_order_id="aar_" + "3" * 32,
            product_id="prod",
            products=_products(),
            balance=_balance("1.30"),
        )

    coordinator.refresh(
        product_id="prod",
        products=_products(),
        balance=_balance("1.30"),
    )
    third = coordinator.reserve(
        client_order_id="aar_" + "3" * 32,
        product_id="prod",
        products=_products(),
        balance=_balance("1.30"),
    )
    assert third.client_order_id.endswith("3" * 32)


def test_quarantine_remains_counted_until_bounded_ttl_expires():
    clock = FakeClock()
    coordinator = LeadBeeCapacityCoordinator(
        monotonic=clock.monotonic,
        quarantine_ttl_seconds=30,
    )
    lease = coordinator.reserve(
        client_order_id="aar_" + "1" * 32,
        product_id="prod",
        products=_products(),
        balance=_balance("1.30"),
    )
    lease.quarantine()

    clock.now = 29.9
    with pytest.raises(LeadBeeCapacityExhausted):
        coordinator.reserve(
            client_order_id="aar_" + "2" * 32,
            product_id="prod",
            products=_products(),
            balance=_balance("1.30"),
        )

    clock.now = 30.0
    replacement = coordinator.reserve(
        client_order_id="aar_" + "2" * 32,
        product_id="prod",
        products=_products(),
        balance=_balance("1.30"),
    )
    assert replacement.client_order_id.endswith("2" * 32)


def test_reserve_from_client_coalesces_cached_capacity_reads():
    clock = FakeClock()

    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, float | None]] = []

        def get_products(self, *, request_timeout=None):
            self.calls.append(("products", request_timeout))
            return _products()

        def get_balance(self, *, request_timeout=None):
            self.calls.append(("balance", request_timeout))
            return _balance()

    client = Client()
    coordinator = LeadBeeCapacityCoordinator(
        monotonic=clock.monotonic,
        products_cache_seconds=60,
        balance_cache_seconds=2,
    )

    for digit in ("1", "2"):
        coordinator.reserve_from_client(
            client=client,
            product_id="prod",
            client_order_id="aar_" + digit * 32,
            deadline=30,
            checkpoint=lambda: None,
        )

    with pytest.raises(LeadBeeCapacityExhausted):
        coordinator.reserve_from_client(
            client=client,
            product_id="prod",
            client_order_id="aar_" + "3" * 32,
            deadline=30,
            checkpoint=lambda: None,
        )

    assert [name for name, _timeout in client.calls] == ["products", "balance"]
    assert all(timeout == pytest.approx(20.0) for _name, timeout in client.calls)


def test_rate_limiter_waits_for_create_and_total_windows():
    clock = FakeClock()
    limiter = LeadBeeApiRateLimiter(
        create_limit=2,
        request_limit=3,
        window_seconds=60,
        monotonic=clock.monotonic,
        sleep_fn=clock.sleep,
        checkpoint_interval_seconds=60,
    )
    checkpoints: list[float] = []

    def checkpoint() -> None:
        checkpoints.append(clock.now)

    limiter.wait(create=True, deadline=120, checkpoint=checkpoint)
    limiter.wait(create=True, deadline=120, checkpoint=checkpoint)
    limiter.wait(create=False, deadline=120, checkpoint=checkpoint)
    limiter.wait(create=True, deadline=120, checkpoint=checkpoint)

    assert clock.now == pytest.approx(60.0)
    assert clock.sleeps == [pytest.approx(60.0)]
    assert checkpoints


def test_rate_limiter_honors_deadline_and_cancellation_before_recording():
    clock = FakeClock()
    limiter = LeadBeeApiRateLimiter(
        create_limit=1,
        request_limit=1,
        window_seconds=60,
        monotonic=clock.monotonic,
        sleep_fn=clock.sleep,
    )
    limiter.wait(create=True, deadline=10, checkpoint=lambda: None)

    with pytest.raises(TimeoutError, match="期限"):
        limiter.wait(create=False, deadline=5, checkpoint=lambda: None)
    assert clock.now == pytest.approx(5.0)

    def cancelled() -> None:
        raise RuntimeError("fixture cancelled")

    with pytest.raises(RuntimeError, match="fixture cancelled"):
        limiter.wait(create=False, deadline=120, checkpoint=cancelled)
