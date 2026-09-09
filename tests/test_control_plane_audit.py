from sqlmodel import Session, create_engine, SQLModel
from sqlalchemy.pool import StaticPool

from core import db
from services.control_plane_audit import run_binding_audit


def make_engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    return engine


def test_audit_reports_duplicate_bindings_assignments_and_statuses():
    engine = make_engine()
    with Session(engine) as session:
        session.add(db.AccountIdentityModel(id="i-1", platform="chatgpt", canonical_email="a@example.com"))
        session.add_all([
            db.AccountTargetBindingModel(identity_id="i-1", local_account_id=1, target_id=2, remote_account_id=7),
            db.AccountTargetBindingModel(identity_id="i-1", local_account_id=2, target_id=2, remote_account_id=8, sync_status="ambiguous", enabled=False),
        ])
        session.add_all([
            db.AccountAssignmentModel(identity_id="i-1", local_account_id=1, pool_id="p", target_id=2, state="active"),
            db.AccountAssignmentModel(identity_id="i-1", local_account_id=2, pool_id="p", target_id=2, state="standby"),
        ])
        session.commit()
    report = run_binding_audit(engine)
    assert report["binding_count"] == 2
    assert report["duplicate_identity_target"]["i-1|2"] == [1, 2]
    assert report["duplicate_active_assignments"]["i-1"] == [1, 2]
    assert report["ambiguous_identities"] == ["i-1"]
    assert report["disabled_bindings"] == 1


def test_audit_is_clean_for_unique_current_records():
    engine = make_engine()
    with Session(engine) as session:
        session.add(db.AccountTargetBindingModel(identity_id="i-1", local_account_id=1, target_id=2, remote_account_id=7, sync_status="synced"))
        session.add(db.AccountAssignmentModel(identity_id="i-1", local_account_id=1, pool_id="p", target_id=2, state="active"))
        session.commit()
    report = run_binding_audit(engine)
    assert report["duplicate_identity_target"] == {}
    assert report["duplicate_remote"] == {}
    assert report["duplicate_active_assignments"] == {}
    assert report["ambiguous_identities"] == []


def test_audit_issue_count_counts_rows_and_unbound_ambiguous_identities():
    """The count must describe findings, including an identity without a binding."""

    engine = make_engine()
    with Session(engine) as session:
        session.add_all([
            db.Codex2APITargetModel(
                id=1,
                name="target-1",
                base_url="https://target-1",
                admin_key_ref="target-1-key",
            ),
            db.AccountIdentityModel(
                id="i-current",
                platform="chatgpt",
                canonical_email="current@example.com",
            ),
            db.AccountIdentityModel(
                id="i-ambiguous",
                platform="chatgpt",
                canonical_email="ambiguous@example.com",
                state="ambiguous",
            ),
            db.AccountTargetBindingModel(
                identity_id="i-current",
                local_account_id=0,
                target_id=1,
                remote_account_id=7,
                sync_status="synced",
                enabled=True,
            ),
            db.AccountTargetBindingModel(
                identity_id="i-current",
                local_account_id=0,
                target_id=1,
                remote_account_id=8,
                sync_status="synced",
                enabled=True,
            ),
            db.AccountAssignmentModel(
                identity_id="i-current",
                local_account_id=0,
                pool_id="p",
                target_id=1,
                state="active",
            ),
            db.AccountAssignmentModel(
                identity_id="i-current",
                local_account_id=0,
                pool_id="p",
                target_id=1,
                state="draining",
            ),
        ])
        session.commit()

    report = run_binding_audit(engine)

    assert report["ambiguous_identities"] == ["i-ambiguous"]
    assert report["duplicate_identity_target"] == {"i-current|1": [1, 2]}
    assert report["duplicate_active_assignments"] == {"i-current": [1, 2]}
    # Two duplicate binding rows + two duplicate assignments + one unbound
    # ambiguous identity.
    assert report["issue_count"] == 5
