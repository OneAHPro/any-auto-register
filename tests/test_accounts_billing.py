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


def test_snapshot_list_uses_cached_billing_without_any_remote_fetch(monkeypatch):
    from services import codex_account_billing, codex_inventory, chatgpt_codex2api_health

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    init_account_pool_schema(engine)
    remote_fetch = Mock(side_effect=AssertionError("snapshot list must not wait on remote services"))
    monkeypatch.setattr(codex_account_billing, "get_target_client", remote_fetch)
    monkeypatch.setattr(codex_inventory, "sync_inventory", remote_fetch)
    monkeypatch.setattr(chatgpt_codex2api_health, "fetch_codex2api_quota_accounts", remote_fetch)
    codex_account_billing._remember(engine, (1, 7), codex_account_billing._summary(23.5))
    with Session(engine) as session:
        session.add(AccountModel(platform="chatgpt", email="legacy@example.com", password="p", status="active",
                                 extra_json=json.dumps({"account_type": "chatgpt_password"})))
        session.add(AccountModel(
            platform="chatgpt", email="snapshot@example.com", password="p",
            status="active", extra_json=json.dumps({"account_type": "chatgpt_password", "codex_remote_snapshot": {
                "email": "snapshot@example.com", "target_id": 1, "remote_id": 7,
                "usage_percent_7d": 12, "billed_7d": 4.5,
            }}),
        ))
        session.commit()
        result = list_accounts(platform="chatgpt", include_live=True, refresh_live=True,
                               snapshot_only=True, session=session)
    assert result["total"] == 2
    snapshot = next(item for item in result["items"] if item["email"] == "snapshot@example.com")
    assert snapshot["chatgpt_display"]["billing"]["billed_usd"] == 23.5
    assert snapshot["chatgpt_display"]["quota"]["usage_percent"] == 12
    # Remote reconciliation is queued after the response path; it is not
    # awaited by the snapshot list.
    assert remote_fetch.call_args_list[0][1] == {"refresh": True}


def test_empty_snapshot_list_does_not_fetch_legacy_remote_inventory(monkeypatch):
    from services import chatgpt_codex2api_health

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    init_account_pool_schema(engine)
    fetch = Mock(side_effect=AssertionError("empty snapshot list must return immediately"))
    monkeypatch.setattr(chatgpt_codex2api_health, "fetch_codex2api_quota_accounts", fetch)
    with Session(engine) as session:
        result = list_accounts(platform="chatgpt", include_live=True, snapshot_only=True, session=session)
    assert result["items"] == []
    fetch.assert_not_called()
