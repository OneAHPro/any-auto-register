from datetime import datetime, timezone
import base64
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

from core import db
from core.base_platform import Account


def make_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db.init_account_pool_schema(engine)
    return engine


def test_identity_prefers_workspace_alias_over_credential_fingerprint():
    from services import account_identity as identity_service

    engine = make_engine()
    first = identity_service.ensure_identity(
        engine,
        account_id=7,
        platform="chatgpt",
        email="A@EXAMPLE.COM",
        workspace_id="ws-1",
        chatgpt_account_id="acct-1",
        credential_fingerprint="fp-1",
    )
    second = identity_service.ensure_identity(
        engine,
        account_id=8,
        platform="chatgpt",
        email="a@example.com",
        workspace_id="ws-1",
        chatgpt_account_id="acct-2",
        credential_fingerprint="fp-2",
    )

    assert first.identity_id == second.identity_id
    with Session(engine) as session:
        row = session.get(db.AccountIdentityModel, first.identity_id)
        assert row.current_account_id == 8
        aliases = session.exec(
            select(db.AccountIdentityAliasModel).where(
                db.AccountIdentityAliasModel.identity_id == first.identity_id
            )
        ).all()
    assert {alias.alias_type for alias in aliases} >= {
        "email",
        "workspace_id",
        "chatgpt_account_id",
    }


def test_ambiguous_email_alias_does_not_merge_different_workspaces():
    from services import account_identity as identity_service

    engine = make_engine()
    one = identity_service.ensure_identity(
        engine,
        account_id=1,
        platform="chatgpt",
        email="a@example.com",
        workspace_id="ws-a",
    )
    two = identity_service.ensure_identity(
        engine,
        account_id=2,
        platform="chatgpt",
        email="a@example.com",
        workspace_id="ws-b",
    )

    assert one.identity_id != two.identity_id
    with Session(engine) as session:
        first_row = session.get(db.AccountIdentityModel, one.identity_id)
        second_row = session.get(db.AccountIdentityModel, two.identity_id)
    assert first_row.state == "ambiguous"
    assert second_row.state == "ambiguous"


def test_reconcile_existing_accounts_assigns_stable_identity():
    from services.account_identity import reconcile_existing_accounts

    engine = make_engine()
    account = db.AccountModel(
        platform="chatgpt",
        email="existing@example.com",
        password="password",
        token="access",
        extra_json='{"workspace_id":"workspace-1","refresh_token":"refresh"}',
    )
    with Session(engine) as session:
        session.add(account)
        session.commit()
        session.refresh(account)

    assert reconcile_existing_accounts(engine) == 1

    with Session(engine) as session:
        saved = session.get(db.AccountModel, account.id)
        identity = session.get(db.AccountIdentityModel, saved.identity_id)
    assert saved.identity_id
    assert identity.canonical_email == "existing@example.com"
    assert identity.current_account_id == account.id


def test_reconcile_ignores_malformed_extra_json_without_blocking_other_accounts():
    from services.account_identity import reconcile_existing_accounts

    engine = make_engine()
    with Session(engine) as session:
        session.add_all(
            [
                db.AccountModel(
                    platform="chatgpt",
                    email="broken@example.com",
                    password="password",
                    extra_json="not-json",
                ),
                db.AccountModel(
                    platform="chatgpt",
                    email="healthy@example.com",
                    password="password",
                    extra_json="{}",
                ),
            ]
        )
        session.commit()

    assert reconcile_existing_accounts(engine) == 2


def test_credential_fingerprint_never_returns_the_raw_token():
    from services.account_identity import credential_fingerprint

    fingerprint = credential_fingerprint(
        "chatgpt",
        "a@example.com",
        refresh_token="refresh-secret",
        access_token="access-secret",
    )

    assert len(fingerprint) == 64
    assert "refresh-secret" not in fingerprint
    assert "access-secret" not in fingerprint


def test_rotating_credentials_without_workspace_reuses_the_email_identity():
    from services.account_identity import ensure_identity

    engine = make_engine()
    first = ensure_identity(
        engine,
        account_id=1,
        platform="chatgpt",
        email="rotate@example.com",
        credential_fingerprint="fingerprint-one",
    )
    second = ensure_identity(
        engine,
        account_id=2,
        platform="chatgpt",
        email="rotate@example.com",
        credential_fingerprint="fingerprint-two",
    )

    assert second.identity_id == first.identity_id
    assert second.ambiguous is False


