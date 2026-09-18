from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

from api.accounts import AccountCreate, ImportRequest, create_account, import_accounts, list_accounts
from core import db


def make_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db.init_account_pool_schema(engine)
    return engine


def test_manual_account_creation_marks_row_as_local_and_creates_identity():
    engine = make_engine()
    with Session(engine) as session:
        response = create_account(
            AccountCreate(
                platform="chatgpt",
                email=" Local@Example.com ",
                password="password",
            ),
            session=session,
        )

    assert response["account_source"] == "local"
    with Session(engine) as session:
        account = session.exec(select(db.AccountModel)).one()
        assert account.email == "local@example.com"
        assert account.account_source == "local"
        assert account.identity_id
        assert session.get(db.AccountIdentityModel, account.identity_id) is not None


def test_imported_account_is_explicitly_local():
    engine = make_engine()
    with Session(engine) as session:
        import_accounts(
            body=ImportRequest(platform="chatgpt", lines=["user@example.com----password"]),
            session=session,
        )
        account = session.exec(select(db.AccountModel)).one()
        assert account.account_source == "local"


def test_create_rejects_duplicate_local_email():
    engine = make_engine()
    with Session(engine) as session:
        create_account(
            AccountCreate(platform="chatgpt", email="user@example.com", password="one"),
            session=session,
        )
        try:
            create_account(
                AccountCreate(platform="chatgpt", email="USER@example.com", password="two"),
                session=session,
            )
        except Exception as exc:
            assert getattr(exc, "status_code", None) == 409
        else:
            raise AssertionError("duplicate local account should be rejected")


def test_create_allows_local_credentials_when_remote_projection_has_same_email():
    engine = make_engine()
    with Session(engine) as session:
        session.add(
            db.AccountModel(
                platform="chatgpt",
                email="shared@example.com",
                password="",
                account_source="codex2api",
                extra_json='{"remote_only":true}',
            )
        )
        session.commit()
        response = create_account(
            AccountCreate(platform="chatgpt", email="shared@example.com", password="local-password"),
            session=session,
        )

    assert response["account_source"] == "local"
    with Session(engine) as session:
        rows = session.exec(select(db.AccountModel)).all()
        assert len(rows) == 2


def test_account_summary_separates_local_and_remote_sources():
    engine = make_engine()
    with Session(engine) as session:
        session.add_all(
            [
                db.AccountModel(
                    platform="chatgpt",
                    email="local@example.com",
                    password="password",
                    extra_json='{"account_type":"chatgpt_password"}',
                    account_source="local",
                ),
                db.AccountModel(
                    platform="chatgpt",
                    email="remote@example.com",
                    password="",
                    account_source="codex2api",
                    extra_json='{"remote_only":true,"remote_status":"active"}',
                ),
            ]
        )
        session.commit()
        result = list_accounts(platform="chatgpt", page=1, page_size=20, session=session)

    assert result["source_summary"]["local"]["total"] == 1
    assert result["source_summary"]["remote"]["total"] == 1
    assert result["source_summary"]["remote"]["normal"] == 1


def test_schema_migration_promotes_legacy_remote_marker_to_source_column():
    engine = make_engine()
    with Session(engine) as session:
        session.add(
            db.AccountModel(
                platform="chatgpt",
                email="legacy-remote@example.com",
                password="",
                account_source="local",
                extra_json='{"remote_only": true}',
            )
        )
        session.commit()
    db.init_account_pool_schema(engine)
    with Session(engine) as session:
        account = session.exec(select(db.AccountModel)).one()
        assert account.account_source == "codex2api"
