"""Single-process coordination for foreground and automatic ChatGPT tasks."""

from collections.abc import Callable, Mapping
from types import MappingProxyType
import threading


Callback = Callable[[], None]
Predicate = Callable[[], bool]


class ChatGPTTaskGate:
    """Give foreground ChatGPT work priority over one automatic task."""

    _WAIT_POLL_SECONDS = 0.1

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._automation_lease: object | None = None
        self._automation_stop_callback: Callback | None = None
        self._automation_stop_requested = False
        self._foreground_leases: set[object] = set()
        self._foreground_waiters = 0

    def try_enter_automation(
        self,
        stop_callback: Callback | None = None,
    ) -> object | None:
        """Enter immediately only while no other ChatGPT work is present."""
        with self._condition:
            if (
                self._automation_lease is not None
                or self._foreground_leases
                or self._foreground_waiters
            ):
                return None
            lease = object()
            self._automation_lease = lease
            self._automation_stop_callback = stop_callback
            self._automation_stop_requested = False
            return lease

    def leave_automation(self, lease: object) -> None:
        with self._condition:
            if lease is not self._automation_lease:
                raise RuntimeError("automation lease is not active")
            self._automation_lease = None
            self._automation_stop_callback = None
            self._automation_stop_requested = False
            self._condition.notify_all()

    def enter_foreground(
        self,
        on_wait: Callback | None = None,
        cancelled: Predicate | None = None,
    ) -> object | None:
        """Wait for automation to leave, then join the foreground cohort."""
        stop_callback: Callback | None = None
        provisional_lease: object | None = None
        waiter_registered = False
        with self._condition:
            if self._automation_lease is None:
                provisional_lease = object()
                self._foreground_leases.add(provisional_lease)
            else:
                self._foreground_waiters += 1
                waiter_registered = True
                if not self._automation_stop_requested:
                    self._automation_stop_requested = True
                    stop_callback = self._automation_stop_callback

        if provisional_lease is not None:
            if self._is_cancelled(cancelled):
                self.leave_foreground(provisional_lease)
                return None
            return provisional_lease

        try:
            self._call_safely(on_wait)
            self._call_safely(stop_callback)
            while True:
                if self._is_cancelled(cancelled):
                    return None

                with self._condition:
                    if self._automation_lease is not None:
                        self._condition.wait(timeout=self._WAIT_POLL_SECONDS)
                        continue

                    provisional_lease = object()
                    self._foreground_leases.add(provisional_lease)
                    if waiter_registered:
                        self._foreground_waiters -= 1
                        waiter_registered = False

                if self._is_cancelled(cancelled):
                    self.leave_foreground(provisional_lease)
                    return None
                return provisional_lease
        finally:
            if waiter_registered:
                with self._condition:
                    self._foreground_waiters -= 1
                    self._condition.notify_all()

    def leave_foreground(self, lease: object) -> None:
        with self._condition:
            if lease not in self._foreground_leases:
                raise RuntimeError("foreground lease is not active")
            self._foreground_leases.remove(lease)
            self._condition.notify_all()

    def snapshot(self) -> Mapping[str, bool | int]:
        """Return an immutable point-in-time view of the gate state."""
        with self._condition:
            return MappingProxyType(
                {
                    "automation_active": self._automation_lease is not None,
                    "automation_stop_requested": self._automation_stop_requested,
                    "foreground_active": len(self._foreground_leases),
                    "foreground_waiters": self._foreground_waiters,
                }
            )

    @staticmethod
    def _call_safely(callback: Callback | None) -> None:
        if callback is None:
            return
        try:
            callback()
        except BaseException:
            # Coordination must survive logging and stop-request failures.
            pass

    @staticmethod
    def _is_cancelled(callback: Predicate | None) -> bool:
        if callback is None:
            return False
        try:
            return bool(callback())
        except BaseException:
            return False


chatgpt_task_gate = ChatGPTTaskGate()
