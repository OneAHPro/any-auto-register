"""Process-local LeadBee capacity leases and request-rate coordination."""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

from .leadbee_capacity import LeadBeeCapacitySnapshot, parse_leadbee_capacity


class LeadBeeCapacityExhausted(RuntimeError):
    """Raised before order creation when the current API capacity is exhausted."""


@dataclass
class _LeaseRecord:
    product_id: str
    state: str
    generation: int
    expires_at: float | None = None


class LeadBeeCapacityLease:
    def __init__(
        self,
        coordinator: "LeadBeeCapacityCoordinator",
        client_order_id: str,
    ) -> None:
        self._coordinator = coordinator
        self.client_order_id = client_order_id

    def commit(self) -> None:
        self._coordinator._transition(self.client_order_id, "committed")

    def release(self) -> None:
        self._coordinator._release(self.client_order_id)

    def quarantine(self) -> None:
        self._coordinator._transition(self.client_order_id, "quarantined")


class LeadBeeCapacityCoordinator:
    """Avoid overselling one balance snapshot across concurrent workers."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] | None = None,
        quarantine_ttl_seconds: float = 600.0,
        products_cache_seconds: float = 60.0,
        balance_cache_seconds: float = 2.0,
        rate_limiter: "LeadBeeApiRateLimiter | None" = None,
    ) -> None:
        self._monotonic = monotonic or time.monotonic
        self._quarantine_ttl_seconds = _positive_finite(
            quarantine_ttl_seconds,
            default=600.0,
        )
        self._products_cache_seconds = _nonnegative_finite(
            products_cache_seconds,
            default=60.0,
        )
        self._balance_cache_seconds = _nonnegative_finite(
            balance_cache_seconds,
            default=2.0,
        )
        self._rate_limiter = rate_limiter
        self._lock = threading.RLock()
        self._fetch_lock = threading.Lock()
        self._generation = 0
        self._snapshot: LeadBeeCapacitySnapshot | None = None
        self._snapshot_product_id = ""
        self._snapshot_fingerprint: tuple[object, ...] | None = None
        self._records: dict[str, _LeaseRecord] = {}
        self._handles: dict[str, LeadBeeCapacityLease] = {}
        self._products_payload: object | None = None
        self._products_cached_at = float("-inf")
        self._balance_payload: object | None = None
        self._balance_cached_at = float("-inf")

    def reserve(
        self,
        *,
        client_order_id: str,
        product_id: str,
        products: object,
        balance: object,
    ) -> LeadBeeCapacityLease:
        reference = str(client_order_id or "").strip()
        normalized_product_id = str(product_id or "").strip()
        if not reference or not normalized_product_id:
            raise ValueError("LeadBee API 容量预留参数无效")
        snapshot = parse_leadbee_capacity(
            products,
            balance,
            product_id=normalized_product_id,
        )
        with self._lock:
            self._apply_snapshot_locked(
                snapshot,
                product_id=normalized_product_id,
                force=False,
            )
            return self._reserve_locked(reference, normalized_product_id)

    def refresh(
        self,
        *,
        product_id: str,
        products: object,
        balance: object,
    ) -> LeadBeeCapacitySnapshot:
        normalized_product_id = str(product_id or "").strip()
        if not normalized_product_id:
            raise ValueError("LeadBee API 产品标识无效")
        snapshot = parse_leadbee_capacity(
            products,
            balance,
            product_id=normalized_product_id,
        )
        with self._lock:
            self._apply_snapshot_locked(
                snapshot,
                product_id=normalized_product_id,
                force=True,
            )
        return snapshot

    def reserve_from_client(
        self,
        *,
        client,
        product_id: str,
        client_order_id: str,
        deadline: float,
        checkpoint: Callable[[], None],
    ) -> LeadBeeCapacityLease:
        normalized_product_id = str(product_id or "").strip()
        with self._fetch_lock:
            checkpoint()
            now = self._monotonic()
            products = self._products_payload
            if (
                products is None
                or now - self._products_cached_at >= self._products_cache_seconds
            ):
                self._wait_for_request(deadline=deadline, checkpoint=checkpoint)
                products = client.get_products(
                    request_timeout=self._remaining_timeout(deadline)
                )
                self._checkpoint_deadline(deadline, checkpoint)
                self._products_payload = products
                self._products_cached_at = self._monotonic()

            now = self._monotonic()
            balance = self._balance_payload
            refreshed_balance = False
            if (
                balance is None
                or now - self._balance_cached_at >= self._balance_cache_seconds
            ):
                self._wait_for_request(deadline=deadline, checkpoint=checkpoint)
                balance = client.get_balance(
                    request_timeout=self._remaining_timeout(deadline)
                )
                self._checkpoint_deadline(deadline, checkpoint)
                self._balance_payload = balance
                self._balance_cached_at = self._monotonic()
                refreshed_balance = True

            snapshot = parse_leadbee_capacity(
                products,
                balance,
                product_id=normalized_product_id,
            )
            with self._lock:
                self._apply_snapshot_locked(
                    snapshot,
                    product_id=normalized_product_id,
                    force=refreshed_balance,
                )
                return self._reserve_locked(
                    str(client_order_id or "").strip(),
                    normalized_product_id,
                )

    def _wait_for_request(
        self,
        *,
        deadline: float,
        checkpoint: Callable[[], None],
    ) -> None:
        if self._rate_limiter is not None:
            self._rate_limiter.wait(
                create=False,
                deadline=deadline,
                checkpoint=checkpoint,
            )

    def _checkpoint_deadline(
        self,
        deadline: float,
        checkpoint: Callable[[], None],
    ) -> None:
        checkpoint()
        if self._monotonic() >= float(deadline):
            raise TimeoutError("LeadBee API 容量检查超过本地期限")

    def _remaining_timeout(self, deadline: float) -> float:
        remaining = float(deadline) - self._monotonic()
        if remaining <= 0:
            raise TimeoutError("LeadBee API 容量检查超过本地期限")
        return min(20.0, remaining)

    @staticmethod
    def _fingerprint(snapshot: LeadBeeCapacitySnapshot) -> tuple[object, ...]:
        return (
            snapshot.configured_product_available,
            snapshot.balance_available,
            snapshot.balance_reserved,
            snapshot.unit_price,
            snapshot.currency,
        )

    def _apply_snapshot_locked(
        self,
        snapshot: LeadBeeCapacitySnapshot,
        *,
        product_id: str,
        force: bool,
    ) -> None:
        fingerprint = self._fingerprint(snapshot)
        changed = (
            self._snapshot is None
            or self._snapshot_product_id != product_id
            or self._snapshot_fingerprint != fingerprint
        )
        if not force and not changed:
            self._purge_expired_locked()
            return
        self._generation += 1
        self._snapshot = snapshot
        self._snapshot_product_id = product_id
        self._snapshot_fingerprint = fingerprint
        for reference, record in tuple(self._records.items()):
            if record.state == "committed":
                self._records.pop(reference, None)
                self._handles.pop(reference, None)
        self._purge_expired_locked()

    def _reserve_locked(
        self,
        reference: str,
        product_id: str,
    ) -> LeadBeeCapacityLease:
        if not reference:
            raise ValueError("LeadBee API 客户端订单标识无效")
        self._purge_expired_locked()
        existing = self._handles.get(reference)
        if existing is not None:
            return existing
        snapshot = self._snapshot
        capacity = snapshot.estimated_order_capacity if snapshot is not None else None
        if (
            snapshot is None
            or self._snapshot_product_id != product_id
            or not snapshot.configured_product_available
            or capacity is None
        ):
            raise LeadBeeCapacityExhausted("LeadBee API 当前产品容量不可用")
        active = sum(
            1 for record in self._records.values() if record.product_id == product_id
        )
        if active >= capacity:
            raise LeadBeeCapacityExhausted("LeadBee API 可用余额不足")
        self._records[reference] = _LeaseRecord(
            product_id=product_id,
            state="pending",
            generation=self._generation,
        )
        lease = LeadBeeCapacityLease(self, reference)
        self._handles[reference] = lease
        return lease

    def _transition(self, reference: str, state: str) -> None:
        with self._lock:
            record = self._records.get(reference)
            if record is None:
                return
            if state == "quarantined":
                record.state = state
                record.expires_at = self._monotonic() + self._quarantine_ttl_seconds
            elif state == "committed":
                record.state = state
                record.expires_at = None

    def _release(self, reference: str) -> None:
        with self._lock:
            self._records.pop(reference, None)
            self._handles.pop(reference, None)

    def _purge_expired_locked(self) -> None:
        now = self._monotonic()
        for reference, record in tuple(self._records.items()):
            if (
                record.state == "quarantined"
                and record.expires_at is not None
                and now >= record.expires_at
            ):
                self._records.pop(reference, None)
                self._handles.pop(reference, None)


class LeadBeeApiRateLimiter:
    """Thread-safe sliding-window limiter for create and total API requests."""

    def __init__(
        self,
        *,
        create_limit: int = 60,
        request_limit: int = 900,
        window_seconds: float = 60.0,
        monotonic: Callable[[], float] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        checkpoint_interval_seconds: float = 0.25,
    ) -> None:
        self.create_limit = _positive_int(create_limit, "create_limit")
        self.request_limit = _positive_int(request_limit, "request_limit")
        self.window_seconds = _positive_finite(window_seconds)
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep_fn or time.sleep
        self._checkpoint_interval_seconds = _positive_finite(
            checkpoint_interval_seconds,
            default=0.25,
        )
        self._lock = threading.Lock()
        self._creates: deque[float] = deque()
        self._requests: deque[float] = deque()

    def wait(
        self,
        *,
        create: bool,
        deadline: float,
        checkpoint: Callable[[], None],
    ) -> None:
        while True:
            checkpoint()
            now = self._monotonic()
            if now >= float(deadline):
                raise TimeoutError("LeadBee API 请求限流等待超过本地期限")
            with self._lock:
                self._purge_locked(now)
                create_blocked = bool(
                    create and len(self._creates) >= self.create_limit
                )
                request_blocked = len(self._requests) >= self.request_limit
                if not create_blocked and not request_blocked:
                    self._requests.append(now)
                    if create:
                        self._creates.append(now)
                    return
                wake_times: list[float] = []
                if create_blocked:
                    wake_times.append(self._creates[0] + self.window_seconds)
                if request_blocked:
                    wake_times.append(self._requests[0] + self.window_seconds)
                wake_at = max(wake_times)
            remaining = float(deadline) - now
            wait_seconds = min(
                max(0.0, wake_at - now),
                remaining,
                self._checkpoint_interval_seconds,
            )
            if wait_seconds <= 0:
                continue
            self._sleep(wait_seconds)

    def _purge_locked(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._creates and self._creates[0] <= cutoff:
            self._creates.popleft()
        while self._requests and self._requests[0] <= cutoff:
            self._requests.popleft()


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a positive integer") from None
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def _positive_finite(value: object, *, default: float | None = None) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        if default is not None:
            return default
        raise ValueError("value must be a positive finite number") from None
    if isinstance(value, bool) or not math.isfinite(parsed) or parsed <= 0:
        if default is not None:
            return default
        raise ValueError("value must be a positive finite number")
    return parsed


def _nonnegative_finite(value: object, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if isinstance(value, bool) or not math.isfinite(parsed) or parsed < 0:
        return default
    return parsed


leadbee_api_rate_limiter = LeadBeeApiRateLimiter(
    create_limit=60,
    request_limit=900,
    window_seconds=60,
)
leadbee_capacity_coordinator = LeadBeeCapacityCoordinator(
    rate_limiter=leadbee_api_rate_limiter,
)
