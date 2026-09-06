from sqlmodel import Session, SQLModel, create_engine
from sqlalchemy.pool import StaticPool

from api.accounts import _account_operational_summary, list_accounts
from core.db import AccountAssignmentModel, AccountModel, init_account_pool_schema


def _live_test_engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    init_account_pool_schema(engine)
    return engine


def _remote_row(**overrides):
    row = {
        "target_id": 1,
        "remote_id": 90210,
        "email": "remote@example.com",
        "chatgpt_account_id": "remote-account",
        "effective_workspace_id": "remote-workspace",
        "plan_type": "pro",
        "remote_status": "active",
        "enabled": True,
        "locked": False,
        "usage_percent_7d": 42,
        "billed_7d": 18.75,
        "usage_7d_requests": 12,
        "updated_at": "2026-09-06T00:00:00+00:00",
    }
    row.update(overrides)
    return row


def test_account_list_includes_full_filtered_operational_summary():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    rows = [
        AccountModel(platform="chatgpt", email="normal@example.com", password="p", status="registered", extra_json='{"account_type":"chatgpt_password"}'),
        AccountModel(platform="chatgpt", email="limited@example.com", password="p", status="registered", extra_json='{"account_type":"chatgpt_password","remote_status":"rate_limited","quota":{"window":"5h"}}'),
        AccountModel(platform="chatgpt", email="invalid@example.com", password="p", status="invalid", extra_json='{"account_type":"chatgpt_password"}'),
        AccountModel(platform="chatgpt", email="error@example.com", password="p", status="registered", extra_json='{"account_type":"chatgpt_password","chatgpt_local":{"auth":{"state":"probe_failed"}}}'),
    ]
    with Session(engine) as session:
        session.add_all(rows)
        session.commit()
        result = list_accounts(platform="chatgpt", page=1, page_size=1, session=session)

    assert result["total"] == 4
    assert result["summary"] == {
        "total": 4,
        "normal": 1,
        "scheduling": 0,
        "rate_limited": 1,
        "rate_limited_5h": 1,
        "rate_limited_7d": 0,
        "abnormal": 2,
        "auth_invalid": 1,
        "errors": 1,
    }


def test_account_summary_uses_control_plane_assignments_and_expired_statuses():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    rows = [
        AccountModel(
            platform="chatgpt",
            email="draining@example.com",
            password="p",
            status="registered",
            identity_id="identity-draining",
            extra_json='{"account_type":"chatgpt_password"}',
        ),
        AccountModel(
            platform="chatgpt",
            email="expired@example.com",
            password="p",
            status="expired",
            extra_json='{"account_type":"chatgpt_password"}',
        ),
    ]
    with Session(engine) as session:
        session.add_all(rows)
        session.add(
            AccountAssignmentModel(
                identity_id="identity-draining",
                local_account_id=0,
                pool_id="PUBLIC_POOL",
                target_id=1,
                state="draining",
                lease_owner="migration-worker",
            )
        )
        session.commit()
        result = list_accounts(platform="chatgpt", page=1, page_size=1, session=session)

    assert result["total"] == 2
    assert result["summary"]["scheduling"] == 1
    assert result["summary"]["abnormal"] == 1
    assert result["summary"]["errors"] == 1


def test_account_summary_marks_disabled_remote_rows_as_errors():
    result = _account_operational_summary(
        [],
        [
            {
                "identity_id": "codex2api:1:42",
                "status": "active",
                "remote_enabled": "false",
                "remote_locked": False,
            }
        ],
        {},
    )

    assert result["total"] == 1
    assert result["abnormal"] == 1
    assert result["errors"] == 1