def test_same_workspace_on_different_email_does_not_merge_identities():
    from services.account_identity import ensure_identity, reconcile_existing_accounts

    engine = make_engine()
    with Session(engine) as session:
        for account_id, email in ((1, "one@example.com"), (2, "two@example.com")):
            session.add(db.AccountModel(
                id=account_id, platform="chatgpt", email=email, password="",
                extra_json=json.dumps({
                    "workspace_id": "shared-workspace",
                    "chatgpt_account_id": "shared-workspace",
                }),
            ))
        session.commit()
    first = ensure_identity(
        engine,
        account_id=1,
        platform="chatgpt",
        email="one@example.com",
        workspace_id="shared-workspace",
        chatgpt_account_id="shared-workspace",
    )
    second = ensure_identity(
        engine,
        account_id=2,
        platform="chatgpt",
        email="two@example.com",
        workspace_id="shared-workspace",
        chatgpt_account_id="shared-workspace",
    )

    assert second.identity_id != first.identity_id
    assert first.state == second.state == "active"
    assert reconcile_existing_accounts(engine) == 2
    db.init_account_pool_schema(engine)
    assert reconcile_existing_accounts(engine) == 2
    with Session(engine) as session:
        assert {row.state for row in session.exec(select(db.AccountIdentityModel)).all()} == {"active"}
        for identity_id in (first.identity_id, second.identity_id):
            aliases = session.exec(select(db.AccountIdentityAliasModel).where(
                db.AccountIdentityAliasModel.identity_id == identity_id,
            )).all()
            assert {row.alias_type for row in aliases} >= {"workspace_id", "chatgpt_account_id"}


def _stale_shared_workspace_members(engine):
    """Seed the asymmetric aliases left behind by the old global index."""
    with Session(engine) as session:
        for account_id, email in ((1, "one@example.com"), (2, "two@example.com")):
            identity_id = f"member-{account_id}"
            session.add(db.AccountModel(
                id=account_id, platform="chatgpt", email=email, password="",
                user_id="shared-workspace", identity_id=identity_id,
                extra_json=json.dumps({
                    "workspace_id": "shared-workspace",
                    "mailbox_login_context": {"email": email, "account_id": email},
                    "codex_remote_snapshot": {
                        "email": email, "chatgpt_account_id": "shared-workspace",
                        "effective_workspace_id": "shared-workspace",
                        "target_id": 1, "remote_id": account_id,
                    },
                }),
            ))
            session.add(db.AccountIdentityModel(
                id=identity_id, platform="chatgpt", canonical_email=email,
                current_account_id=account_id, state="ambiguous",
            ))
            for alias_type, value in (("email", email), ("chatgpt_account_id", email)):
                session.add(db.AccountIdentityAliasModel(
                    identity_id=identity_id, alias_type=alias_type, normalized_value=value,
                ))
            session.add(db.AccountTargetBindingModel(
                identity_id=identity_id, local_account_id=account_id,
                target_id=1, remote_account_id=account_id, remote_email=email,
                enabled=False, sync_status="ambiguous", remote_status="ambiguous",
                last_error="身份存在歧义，等待人工确认",
            ))
        session.add(db.AccountIdentityAliasModel(
            identity_id="member-1", alias_type="workspace_id", normalized_value="shared-workspace",
        ))
        session.commit()


