"""Stable account identity resolution for the account-control plane."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping
from uuid import uuid4

from sqlalchemy import func, update
from sqlmodel import Session, select

from core.db import (
    AccountIdentityAliasModel,
    AccountAssignmentModel,
    AccountIdentityModel,
    AccountModel,
    AccountTargetBindingModel,
    engine as default_engine,
)


_IDENTITY_HMAC_KEY = b"any-auto-register-account-identity-v1"
_IDENTITY_ALIAS_TYPES = {"workspace_id", "chatgpt_account_id"}
_STRONG_ALIAS_TYPES = {
    "workspace_id",
    "chatgpt_account_id",
    "credential_fingerprint",
}
_IDENTITY_MUTATION_LOCK = threading.RLock()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_email(value: Any) -> str:
    return str(value or "").strip().lower()


def normalize_alias(value: Any) -> str:
    return str(value or "").strip().lower()


def credential_fingerprint(
    platform: str,
    email: str,
    *,
    refresh_token: str = "",
    access_token: str = "",
    session_token: str = "",
    workspace_id: str = "",
    chatgpt_account_id: str = "",
) -> str:
    """Return a stable non-secret fingerprint for a credential identity."""

    canonical = {
        "platform": normalize_alias(platform),
        "email": normalize_email(email),
        "refresh_token": str(refresh_token or ""),
        "access_token": str(access_token or ""),
        "session_token": str(session_token or ""),
        "workspace_id": normalize_alias(workspace_id),
        "chatgpt_account_id": normalize_alias(chatgpt_account_id),
    }
    payload = json.dumps(canonical, ensure_ascii=True, sort_keys=True).encode("utf-8")
    return hmac.new(_IDENTITY_HMAC_KEY, payload, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class IdentityResolution:
    identity_id: str
    created: bool
    ambiguous: bool
    state: str


def _alias_values(
    *,
    email: str,
    workspace_id: str = "",
    chatgpt_account_id: str = "",
    credential_fingerprint_value: str = "",
) -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []
    for alias_type, value in (
        ("email", email),
        ("workspace_id", workspace_id),
        ("chatgpt_account_id", chatgpt_account_id),
        ("credential_fingerprint", credential_fingerprint_value),
    ):
        normalized = normalize_alias(value)
        if normalized:
            values.append((alias_type, normalized))
    return values


def _identity_ids_for_aliases(
    session: Session,
    *,
    platform: str,
    email: str,
    aliases: Iterable[tuple[str, str]],
) -> dict[tuple[str, str], set[str]]:
    normalized_platform = normalize_alias(platform)
    result: dict[tuple[str, str], set[str]] = {}
    for alias_type, value in aliases:
        rows = session.exec(
            select(AccountIdentityAliasModel)
            .join(
                AccountIdentityModel,
                AccountIdentityModel.id == AccountIdentityAliasModel.identity_id,
            )
            .where(AccountIdentityModel.platform == normalized_platform)
            .where(AccountIdentityModel.canonical_email == normalize_email(email))
            .where(AccountIdentityAliasModel.alias_type == alias_type)
            .where(AccountIdentityAliasModel.normalized_value == value)
        ).all()
        result[(alias_type, value)] = {
            str(row.identity_id) for row in rows if str(row.identity_id or "").strip()
        }
    return result


def _mark_ambiguous(
    session: Session, identity_ids: Iterable[str], *, reason: str = "",
) -> None:
    now = _utcnow()
    for identity_id in set(identity_ids):
        row = session.get(AccountIdentityModel, identity_id)
        if row is not None:
            row.state = "ambiguous"
            if reason and not row.ambiguity_reason:
                row.ambiguity_reason = reason
            row.updated_at = now
            session.add(row)


def _upsert_aliases(
    session: Session,
    *,
    identity_id: str,
    aliases: Iterable[tuple[str, str]],
    source: str,
) -> set[str]:
    now = _utcnow()
    identity = session.get(AccountIdentityModel, identity_id)
    identity_platform = normalize_alias(identity.platform if identity else "")
    identity_email = normalize_email(identity.canonical_email if identity else "")
    conflicts: set[str] = set()
    for alias_type, value in aliases:
        statement = (
            select(AccountIdentityAliasModel)
            .join(
                AccountIdentityModel,
                AccountIdentityModel.id == AccountIdentityAliasModel.identity_id,
            )
            .where(AccountIdentityModel.platform == identity_platform)
            .where(AccountIdentityAliasModel.alias_type == alias_type)
            .where(AccountIdentityAliasModel.normalized_value == value)
        )
        if alias_type in _IDENTITY_ALIAS_TYPES:
            # Business workspace/account IDs identify the shared workspace,
            # while the mailbox identifies its member. Fingerprints remain
            # globally unique within the platform.
            statement = statement.where(
                AccountIdentityModel.canonical_email == identity_email
            )
        rows = session.exec(statement).all()
        other_ids = {
            str(row.identity_id)
            for row in rows
            if str(row.identity_id or "") and str(row.identity_id) != identity_id
        }
        if other_ids and alias_type in _STRONG_ALIAS_TYPES:
            conflicts.update(other_ids)
            if alias_type == "credential_fingerprint":
                _mark_ambiguous(
                    session, other_ids | {identity_id}, reason="credential_fingerprint_conflict",
                )
            continue
        own = next(
            (row for row in rows if str(row.identity_id) == identity_id),
            None,
        )
        if own is None:
            session.add(
                AccountIdentityAliasModel(
                    identity_id=identity_id,
                    platform=identity_platform,
                    alias_type=alias_type,
                    normalized_value=value,
                    source=str(source or ""),
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
        else:
            own.last_seen_at = now
            if source:
                own.source = str(source)
            session.add(own)
    return conflicts


def _ensure_identity_impl(
    database_engine,
    *,
    account_id: int,
    platform: str,
    email: str,
    workspace_id: str = "",
    chatgpt_account_id: str = "",
    credential_fingerprint: str = "",
    source: str = "",
) -> IdentityResolution:
    """Resolve or create one identity using strong aliases before email."""

    target_engine = database_engine or default_engine
    normalized_platform = normalize_alias(platform)
    normalized_email = normalize_email(email)
    if not normalized_platform:
        raise ValueError("platform is required for account identity")
    if not normalized_email:
        raise ValueError("email is required for account identity")

    aliases = _alias_values(
        email=normalized_email,
        workspace_id=workspace_id,
        chatgpt_account_id=chatgpt_account_id,
        credential_fingerprint_value=credential_fingerprint,
    )
    identity_aliases = [alias for alias in aliases if alias[0] in _IDENTITY_ALIAS_TYPES]
    fingerprint_aliases = [
        alias for alias in aliases if alias[0] == "credential_fingerprint"
    ]

    with Session(target_engine) as session:
        existing_account = session.get(AccountModel, int(account_id or 0))
        existing_identity_id = str(
            getattr(existing_account, "identity_id", "") or ""
        ).strip()
        existing_identity = (
            session.get(AccountIdentityModel, existing_identity_id)
            if existing_identity_id
            else None
        )
        ids_by_alias = _identity_ids_for_aliases(
            session,
            platform=normalized_platform,
            email=normalized_email,
            aliases=aliases,
        )
        identity_ids = set().union(
            *(ids_by_alias.get(alias, set()) for alias in identity_aliases)
        )
        fingerprint_ids = set().union(
            *(ids_by_alias.get(alias, set()) for alias in fingerprint_aliases)
        )
        email_ids = ids_by_alias.get(("email", normalized_email), set())
        conflict_ids: set[str] = set()
        created = False

        if (
            existing_identity is not None
            and normalize_alias(existing_identity.platform) == normalized_platform
        ):
            # A re-login/upsert of the same local row is authoritative even
            # when every remote credential fingerprint has changed.
            identity_id = str(existing_identity.id)
            if email_ids and not email_ids.issubset({identity_id}):
                conflict_ids.update(email_ids)
                conflict_ids.add(identity_id)
        elif len(identity_ids) == 1:
            identity_id = next(iter(identity_ids))
            if email_ids and not email_ids.issubset({identity_id}):
                # An exact workspace/account alias is stronger than a shared
                # email.  Reuse it, but keep every same-email identity marked
                # ambiguous for operator visibility.
                conflict_ids.update(email_ids)
                conflict_ids.add(identity_id)
        elif len(identity_ids) > 1:
            conflict_ids.update(identity_ids)
            identity_id = ""
        elif len(email_ids) == 1 and not identity_aliases:
            identity_id = next(iter(email_ids))
        elif len(fingerprint_ids) == 1 and not identity_aliases:
            identity_id = next(iter(fingerprint_ids))
        else:
            identity_id = ""
            conflict_ids.update(email_ids)

        if not identity_id:
            identity_id = str(uuid4())
            created = True
            session.add(
                AccountIdentityModel(
                    id=identity_id,
                    platform=normalized_platform,
                    canonical_email=normalized_email,
                    state="ambiguous" if conflict_ids else "active",
                    current_account_id=int(account_id or 0),
                )
            )

        identity = session.get(AccountIdentityModel, identity_id)
        if identity is None:
            # The row can be absent only when a concurrent writer won a race;
            # create it here and let the database commit establish ownership.
            identity = AccountIdentityModel(
                id=identity_id,
                platform=normalized_platform,
                canonical_email=normalized_email,
                state="active",
                current_account_id=int(account_id or 0),
            )
            session.add(identity)
        identity.canonical_email = normalized_email or identity.canonical_email
        identity.current_account_id = int(account_id or 0)
        identity.updated_at = _utcnow()
        session.add(identity)

        alias_conflicts = _upsert_aliases(
            session,
            identity_id=identity_id,
            aliases=aliases,
            source=source,
        )
        conflict_ids.update(alias_conflicts)
        if conflict_ids:
            conflict_ids.add(identity_id)
            _mark_ambiguous(session, conflict_ids)
            identity.state = "ambiguous"
            session.add(identity)

        account = session.get(AccountModel, int(account_id or 0))
        if account is not None:
            if str(account.identity_id or "") != identity_id:
                account.identity_id = identity_id
                account.updated_at = _utcnow()
                session.add(account)
        else:
            # Legacy test databases or an import race may not have the row;
            # the identity remains valid and can be linked during reconcile.
            session.exec(
                update(AccountModel)
                .where(AccountModel.id == int(account_id or 0))
                .values(identity_id=identity_id)
            )
        session.commit()
        session.refresh(identity)
        return IdentityResolution(
            identity_id=identity_id,
            created=created,
            ambiguous=identity.state == "ambiguous",
            state=str(identity.state or "active"),
        )


def ensure_identity(
    database_engine,
    *,
    account_id: int,
    platform: str,
    email: str,
    workspace_id: str = "",
    chatgpt_account_id: str = "",
    credential_fingerprint: str = "",
    source: str = "",
) -> IdentityResolution:
    """Serialize local identity upserts and retry a transient uniqueness race."""

    with _IDENTITY_MUTATION_LOCK:
        return _ensure_identity_impl(
            database_engine,
            account_id=account_id,
            platform=platform,
            email=email,
            workspace_id=workspace_id,
            chatgpt_account_id=chatgpt_account_id,
            credential_fingerprint=credential_fingerprint,
            source=source,
        )


def get_identity(database_engine, identity_id: str) -> AccountIdentityModel | None:
    target_engine = database_engine or default_engine
    with Session(target_engine) as session:
        return session.get(AccountIdentityModel, str(identity_id or ""))


def identity_for_account(database_engine, account_id: int) -> AccountIdentityModel | None:
    target_engine = database_engine or default_engine
    with Session(target_engine) as session:
        account = session.get(AccountModel, int(account_id or 0))
        if account is None or not str(account.identity_id or "").strip():
            return None
        return session.get(AccountIdentityModel, str(account.identity_id))


def ensure_identity_for_model(database_engine, account: AccountModel) -> IdentityResolution:
    """Attach a saved ORM account to its stable identity."""

    values = _account_identity_values(account)
    return ensure_identity(
        database_engine,
        account_id=int(account.id or 0),
        platform=str(account.platform or ""),
        email=str(account.email or ""),
        workspace_id=values["workspace_id"],
        chatgpt_account_id=values["chatgpt_account_id"],
        credential_fingerprint=values["credential_fingerprint"],
        source="account_save",
    )


def supersede_other_target_bindings(
    session: Session,
    *,
    identity_id: str,
    current_target_id: int,
    reason: str = "identity_target_changed",
) -> int:
    """Disable enabled bindings for an identity on targets it left.

    ``AccountAssignmentModel`` represents the current target, while binding
    rows also retain the remote history. Keeping an old row enabled makes
    target account counts and reconciliation treat it as current, so target
    changes must explicitly supersede those rows.
    """
    normalized_identity = str(identity_id or "").strip()
    if not normalized_identity:
        return 0
    rows = session.exec(
        select(AccountTargetBindingModel)
        .where(AccountTargetBindingModel.identity_id == normalized_identity)
        .where(AccountTargetBindingModel.target_id != int(current_target_id))
        .where(AccountTargetBindingModel.enabled == True)  # noqa: E712
    ).all()
    now = _utcnow()
    changed = 0
    for row in rows:
        row.enabled = False
        row.sync_status = "superseded"
        row.remote_status = "superseded"
        row.last_error = str(reason or "identity_target_changed")[:240]
        row.updated_at = now
        session.add(row)
        changed += 1
    return changed


def move_assignments_to_standby(
    session: Session,
    *,
    identity_id: str,
    reason: str,
    target_id: int | None = None,
) -> int:
    """Quarantine in-flight assignments and advance their CAS version."""
    normalized_identity = str(identity_id or "").strip()
    if not normalized_identity:
        return 0
    states = {
        "active", "draining", "planned", "locking", "uploading", "pending",
        "verifying", "assignment_committing", "source_cleaning", "target_enabling",
        "migrating", "target_disabled",
    }
    # Include an already-standby row when the quarantine reason changes (or
    # when a legacy row has never recorded a reason).  Advancing the version
    # in that case invalidates an in-flight plan that may still hold the old
    # CAS token, while the same reconciliation is idempotent once the reason
    # has been persisted.
    statement = select(AccountAssignmentModel).where(
        AccountAssignmentModel.identity_id == normalized_identity,
        AccountAssignmentModel.state.in_(states | {"standby"}),
    )
    if target_id is not None:
        statement = statement.where(AccountAssignmentModel.target_id == int(target_id))
    rows = session.exec(statement).all()
    if len(rows) > 1:
        def _assignment_stamp(row):
            value = row.updated_at
            if value is None:
                return datetime.min.replace(tzinfo=timezone.utc)
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)

        rows.sort(
            key=lambda row: (
                _assignment_stamp(row),
                int(row.id or 0),
            ),
            reverse=True,
        )
        # The partial current-assignment index should permit only one active
        # row.  If legacy transitional duplicates exist, fence the older rows
        # outside that index before quarantining the canonical one.
        for duplicate in rows[1:]:
            duplicate.state = "superseded"
            duplicate.assignment_version = max(
                1, int(duplicate.assignment_version or 0)
            ) + 1
            duplicate.lease_owner = ""
            duplicate.lease_expires_at = None
            duplicate.lease_reason = "duplicate_assignment_fenced"
            duplicate.updated_at = _utcnow()
            session.add(duplicate)
        rows = rows[:1]
    now = _utcnow()
    normalized_reason = str(reason or "assignment_quarantined")[:200]
    for row in rows:
        if row.state == "standby" and str(row.lease_reason or "") == normalized_reason:
            if not str(row.lease_owner or "").strip() and row.lease_expires_at is None:
                continue
        row.state = "standby"
        row.assignment_version = max(1, int(row.assignment_version or 0)) + 1
        # A quarantined assignment no longer owns a scheduler lease.  Leaving
        # the old owner/expiry populated makes the account appear leased to
        # readers that inspect those fields independently of ``state``.
        row.lease_owner = ""
        row.lease_expires_at = None
        row.lease_reason = normalized_reason
        row.updated_at = now
        session.add(row)
    return len(rows)


def _account_identity_values(account: AccountModel) -> dict[str, str]:
    try:
        extra = account.get_extra()
    except (TypeError, ValueError, json.JSONDecodeError):
        extra = {}
    if not isinstance(extra, dict):
        extra = {}

    # Remote-only rows keep the provider identifiers inside
    # ``codex_remote_snapshot``.  Walk the credential-free projection as well
    # as the legacy top-level fields so startup reconciliation does not treat a
    # pair of target-scoped projections as unrelated mailboxes.  The values are
    # used only to derive a one-way fingerprint/alias; secrets are never read
    # from arbitrary nested fields here.
    sources: list[Mapping[str, Any]] = []

    def collect(value: object, depth: int = 0) -> None:
        if not isinstance(value, Mapping) or depth > 6:
            return
        sources.append(value)
        for child in value.values():
            if isinstance(child, Mapping):
                collect(child, depth + 1)
            elif isinstance(child, (list, tuple, set)):
                for item in child:
                    if isinstance(item, Mapping):
                        collect(item, depth + 1)

    collect(extra)

    def first(*keys: str) -> str:
        for source in sources:
            for key in keys:
                value = str(source.get(key) or "").strip()
                if value:
                    return value
        return ""

    workspace_id = first("workspace_id", "workspaceId", "effective_workspace_id")
    account_id = first(
        "chatgpt_account_id", "chatgptAccountId", "account_id", "accountId", "user_id"
    )
    fingerprint = credential_fingerprint(
        account.platform,
        account.email,
        refresh_token=first("refresh_token", "refreshToken"),
        access_token=first("access_token", "accessToken") or account.token,
        session_token=first("session_token", "sessionToken"),
        workspace_id=workspace_id,
        chatgpt_account_id=account_id,
    )
    return {
        "workspace_id": workspace_id,
        "chatgpt_account_id": account_id,
        "credential_fingerprint": fingerprint,
    }


def _is_remote_only_projection(account: AccountModel) -> bool:
    """Whether an account row is a credential-free remote projection.

    These rows intentionally have target-scoped identities.  They can appear
    in more than one pool, so the normal credential identity resolver must not
    mark them ambiguous merely because their mailbox alias is shared.
    """

    try:
        extra = account.get_extra()
    except Exception:
        extra = {}
    return isinstance(extra, Mapping) and bool(extra.get("remote_only"))


def _repair_remote_projection_states(database_engine) -> int:
    """Clear stale ambiguity flags for identical remote projections.

    Older boots ran the credential resolver against remote-only rows and could
    mark two target-scoped identities ambiguous after seeing the same mailbox
    email.  When the provider also supplies the same strong account/workspace
    alias, those rows are the same display identity; keep their target-scoped
    bindings intact and restore their operational state.  Rows with different
    aliases or different mailboxes remain untouched.
    """

    changed = 0
    try:
        with Session(database_engine) as session:
            accounts = [
                account
                for account in session.exec(
                    select(AccountModel).where(AccountModel.platform == "chatgpt")
                ).all()
                if _is_remote_only_projection(account)
                and str(account.identity_id or "").strip()
            ]
            by_alias: dict[tuple[str, str], list[AccountModel]] = {}
            for account in accounts:
                values = _account_identity_values(account)
                for alias_type in ("workspace_id", "chatgpt_account_id"):
                    value = normalize_alias(values.get(alias_type))
                    if value:
                        by_alias.setdefault((alias_type, value), []).append(account)
            repaired_identity_ids: set[str] = set()
            for rows in by_alias.values():
                if len(rows) < 2:
                    continue
                emails = {
                    normalize_email(account.email)
                    for account in rows
                    if normalize_email(account.email)
                }
                if len(emails) != 1:
                    continue
                for account in rows:
                    identity_id = str(account.identity_id or "").strip()
                    identity = session.get(AccountIdentityModel, identity_id)
                    if identity is None or identity.state != "ambiguous" or identity.ambiguity_reason:
                        continue
                    identity.state = "active"
                    identity.updated_at = _utcnow()
                    session.add(identity)
                    repaired_identity_ids.add(identity_id)
            if repaired_identity_ids:
                session.commit()
                changed = len(repaired_identity_ids)
    except Exception:
        # Startup reconciliation is best-effort for legacy installations.  A
        # failed repair must not prevent the application from serving cards.
        return 0
    return changed


def _current_shared_workspace(account: AccountModel) -> str:
    """Return one workspace only when current local/provider evidence agrees.

    Historical aliases and mailbox login IDs are not authoritative workspace
    evidence. A legacy account ID equal to the member email is a placeholder.
    JWT claims here are only a consistency veto, never token authentication.
    """
    try:
        extra = account.get_extra()
    except (TypeError, ValueError):
        return ""
    if not isinstance(extra, Mapping):
        return ""
    email = normalize_email(account.email)
    sources = [extra, extra.get("credentials")]
    local = extra.get("chatgpt_local")
    if isinstance(local, Mapping):
        sources.extend(local.get(key) for key in ("subscription", "codex"))
    snapshot = extra.get("codex_remote_snapshot")
    if isinstance(snapshot, Mapping):
        if normalize_email(snapshot.get("email")) != email:
            return ""
        if any(snapshot.get(key) for key in ("_inventory_missing", "_inventory_stale", "_inventory_error")):
            return ""
        sources.append(snapshot)
        sources.append(snapshot.get("credentials"))
    token = str(extra.get("access_token") or account.token or "")
    if token.count(".") == 2:
        try:
            payload = token.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        except (TypeError, ValueError, UnicodeError):
            return ""
        if not isinstance(claims, Mapping):
            return ""
        if claims.get("email") and normalize_email(claims.get("email")) != email:
            return ""
        sources.extend((claims, claims.get("https://api.openai.com/auth")))
    values = {
        normalize_alias(source.get(key))
        for source in sources if isinstance(source, Mapping)
        for key in (
            "workspace_id", "workspaceId", "effective_workspace_id",
            "chatgpt_account_id", "chatgptAccountId", "account_id", "accountId",
        )
        if normalize_alias(source.get(key)) not in {"", email}
    }
    return next(iter(values)) if len(values) == 1 else ""


def _repair_shared_workspace_member_states(database_engine) -> int:
    """Repair the old cross-mailbox workspace conflict without clearing others.

    This intentionally requires a single current workspace and mailbox per
    identity. Existing alias conflicts, provider mismatches, or other explicit
    quarantine reasons remain for operator review. Binding enablement is left
    to the normal inventory reconciliation with fresh provider status.
    """
    with Session(database_engine) as session:
        identities = {
            row.id: row for row in session.exec(select(AccountIdentityModel).where(
                AccountIdentityModel.platform == "chatgpt",
            )).all()
        }
        email_ids: dict[str, set[str]] = {}
        for identity in identities.values():
            email_ids.setdefault(normalize_email(identity.canonical_email), set()).add(identity.id)
        aliases_by_identity: dict[str, list[AccountIdentityAliasModel]] = {}
        fingerprints: dict[str, set[str]] = {}
        for alias in session.exec(select(AccountIdentityAliasModel).where(
            AccountIdentityAliasModel.platform == "chatgpt",
        )).all():
            aliases_by_identity.setdefault(alias.identity_id, []).append(alias)
            if alias.alias_type == "credential_fingerprint":
                fingerprints.setdefault(alias.normalized_value, set()).add(alias.identity_id)
        accounts = session.exec(select(AccountModel).where(
            func.lower(AccountModel.platform) == "chatgpt",
        )).all()
        workspaces: dict[str, list[AccountModel]] = {}
        accounts_by_identity: dict[str, list[AccountModel]] = {}
        for account in accounts:
            accounts_by_identity.setdefault(account.identity_id, []).append(account)
            fingerprint = _account_identity_values(account)["credential_fingerprint"]
            if account.identity_id:
                fingerprints.setdefault(fingerprint, set()).add(account.identity_id)
            workspace = _current_shared_workspace(account)
            if workspace and normalize_email(account.email):
                workspaces.setdefault(workspace, []).append(account)
        fingerprint_conflicts = set().union(*(
            owners for owners in fingerprints.values() if len(owners) > 1
        ))
        changed = 0
        for workspace, members in workspaces.items():
            if len({normalize_email(account.email) for account in members}) < 2:
                continue
            for account in members:
                identity = identities.get(account.identity_id)
                email = normalize_email(account.email)
                if (
                    identity is None or identity.state != "ambiguous"
                    or identity.ambiguity_reason
                    or _is_remote_only_projection(account)
                    or email_ids.get(email) != {identity.id}
                    or normalize_email(identity.canonical_email) != email
                    or identity.current_account_id != account.id
                    or len(accounts_by_identity.get(identity.id, [])) != 1
                    or identity.id in fingerprint_conflicts
                ):
                    continue
                aliases = aliases_by_identity.get(identity.id, [])
                if any(
                    alias.alias_type in _IDENTITY_ALIAS_TYPES
                    and alias.normalized_value not in {workspace, email}
                    for alias in aliases
                ):
                    continue
                bindings = session.exec(select(AccountTargetBindingModel).where(
                    AccountTargetBindingModel.identity_id == identity.id,
                )).all()
                if any(
                    binding.local_account_id != account.id
                    or normalize_email(binding.remote_email) not in {"", email}
                    or binding.last_error not in {"", "身份存在歧义，等待人工确认"}
                    for binding in bindings
                ):
                    continue
                assignments = session.exec(select(AccountAssignmentModel).where(
                    AccountAssignmentModel.identity_id == identity.id,
                )).all()
                if any("mismatch" in str(row.lease_reason or "") for row in assignments):
                    continue
                identity.state = "active"
                identity.updated_at = _utcnow()
                session.add(identity)
                changed += 1
        if changed:
            session.commit()
        return changed


def reconcile_existing_accounts(database_engine=None) -> int:
    """Backfill identities for existing ChatGPT rows without changing tokens."""

    target_engine = database_engine or default_engine
    from sqlalchemy import inspect

    try:
        table_names = set(inspect(target_engine).get_table_names())
    except Exception:
        return 0
    if "accounts" not in table_names or "account_identities" not in table_names:
        return 0
    with Session(target_engine) as session:
        accounts = session.exec(
            select(AccountModel).where(func.lower(AccountModel.platform) == "chatgpt")
        ).all()
        account_values = [
            (
                int(account.id or 0),
                str(account.platform or ""),
                str(account.email or ""),
                _account_identity_values(account),
                _is_remote_only_projection(account),
                str(account.identity_id or "").strip(),
            )
            for account in accounts
            if account.id and normalize_email(account.email)
        ]
    reconciled = 0
    for account_id, platform, email, values, remote_only, existing_identity_id in account_values:
        if remote_only and existing_identity_id:
            # Remote projections already carry a target-scoped identity from
            # inventory materialization.  Running the credential resolver here
            # would interpret the same mailbox alias on another target as a
            # credential conflict and mark both rows ambiguous.
            reconciled += 1
            continue
        ensure_identity(
            target_engine,
            account_id=account_id,
            platform=platform,
            email=email,
            workspace_id=values["workspace_id"],
            chatgpt_account_id=values["chatgpt_account_id"],
            credential_fingerprint=values["credential_fingerprint"],
            source="startup_reconcile",
        )
        reconciled += 1
    _repair_remote_projection_states(target_engine)
    _repair_shared_workspace_member_states(target_engine)
    return reconciled


__all__ = [
    "IdentityResolution",
    "credential_fingerprint",
    "ensure_identity",
    "ensure_identity_for_model",
    "get_identity",
    "identity_for_account",
    "normalize_alias",
    "normalize_email",
    "move_assignments_to_standby",
    "reconcile_existing_accounts",
    "supersede_other_target_bindings",
]
