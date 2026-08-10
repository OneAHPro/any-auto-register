"""注册任务运行时控制与状态存储。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
import threading
import time
from typing import Any


class TaskInterruption(RuntimeError):
    """任务执行过程中触发的协作式中断。"""


class StopTaskRequested(TaskInterruption):
    """整个任务被手动停止。"""

    def __init__(self, message: str = "任务已手动停止"):
        super().__init__(message)


class SkipCurrentAttemptRequested(TaskInterruption):
    """当前账号被手动跳过。"""

    def __init__(self, message: str = "已手动跳过当前账号"):
        super().__init__(message)


class AttemptOutcome(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    REMOVED = "removed"
    STOPPED = "stopped"


@dataclass(slots=True)
class AttemptResult:
    outcome: AttemptOutcome
    message: str = ""

    @classmethod
    def success(cls) -> "AttemptResult":
        return cls(AttemptOutcome.SUCCESS)

    @classmethod
    def failed(cls, message: str) -> "AttemptResult":
        return cls(AttemptOutcome.FAILED, message)

    @classmethod
    def skipped(cls, message: str) -> "AttemptResult":
        return cls(AttemptOutcome.SKIPPED, message)

    @classmethod
    def removed(cls, message: str) -> "AttemptResult":
        return cls(AttemptOutcome.REMOVED, message)

    @classmethod
    def stopped(cls, message: str) -> "AttemptResult":
        return cls(AttemptOutcome.STOPPED, message)


class RegisterTaskControl:
    """协作式任务控制器：支持停止整个任务、跳过一个当前账号。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._stop_requested = False
        self._pending_skip_requests = 0
        self._next_attempt_id = 1
        self._active_attempt_ids: set[int] = set()
        self._skip_active_attempt_ids: set[int] = set()
        self._active_slot_semaphore: threading.BoundedSemaphore | None = None
        self._active_slot_limit = 0
        self._slot_holding_attempt_ids: set[int] = set()
        self._next_interrupt_id = 1
        self._attempt_interrupts: dict[int, dict[int, Callable[[], None]]] = {}

    @staticmethod
    def _invoke_interrupts(callbacks: list[Callable[[], None]]) -> None:
        for callback in callbacks:
            try:
                callback()
            except Exception:
                # A broken resource callback must not block the sticky task
                # control flag or other active attempts from being interrupted.
                pass

    def _pop_attempt_interrupts_locked(
        self,
        attempt_ids: set[int],
    ) -> list[Callable[[], None]]:
        callbacks: list[Callable[[], None]] = []
        for attempt_id in attempt_ids:
            callbacks.extend(self._attempt_interrupts.pop(attempt_id, {}).values())
        return callbacks

    def register_attempt_interrupt(
        self,
        attempt_id: int,
        callback: Callable[[], None],
    ) -> Callable[[], None]:
        """Register one idempotent resource interrupt for an active attempt.

        Callbacks must be fast and must not re-enter ``RegisterTaskStore``: a
        caller may request task control while holding its own coordination lock.
        """
        normalized_attempt_id = int(attempt_id)
        invoke_now = False
        interrupt_id = 0
        with self._lock:
            if normalized_attempt_id not in self._active_attempt_ids:
                raise RuntimeError("只能为活跃账号登记中断资源")
            if (
                self._stop_requested
                or normalized_attempt_id in self._skip_active_attempt_ids
            ):
                invoke_now = True
            else:
                interrupt_id = self._next_interrupt_id
                self._next_interrupt_id += 1
                self._attempt_interrupts.setdefault(
                    normalized_attempt_id,
                    {},
                )[interrupt_id] = callback

        if invoke_now:
            self._invoke_interrupts([callback])

        def unregister() -> None:
            if not interrupt_id:
                return
            with self._lock:
                callbacks = self._attempt_interrupts.get(normalized_attempt_id)
                if callbacks is None:
                    return
                callbacks.pop(interrupt_id, None)
                if not callbacks:
                    self._attempt_interrupts.pop(normalized_attempt_id, None)

        return unregister

    def configure_active_slots(self, limit: int) -> None:
        """Limit foreground attempts while allowing mailbox waits to yield."""
        resolved_limit = max(int(limit or 0), 1)
        with self._lock:
            if self._active_attempt_ids:
                raise RuntimeError("活跃账号并发槽必须在任务开始前配置")
            self._active_slot_limit = resolved_limit
            self._active_slot_semaphore = threading.BoundedSemaphore(
                resolved_limit
            )
            self._slot_holding_attempt_ids.clear()

    def _acquire_new_attempt_slot(self) -> bool:
        with self._lock:
            semaphore = self._active_slot_semaphore
        if semaphore is None:
            return False

        while True:
            self.checkpoint(consume_skip=False)
            if semaphore.acquire(timeout=0.1):
                return True

    def _resume_active_slot(self, attempt_id: int) -> None:
        with self._lock:
            semaphore = self._active_slot_semaphore
        if semaphore is None:
            return

        while True:
            self.checkpoint(attempt_id=attempt_id)
            if semaphore.acquire(timeout=0.1):
                break

        try:
            self.checkpoint(attempt_id=attempt_id)
            with self._lock:
                if attempt_id not in self._active_attempt_ids:
                    return
                self._slot_holding_attempt_ids.add(attempt_id)
                semaphore = None
        finally:
            if semaphore is not None:
                semaphore.release()

    @contextmanager
    def pause_active_slot(self, attempt_id: int | None):
        """Temporarily yield one foreground slot during a mailbox wait."""
        semaphore = None
        if attempt_id is not None:
            with self._lock:
                if attempt_id in self._slot_holding_attempt_ids:
                    self._slot_holding_attempt_ids.remove(attempt_id)
                    semaphore = self._active_slot_semaphore
        if semaphore is None:
            yield False
            return

        semaphore.release()
        interrupted = False
        try:
            yield True
        except TaskInterruption:
            interrupted = True
            raise
        finally:
            if not interrupted:
                self._resume_active_slot(attempt_id)

    def request_stop(self) -> None:
        self.request_stop_once()

    def request_stop_once(self) -> bool:
        """Set the sticky stop flag and report whether this call changed it."""
        callbacks: list[Callable[[], None]] = []
        with self._lock:
            first_request = not self._stop_requested
            self._stop_requested = True
            if first_request:
                callbacks = self._pop_attempt_interrupts_locked(
                    set(self._active_attempt_ids)
                )
        self._invoke_interrupts(callbacks)
        return first_request

    def request_skip_current(self) -> None:
        callbacks: list[Callable[[], None]] = []
        with self._lock:
            if self._active_attempt_ids:
                self._skip_active_attempt_ids.update(self._active_attempt_ids)
                callbacks = self._pop_attempt_interrupts_locked(
                    set(self._active_attempt_ids)
                )
            else:
                self._pending_skip_requests += 1
        self._invoke_interrupts(callbacks)

    def start_attempt(self) -> int:
        acquired_slot = self._acquire_new_attempt_slot()
        try:
            with self._lock:
                if self._stop_requested:
                    raise StopTaskRequested()
                attempt_id = self._next_attempt_id
                self._next_attempt_id += 1
                self._active_attempt_ids.add(attempt_id)
                if acquired_slot:
                    self._slot_holding_attempt_ids.add(attempt_id)
                return attempt_id
        except Exception:
            if acquired_slot:
                with self._lock:
                    semaphore = self._active_slot_semaphore
                if semaphore is not None:
                    semaphore.release()
            raise

    def finish_attempt(self, attempt_id: int | None) -> None:
        if attempt_id is None:
            return
        semaphore = None
        with self._lock:
            self._active_attempt_ids.discard(attempt_id)
            self._skip_active_attempt_ids.discard(attempt_id)
            self._attempt_interrupts.pop(attempt_id, None)
            if attempt_id in self._slot_holding_attempt_ids:
                self._slot_holding_attempt_ids.remove(attempt_id)
                semaphore = self._active_slot_semaphore
        if semaphore is not None:
            semaphore.release()

    def checkpoint(
        self,
        *,
        consume_skip: bool = True,
        attempt_id: int | None = None,
    ) -> None:
        with self._lock:
            if self._stop_requested:
                raise StopTaskRequested()
            if consume_skip:
                if (
                    attempt_id is not None
                    and attempt_id in self._skip_active_attempt_ids
                ):
                    self._skip_active_attempt_ids.discard(attempt_id)
                    raise SkipCurrentAttemptRequested()
                if self._pending_skip_requests > 0:
                    self._pending_skip_requests -= 1
                    raise SkipCurrentAttemptRequested()

    def is_stop_requested(self) -> bool:
        with self._lock:
            return self._stop_requested

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "stop_requested": self._stop_requested,
                "pending_skip_requests": self._pending_skip_requests,
                "active_attempts": len(self._active_attempt_ids),
                "targeted_skip_attempts": len(self._skip_active_attempt_ids),
                "active_slot_limit": self._active_slot_limit,
                "active_slots_in_use": len(self._slot_holding_attempt_ids),
            }