def test_account_summary_prioritizes_auth_invalid_over_generic_error():
    result = _account_operational_summary(
        [],
        [
            {
                "status": "unauthorized",
                "remote_enabled": True,
                "remote_locked": False,
            }
        ],
        {},
    )

    assert result["abnormal"] == 1
    assert result["auth_invalid"] == 1
    assert result["errors"] == 0

    banned = _account_operational_summary(
        [],
        [{"status": "active", "chatgpt_display": {"remote_status": "banned_like"}}],
        {},
    )
    assert banned["abnormal"] == 1
    assert banned["errors"] == 1


def test_account_summary_finds_assignment_by_local_account_id_for_legacy_rows():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    account = AccountModel(
        platform="chatgpt",
        email="legacy-assignment@example.com",
        password="p",
        status="registered",
        extra_json='{"account_type":"chatgpt_password"}',
    )
    with Session(engine) as session:
        session.add(account)
        session.flush()
        session.add(
            AccountAssignmentModel(
                identity_id="legacy-assignment-identity",
                local_account_id=int(account.id or 0),
                pool_id="PUBLIC_POOL",
                target_id=1,
                state="draining",
            )
        )
        session.commit()
        result = list_accounts(platform="chatgpt", page=1, page_size=1, session=session)

    assert result["summary"]["scheduling"] == 1
    assert result["summary"]["normal"] == 0


def test_account_summary_groups_all_quota_limit_status_variants():
    for status, expected_window in (
        ("rate_limited", "7d"),
        ("rate_limited_5h", "5h"),
        ("rate_limited_7d", "7d"),
        ("usage_exhausted", "7d"),
        ("usage_limited", "7d"),
        ("quota_paused", "7d"),
    ):
        result = _account_operational_summary(
            [],
            [{"status": status}],
            {},
        )
        assert result["normal"] == 0
        assert result["rate_limited"] == 1
        assert result[f"rate_limited_{expected_window}"] == 1


def test_account_summary_keeps_persisted_invalid_status_when_live_probe_is_active():
    account = AccountModel(
        platform="chatgpt",
        email="invalid-local@example.com",
        password="p",
        status="invalid",
        extra_json='{"account_type":"chatgpt_password"}',
    )
    result = _account_operational_summary(
        [account],
        [],
        {None: {"remote_status": "active"}},
    )

    assert result["abnormal"] == 1
    assert result["auth_invalid"] == 1
    assert result["normal"] == 0


def test_account_summary_marks_missing_or_failed_live_rows_as_errors():
    for quota_status in ("error", "not_found"):
        account = AccountModel(
            platform="chatgpt",
            email=f"{quota_status}@example.com",
            password="p",
            status="registered",
            extra_json='{"account_type":"chatgpt_password"}',
        )
        result = _account_operational_summary(
            [account],
            [],
            {None: {"quota_status": quota_status, "remote_status": None}},
        )
        assert result["abnormal"] == 1
        assert result["errors"] == 1


def test_list_accounts_summary_includes_remote_rows_before_pagination(monkeypatch):
    engine = _live_test_engine()
    local = AccountModel(
        platform="chatgpt",
        email="local@example.com",
        password="p",
        status="registered",
        extra_json='{"account_type":"chatgpt_password"}',
    )
    monkeypatch.setattr(
        "services.chatgpt_codex2api_health.fetch_codex2api_quota_accounts",
        lambda **kwargs: [_remote_row(email="remote@example.com", remote_id=99)],
    )
    with Session(engine) as session:
        session.add(local)
        session.commit()
        result = list_accounts(
            platform="chatgpt",
            page=1,
            page_size=1,
            include_live=True,
            session=session,
        )

    assert len(result["items"]) == 1
    assert result["total"] == 2
    assert result["summary"]["total"] == 2
    assert sum(result["summary"][key] for key in ("normal", "scheduling", "rate_limited", "abnormal")) == 2