def test_reconcile_repairs_stale_shared_workspace_member_ambiguity():
    from services.account_identity import reconcile_existing_accounts
    from services.codex_inventory import materialize_inventory

    engine = make_engine()
    _stale_shared_workspace_members(engine)
    for _ in range(2):
        assert reconcile_existing_accounts(engine) == 2
        with Session(engine) as session:
            assert {row.state for row in session.exec(select(db.AccountIdentityModel)).all()} == {"active"}
            # Inventory reconciliation, not identity repair, enables bindings.
            assert not any(row.enabled for row in session.exec(select(db.AccountTargetBindingModel)).all())
    with Session(engine) as session:
        for account in session.exec(select(db.AccountModel)).all():
            snapshot = account.get_extra()["codex_remote_snapshot"]
            session.add(db.CodexInventorySnapshotModel(
                target_id=1, remote_id=account.id,
                summary_json=json.dumps({**snapshot, "status": "active", "enabled": True}),
            ))
        session.commit()
    materialize_inventory(engine)
    with Session(engine) as session:
        bindings = session.exec(select(db.AccountTargetBindingModel)).all()
        assert len(bindings) == 2
        assert all(binding.enabled and binding.sync_status == "synced" for binding in bindings)
        assignments = session.exec(select(db.AccountAssignmentModel)).all()
        assert len(assignments) == 2
        assert all(assignment.state == "active" for assignment in assignments)


@pytest.mark.parametrize("conflict", [
    "same_email", "local_workspace", "remote_workspace", "remote_email",
    "historical_workspace", "binding_mismatch", "fingerprint", "token_workspace",
])
def test_shared_workspace_repair_preserves_real_conflicts(conflict):
    from services.account_identity import reconcile_existing_accounts

    engine = make_engine()
    _stale_shared_workspace_members(engine)
    with Session(engine) as session:
        account = session.get(db.AccountModel, 1)
        extra = account.get_extra()
        if conflict == "same_email":
            session.add(db.AccountIdentityModel(
                id="other-workspace", platform="chatgpt", canonical_email=account.email,
                state="ambiguous",
            ))
            session.add(db.AccountIdentityAliasModel(
                identity_id="other-workspace", alias_type="email", normalized_value=account.email,
            ))
        elif conflict == "local_workspace":
            extra["account_id"] = "another-workspace"
        elif conflict == "remote_workspace":
            extra["codex_remote_snapshot"]["chatgpt_account_id"] = "another-workspace"
        elif conflict == "remote_email":
            extra["codex_remote_snapshot"]["email"] = "wrong@example.com"
        elif conflict == "historical_workspace":
            session.add(db.AccountIdentityAliasModel(
                identity_id="member-1", alias_type="workspace_id", normalized_value="another-workspace",
            ))
        elif conflict == "binding_mismatch":
            binding = session.exec(select(db.AccountTargetBindingModel).where(
                db.AccountTargetBindingModel.identity_id == "member-1",
            )).one()
            binding.last_error = "本地与远端稳定身份不一致"
            session.add(binding)
        elif conflict == "token_workspace":
            payload = base64.urlsafe_b64encode(json.dumps({
                "https://api.openai.com/auth": {"chatgpt_account_id": "another-workspace"},
            }).encode()).decode().rstrip("=")
            account.token = f"header.{payload}.signature"
        elif conflict == "fingerprint":
            from services.account_identity import _account_identity_values

            fingerprint = _account_identity_values(account)["credential_fingerprint"]
            session.add(db.AccountIdentityAliasModel(
                identity_id="member-2", alias_type="credential_fingerprint", normalized_value=fingerprint,
            ))
        account.set_extra(extra)
        session.add(account)
        session.commit()

    reconcile_existing_accounts(engine)

    with Session(engine) as session:
        assert session.get(db.AccountIdentityModel, "member-1").state == "ambiguous"
        if conflict == "fingerprint":
            assert session.get(db.AccountIdentityModel, "member-2").state == "ambiguous"


def test_schema_upgrade_removes_global_workspace_uniqueness():
    from services.account_identity import ensure_identity

    engine = make_engine()
    ensure_identity(engine, account_id=1, platform="chatgpt", email="one@example.com", workspace_id="shared")
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX uq_account_identity_alias_platform_type_value")
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX uq_account_identity_alias_platform_type_value "
            "ON account_identity_aliases (platform, alias_type, normalized_value) WHERE alias_type != 'email'"
        )
    db.init_account_pool_schema(engine)
    second = ensure_identity(engine, account_id=2, platform="chatgpt", email="two@example.com", workspace_id="shared")
    assert second.state == "active"


