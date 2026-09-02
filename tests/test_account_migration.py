from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

from core import db
from services.account_identity import ensure_identity


NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)


class FakeTarget:
    def __init__(self, remote_id, email="a@example.com"):
        self.remote_id = remote_id
        self.email = email
        self.calls = []
        self.active_requests = 0
        self.enabled = True
        self.fail_on = ""
        self.imported = False
        self.on_test = None
        self.rows_override = None
        self.deleted = False

    def _fail(self, name):
        if self.fail_on == name or (
            isinstance(self.fail_on, set) and name in self.fail_on
        ):
            raise RuntimeError(f"injected {name} failure")

    def list_accounts(self):
        self.calls.append(("list",))
        self._fail("list")
        if self.rows_override is not None:
            return list(self.rows_override)
        rows = []
        if not self.deleted and (self.imported or self.remote_id == 55):
            rows.append(
                {
                    "id": self.remote_id,
                    "email": self.email,
                    "status": "active" if self.enabled else "disabled",
                    "enabled": self.enabled,
                    "active_requests": self.active_requests,
                    "workspace_id": "ws-1",
                    "usage_percent_7d": 10,
                    "billed_7d": 100,
                }
            )
        return rows

    def set_locked(self, remote_id, locked):
        self.calls.append(("lock", remote_id, locked))
        self._fail("lock")
        return {"message": "ok"}

    def set_enabled(self, remote_id, enabled):
        self.calls.append(("enable", remote_id, enabled))
        self._fail("enable")
        if enabled:
            self._fail("enable_true")
        self.enabled = bool(enabled)
        return {"message": "ok"}

    def wait_for_zero_active_requests(self, remote_id, **kwargs):
        self.calls.append(("drain", remote_id))
        self._fail("drain")
        return self.active_requests == 0

    def import_full_json(self, payload):
        self.calls.append(("import",))
        self._fail("import")
        self.imported = True
        return {"success": 1, "failed": 0, "remote_id": self.remote_id}

    def test_account(self, remote_id):
        self.calls.append(("test", remote_id))
        self._fail("test")
        if self.on_test is not None:
            self.on_test()
        return {"success": True}

    def delete_account(self, remote_id):
        self.calls.append(("delete", remote_id))
        self._fail("delete")
        self.imported = False
        self.enabled = False
        self.deleted = True
        return {"message": "deleted"}

    def restore_account(self, remote_id):
        self.calls.append(("restore", remote_id))
        self.enabled = True
        self.deleted = False
        return {"message": "restored"}


def make_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db.init_account_pool_schema(engine)
    return engine


def seed_account(engine):
    account = db.AccountModel(
        platform="chatgpt",
        email="a@example.com",
        password="password",
        token="access-token",
        extra_json='{"refresh_token":"refresh-token","access_token":"access-token","workspace_id":"ws-1"}',
    )
    with Session(engine) as session:
        session.add(account)
        session.commit()
        session.refresh(account)
    identity = ensure_identity(
        engine,
        account_id=account.id,
        platform="chatgpt",
        email=account.email,
        workspace_id="ws-1",
    )
    with Session(engine) as session:
        session.add(
            db.AccountAssignmentModel(
                identity_id=identity.identity_id,
                local_account_id=account.id,
                pool_id="PUBLIC_POOL",
                target_id=1,
                state="active",
                assignment_version=3,
            )
        )
        session.add(
            db.AccountTargetBindingModel(
                identity_id=identity.identity_id,
                local_account_id=account.id,
                target_id=1,
                remote_account_id=55,
                remote_email=account.email,
                sync_status="synced",
                remote_status="active",
                enabled=True,
            )
        )
        session.commit()
    return account.id, identity.identity_id


def make_migration(
    engine,
    identity_id,
    account_id,
    expected_version=3,
    expected_revision="",
):
    from services import account_migration

    return account_migration.plan_migration(
        engine,
        identity_id=identity_id,
        local_account_id=account_id,
        source_target_id=1,
        destination_target_id=2,
        expected_assignment_version=expected_version,
        expected_credential_revision=expected_revision,
    )


def test_migration_disables_source_drains_uploads_verifies_and_enables_destination():
    from services import account_migration

    engine = make_engine()
    account_id, identity_id = seed_account(engine)
    migration_id = make_migration(engine, identity_id, account_id)
    targets = {1: FakeTarget(55), 2: FakeTarget(77)}

    result = account_migration.run_migration(
        engine,
        migration_id,
        clients=targets,
        now=NOW,
        drain_timeout_seconds=2,
        sleep_fn=lambda seconds: None,
    )

    assert result.state == "committed"
    assert targets[1].calls[:3] == [
        ("lock", 55, True),
        ("enable", 55, False),
        ("drain", 55),
    ]
    assert ("import",) in targets[2].calls
    assert ("enable", 77, True) in targets[2].calls
    with Session(engine) as session:
        assignments = session.exec(select(db.AccountAssignmentModel)).all()
        events = session.exec(select(db.AccountAssignmentEventModel)).all()
        destination_binding = session.exec(
            select(db.AccountTargetBindingModel).where(
                db.AccountTargetBindingModel.target_id == 2
            )
        ).one()
    assert assignments[0].target_id == 2
    assert events[0].event_type == "migration_committed"
    assert destination_binding.enabled is True
    assert destination_binding.sync_status == "synced"
    assert destination_binding.remote_status == "active"


