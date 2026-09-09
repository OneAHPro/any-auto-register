"""Read-only integrity audit for account identities and remote bindings."""
from __future__ import annotations

from collections import Counter
from typing import Any

from sqlmodel import Session, select

from core.db import (
    AccountAssignmentModel,
    AccountIdentityModel,
    AccountModel,
    AccountTargetBindingModel,
    Codex2APITargetModel,
    CodexInventorySnapshotModel,
)


def run_binding_audit(engine) -> dict[str, Any]:
    """Return deterministic diagnostics without mutating the database."""
    with Session(engine) as session:
        bindings = session.exec(select(AccountTargetBindingModel)).all()
        assignments = session.exec(select(AccountAssignmentModel)).all()
        identities = {str(x.id): x for x in session.exec(select(AccountIdentityModel)).all()}
        accounts = {int(x.id): x for x in session.exec(select(AccountModel)).all() if x.id is not None}
        targets = {int(x.id): x for x in session.exec(select(Codex2APITargetModel)).all() if x.id is not None}
        inventory = session.exec(select(CodexInventorySnapshotModel)).all()

    def groups(rows, key):
        out = {}
        for row in rows:
            out.setdefault(key(row), []).append(int(row.id or 0))
        return {"|".join(map(str, k)): v for k, v in out.items() if len(v) > 1}

    duplicate_identity_target = groups(bindings, lambda x: (x.identity_id, x.target_id))
    duplicate_remote = groups(
        [x for x in bindings if int(x.remote_account_id or 0) > 0],
        lambda x: (x.target_id, x.remote_account_id),
    )
    active_assignments = [x for x in assignments if x.state in {"active", "draining", "standby"}]
    duplicate_assignments = groups(active_assignments, lambda x: (x.identity_id,))
    # An identity can be marked ambiguous before a target binding is created
    # (for example, when two imports claim the same stable workspace id).  Keep
    # that state visible in the audit even when there is no binding row yet.
    ambiguous = sorted(
        {
            str(identity_id)
            for identity_id, identity in identities.items()
            if str(identity.state or "").strip().lower() == "ambiguous"
        }
        | {
            str(binding.identity_id)
            for binding in bindings
            if str(binding.sync_status or "").strip().lower() == "ambiguous"
            and str(binding.identity_id or "").strip()
        }
    )
    orphan_bindings = {
        str(row.id): {
            "identity_missing": str(row.identity_id or "") not in identities,
            "account_missing": (
                int(row.local_account_id or 0) > 0
                and int(row.local_account_id or 0) not in accounts
            ),
            "target_missing": int(row.target_id or 0) not in targets,
        }
        for row in bindings
        if (
            str(row.identity_id or "") not in identities
            or (
                int(row.local_account_id or 0) > 0
                and int(row.local_account_id or 0) not in accounts
            )
            or int(row.target_id or 0) not in targets
        )
    }
    orphan_assignments = {
        str(row.id): {
            "identity_missing": str(row.identity_id or "") not in identities,
            "account_missing": (
                int(row.local_account_id or 0) > 0
                and int(row.local_account_id or 0) not in accounts
            ),
            "target_missing": int(row.target_id or 0) not in targets,
        }
        for row in assignments
        if (
            str(row.identity_id or "") not in identities
            or (
                int(row.local_account_id or 0) > 0
                and int(row.local_account_id or 0) not in accounts
            )
            or int(row.target_id or 0) not in targets
        )
    }
    active_states = {"active", "draining"}
    valid_binding_keys = {
        (str(row.identity_id or ""), int(row.target_id or 0))
        for row in bindings
        if bool(row.enabled)
        and str(row.sync_status or "").lower() == "synced"
    }
    active_without_binding = {
        str(row.id): {
            "identity_id": str(row.identity_id),
            "target_id": int(row.target_id or 0),
            "state": str(row.state or ""),
        }
        for row in assignments
        if str(row.state or "").lower() in active_states
        and (str(row.identity_id or ""), int(row.target_id or 0)) not in valid_binding_keys
    }
    ambiguous_active_bindings = [
        int(row.id or 0)
        for row in bindings
        if bool(row.enabled)
        and str(
            getattr(identities.get(str(row.identity_id or "")), "state", "")
        ).strip().lower()
        == "ambiguous"
    ]
    ambiguous_active_assignments = [
        int(row.id or 0)
        for row in assignments
        if str(row.state or "").lower() in active_states
        and str(
            getattr(identities.get(str(row.identity_id or "")), "state", "")
        ).strip().lower()
        == "ambiguous"
    ]
    remote_missing_active_assignments = [
        int(row.id or 0)
        for row in assignments
        if str(row.state or "").lower() in active_states
        and any(
            str(binding.identity_id or "") == str(row.identity_id or "")
            and int(binding.target_id or 0) == int(row.target_id or 0)
            and str(binding.sync_status or "").lower() == "remote_missing"
            for binding in bindings
        )
    ]
    standby_with_lease = [
        int(row.id or 0)
        for row in assignments
        if str(row.state or "").lower() == "standby"
        and (str(row.lease_owner or "").strip() or row.lease_expires_at is not None)
    ]
    inventory_duplicate_keys = groups(
        inventory,
        lambda row: (int(row.target_id or 0), int(row.remote_id or 0)),
    )
    # ``issue_count`` is consumed by the CLI's ``--fail-on-issues`` output and
    # by operators scanning a report.  Count the concrete rows/groups named in
    # each diagnostic rather than returning one point for each non-empty
    # category.  Duplicate maps contain the affected row IDs as values; the
    # other mappings are keyed by one affected row, so both are counted at the
    # row level.  Categories intentionally remain separate in the public
    # payload for backwards compatibility, even if one row contributes to more
    # than one independent integrity finding.
    def _duplicate_rows(groups: dict) -> int:
        return sum(
            len(value) if isinstance(value, (list, tuple, set, frozenset)) else 1
            for value in groups.values()
        )

    issue_count = (
        _duplicate_rows(duplicate_identity_target)
        + _duplicate_rows(duplicate_remote)
        + _duplicate_rows(duplicate_assignments)
        + len(orphan_bindings)
        + len(orphan_assignments)
        + len(active_without_binding)
        + len(ambiguous_active_bindings)
        + len(ambiguous_active_assignments)
        + len(remote_missing_active_assignments)
        + len(standby_with_lease)
        + _duplicate_rows(inventory_duplicate_keys)
        + len(ambiguous)
    )
    return {
        "binding_count": len(bindings),
        "assignment_count": len(assignments),
        "duplicate_identity_target": duplicate_identity_target,
        "duplicate_remote": duplicate_remote,
        "duplicate_active_assignments": duplicate_assignments,
        "ambiguous_identities": ambiguous,
        "orphan_bindings": orphan_bindings,
        "orphan_assignments": orphan_assignments,
        "active_assignments_without_enabled_binding": active_without_binding,
        "ambiguous_active_bindings": ambiguous_active_bindings,
        "ambiguous_active_assignments": ambiguous_active_assignments,
        "remote_missing_active_assignments": remote_missing_active_assignments,
        "standby_assignments_with_lease": standby_with_lease,
        "duplicate_inventory_keys": inventory_duplicate_keys,
        "issue_count": issue_count,
        "disabled_bindings": sum(1 for x in bindings if not x.enabled),
        "status_counts": dict(Counter(str(x.sync_status or "unknown") for x in bindings)),
    }