def test_same_fingerprint_across_emails_remains_a_conflict():
    from services.account_identity import ensure_identity

    engine = make_engine()
    one = ensure_identity(engine, account_id=1, platform="chatgpt", email="one@example.com",
                          workspace_id="shared", credential_fingerprint="shared-fingerprint")
    two = ensure_identity(engine, account_id=2, platform="chatgpt", email="two@example.com",
                          workspace_id="shared", credential_fingerprint="shared-fingerprint")
    assert two.state == "ambiguous"
    with Session(engine) as session:
        assert session.get(db.AccountIdentityModel, one.identity_id).state == "ambiguous"


@pytest.mark.parametrize("conflict", ["remote_binding", "fingerprint", "identity_binding", "assignment"])
def test_shared_workspace_repair_preserves_conflicts_after_migration_cleanup(conflict):
    from services.account_identity import reconcile_existing_accounts

    engine = make_engine()
    _stale_shared_workspace_members(engine)
    with engine.begin() as connection:
        if conflict == "remote_binding":
            connection.exec_driver_sql("DROP INDEX uq_account_target_binding_remote_id")
            connection.exec_driver_sql("UPDATE account_target_bindings SET remote_account_id = 1")
        elif conflict == "fingerprint":
            connection.exec_driver_sql("DROP INDEX uq_account_identity_alias_platform_type_value")
            connection.exec_driver_sql(
                "INSERT INTO account_identity_aliases "
                "(identity_id, platform, alias_type, normalized_value, source, first_seen_at, last_seen_at) "
                "SELECT id, platform, 'credential_fingerprint', 'legacy-shared-fingerprint', "
                "'legacy_import', created_at, updated_at FROM account_identities"
            )
        elif conflict == "identity_binding":
            connection.exec_driver_sql("DROP INDEX uq_account_target_binding_identity_target")
            connection.exec_driver_sql(
                "INSERT INTO account_target_bindings "
                "(identity_id, local_account_id, target_id, remote_account_id, remote_email, "
                "sync_status, remote_status, enabled, credential_revision, last_error, created_at, updated_at) "
                "SELECT identity_id, local_account_id, target_id, 3, remote_email, "
                "sync_status, remote_status, enabled, credential_revision, last_error, created_at, updated_at "
                "FROM account_target_bindings WHERE identity_id = 'member-1'"
            )
        else:
            connection.exec_driver_sql("DROP INDEX uq_account_assignment_current_identity")
    if conflict == "assignment":
        with Session(engine) as session:
            for target_id in (1, 2):
                session.add(db.AccountAssignmentModel(
                    identity_id="member-1", local_account_id=1, target_id=target_id,
                    pool_id="PUBLIC_POOL", state="active",
                ))
            session.commit()

    for _ in range(2):
        db.init_account_pool_schema(engine)
        reconcile_existing_accounts(engine)
        with Session(engine) as session:
            first = session.get(db.AccountIdentityModel, "member-1")
            assert first.state == "ambiguous"
            assert first.ambiguity_reason.startswith("legacy_duplicate_")
            if conflict in {"remote_binding", "fingerprint"}:
                second = session.get(db.AccountIdentityModel, "member-2")
                assert second.state == "ambiguous"
                assert second.ambiguity_reason == first.ambiguity_reason


def test_fingerprint_conflict_remains_quarantined_after_credentials_rotate():
    from services.account_identity import ensure_identity, reconcile_existing_accounts

    engine = make_engine()
    _stale_shared_workspace_members(engine)
    for account_id, email in ((1, "one@example.com"), (2, "two@example.com")):
        ensure_identity(
            engine, account_id=account_id, platform="chatgpt", email=email,
            workspace_id="shared-workspace", credential_fingerprint="old-conflicting-fingerprint",
        )

    reconcile_existing_accounts(engine)

    with Session(engine) as session:
        identities = session.exec(select(db.AccountIdentityModel)).all()
        assert len(identities) == 2
        assert all(row.state == "ambiguous" for row in identities)
        assert all(row.ambiguity_reason == "credential_fingerprint_conflict" for row in identities)