def test_list_accounts_summary_respects_email_filter(monkeypatch):
    engine = _live_test_engine()
    rows = [
        AccountModel(
            platform="chatgpt",
            email="one@example.com",
            password="p",
            status="registered",
            extra_json='{"account_type":"chatgpt_password"}',
        ),
        AccountModel(
            platform="chatgpt",
            email="two@example.com",
            password="p",
            status="invalid",
            extra_json='{"account_type":"chatgpt_password"}',
        ),
    ]
    with Session(engine) as session:
        session.add_all(rows)
        session.commit()
        result = list_accounts(
            platform="chatgpt",
            email="two@",
            page=1,
            page_size=1,
            session=session,
        )

    assert result["total"] == 1
    assert result["summary"]["total"] == 1
    assert result["summary"]["abnormal"] == 1
    assert result["summary"]["auth_invalid"] == 1


def test_list_accounts_summary_respects_subscription_plan_filter_without_remote_fetch(monkeypatch):
    engine = _live_test_engine()
    rows = [
        AccountModel(
            platform="chatgpt",
            email="pro@example.com",
            password="p",
            status="registered",
            extra_json='{"account_type":"chatgpt_password","codex_remote_snapshot":{"email":"pro@example.com","remote_status":"active","plan_type":"pro","usage_percent_7d":10}}',
        ),
        AccountModel(
            platform="chatgpt",
            email="plus@example.com",
            password="p",
            status="registered",
            extra_json='{"account_type":"chatgpt_password","codex_remote_snapshot":{"email":"plus@example.com","remote_status":"active","plan_type":"plus","usage_percent_7d":20}}',
        ),
    ]
    def unexpected_fetch(**kwargs):
        raise AssertionError("snapshot-backed rows should not fetch live data")

    monkeypatch.setattr(
        "services.chatgpt_codex2api_health.fetch_codex2api_quota_accounts",
        unexpected_fetch,
    )
    with Session(engine) as session:
        session.add_all(rows)
        session.commit()
        result = list_accounts(
            platform="chatgpt",
            subscription_plan="plus",
            page=1,
            page_size=1,
            include_live=True,
            session=session,
        )

    assert result["total"] == 1
    assert result["items"][0]["email"] == "plus@example.com"
    assert result["summary"]["total"] == 1
    assert result["summary"]["normal"] == 1


def test_list_accounts_summary_marks_live_probe_failure_as_error(monkeypatch):
    engine = _live_test_engine()
    account = AccountModel(
        platform="chatgpt",
        email="probe-error@example.com",
        password="p",
        status="registered",
        extra_json='{"account_type":"chatgpt_password"}',
    )
    def fail_fetch(**kwargs):
        raise RuntimeError("fixture probe failure")

    monkeypatch.setattr(
        "services.chatgpt_codex2api_health.fetch_codex2api_quota_accounts",
        fail_fetch,
    )
    with Session(engine) as session:
        session.add(account)
        session.commit()
        result = list_accounts(
            platform="chatgpt",
            page=1,
            page_size=1,
            include_live=True,
            session=session,
        )

    assert result["summary"]["total"] == 1
    assert result["summary"]["abnormal"] == 1
    assert result["summary"]["errors"] == 1


def test_list_accounts_summary_marks_unmatched_live_account_as_error(monkeypatch):
    engine = _live_test_engine()
    account = AccountModel(
        platform="chatgpt",
        email="missing-remote@example.com",
        password="p",
        status="registered",
        extra_json='{"account_type":"chatgpt_password"}',
    )
    monkeypatch.setattr(
        "services.chatgpt_codex2api_health.fetch_codex2api_quota_accounts",
        lambda **kwargs: [_remote_row(email="different@example.com", remote_id=100)],
    )
    with Session(engine) as session:
        session.add(account)
        session.commit()
        result = list_accounts(
            platform="chatgpt",
            page=1,
            page_size=20,
            include_live=True,
            session=session,
        )

    local_item = next(item for item in result["items"] if item["email"] == "missing-remote@example.com")
    assert local_item["chatgpt_display"]["quota_status"] == "not_found"
    assert result["summary"]["abnormal"] == 1
    assert result["summary"]["errors"] == 1