def test_upload_failure_restores_source_and_removes_destination():
    from services import account_migration

    engine = make_engine()
    account_id, identity_id = seed_account(engine)
    migration_id = make_migration(engine, identity_id, account_id)
    targets = {1: FakeTarget(55), 2: FakeTarget(77)}
    targets[2].fail_on = "import"

    result = account_migration.run_migration(
        engine,
        migration_id,
        clients=targets,
        now=NOW,
        drain_timeout_seconds=2,
        sleep_fn=lambda seconds: None,
    )

    assert result.state == "rolled_back"
    assert ("enable", 55, True) in targets[1].calls
    assert ("delete", 77) not in targets[2].calls


def test_destination_verification_can_finish_before_first_quota_snapshot():
    from services import account_migration

    engine = make_engine()
    account_id, identity_id = seed_account(engine)
    migration_id = make_migration(engine, identity_id, account_id)
    targets = {1: FakeTarget(55), 2: FakeTarget(77)}
    targets[2].rows_override = [
        {
            "id": 77,
            "email": "a@example.com",
            "status": "active",
            "enabled": True,
            "workspace_id": "ws-1",
        }
    ]

    result = account_migration.run_migration(
        engine,
        migration_id,
        clients=targets,
        now=NOW,
        drain_timeout_seconds=2,
        sleep_fn=lambda seconds: None,
    )

    assert result.state == "committed"


def test_drain_timeout_never_deletes_source():
    from services import account_migration

    engine = make_engine()
    account_id, identity_id = seed_account(engine)
    migration_id = make_migration(engine, identity_id, account_id)
    targets = {1: FakeTarget(55), 2: FakeTarget(77)}
    targets[1].active_requests = 2

    result = account_migration.run_migration(
        engine,
        migration_id,
        clients=targets,
        now=NOW,
        drain_timeout_seconds=1,
        sleep_fn=lambda seconds: None,
    )

    assert result.state == "rolled_back"
    assert ("delete", 55) not in targets[1].calls
    assert ("enable", 55, True) in targets[1].calls


def test_assignment_version_conflict_stops_before_remote_cleanup():
    from services import account_migration

    engine = make_engine()
    account_id, identity_id = seed_account(engine)
    migration_id = make_migration(engine, identity_id, account_id, expected_version=3)
    targets = {1: FakeTarget(55), 2: FakeTarget(77)}
    with Session(engine) as session:
        assignment = session.get(db.AccountAssignmentModel, 1)
        assignment.assignment_version = 4
        session.add(assignment)
        session.commit()

    result = account_migration.run_migration(
        engine,
        migration_id,
        clients=targets,
        now=NOW,
        drain_timeout_seconds=2,
        sleep_fn=lambda seconds: None,
    )

    assert result.state == "rollback_required"
    assert targets[1].calls == []
    assert targets[2].calls == []


def test_credential_revision_conflict_stops_before_remote_operations():
    from services import account_migration

    engine = make_engine()
    account_id, identity_id = seed_account(engine)
    with Session(engine) as session:
        session.add(
            db.ChatGPTAuthStateModel(
                account_id=account_id,
                credential_revision="revision-1",
            )
        )
        session.commit()
    migration_id = make_migration(
        engine,
        identity_id,
        account_id,
        expected_revision="revision-1",
    )
    with Session(engine) as session:
        auth_state = session.exec(
            select(db.ChatGPTAuthStateModel).where(
                db.ChatGPTAuthStateModel.account_id == account_id
            )
        ).one()
        auth_state.credential_revision = "revision-2"
        session.add(auth_state)
        session.commit()
    targets = {1: FakeTarget(55), 2: FakeTarget(77)}

    result = account_migration.run_migration(
        engine,
        migration_id,
        clients=targets,
        now=NOW,
        drain_timeout_seconds=2,
        sleep_fn=lambda seconds: None,
    )

    assert result.state == "rollback_required"
    assert targets[1].calls == []
    assert targets[2].calls == []


def test_duplicate_email_remote_rows_are_disambiguated_by_workspace_alias():
    from services import account_migration

    engine = make_engine()
    _account_id, identity_id = seed_account(engine)
    target = FakeTarget(77)
    target.rows_override = [
        {
            "id": 70,
            "email": "a@example.com",
            "workspace_id": "other-workspace",
        },
        {
            "id": 77,
            "email": "a@example.com",
            "workspace_id": "ws-1",
        },
    ]

    remote_id = account_migration._find_remote_by_identity(
        target,
        "a@example.com",
        identity_id,
        engine,
    )

    assert remote_id == 77