def test_exact_workspace_alias_reuses_identity_after_email_conflict():
    from services.account_identity import ensure_identity

    engine = make_engine()
    first = ensure_identity(
        engine,
        account_id=1,
        platform="chatgpt",
        email="same@example.com",
        workspace_id="workspace-a",
    )
    ensure_identity(
        engine,
        account_id=2,
        platform="chatgpt",
        email="same@example.com",
        workspace_id="workspace-b",
    )
    resolved = ensure_identity(
        engine,
        account_id=3,
        platform="chatgpt",
        email="same@example.com",
        workspace_id="workspace-a",
    )

    assert resolved.identity_id == first.identity_id


def test_save_account_assigns_a_stable_identity():
    from unittest import mock

    engine = make_engine()
    account = Account(
        platform="chatgpt",
        email="saved@example.com",
        password="password",
        token="access",
        extra={
            "access_token": "access",
            "refresh_token": "refresh",
            "workspace_id": "workspace-1",
        },
    )

    with mock.patch.object(db, "engine", engine):
        saved = db.save_account(account)

    assert saved.identity_id
    with Session(engine) as session:
        identity = session.get(db.AccountIdentityModel, saved.identity_id)
    assert identity is not None
    assert identity.current_account_id == saved.id


def test_reconcile_does_not_change_account_updated_at_after_identity_exists():
    from services.account_identity import reconcile_existing_accounts

    engine = make_engine()
    account = db.AccountModel(
        platform="chatgpt",
        email="stable@example.com",
        password="password",
        extra_json="{}",
    )
    with Session(engine) as session:
        session.add(account)
        session.commit()
        session.refresh(account)
    reconcile_existing_accounts(engine)
    with Session(engine) as session:
        first = session.get(db.AccountModel, account.id)
        first_updated_at = first.updated_at

    reconcile_existing_accounts(engine)

    with Session(engine) as session:
        second = session.get(db.AccountModel, account.id)
    assert second.updated_at == first_updated_at


@pytest.mark.parametrize("ambiguity_reason", ["", "legacy_duplicate_remote_binding"])
def test_reconcile_keeps_identical_remote_projection_identities_active(ambiguity_reason):
    """Nested provider aliases must not become credential conflicts on boot."""

    from services.account_identity import reconcile_existing_accounts

    engine = make_engine()
    email = "remote-shared@example.com"
    provider_id = "provider-account-1"
    with Session(engine) as session:
        for account_id, target_id, remote_id in ((1, 1, 101), (2, 2, 202)):
            identity_id = f"codex2api:{target_id}:{remote_id}"
            session.add(
                db.AccountModel(
                    id=account_id,
                    platform="chatgpt",
                    email=email,
                    password="",
                    identity_id=identity_id,
                    extra_json=json.dumps(
                        {
                            "remote_only": True,
                            "remote_target_id": target_id,
                            "remote_id": remote_id,
                            "codex_remote_snapshot": {
                                "email": email,
                                "chatgpt_account_id": provider_id,
                                "effective_workspace_id": provider_id,
                                "target_id": target_id,
                                "remote_id": remote_id,
                            },
                        }
                    ),
                )
            )
            session.add(
                db.AccountIdentityModel(
                    id=identity_id,
                    platform="chatgpt",
                    canonical_email=email,
                    current_account_id=account_id,
                    state="ambiguous",
                    ambiguity_reason=ambiguity_reason,
                )
            )
        session.commit()

    assert reconcile_existing_accounts(engine) == 2

    with Session(engine) as session:
        identities = session.exec(
            select(db.AccountIdentityModel).order_by(db.AccountIdentityModel.id)
        ).all()
    expected_state = "ambiguous" if ambiguity_reason else "active"
    assert [identity.state for identity in identities] == [expected_state, expected_state]


def test_concurrent_identity_resolution_reuses_one_identity(tmp_path):
    from services.account_identity import ensure_identity

    database_path = tmp_path / "identity-race.db"
    engine = db._create_database_engine(f"sqlite:///{database_path}")
    db.init_account_pool_schema(engine)

    def resolve(index):
        return ensure_identity(
            engine,
            account_id=index + 1,
            platform="chatgpt",
            email="race@example.com",
        ).identity_id

    with ThreadPoolExecutor(max_workers=8) as executor:
        identity_ids = list(executor.map(resolve, range(20)))

    assert len(set(identity_ids)) == 1