@dataclass(frozen=True, slots=True)
class TaskAttemptContext:
    control: RegisterTaskControl
    attempt_id: int


_task_attempt_local = threading.local()


def current_task_attempt_context() -> TaskAttemptContext | None:
    return getattr(_task_attempt_local, "current", None)


@contextmanager
def bind_task_attempt_context(
    control: RegisterTaskControl,
    attempt_id: int,
):
    """Expose one active attempt to deeply nested blocking resources."""
    previous = current_task_attempt_context()
    _task_attempt_local.current = TaskAttemptContext(
        control=control,
        attempt_id=int(attempt_id),
    )
    try:
        yield _task_attempt_local.current
    finally:
        if previous is None:
            try:
                del _task_attempt_local.current
            except AttributeError:
                pass
        else:
            _task_attempt_local.current = previous


def checkpoint_current_task_attempt() -> None:
    context = current_task_attempt_context()
    if context is not None:
        context.control.checkpoint(attempt_id=context.attempt_id)


@dataclass
class RegisterTaskRecord:
    id: str
    platform: str
    source: str
    total: int
    meta: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    progress: str = "0/0"
    logs: list[str] = field(default_factory=list)
    success: int = 0
    registered: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    cashier_urls: list[str] = field(default_factory=list)
    error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    control: RegisterTaskControl = field(
        default_factory=RegisterTaskControl,
        repr=False,
    )

    def to_dict(self) -> dict[str, Any]:
        data = {
            "id": self.id,
            "status": self.status,
            "platform": self.platform,
            "source": self.source,
            "meta": dict(self.meta),
            "total": self.total,
            "progress": self.progress,
            "logs": list(self.logs),
            "success": self.success,
            "registered": self.registered,
            "skipped": self.skipped,
            "errors": list(self.errors),
            "control": self.control.snapshot(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.cashier_urls:
            data["cashier_urls"] = list(self.cashier_urls)
        if self.error:
            data["error"] = self.error
        return data


class RegisterTaskStore:
    """线程安全的注册任务存储。"""

    def __init__(
        self,
        *,
        max_finished_tasks: int = 200,
        cleanup_threshold: int = 250,
    ):
        self._lock = threading.Lock()
        self._records: dict[str, RegisterTaskRecord] = {}
        self._cleanup_protected_task_ids: set[str] = set()
        self.max_finished_tasks = max_finished_tasks
        self.cleanup_threshold = cleanup_threshold

    def create(
        self,
        task_id: str,
        *,
        platform: str,
        total: int,
        source: str,
        meta: dict[str, Any] | None = None,
    ) -> RegisterTaskRecord:
        with self._lock:
            record = RegisterTaskRecord(
                id=task_id,
                platform=platform,
                total=total,
                source=source,
                meta=dict(meta or {}),
                progress=f"0/{total}",
            )
            self._records[task_id] = record
            return record

    def exists(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._records

    def has_active(
        self,
        *,
        platform: str | None = None,
        source: str | None = None,
    ) -> bool:
        with self._lock:
            for record in self._records.values():
                if record.status not in ("pending", "running"):
                    continue
                if platform and record.platform != platform:
                    continue
                if source and record.source != source:
                    continue
                return True
        return False

    def control_for(self, task_id: str) -> RegisterTaskControl:
        with self._lock:
            return self._records[task_id].control

    def request_stop(self, task_id: str) -> dict[str, Any]:
        control = self.control_for(task_id)
        control.request_stop()
        return control.snapshot()

    def request_stop_once(self, task_id: str) -> tuple[bool, dict[str, Any]]:
        control = self.control_for(task_id)
        first_request = control.request_stop_once()
        return first_request, control.snapshot()

    def request_stop_if_active(
        self,
        task_id: str,
    ) -> tuple[str, bool, dict[str, Any]]:
        """Atomically validate active state and set the sticky stop flag."""
        with self._lock:
            record = self._records.get(task_id)
            if record is None:
                return "missing", False, {}
            if record.status in ("done", "failed", "stopped"):
                return "terminal", False, record.control.snapshot()
            first_request = record.control.request_stop_once()
            record.updated_at = time.time()
            return "active", first_request, record.control.snapshot()

    def request_skip_current(self, task_id: str) -> dict[str, Any]:
        control = self.control_for(task_id)
        control.request_skip_current()
        return control.snapshot()

    def append_log(self, task_id: str, entry: str) -> None:
        with self._lock:
            record = self._records.get(task_id)
            if record is None:
                return
            record.logs.append(entry)
            record.updated_at = time.time()

    def mark_running(self, task_id: str) -> None:
        with self._lock:
            record = self._records[task_id]
            record.status = "running"
            record.updated_at = time.time()

    def set_progress(self, task_id: str, progress: str) -> None:
        with self._lock:
            record = self._records[task_id]
            record.progress = progress
            record.updated_at = time.time()

    def add_cashier_url(self, task_id: str, cashier_url: str) -> None:
        with self._lock:
            record = self._records[task_id]
            record.cashier_urls.append(cashier_url)
            record.updated_at = time.time()

    def update_counters(
        self,
        task_id: str,
        *,
        success: int | None = None,
        registered: int | None = None,
    ) -> None:
        with self._lock:
            record = self._records[task_id]
            if success is not None:
                record.success = max(0, int(success))
            if registered is not None:
                record.registered = max(0, int(registered))
            record.updated_at = time.time()

    def update_meta(self, task_id: str, **values: Any) -> None:
        with self._lock:
            record = self._records[task_id]
            record.meta.update(values)
            record.updated_at = time.time()

    def finish(
        self,
        task_id: str,
        *,
        status: str,
        success: int,
        registered: int | None = None,
        skipped: int,
        errors: list[str],
        error: str = "",
    ) -> str:
        with self._lock:
            record = self._records[task_id]
            completed_at = time.time()
            resolved_status = str(status or "")
            if (
                resolved_status == "done"
                and record.control.is_stop_requested()
            ):
                resolved_status = "stopped"
            record.status = resolved_status
            record.success = success
            if registered is None:
                record.registered = max(success + skipped + len(errors), 0)
            else:
                record.registered = max(0, int(registered))
            record.skipped = skipped
            record.errors = list(errors)
            record.error = error
            # This remains stable while optional alerting and cleanup append
            # logs after the business task has already reached a terminal state.
            record.meta.setdefault("completed_at", completed_at)
            record.updated_at = completed_at
            return resolved_status

    def snapshot(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            return self._records[task_id].to_dict()

    def snapshot_if_present(self, task_id: str) -> dict[str, Any] | None:
        """Return a snapshot without an exists/snapshot TOCTOU window."""
        with self._lock:
            record = self._records.get(task_id)
            return record.to_dict() if record is not None else None

    def log_snapshot_if_present(
        self,
        task_id: str,
    ) -> tuple[list[str], str, dict[str, Any]] | None:
        """Copy log state and counters from the same locked record version."""
        with self._lock:
            record = self._records.get(task_id)
            if record is None:
                return None
            snapshot = record.to_dict()
            return list(snapshot["logs"]), str(snapshot["status"]), snapshot

    def list_snapshots(self) -> list[dict[str, Any]]:
        with self._lock:
            return [record.to_dict() for record in self._records.values()]

    def protect_from_cleanup(self, task_id: str) -> None:
        """Keep a task record alive while its runner still needs post-processing."""
        with self._lock:
            if task_id not in self._records:
                raise KeyError(task_id)
            self._cleanup_protected_task_ids.add(task_id)

    def release_cleanup_protection(self, task_id: str) -> None:
        with self._lock:
            self._cleanup_protected_task_ids.discard(task_id)

    def log_state(self, task_id: str) -> tuple[list[str], str]:
        with self._lock:
            record = self._records[task_id]
            return list(record.logs), record.status

    def cleanup(self) -> None:
        with self._lock:
            if len(self._records) <= self.cleanup_threshold:
                return
            finished = [
                (task_id, record)
                for task_id, record in self._records.items()
                if record.status in ("done", "failed", "stopped")
            ]
            if len(finished) <= self.max_finished_tasks:
                return
            removable = [
                item
                for item in finished
                if item[0] not in self._cleanup_protected_task_ids
            ]
            removable.sort(key=lambda item: item[1].created_at)
            excess = len(finished) - self.max_finished_tasks
            to_remove = removable[:excess]
            for task_id, _ in to_remove:
                self._records.pop(task_id, None)


__all__ = [
    "AttemptOutcome",
    "AttemptResult",
    "RegisterTaskControl",
    "RegisterTaskRecord",
    "RegisterTaskStore",
    "SkipCurrentAttemptRequested",
    "StopTaskRequested",
    "TaskInterruption",
]
