from datetime import datetime, timezone

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