def test_failed_destination_cleanup_requires_manual_rollback():
    from services import account_migration

    engine = make_engine()
    account_id, identity_id = seed_account(engine)
    migration_id = make_migration(engine, identity_id, account_id)
    targets = {1: FakeTarget(55), 2: FakeTarget(77)}
    targets[2].fail_on = {"test", "delete"}

    result = account_migration.run_migration(
        engine,
        migration_id,
        clients=targets,
        now=NOW,
        drain_timeout_seconds=2,
        sleep_fn=lambda seconds: None,
    )

    assert result.state == "rollback_required"
    assert ("enable", 55, True) in targets[1].calls


def test_restart_reconciliation_is_idempotent():
    from services import account_migration

    engine = make_engine()
    account_id, identity_id = seed_account(engine)
    migration_id = make_migration(engine, identity_id, account_id)
    targets = {1: FakeTarget(55), 2: FakeTarget(77)}

    partial = account_migration.run_migration(
        engine,
        migration_id,
        clients=targets,
        now=NOW,
        stop_after="verifying",
        drain_timeout_seconds=2,
        sleep_fn=lambda seconds: None,
    )
    assert partial.state == "verifying"
    resumed = account_migration.resume_pending_migrations(
        engine,
        clients=targets,
        now=NOW,
        sleep_fn=lambda seconds: None,
    )

    assert resumed[0].state == "committed"


def test_destination_enable_failure_restores_deleted_source():
    from services import account_migration

    engine = make_engine()
    account_id, identity_id = seed_account(engine)
    migration_id = make_migration(engine, identity_id, account_id)
    targets = {1: FakeTarget(55), 2: FakeTarget(77)}
    targets[2].fail_on = "enable_true"

    result = account_migration.run_migration(
        engine,
        migration_id,
        clients=targets,
        now=NOW,
        drain_timeout_seconds=2,
        sleep_fn=lambda seconds: None,
    )

    assert result.state == "rolled_back"
    assert ("restore", 55) in targets[1].calls


def test_restart_after_source_lock_repeats_idempotent_drain_steps():
    from services import account_migration

    engine = make_engine()
    account_id, identity_id = seed_account(engine)
    migration_id = make_migration(engine, identity_id, account_id)
    targets = {1: FakeTarget(55), 2: FakeTarget(77)}

    partial = account_migration.run_migration(
        engine,
        migration_id,
        clients=targets,
        now=NOW,
        stop_after="locking",
        drain_timeout_seconds=2,
        sleep_fn=lambda seconds: None,
    )
    assert partial.state == "locking"

    resumed = account_migration.resume_pending_migrations(
        engine,
        clients=targets,
        now=NOW,
        sleep_fn=lambda seconds: None,
    )

    assert resumed[0].state == "committed"
    assert ("enable", 55, False) in targets[1].calls


def test_source_cleanup_failure_is_visible_as_cleanup_pending():
    from services import account_migration

    engine = make_engine()
    account_id, identity_id = seed_account(engine)
    migration_id = make_migration(engine, identity_id, account_id)
    targets = {1: FakeTarget(55), 2: FakeTarget(77)}
    targets[1].fail_on = "delete"

    result = account_migration.run_migration(
        engine,
        migration_id,
        clients=targets,
        now=NOW,
        drain_timeout_seconds=2,
        sleep_fn=lambda seconds: None,
    )

    assert result.state == "cleanup_pending"
    assert ("enable", 77, True) in targets[2].calls
    assert ("enable", 55, True) not in targets[1].calls


def test_assignment_conflict_after_remote_verification_rolls_back_remote_state():
    from services import account_migration

    engine = make_engine()
    account_id, identity_id = seed_account(engine)
    migration_id = make_migration(engine, identity_id, account_id)
    targets = {1: FakeTarget(55), 2: FakeTarget(77)}

    def bump_assignment_version():
        with Session(engine) as session:
            assignment = session.get(db.AccountAssignmentModel, 1)
            assignment.assignment_version += 1
            session.add(assignment)
            session.commit()

    targets[2].on_test = bump_assignment_version
    result = account_migration.run_migration(
        engine,
        migration_id,
        clients=targets,
        now=NOW,
        drain_timeout_seconds=2,
        sleep_fn=lambda seconds: None,
    )

    assert result.state == "rollback_required"
    assert ("enable", 55, True) in targets[1].calls
    assert ("delete", 77) in targets[2].calls


def test_cleanup_pending_resume_retries_source_delete_before_commit():
    from services import account_migration

    engine = make_engine()
    account_id, identity_id = seed_account(engine)
    migration_id = make_migration(engine, identity_id, account_id)
    targets = {1: FakeTarget(55), 2: FakeTarget(77)}
    targets[1].fail_on = "delete"

    first = account_migration.run_migration(
        engine,
        migration_id,
        clients=targets,
        now=NOW,
        drain_timeout_seconds=2,
        sleep_fn=lambda seconds: None,
    )
    assert first.state == "cleanup_pending"

    targets[1].fail_on = ""
    resumed = account_migration.resume_pending_migrations(
        engine,
        clients=targets,
        now=NOW,
        sleep_fn=lambda seconds: None,
    )

    assert resumed[0].state == "committed"
    assert targets[1].calls.count(("delete", 55)) >= 2
