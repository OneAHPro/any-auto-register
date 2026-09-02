"""Stateful, deterministic stand-in for an unmodified Codex2API target."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from services.codex2api_target_client import Codex2APITargetError


class FakeCodex2APITarget:
    """Implement the public target-client contract with failure injection."""

    def __init__(
        self,
        target_id: int,
        *,
        fail_on: str | set[str] = "",
    ) -> None:
        self.target_id = int(target_id)
        self.fail_on = {fail_on} if isinstance(fail_on, str) and fail_on else set(fail_on)
        self.accounts: dict[int, dict[str, Any]] = {}
        self.trash: dict[int, dict[str, Any]] = {}
        self.calls: list[tuple[Any, ...]] = []
        self._next_id = 100 * self.target_id
        self.usage_probe_running = False
        self.default_reset_at: str | None = None

    def _fails(self, operation: str) -> bool:
        return "network" in self.fail_on or operation in self.fail_on

    def _maybe_fail(self, operation: str) -> None:
        if self._fails(operation):
            raise Codex2APITargetError(
                f"fixture target failure at {operation}",
                status_code=503,
                endpoint=operation,
            )

    def seed_account(
        self,
        *,
        email: str,
        workspace_id: str,
        remote_id: int = 55,
        billed_7d: float = 1200,
        usage_percent_7d: float = 66.6667,
        enabled: bool = True,
        active_requests: int = 0,
    ) -> int:
        reset_at = self.default_reset_at or (
            datetime.now(timezone.utc) + timedelta(days=3)
        ).isoformat()
        self.accounts[int(remote_id)] = {
            "id": int(remote_id),
            "email": str(email).strip().lower(),
            "name": str(email).strip().lower(),
            "workspace_id": workspace_id,
            "account_id": workspace_id,
            "status": "active",
            "enabled": bool(enabled),
            "locked": False,
            "active_requests": int(active_requests),
            "billed_7d": billed_7d,
            "usage_percent_7d": usage_percent_7d,
            "reset_7d_at": reset_at,
            "quota_7d_updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._next_id = max(self._next_id, int(remote_id) + 1)
        return int(remote_id)

    def health(self) -> dict[str, Any]:
        self.calls.append(("health",))
        self._maybe_fail("health")
        return {"status": "ok", "target_id": self.target_id}

    def capabilities(self) -> dict[str, Any]:
        self.calls.append(("capabilities",))
        self._maybe_fail("capabilities")
        return {
            "list_accounts": True,
            "usage_probe": True,
            "account_test": True,
            "enable_toggle": True,
            "lock_toggle": True,
            "delete": True,
            "restore": True,
            "soft_delete": True,
            "migratable": True,
            "rollback_strategy": "restore_or_reimport",
        }

    def list_accounts(self) -> list[dict[str, Any]]:
        self.calls.append(("list",))
        self._maybe_fail("list")
        return [deepcopy(self.accounts[key]) for key in sorted(self.accounts)]

    def trigger_usage_probe(self) -> dict[str, Any]:
        self.calls.append(("probe",))
        self._maybe_fail("probe")
        self.usage_probe_running = False
        return {"started": True}

    def runtime_status(self) -> dict[str, Any]:
        self.calls.append(("runtime",))
        self._maybe_fail("runtime")
        return {"probes": {"usage_probe_running": self.usage_probe_running}}

    def api_key_usage(self, *, start: Any, end: Any) -> list[dict[str, Any]]:
        self.calls.append(("api_key_usage", start, end))
        self._maybe_fail("api_key_usage")
        return []

    def _import(self, payload: Mapping[str, Any], operation: str) -> dict[str, Any]:
        self.calls.append((operation, str(payload.get("email") or payload.get("name") or "")))
        self._maybe_fail("import")
        self._maybe_fail(operation)
        email = str(payload.get("email") or payload.get("name") or "").strip().lower()
        workspace_id = str(
            payload.get("workspace_id")
            or payload.get("account_id")
            or payload.get("user_id")
            or ""
        ).strip()
        for row in self.accounts.values():
            if row.get("email") == email and (
                not workspace_id or row.get("workspace_id") == workspace_id
            ):
                return {"id": int(row["id"]), "created": False}
        remote_id = self._next_id
        self._next_id += 1
        self.seed_account(
            email=email,
            workspace_id=workspace_id,
            remote_id=remote_id,
            billed_7d=0,
            usage_percent_7d=0,
            enabled=True,
        )
        return {"id": remote_id, "created": True}

    def import_refresh_token(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._import(payload, "import_refresh_token")

    def import_access_token(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._import(payload, "import_access_token")

    def import_full_json(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._import(payload, "import_full_json")

    def test_account(self, remote_id: int) -> dict[str, Any]:
        self.calls.append(("test", int(remote_id)))
        if self._fails("verify"):
            return {"success": False, "verified": False}
        self._maybe_fail("test")
        return {
            "success": int(remote_id) in self.accounts,
            "verified": int(remote_id) in self.accounts,
        }

    def set_enabled(self, remote_id: int, enabled: bool) -> dict[str, Any]:
        self.calls.append(("enable", int(remote_id), bool(enabled)))
        self._maybe_fail("enable")
        self._maybe_fail("enable_true" if enabled else "enable_false")
        row = self.accounts.get(int(remote_id))
        if row is None:
            raise Codex2APITargetError(
                "fixture account not found",
                status_code=404,
                endpoint="enable",
            )
        row["enabled"] = bool(enabled)
        return {"id": int(remote_id), "enabled": bool(enabled)}

    def set_locked(self, remote_id: int, locked: bool) -> dict[str, Any]:
        self.calls.append(("lock", int(remote_id), bool(locked)))
        self._maybe_fail("lock")
        row = self.accounts.get(int(remote_id))
        if row is None:
            raise Codex2APITargetError(
                "fixture account not found",
                status_code=404,
                endpoint="lock",
            )
        row["locked"] = bool(locked)
        return {"id": int(remote_id), "locked": bool(locked)}

    def refresh_account(self, remote_id: int) -> dict[str, Any]:
        self.calls.append(("refresh", int(remote_id)))
        self._maybe_fail("refresh")
        return {"id": int(remote_id), "refreshed": int(remote_id) in self.accounts}

    def delete_account(self, remote_id: int) -> dict[str, Any]:
        self.calls.append(("delete", int(remote_id)))
        self._maybe_fail("delete")
        row = self.accounts.pop(int(remote_id), None)
        if row is None:
            raise Codex2APITargetError(
                "fixture account already absent",
                status_code=404,
                endpoint="delete",
            )
        self.trash[int(remote_id)] = row
        return {"id": int(remote_id), "deleted": True}

    def restore_account(self, remote_id: int) -> dict[str, Any]:
        self.calls.append(("restore", int(remote_id)))
        self._maybe_fail("restore")
        if int(remote_id) in self.accounts:
            return {"id": int(remote_id), "restored": False}
        row = self.trash.pop(int(remote_id), None)
        if row is None:
            raise Codex2APITargetError(
                "fixture account is not restorable",
                status_code=404,
                endpoint="restore",
            )
        self.accounts[int(remote_id)] = row
        return {"id": int(remote_id), "restored": True}

    def update_scheduler(
        self,
        remote_id: int,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        self.calls.append(("scheduler", int(remote_id)))
        self._maybe_fail("scheduler")
        row = self.accounts.get(int(remote_id))
        if row is None:
            raise Codex2APITargetError(
                "fixture account not found",
                status_code=404,
                endpoint="scheduler",
            )
        row["scheduler"] = dict(payload)
        return {"id": int(remote_id), "updated": True}

    def wait_for_zero_active_requests(
        self,
        remote_id: int,
        *,
        timeout_seconds: float = 600,
        poll_interval_seconds: float = 2,
        sleep_fn=lambda _seconds: None,
    ) -> bool:
        del timeout_seconds, poll_interval_seconds, sleep_fn
        self.calls.append(("drain", int(remote_id)))
        self._maybe_fail("drain_network")
        if self._fails("drain"):
            return False
        row = self.accounts.get(int(remote_id))
        if row is None:
            raise Codex2APITargetError(
                "fixture account not found",
                status_code=404,
                endpoint="drain",
            )
        return int(row.get("active_requests") or 0) <= 0


__all__ = ["FakeCodex2APITarget"]
