import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine

from api.accounts import list_accounts
from core.db import AccountModel, init_account_pool_schema


def test_account_cards_attach_all_time_billing_by_target_and_remote_id_after_pagination(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    init_account_pool_schema(engine)
    billing = {
        (1, 7): {"scope": "all", "source": "codex2api", "status": "available", "billed_usd": 12604.60},
        (2, 7): {"scope": "all", "source": "codex2api", "status": "available", "billed_usd": 20.00},
    }
    fetch = Mock(return_value=billing)
    monkeypatch.setitem(sys.modules, "services.codex_account_billing", SimpleNamespace(fetch_account_billing_summaries=fetch))
    with Session(engine) as session:
        for local_id, target_id, remote_id in [(1, 1, 7), (2, 2, 7), (3, 1, 8)]:
            session.add(AccountModel(
                id=local_id, platform="chatgpt", email="same@example.com", password="",
                extra_json=json.dumps({
                    "remote_only": True, "remote_target_id": target_id, "remote_id": remote_id,
                    "codex_remote_snapshot": {
                        "email": "same@example.com", "target_id": target_id, "remote_id": remote_id,
                        "remote_status": "active", "plan_type": "pro", "usage_percent_7d": 60,
                        "billed_7d": 10.40, "usage_7d_requests": 137,
                    },
                }),
            ))
        session.commit()
        result = list_accounts(platform="chatgpt", page=1, page_size=2, include_live=True, session=session)

    first, second = [item["chatgpt_display"] for item in result["items"]]
    assert first["billing"]["billed_usd"] == 12604.60
    assert second["billing"]["billed_usd"] == 20.00
    assert first["quota"]["billed_usd"] == 10.40
    assert first["quota"]["request_count"] == 137
    assert first["quota"]["usage_percent"] == 60
    assert result["total"] == 3
    fetch.assert_called_once_with(engine, [(1, 7), (2, 7)], refresh=False)


def test_billing_failure_keeps_card_and_quota_without_substituting_weekly_cost(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    init_account_pool_schema(engine)
    fetch = Mock(side_effect=RuntimeError("fixture failure"))
    monkeypatch.setitem(sys.modules, "services.codex_account_billing", SimpleNamespace(fetch_account_billing_summaries=fetch))
    with Session(engine) as session:
        session.add(AccountModel(
            platform="chatgpt", email="cost@example.com", password="p",
            extra_json=json.dumps({"account_type": "chatgpt_password", "codex_remote_snapshot": {
                "email": "cost@example.com", "target_id": 1, "remote_id": 7,
                "usage_percent_7d": 50, "billed_7d": 10.4,
            }}),
        ))
        session.commit()
        result = list_accounts(platform="chatgpt", include_live=True, session=session)
    display = result["items"][0]["chatgpt_display"]
    assert display["billing"]["scope"] == "all"
    assert display["billing"]["billed_usd"] is None
    assert display["quota"]["billed_usd"] == 10.4
