import base64
import json
from unittest.mock import patch

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from core.db import AccountModel
from api.accounts import list_accounts
from services.chatgpt_account_display import build_chatgpt_account_display_map


def _token(*, plan: str, account_id: str) -> str:
    payload = {
        "https://api.openai.com/auth": {
            "chatgpt_plan_type": plan,
            "chatgpt_account_id": account_id,
            "chatgpt_user_id": f"user-{account_id}",
        },
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


def _account(*, email: str, plan: str, account_id: str) -> AccountModel:
    account = AccountModel(
        id=None,
        platform="chatgpt",
        email=email,
        password="password",
        user_id=account_id,
        token=_token(plan=plan, account_id=account_id),
    )
    account.set_extra({"access_token": account.token, "account_id": account_id})
    return account


def test_live_projection_prefers_codex2api_plan_and_weekly_usage():
    account = _account(email="local@example.com", plan="plus", account_id="acct-1")
    rows = [
        {
            "email": "local@example.com",
            "chatgpt_account_id": "acct-1",
            "effective_workspace_id": "acct-1",
            "plan_type": "self_serve_business_prolite",
            "remote_status": "active",
            "usage_percent_7d": 25,
            "billed_7d": 98.34,
            "reset_7d_at": "2026-09-11T21:54:07+08:00",
            "quota_7d_updated_at": "2026-09-05T02:17:39+08:00",
            "usage_7d_requests": 506,
        },
    ]

    result = build_chatgpt_account_display_map([account], rows)[None]

    assert result["plan_type"] == "self_serve_business_prolite"
    assert result["plan_source"] == "codex2api_live"
    assert result["quota_status"] == "live"
    assert result["quota"] == {
        "window": "7d",
        "usage_percent": 25.0,
        "billed_usd": 98.34,
        "reset_at": "2026-09-11T21:54:07+08:00",
        "captured_at": "2026-09-05T02:17:39+08:00",
        "request_count": 506,
        "remote_status": "active",
        "remote_id": None,
        "source": "codex2api_live",
    }


def test_live_projection_uses_unique_account_id_when_email_changed():
    account = _account(email="renamed-local@example.com", plan="plus", account_id="acct-2")
    rows = [
        {
            "email": "historical@example.com",
            "chatgpt_account_id": "acct-2",
            "effective_workspace_id": "acct-2",
            "plan_type": "pro",
            "usage_percent_7d": 40,
            "billed_7d": 20,
            "usage_7d_requests": 12,
            "remote_id": 17,
        },
    ]

    result = build_chatgpt_account_display_map([account], rows)[None]

    assert result["plan_type"] == "pro"
    assert result["quota"]["usage_percent"] == 40.0
    assert result["match"] == "account_id"


def test_live_projection_does_not_guess_when_account_id_is_ambiguous():
    account = _account(email="local@example.com", plan="free", account_id="shared")
    rows = [
        {"email": "other-a@example.com", "chatgpt_account_id": "shared", "plan_type": "plus"},
        {"email": "other-b@example.com", "chatgpt_account_id": "shared", "plan_type": "pro"},
    ]

    result = build_chatgpt_account_display_map([account], rows)[None]

    assert result["plan_type"] == "free"
    assert result["plan_source"] == "access_token_claim"
    assert result["quota"] is None
    assert result["quota_status"] == "not_found"


def test_live_projection_ignores_placeholder_name_when_remote_email_is_missing():
    account = _account(email="local@example.com", plan="plus", account_id="acct-3")
    rows = [
        {
            "name": "local@example.com",
            "email": "",
            "_remote_email_missing": True,
            "plan_type": "pro",
            "usage_percent_7d": 50,
        },
    ]

    result = build_chatgpt_account_display_map([account], rows)[None]

    assert result["plan_type"] == "plus"
    assert result["plan_source"] == "access_token_claim"
    assert result["quota"] is None
    assert result["quota_status"] == "not_found"


def test_account_list_can_attach_live_projection_without_changing_codex2api():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    account = _account(email="live-list@example.com", plan="plus", account_id="acct-list")
    account_extra = account.get_extra()
    account_extra["refresh_token"] = "refresh-list"
    account.set_extra(account_extra)
    with Session(engine) as session:
        session.add(account)
        session.commit()
        session.refresh(account)

        with patch(
            "services.chatgpt_codex2api_health.fetch_codex2api_quota_accounts",
            return_value=[
                {
                    "email": "live-list@example.com",
                    "chatgpt_account_id": "acct-list",
                    "plan_type": "pro",
                    "usage_percent_7d": 10,
                    "billed_7d": 30,
                    "usage_7d_requests": 9,
                },
            ],
        ):
            result = list_accounts(
                platform="chatgpt",
                page=1,
                page_size=20,
                include_live=True,
                session=session,
            )

    display = result["items"][0]["chatgpt_display"]
    assert display["plan_type"] == "pro"
    assert display["quota"]["usage_percent"] == 10.0
    assert display["quota"]["request_count"] == 9
