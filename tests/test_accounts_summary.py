import json
from datetime import datetime, timezone

from sqlmodel import Session, SQLModel, create_engine
from sqlalchemy.pool import StaticPool
from unittest.mock import Mock

from api.accounts import (
    _account_operational_summary,
    _deduplicate_display_accounts,
    _aggregate_billing_summaries,
    _deduplicate_remote_items,
    _hide_missing_remote_only_accounts,
    list_accounts,
    get_stats,
)
from core.db import (
    AccountAssignmentModel,
    AccountModel,
    AccountTargetBindingModel,
    Codex2APITargetModel,
    CodexInventorySnapshotModel,
    AccountIdentityModel,
    AccountIdentityAliasModel,
    init_account_pool_schema,
)
from core.operations_models import OperationsBillingSnapshotModel


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


class _InventoryClient:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def list_accounts(self):
        self.calls += 1
        return self.rows


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


def test_display_dedup_uses_strong_alias_but_not_email():
    engine = _live_test_engine()
    same_one = AccountModel(
        platform="chatgpt", email="same@example.com", password="p1",
        identity_id="identity-one", extra_json='{"chatgpt_account_id":"acct-1"}',
    )
    same_two = AccountModel(
        platform="chatgpt", email="same@example.com", password="p2",
        identity_id="identity-two", extra_json='{"chatgpt_account_id":"acct-1"}',
    )
    different = AccountModel(
        platform="chatgpt", email="same@example.com", password="p3",
        identity_id="identity-three", extra_json='{"chatgpt_account_id":"acct-2"}',
    )
    with Session(engine) as session:
        session.add_all([
            same_one, same_two, different,
            AccountIdentityModel(id="identity-one", platform="chatgpt", canonical_email="same@example.com"),
            AccountIdentityModel(id="identity-two", platform="chatgpt", canonical_email="same@example.com"),
            AccountIdentityModel(id="identity-three", platform="chatgpt", canonical_email="same@example.com"),
        ])
        session.commit()
        rows = _deduplicate_display_accounts([same_one, same_two, different], session)
    assert len(rows) == 2
    assert "identity-three" in {row.identity_id for row in rows}
    assert {row.identity_id for row in rows} & {"identity-one", "identity-two"}


def test_display_dedup_uses_identical_credential_token_without_email_only_merge():
    engine = _live_test_engine()
    one = AccountModel(platform="chatgpt", email="same@example.com", password="p1", token="token-shared", identity_id="token-one")
    two = AccountModel(platform="chatgpt", email="same@example.com", password="p2", token="token-shared", identity_id="token-two")
    other = AccountModel(platform="chatgpt", email="same@example.com", password="p3", token="token-other", identity_id="token-three")
    with Session(engine) as session:
        session.add_all([one, two, other])
        session.add_all([
            AccountIdentityModel(id="token-one", platform="chatgpt", canonical_email="same@example.com"),
            AccountIdentityModel(id="token-two", platform="chatgpt", canonical_email="same@example.com"),
            AccountIdentityModel(id="token-three", platform="chatgpt", canonical_email="same@example.com"),
        ])
        session.commit()
        rows = _deduplicate_display_accounts([one, two, other], session)
    assert len(rows) == 2


def test_display_dedup_reads_strong_aliases_from_remote_snapshot():
    engine = _live_test_engine()
    one = AccountModel(
        platform="chatgpt", email="pool@example.com", password="", identity_id="pool-one",
        extra_json=json.dumps({"remote_only": True, "codex_remote_snapshot": {"chatgpt_account_id": "nested-acct"}}),
    )
    two = AccountModel(
        platform="chatgpt", email="pool@example.com", password="", identity_id="pool-two",
        extra_json=json.dumps({"remote_only": True, "codex_remote_snapshot": {"chatgpt_account_id": "nested-acct"}}),
    )
    with Session(engine) as session:
        session.add_all([one, two])
        session.add_all([
            AccountIdentityModel(id="pool-one", platform="chatgpt", canonical_email="pool@example.com"),
            AccountIdentityModel(id="pool-two", platform="chatgpt", canonical_email="pool@example.com"),
        ])
        session.commit()
        rows = _deduplicate_display_accounts([one, two], session)
    assert len(rows) == 1


def test_display_dedup_does_not_merge_reused_aliases_across_mailboxes():
    engine = _live_test_engine()
    one = AccountModel(
        platform="chatgpt", email="one@example.com", password="", identity_id="mailbox-one",
        extra_json=json.dumps({"remote_only": True, "codex_remote_snapshot": {"workspace_id": "reused"}}),
    )
    two = AccountModel(
        platform="chatgpt", email="two@example.com", password="", identity_id="mailbox-two",
        extra_json=json.dumps({"remote_only": True, "codex_remote_snapshot": {"workspace_id": "reused"}}),
    )
    with Session(engine) as session:
        session.add_all([
            one, two,
            AccountIdentityModel(id="mailbox-one", platform="chatgpt", canonical_email="one@example.com"),
            AccountIdentityModel(id="mailbox-two", platform="chatgpt", canonical_email="two@example.com"),
        ])
        session.commit()
        rows = _deduplicate_display_accounts([one, two], session)
    assert len(rows) == 2


def test_display_dedup_does_not_hide_ambiguous_identity_aliases():
    engine = _live_test_engine()
    one = AccountModel(
        platform="chatgpt", email="one@example.com", password="", identity_id="amb-one",
        extra_json='{"chatgpt_account_id":"ambiguous-id"}',
    )
    two = AccountModel(
        platform="chatgpt", email="one@example.com", password="", identity_id="amb-two",
        extra_json='{"chatgpt_account_id":"ambiguous-id"}',
    )
    with Session(engine) as session:
        session.add_all([
            one, two,
            AccountIdentityModel(id="amb-one", platform="chatgpt", canonical_email="one@example.com", state="ambiguous"),
            AccountIdentityModel(id="amb-two", platform="chatgpt", canonical_email="one@example.com", state="active"),
        ])
        session.commit()
        rows = _deduplicate_display_accounts([one, two], session)
    assert len(rows) == 2


def test_display_dedup_treats_remote_email_placeholders_as_missing():
    engine = _live_test_engine()
    one = AccountModel(
        platform="chatgpt", email="remote-account-7", password="", identity_id="placeholder-one",
        extra_json=json.dumps({
            "remote_only": True, "remote_email_missing": True,
            "codex_remote_snapshot": {"chatgpt_account_id": "placeholder-id"},
        }),
    )
    two = AccountModel(
        platform="chatgpt", email="remote-account-8", password="", identity_id="placeholder-two",
        extra_json=json.dumps({
            "remote_only": True, "remote_email_missing": True,
            "codex_remote_snapshot": {"chatgpt_account_id": "placeholder-id"},
        }),
    )
    with Session(engine) as session:
        session.add_all([
            one, two,
            AccountIdentityModel(id="placeholder-one", platform="chatgpt", canonical_email=""),
            AccountIdentityModel(id="placeholder-two", platform="chatgpt", canonical_email=""),
        ])
        session.commit()
        rows = _deduplicate_display_accounts([one, two], session)
    assert len(rows) == 1


def test_account_stats_uses_the_same_unique_chatgpt_population():
    engine = _live_test_engine()
    one = AccountModel(
        platform="chatgpt", email="stats@example.com", password="p", identity_id="stats-one",
        extra_json='{"chatgpt_account_id":"stats-shared","account_type":"chatgpt_password"}',
    )
    two = AccountModel(
        platform="chatgpt", email="stats@example.com", password="p", identity_id="stats-two",
        extra_json='{"chatgpt_account_id":"stats-shared","account_type":"chatgpt_password"}',
    )
    with Session(engine) as session:
        session.add_all([
            one, two,
            AccountIdentityModel(id="stats-one", platform="chatgpt", canonical_email=one.email),
            AccountIdentityModel(id="stats-two", platform="chatgpt", canonical_email=two.email),
        ])
        session.commit()
        result = get_stats(session=session)
    assert result["total"] == 1
    assert result["by_platform"]["chatgpt"] == 1


def test_remote_compatibility_rows_are_deduplicated_by_strong_alias():
    items = [
        {
            "id": -((1 << 32) | 71), "email": "remote@example.com",
            "user_id": "remote-id", "remote_id": 71, "remote_target_id": 1,
            "remote_status": "active",
        },
        {
            "id": -((2 << 32) | 72), "email": "remote@example.com",
            "user_id": "remote-id", "remote_id": 72, "remote_target_id": 2,
            "remote_status": "active",
        },
    ]
    representatives, components = _deduplicate_remote_items(items)
    assert len(representatives) == 1
    assert {item["remote_id"] for item in components[id(representatives[0])]} == {71, 72}


def test_remote_compatibility_card_aggregates_hidden_pool_billing(monkeypatch):
    engine = _live_test_engine()
    with Session(engine) as session:
        session.add_all([
            Codex2APITargetModel(
                id=1, name="remote-one", base_url="https://one", admin_key_ref="key-one",
            ),
            Codex2APITargetModel(
                id=2, name="remote-two", base_url="https://two", admin_key_ref="key-two",
            ),
        ])
        session.commit()
    remote_rows = [
        _remote_row(
            target_id=1, remote_id=71, email="remote-shared@example.com",
            chatgpt_account_id="remote-shared-id",
        ),
        _remote_row(
            target_id=2, remote_id=72, email="remote-shared@example.com",
            chatgpt_account_id="remote-shared-id",
        ),
    ]
    monkeypatch.setattr(
        "services.chatgpt_codex2api_health.fetch_codex2api_quota_accounts",
        lambda **kwargs: remote_rows,
    )
    monkeypatch.setattr(
        "services.codex_account_billing.fetch_account_billing_summaries",
        lambda _engine, keys, **kwargs: {
            key: {"scope": "all", "billed_usd": 2.0 if key == (1, 71) else 3.0,
                  "source": "codex2api", "status": "available",
                  "fetched_at": "2026-09-09T00:00:00+00:00"}
            for key in keys
        },
    )
    with Session(engine) as session:
        result = list_accounts(platform="chatgpt", include_live=True, page=1, page_size=20, session=session)
    assert result["total"] == 1
    assert result["items"][0]["chatgpt_display"]["billing"]["billed_usd"] == 5.0


def test_remote_compatibility_row_hidden_by_one_local_email_keeps_billing_key(monkeypatch):
    """A pre-materialization remote row must still contribute to the card."""

    engine = _live_test_engine()
    local = AccountModel(
        platform="chatgpt",
        email="compat-shared@example.com",
        password="p",
        extra_json='{"account_type":"chatgpt_password"}',
    )
    with Session(engine) as session:
        session.add(local)
        session.commit()

    remote_rows = [
        _remote_row(
            target_id=1, remote_id=81, email="compat-shared@example.com",
            chatgpt_account_id="compat-shared-id",
        ),
        _remote_row(
            target_id=2, remote_id=82, email="compat-shared@example.com",
            chatgpt_account_id="compat-shared-id",
        ),
    ]
    monkeypatch.setattr(
        "services.chatgpt_codex2api_health.fetch_codex2api_quota_accounts",
        lambda **kwargs: remote_rows,
    )
    monkeypatch.setattr(
        "services.codex_account_billing.fetch_account_billing_summaries",
        lambda _engine, keys, **kwargs: {
            key: {
                "scope": "all",
                "billed_usd": 4.0 if key == (1, 81) else 6.0,
                "source": "codex2api",
                "status": "available",
                "fetched_at": "2026-09-09T00:00:00+00:00",
            }
            for key in keys
        },
    )
    with Session(engine) as session:
        result = list_accounts(
            platform="chatgpt",
            include_live=True,
            page=1,
            page_size=20,
            session=session,
        )

    assert result["total"] == 1
    assert result["items"][0]["chatgpt_display"]["billing"]["billed_usd"] == 10.0
    assert result["items"][0]["chatgpt_display"]["billing"]["component_count"] == 2


def test_identityless_representative_keeps_billing_from_hidden_identity_member(monkeypatch):
    engine = _live_test_engine()
    newest = AccountModel(
        platform="chatgpt", email="identityless@example.com", password="p",
        identity_id="",
        extra_json=json.dumps({
            "account_type": "chatgpt_password",
            "chatgpt_account_id": "identityless-shared",
            "remote_target_id": 1, "remote_id": 71,
            "codex_remote_snapshot": _remote_row(
                target_id=1, remote_id=71, email="identityless@example.com",
                chatgpt_account_id="identityless-shared",
            ),
        }),
    )
    hidden = AccountModel(
        platform="chatgpt", email="identityless@example.com", password="",
        identity_id="identityless-hidden",
        extra_json=json.dumps({
            "remote_only": True,
            "chatgpt_account_id": "identityless-shared",
            "remote_target_id": 2, "remote_id": 72,
            "codex_remote_snapshot": _remote_row(
                target_id=2, remote_id=72, email="identityless@example.com",
                chatgpt_account_id="identityless-shared",
            ),
        }),
    )
    with Session(engine) as session:
        session.add_all([
            newest, hidden,
            AccountIdentityModel(
                id="identityless-hidden", platform="chatgpt",
                canonical_email="identityless@example.com",
            ),
        ])
        session.commit()
    monkeypatch.setattr(
        "services.codex_account_billing.fetch_account_billing_summaries",
        lambda _engine, keys, **kwargs: {
            key: {"scope": "all", "billed_usd": 1.0 if key == (1, 71) else 2.0,
                  "source": "codex2api", "status": "available",
                  "fetched_at": "2026-09-09T00:00:00+00:00"}
            for key in keys
        },
    )
    with Session(engine) as session:
        result = list_accounts(platform="chatgpt", include_live=True, page=1, page_size=20, session=session)
    assert result["total"] == 1
    assert result["items"][0]["chatgpt_display"]["billing"]["billed_usd"] == 3.0


def test_aggregate_billing_sums_independent_pool_snapshots():
    result = _aggregate_billing_summaries([
        {"scope": "all", "billed_usd": 1.25, "source": "codex2api", "status": "available", "fetched_at": "2026-09-09T00:00:00+00:00"},
        {"scope": "all", "billed_usd": 2.75, "source": "codex2api", "status": "available", "fetched_at": "2026-09-09T01:00:00+00:00"},
    ])
    assert result["billed_usd"] == 4.0
    assert result["status"] == "available"


def test_aggregate_billing_marks_missing_pool_component_as_partial():
    result = _aggregate_billing_summaries([
        {"scope": "all", "billed_usd": "1.25", "source": "codex2api", "status": "available"},
        {"scope": "all", "billed_usd": None, "source": "codex2api", "status": "error"},
    ], expected_components=3)
    assert result["billed_usd"] == 1.25
    assert result["status"] == "partial"
    assert result["component_count"] == 3
    assert result["missing_components"] == 2


def test_aggregate_billing_keeps_legacy_component_count_when_not_provided():
    result = _aggregate_billing_summaries([
        {"scope": "all", "billed_usd": "1.25", "source": "codex2api", "status": "available"},
        {"scope": "all", "billed_usd": None, "source": "codex2api", "status": "error"},
    ])
    assert result["billed_usd"] == 1.25
    assert result["status"] == "partial"
    assert result["missing_components"] == 1


def test_account_list_shows_one_identity_with_aggregated_pool_billing(monkeypatch):
    engine = _live_test_engine()
    account = AccountModel(
        platform="chatgpt", email="shared@example.com", password="p",
        identity_id="identity-shared",
        extra_json=json.dumps({
            "account_type": "chatgpt_password",
            "codex_remote_snapshot": _remote_row(
                target_id=1, remote_id=7, email="shared@example.com",
                chatgpt_account_id="acct-shared",
            ),
            "remote_target_id": 1, "remote_id": 7,
        }),
    )
    with Session(engine) as session:
        session.add(account)
        session.add(AccountIdentityModel(
            id="identity-shared", platform="chatgpt", canonical_email="shared@example.com",
        ))
        session.add_all([
            AccountTargetBindingModel(
                identity_id="identity-shared", local_account_id=0, target_id=1,
                remote_account_id=7, enabled=True, sync_status="synced",
            ),
            AccountTargetBindingModel(
                identity_id="identity-shared", local_account_id=0, target_id=2,
                remote_account_id=8, enabled=True, sync_status="synced",
            ),
        ])
        session.commit()
    monkeypatch.setattr(
        "services.codex_account_billing.fetch_account_billing_summaries",
        lambda _engine, keys, **kwargs: {
            key: {
                "scope": "all", "billed_usd": 1.5 if key == (1, 7) else 2.5,
                "source": "codex2api", "status": "available", "fetched_at": "2026-09-09T00:00:00+00:00",
            }
            for key in keys
        },
    )
    with Session(engine) as session:
        result = list_accounts(
            platform="chatgpt", include_live=True, page=1, page_size=20, session=session,
        )
    assert result["total"] == 1
    assert result["items"][0]["chatgpt_display"]["billing"]["billed_usd"] == 4.0


def test_account_list_aggregates_legacy_duplicate_identity_ids_by_strong_alias(monkeypatch):
    engine = _live_test_engine()
    first = AccountModel(
        platform="chatgpt", email="shared@example.com", password="p1",
        identity_id="legacy-one",
        extra_json=json.dumps({
            "account_type": "chatgpt_password", "chatgpt_account_id": "acct-shared",
            "codex_remote_snapshot": _remote_row(target_id=1, remote_id=7, email="shared@example.com"),
            "remote_target_id": 1, "remote_id": 7,
        }),
    )
    second = AccountModel(
        platform="chatgpt", email="shared@example.com", password="p2",
        identity_id="legacy-two",
        extra_json=json.dumps({
            "account_type": "chatgpt_password", "chatgpt_account_id": "acct-shared",
            "codex_remote_snapshot": _remote_row(target_id=2, remote_id=8, email="shared@example.com"),
            "remote_target_id": 2, "remote_id": 8,
        }),
    )
    with Session(engine) as session:
        session.add_all([first, second])
        session.flush()
        session.add_all([
            AccountIdentityModel(id="legacy-one", platform="chatgpt", canonical_email="shared@example.com"),
            AccountIdentityModel(id="legacy-two", platform="chatgpt", canonical_email="shared@example.com"),
            AccountTargetBindingModel(identity_id="legacy-one", local_account_id=int(first.id), target_id=1, remote_account_id=7, enabled=True, sync_status="synced"),
            AccountTargetBindingModel(identity_id="legacy-two", local_account_id=int(second.id), target_id=2, remote_account_id=8, enabled=True, sync_status="synced"),
        ])
        session.commit()
    monkeypatch.setattr(
        "services.codex_account_billing.fetch_account_billing_summaries",
        lambda _engine, keys, **kwargs: {
            key: {"scope": "all", "billed_usd": 1.0 if key == (1, 7) else 3.0,
                  "source": "codex2api", "status": "available", "fetched_at": "2026-09-09T00:00:00+00:00"}
            for key in keys
        },
    )
    with Session(engine) as session:
        result = list_accounts(platform="chatgpt", include_live=True, page=1, page_size=20, session=session)
    assert result["total"] == 1
    assert result["items"][0]["chatgpt_display"]["billing"]["billed_usd"] == 4.0


def test_account_list_reproduces_cross_pool_elza_shape_as_one_card(monkeypatch):
    """Production-shaped remote-only rows from public and enterprise pools.

    The two rows intentionally have target-scoped identity IDs and no alias
    records.  Their nested provider snapshots carry the same ChatGPT account
    and workspace ID, which is the evidence used by the presentation grouping
    and billing aggregation paths.
    """

    engine = _live_test_engine()
    # Keep the production shape without persisting a real operator mailbox in
    # the repository.
    email = "cross-pool-shared@example.com"
    provider_id = "688326d1-7d21-46d6-9df7-6e4de74f1b24"
    now = datetime.now(timezone.utc)

    def snapshot(target_id: int, remote_id: int, billed: float, created_at: str) -> dict[str, object]:
        return {
            "id": remote_id,
            "name": f"{email} #45",
            "email": email,
            "chatgpt_account_id": provider_id,
            "effective_workspace_id": provider_id,
            "plan_type": "pro",
            "subscription_expires_at": "2026-09-10T09:32:13Z",
            "status": "active",
            "account_type": "oauth",
            "created_at": created_at,
            "updated_at": "2026-09-09T18:38:08+08:00",
            "codex_usage_updated_at": "2026-09-09T18:38:08+08:00",
            "usage_percent_7d": 49,
            "usage_7d_detail": {
                "requests": 100,
                "account_billed": billed,
                "user_billed": billed,
            },
            "billed_7d": billed,
            "enabled": True,
            "locked": False,
            "remote_id": remote_id,
            "quota_7d_updated_at": "2026-09-09T18:38:08+08:00",
            "remote_status": "active",
            "usage_7d_requests": 100,
            "target_id": target_id,
        }

    rows = [
        (1, 25352, 417.225111, "2026-09-09T13:37:42+08:00"),
        (2, 2, 402.417939, "2026-09-09T13:38:05+08:00"),
    ]
    with Session(engine) as session:
        for index, (target_id, remote_id, billed, created_at) in enumerate(rows, 1643):
            identity_id = f"codex2api:{target_id}:{remote_id}"
            projected = snapshot(target_id, remote_id, billed, created_at)
            session.add(
                Codex2APITargetModel(
                    id=target_id,
                    name="default" if target_id == 1 else "漫游星球",
                    target_type="public" if target_id == 1 else "enterprise",
                    server_label="" if target_id == 1 else "美区",
                    base_url="https://target.invalid",
                    admin_key_ref=f"key-{target_id}",
                    default_pool_id="PUBLIC_POOL",
                    enabled=True,
                    inventory_last_sync_at=now,
                )
            )
            session.add(
                AccountModel(
                    id=index,
                    platform="chatgpt",
                    email=email,
                    password="",
                    status="registered",
                    identity_id=identity_id,
                    extra_json=json.dumps(
                        {
                            "account_source": "codex2api",
                            "remote_only": True,
                            "remote_target_id": target_id,
                            "remote_id": remote_id,
                            "codex_remote_snapshot": projected,
                        },
                        ensure_ascii=False,
                    ),
                )
            )
            session.add(
                AccountIdentityModel(
                    id=identity_id,
                    platform="chatgpt",
                    canonical_email=email,
                    state="active",
                    current_account_id=index,
                )
            )
            session.add(
                CodexInventorySnapshotModel(
                    target_id=target_id,
                    remote_id=remote_id,
                    summary_json=json.dumps(projected, ensure_ascii=False),
                    fetched_at=now,
                    source_updated_at="2026-09-09T18:38:08+08:00",
                )
            )
            session.add(
                AccountTargetBindingModel(
                    identity_id=identity_id,
                    local_account_id=index,
                    target_id=target_id,
                    remote_account_id=remote_id,
                    remote_email=email,
                    sync_status="synced",
                    remote_status="active",
                    enabled=True,
                )
            )
            session.add(
                AccountAssignmentModel(
                    identity_id=identity_id,
                    local_account_id=index,
                    pool_id="PUBLIC_POOL",
                    target_id=target_id,
                    state="active",
                )
            )
            micros = int(round(billed * 1_000_000))
            session.add(
                OperationsBillingSnapshotModel(
                    target_id=target_id,
                    remote_id=remote_id,
                    total_billed_micros=micros,
                    today_date="2026-09-09",
                    today_billed_micros=micros,
                    today_requests=100,
                    captured_at=now,
                )
            )
        session.commit()

        result = list_accounts(
            platform="chatgpt",
            include_live=True,
            snapshot_only=True,
            page=1,
            page_size=20,
            session=session,
        )

    assert result["total"] == 1
    billing = result["items"][0]["chatgpt_display"]["billing"]
    assert billing["billed_usd"] == 819.64305
    assert billing["component_count"] == 2
    assert billing["status"] == "available"


def test_account_list_aggregates_materialized_cross_target_rows_after_old_binding_is_superseded(monkeypatch):
    from services.codex_inventory import materialize_inventory, sync_inventory

    engine = _live_test_engine()
    first = _remote_row(
        target_id=1, remote_id=7, email="shared@example.com",
        chatgpt_account_id="acct-materialized",
    )
    second = _remote_row(
        target_id=2, remote_id=8, email="shared@example.com",
        chatgpt_account_id="acct-materialized",
    )

    class Client:
        def __init__(self, row):
            self.row = row

        def list_accounts(self):
            return [self.row]

    sync_inventory(engine, target_id=1, clients={1: Client(first)})
    sync_inventory(engine, target_id=2, clients={2: Client(second)})
    materialize_inventory(engine)
    monkeypatch.setattr(
        "services.codex_account_billing.fetch_account_billing_summaries",
        lambda _engine, keys, **kwargs: {
            key: {
                "scope": "all", "billed_usd": 2.0 if key == (1, 7) else 5.0,
                "source": "codex2api", "status": "available",
                "fetched_at": "2026-09-09T00:00:00+00:00",
            }
            for key in keys
        },
    )
    with Session(engine) as session:
        result = list_accounts(platform="chatgpt", include_live=True, page=1, page_size=20, session=session)
    assert result["total"] == 1
    assert result["items"][0]["chatgpt_display"]["billing"]["billed_usd"] == 7.0


def test_account_list_aggregates_hidden_snapshot_keys_without_backfilled_bindings(monkeypatch):
    engine = _live_test_engine()
    rows = []
    for identity_id, target_id, remote_id, amount in (
        ("legacy-a", 1, 17, 1.25),
        ("legacy-b", 2, 18, 2.75),
    ):
        rows.append(AccountModel(
            platform="chatgpt", email="legacy-shared@example.com", password="p",
            identity_id=identity_id,
            extra_json=json.dumps({
                "account_type": "chatgpt_password",
                "chatgpt_account_id": "legacy-shared-id",
                "codex_remote_snapshot": _remote_row(
                    target_id=target_id, remote_id=remote_id,
                    email="legacy-shared@example.com",
                    chatgpt_account_id="legacy-shared-id",
                ),
                "remote_target_id": target_id, "remote_id": remote_id,
            }),
        ))
    with Session(engine) as session:
        session.add_all(rows)
        session.add_all([
            AccountIdentityModel(id="legacy-a", platform="chatgpt", canonical_email="legacy-shared@example.com"),
            AccountIdentityModel(id="legacy-b", platform="chatgpt", canonical_email="legacy-shared@example.com"),
        ])
        session.commit()
    monkeypatch.setattr(
        "services.codex_account_billing.fetch_account_billing_summaries",
        lambda _engine, keys, **kwargs: {
            key: {"scope": "all", "billed_usd": 1.25 if key == (1, 17) else 2.75,
                  "source": "codex2api", "status": "available",
                  "fetched_at": "2026-09-09T00:00:00+00:00"}
            for key in keys
        },
    )
    with Session(engine) as session:
        result = list_accounts(platform="chatgpt", include_live=True, page=1, page_size=20, session=session)
    assert result["total"] == 1
    assert result["items"][0]["chatgpt_display"]["billing"]["billed_usd"] == 4.0


def test_account_list_aggregates_binding_only_identity_aliases(monkeypatch):
    engine = _live_test_engine()
    account = AccountModel(
        platform="chatgpt", email="binding-card@example.com", password="p",
        identity_id="binding-card", extra_json=json.dumps({
            "account_type": "chatgpt_password",
            "chatgpt_account_id": "binding-shared",
            "remote_target_id": 1, "remote_id": 41,
            "codex_remote_snapshot": _remote_row(
                target_id=1, remote_id=41, email="binding-card@example.com",
                chatgpt_account_id="binding-shared",
            ),
        }),
    )
    with Session(engine) as session:
        session.add(account)
        session.add_all([
            AccountIdentityModel(id="binding-card", platform="chatgpt", canonical_email=account.email),
            AccountIdentityModel(id="binding-only", platform="chatgpt", canonical_email=account.email),
            AccountIdentityAliasModel(
                identity_id="binding-only", platform="chatgpt",
                alias_type="chatgpt_account_id", normalized_value="binding-shared",
            ),
            AccountTargetBindingModel(
                identity_id="binding-card", local_account_id=0, target_id=1,
                remote_account_id=41, enabled=True, sync_status="synced",
            ),
            AccountTargetBindingModel(
                identity_id="binding-only", local_account_id=0, target_id=2,
                remote_account_id=42, enabled=True, sync_status="synced",
            ),
        ])
        session.commit()
    fetch = Mock(side_effect=lambda _engine, keys, **kwargs: {
        key: {"scope": "all", "billed_usd": 1.0 if key == (1, 41) else 2.0,
              "source": "codex2api", "status": "available", "fetched_at": "2026-09-09T00:00:00+00:00"}
        for key in keys
    })
    monkeypatch.setattr("services.codex_account_billing.fetch_account_billing_summaries", fetch)
    with Session(engine) as session:
        result = list_accounts(platform="chatgpt", include_live=True, page=1, page_size=20, session=session)
    assert result["total"] == 1
    assert result["items"][0]["chatgpt_display"]["billing"]["billed_usd"] == 3.0


def test_account_list_excludes_superseded_binding_without_live_inventory(monkeypatch):
    engine = _live_test_engine()
    account = AccountModel(
        platform="chatgpt", email="moved@example.com", password="p",
        identity_id="moved-identity",
        extra_json=json.dumps({
            "account_type": "chatgpt_password",
            "chatgpt_account_id": "moved-id",
            "remote_target_id": 2,
            "remote_id": 22,
            "codex_remote_snapshot": _remote_row(
                target_id=2, remote_id=22, email="moved@example.com",
                chatgpt_account_id="moved-id",
            ),
        }),
    )
    with Session(engine) as session:
        session.add(account)
        session.add(AccountIdentityModel(
            id="moved-identity", platform="chatgpt", canonical_email="moved@example.com",
        ))
        session.add_all([
            AccountTargetBindingModel(
                identity_id="moved-identity", local_account_id=0, target_id=1,
                remote_account_id=11, enabled=False, sync_status="superseded",
            ),
            AccountTargetBindingModel(
                identity_id="moved-identity", local_account_id=0, target_id=2,
                remote_account_id=22, enabled=True, sync_status="synced",
            ),
        ])
        session.commit()
    seen = []
    monkeypatch.setattr(
        "services.codex_account_billing.fetch_account_billing_summaries",
        lambda _engine, keys, **kwargs: (
            seen.extend(keys),
            {
                key: {"scope": "all", "billed_usd": 9.0, "source": "codex2api",
                      "status": "available", "fetched_at": "2026-09-09T00:00:00+00:00"}
                for key in keys
            },
        )[1],
    )
    with Session(engine) as session:
        result = list_accounts(platform="chatgpt", include_live=True, page=1, page_size=20, session=session)
    assert result["total"] == 1
    assert seen == [(2, 22)]


def test_account_list_keeps_persisted_historical_billing_for_superseded_pool(monkeypatch):
    engine = _live_test_engine()
    account = AccountModel(
        platform="chatgpt", email="history@example.com", password="p",
        identity_id="history-identity",
        extra_json=json.dumps({
            "account_type": "chatgpt_password",
            "remote_target_id": 2, "remote_id": 22,
            "codex_remote_snapshot": _remote_row(
                target_id=2, remote_id=22, email="history@example.com",
                chatgpt_account_id="history-id",
            ),
        }),
    )
    with Session(engine) as session:
        session.add(account)
        session.flush()
        session.add(AccountIdentityModel(
            id="history-identity", platform="chatgpt", canonical_email=account.email,
        ))
        session.add_all([
            AccountTargetBindingModel(
                identity_id="history-identity", local_account_id=account.id or 0,
                target_id=1, remote_account_id=11, enabled=False, sync_status="superseded",
            ),
            AccountTargetBindingModel(
                identity_id="history-identity", local_account_id=account.id or 0,
                target_id=2, remote_account_id=22, enabled=True, sync_status="synced",
            ),
        ])
        session.add(OperationsBillingSnapshotModel(
            target_id=1, remote_id=11, total_billed_micros=1_000_000,
            captured_at=datetime(2026, 9, 8, tzinfo=timezone.utc),
        ))
        session.commit()
    seen = []
    monkeypatch.setattr(
        "services.codex_account_billing.fetch_account_billing_summaries",
        lambda _engine, keys, **kwargs: (
            seen.extend(keys),
            {
                key: {"scope": "all", "billed_usd": 1.0 if key == (1, 11) else 2.0,
                      "source": "codex2api", "status": "available",
                      "fetched_at": "2026-09-09T00:00:00+00:00"}
                for key in keys
            },
        )[1],
    )
    with Session(engine) as session:
        result = list_accounts(platform="chatgpt", include_live=True, page=1, page_size=20, session=session)
    assert result["total"] == 1
    assert seen == [(1, 11), (2, 22)]
    assert result["items"][0]["chatgpt_display"]["billing"]["billed_usd"] == 3.0


def test_legacy_live_fallback_does_not_create_remote_duplicate_for_known_snapshot_key(monkeypatch):
    engine = _live_test_engine()
    known = AccountModel(
        platform="chatgpt", email="shared-fallback@example.com", password="p1",
        identity_id="fallback-known",
        extra_json=json.dumps({
            "account_type": "chatgpt_password",
            "chatgpt_account_id": "fallback-id",
            "remote_target_id": 1, "remote_id": 71,
            "codex_remote_snapshot": _remote_row(
                target_id=1, remote_id=71, email="shared-fallback@example.com",
                chatgpt_account_id="fallback-id",
            ),
        }),
    )
    other = AccountModel(
        platform="chatgpt", email="shared-fallback@example.com", password="p2",
        identity_id="fallback-other",
        extra_json='{"account_type":"chatgpt_password"}',
    )
    with Session(engine) as session:
        session.add_all([known, other])
        session.add_all([
            AccountIdentityModel(id="fallback-known", platform="chatgpt", canonical_email=known.email),
            AccountIdentityModel(id="fallback-other", platform="chatgpt", canonical_email=other.email),
        ])
        session.commit()
    monkeypatch.setattr(
        "services.chatgpt_codex2api_health.fetch_codex2api_quota_accounts",
        lambda **kwargs: [_remote_row(
            target_id=1, remote_id=71, email="shared-fallback@example.com",
            chatgpt_account_id="fallback-id",
        )],
    )
    with Session(engine) as session:
        result = list_accounts(platform="chatgpt", include_live=True, page=1, page_size=20, session=session)
    assert result["total"] == 2
    assert not any(item.get("remote_only") for item in result["items"])


def test_account_list_does_not_use_disabled_binding_even_if_inventory_row_is_present(monkeypatch):
    engine = _live_test_engine()
    account = AccountModel(
        platform="chatgpt", email="disabled-binding@example.com", password="p",
        identity_id="disabled-binding-identity",
        extra_json=json.dumps({
            "account_type": "chatgpt_password",
            "remote_target_id": 1, "remote_id": 33,
            "codex_remote_snapshot": _remote_row(
                target_id=1, remote_id=33, email="disabled-binding@example.com",
                chatgpt_account_id="disabled-binding-id",
            ),
        }),
    )
    with Session(engine) as session:
        session.add(account)
        session.add(AccountIdentityModel(
            id="disabled-binding-identity", platform="chatgpt",
            canonical_email="disabled-binding@example.com",
        ))
        session.flush()
        session.add(AccountTargetBindingModel(
            identity_id="disabled-binding-identity", local_account_id=account.id or 0, target_id=1,
            remote_account_id=33, enabled=False, sync_status="remote_missing",
        ))
        session.add(CodexInventorySnapshotModel(
            target_id=1, remote_id=33,
            summary_json=json.dumps({"email": "disabled-binding@example.com", "status": "active"}),
            missing=False, error="",
        ))
        session.commit()
    fetch = Mock(return_value={})
    monkeypatch.setattr("services.codex_account_billing.fetch_account_billing_summaries", fetch)
    with Session(engine) as session:
        result = list_accounts(platform="chatgpt", include_live=True, page=1, page_size=20, session=session)
    assert result["total"] == 1
    fetch.assert_not_called()


def test_account_list_can_show_persisted_history_when_current_remote_snapshot_is_missing(monkeypatch):
    engine = _live_test_engine()
    account = AccountModel(
        platform="chatgpt", email="history-missing@example.com", password="p",
        identity_id="history-missing-identity",
        extra_json='{"account_type":"chatgpt_password"}',
    )
    with Session(engine) as session:
        session.add(account)
        session.flush()
        session.add(AccountIdentityModel(
            id="history-missing-identity", platform="chatgpt", canonical_email=account.email,
        ))
        session.add(AccountTargetBindingModel(
            identity_id="history-missing-identity", local_account_id=account.id or 0,
            target_id=1, remote_account_id=51, enabled=False, sync_status="remote_missing",
        ))
        session.add(OperationsBillingSnapshotModel(
            target_id=1, remote_id=51, total_billed_micros=4_250_000,
            captured_at=datetime(2026, 9, 8, tzinfo=timezone.utc),
        ))
        session.commit()
    monkeypatch.setattr(
        "services.codex_account_billing.fetch_account_billing_summaries",
        lambda _engine, keys, **kwargs: {
            key: {"scope": "all", "billed_usd": 4.25, "source": "codex2api",
                  "status": "available", "fetched_at": "2026-09-08T00:00:00+00:00"}
            for key in keys
        },
    )
    with Session(engine) as session:
        result = list_accounts(platform="chatgpt", include_live=True, page=1, page_size=20, session=session)
    assert result["total"] == 1
    assert result["items"][0]["chatgpt_display"]["billing"]["billed_usd"] == 4.25


def test_account_list_does_not_fetch_for_unconfirmed_binding_without_evidence(monkeypatch):
    engine = _live_test_engine()
    account = AccountModel(
        platform="chatgpt", email="unknown-binding@example.com", password="p",
        identity_id="unknown-binding-identity",
        extra_json='{"account_type":"chatgpt_password"}',
    )
    with Session(engine) as session:
        session.add(account)
        session.flush()
        session.add(AccountIdentityModel(
            id="unknown-binding-identity", platform="chatgpt", canonical_email=account.email,
        ))
        session.add(AccountTargetBindingModel(
            identity_id="unknown-binding-identity", local_account_id=account.id or 0,
            target_id=1, remote_account_id=61, enabled=True, sync_status="unknown",
        ))
        session.commit()
    fetch = Mock(side_effect=AssertionError("unconfirmed binding must not fetch"))
    monkeypatch.setattr("services.codex_account_billing.fetch_account_billing_summaries", fetch)
    with Session(engine) as session:
        result = list_accounts(platform="chatgpt", include_live=True, page=1, page_size=20, session=session)
    assert result["total"] == 1
    fetch.assert_not_called()


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


def test_account_summary_includes_planned_assignment_states_as_scheduling():
    engine = _live_test_engine()
    account = AccountModel(
        platform="chatgpt",
        email="planned@example.com",
        password="p",
        status="registered",
        identity_id="identity-planned",
        extra_json='{"account_type":"chatgpt_password"}',
    )
    with Session(engine) as session:
        session.add(account)
        session.add(
            AccountAssignmentModel(
                identity_id="identity-planned",
                local_account_id=0,
                pool_id="PUBLIC_POOL",
                target_id=1,
                state="planned",
            )
        )
        session.commit()
        result = list_accounts(platform="chatgpt", page=1, page_size=20, session=session)

    assert result["summary"]["scheduling"] == 1
    assert result["summary"]["normal"] == 0


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


def test_account_summary_marks_disabled_target_binding_as_error():
    engine = _live_test_engine()
    account = AccountModel(
        platform="chatgpt",
        email="disabled-target-summary@example.com",
        password="p",
        status="registered",
        identity_id="disabled-target-summary",
        extra_json='{"account_type":"chatgpt_password"}',
    )
    with Session(engine) as session:
        session.add(account)
        session.commit()
        session.refresh(account)
        session.add_all([
            AccountAssignmentModel(
                identity_id=account.identity_id,
                local_account_id=account.id,
                pool_id="PUBLIC_POOL",
                target_id=1,
                state="standby",
                lease_reason="target_disabled",
            ),
            AccountTargetBindingModel(
                identity_id=account.identity_id,
                local_account_id=account.id,
                target_id=1,
                remote_account_id=77,
                sync_status="target_disabled",
                enabled=False,
            ),
        ])
        session.commit()

    with Session(engine) as session:
        result = list_accounts(
            platform="chatgpt",
            page=1,
            page_size=20,
            session=session,
        )

    assert result["summary"]["errors"] == 1
    assert result["summary"]["abnormal"] == 1


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


def test_account_summary_reads_persisted_remote_snapshot_without_live_display():
    account = AccountModel(
        platform="chatgpt",
        email="snapshot-limit@example.com",
        password="",
        status="registered",
        extra_json=json.dumps({
            "account_type": "chatgpt_password",
            "remote_only": True,
            "remote_target_id": 1,
            "remote_id": 77,
            "codex_remote_snapshot": {
                "remote_status": "rate_limited_5h",
                "usage_percent_5h": 100,
            },
        }),
    )
    result = _account_operational_summary([account], [], {})

    assert result["normal"] == 0
    assert result["rate_limited"] == 1
    assert result["rate_limited_5h"] == 1


def test_account_summary_marks_a_stale_inventory_snapshot_as_an_error():
    account = AccountModel(
        platform="chatgpt",
        email="stale-snapshot@example.com",
        password="",
        status="registered",
        extra_json=json.dumps({
            "remote_only": True,
            "remote_target_id": 1,
            "remote_id": 77,
            "codex_remote_snapshot": {
                "remote_status": "active",
                "_inventory_stale": True,
                "_inventory_error": "target unavailable",
            },
        }),
    )
    result = _account_operational_summary([account], [], {})

    assert result["normal"] == 0
    assert result["abnormal"] == 1
    assert result["errors"] == 1


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


def test_refresh_live_syncs_inventory_before_rendering_the_account_list(monkeypatch):
    engine = _live_test_engine()
    initial = AccountModel(
        platform="chatgpt",
        email="initial@example.com",
        password="p",
        status="registered",
        extra_json='{"account_type":"chatgpt_password","codex_remote_snapshot":{"email":"initial@example.com","remote_status":"active","plan_type":"pro"}}',
    )
    sync = Mock(return_value={"targets": 1, "errors": 0})
    materialize = Mock()

    def materialize_after_sync(database_engine):
        with Session(database_engine) as session:
            session.add(
                AccountModel(
                    platform="chatgpt",
                    email="new@example.com",
                    password="",
                    status="registered",
                    extra_json='{"account_source":"codex2api","remote_only":true,"codex_remote_snapshot":{"email":"new@example.com","remote_status":"active","plan_type":"pro"}}',
                )
            )
            session.commit()
        return {"created": 1, "updated": 0, "total": 1}

    materialize.side_effect = materialize_after_sync
    monkeypatch.setattr("services.codex_inventory.sync_inventory", sync)
    monkeypatch.setattr("services.codex_inventory.materialize_inventory", materialize)
    with Session(engine) as session:
        session.add(initial)
        session.commit()
        result = list_accounts(
            platform="chatgpt",
            page=1,
            page_size=20,
            include_live=True,
            refresh_live=True,
            session=session,
        )

    assert sync.call_count == 1
    assert materialize.call_count == 1
    assert {item["email"] for item in result["items"]} == {"initial@example.com", "new@example.com"}


def test_refresh_live_reconciles_new_remote_rows_and_hides_missing_remote_only_rows(monkeypatch):
    engine = _live_test_engine()
    client = _InventoryClient([
        _remote_row(remote_id=2, email="current@example.com", remote_status="active"),
    ])
    stale = AccountModel(
        platform="chatgpt",
        email="stale@example.com",
        password="",
        status="registered",
        identity_id="codex2api:1:1",
        extra_json=json.dumps({
            "account_source": "codex2api",
            "remote_only": True,
            "remote_target_id": 1,
            "remote_id": 1,
            "codex_remote_snapshot": _remote_row(remote_id=1, email="stale@example.com"),
        }),
    )
    with Session(engine) as session:
        session.add(
            Codex2APITargetModel(
                id=1,
                name="default",
                base_url="https://codex2api.example",
                admin_key_ref="test-key",
                enabled=True,
            )
        )
        session.add(stale)
        session.commit()

    monkeypatch.setattr("services.codex2api_target_client.get_target_client", lambda *_args: client)
    with Session(engine) as session:
        result = list_accounts(
            platform="chatgpt",
            page=1,
            page_size=20,
            include_live=True,
            refresh_live=True,
            session=session,
        )

    assert client.calls == 1
    assert {item["email"] for item in result["items"]} == {"current@example.com"}
    assert result["summary"]["total"] == 1


def test_cached_requests_keep_missing_remote_only_rows_hidden_after_a_refresh(monkeypatch):
    engine = _live_test_engine()
    client = _InventoryClient([_remote_row(remote_id=2, email="current@example.com")])
    stale = AccountModel(
        platform="chatgpt",
        email="stale-cached@example.com",
        password="",
        status="registered",
        identity_id="codex2api:1:1",
        extra_json=json.dumps({
            "account_source": "codex2api",
            "remote_only": True,
            "remote_target_id": 1,
            "remote_id": 1,
            "codex_remote_snapshot": _remote_row(remote_id=1, email="stale-cached@example.com"),
        }),
    )
    with Session(engine) as session:
        session.add(Codex2APITargetModel(
            id=1,
            name="default",
            base_url="https://codex2api.example",
            admin_key_ref="test-key",
            enabled=True,
        ))
        session.add(stale)
        session.commit()
    monkeypatch.setattr("services.codex2api_target_client.get_target_client", lambda *_args: client)
    with Session(engine) as session:
        list_accounts(platform="chatgpt", include_live=True, refresh_live=True, session=session)
    with Session(engine) as session:
        cached = list_accounts(platform="chatgpt", include_live=True, refresh_live=False, session=session)

    assert {item["email"] for item in cached["items"]} == {"current@example.com"}


def test_missing_local_snapshot_is_reported_as_not_found_after_a_complete_sync(monkeypatch):
    engine = _live_test_engine()
    local = AccountModel(
        platform="chatgpt",
        email="gone-local@example.com",
        password="p",
        status="registered",
        extra_json=json.dumps({
            "account_type": "chatgpt_password",
            "remote_target_id": 1,
            "remote_id": 1,
            "codex_remote_snapshot": {
                "remote_status": "active",
                "email": "gone-local@example.com",
            },
        }),
    )
    client = _InventoryClient([_remote_row(remote_id=2, email="current@example.com")])
    with Session(engine) as session:
        session.add(Codex2APITargetModel(
            id=1,
            name="default",
            base_url="https://codex2api.example",
            admin_key_ref="test-key",
            enabled=True,
        ))
        session.add(local)
        session.commit()
    monkeypatch.setattr("services.codex2api_target_client.get_target_client", lambda *_args: client)
    with Session(engine) as session:
        result = list_accounts(
            platform="chatgpt",
            page=1,
            page_size=20,
            include_live=True,
            refresh_live=True,
            session=session,
        )

    old = next(item for item in result["items"] if item["email"] == "gone-local@example.com")
    assert old["chatgpt_display"]["quota_status"] == "not_found"
    assert result["summary"]["errors"] == 1


def test_inventory_sync_failure_marks_existing_snapshot_as_unavailable(monkeypatch):
    engine = _live_test_engine()
    local = AccountModel(
        platform="chatgpt",
        email="stale-local@example.com",
        password="p",
        status="registered",
        extra_json=json.dumps({
            "account_type": "chatgpt_password",
            "remote_target_id": 1,
            "remote_id": 1,
            "codex_remote_snapshot": {
                "remote_status": "active",
                "email": "stale-local@example.com",
            },
        }),
    )
    with Session(engine) as session:
        session.add(Codex2APITargetModel(
            id=1,
            name="default",
            base_url="https://codex2api.example",
            admin_key_ref="test-key",
            enabled=True,
        ))
        session.add(local)
        session.commit()
    class FailingClient:
        def list_accounts(self):
            raise RuntimeError("offline")

    monkeypatch.setattr("services.codex2api_target_client.get_target_client", lambda *_args: FailingClient())
    from services.codex_inventory import sync_inventory
    sync_inventory(
        engine,
        target_id=1,
        clients={1: _InventoryClient([_remote_row(remote_id=1, email="stale-local@example.com")])},
    )
    with Session(engine) as session:
        result = list_accounts(
            platform="chatgpt",
            page=1,
            page_size=20,
            include_live=True,
            refresh_live=True,
            session=session,
        )

    item = result["items"][0]
    assert item["chatgpt_display"]["quota_status"] == "error"
    assert result["summary"]["errors"] == 1


def test_cached_account_display_uses_latest_inventory_summary_over_local_snapshot():
    engine = _live_test_engine()
    account = AccountModel(
        platform="chatgpt",
        email="cached@example.com",
        password="p",
        status="registered",
        identity_id="codex2api:1:42",
        extra_json=json.dumps({
            "account_type": "chatgpt_password",
            "remote_target_id": 1,
            "remote_id": 42,
            "codex_remote_snapshot": _remote_row(
                remote_id=42,
                email="cached@example.com",
                usage_percent_7d=60,
                billed_7d=130.12,
                usage_7d_requests=1848,
                updated_at="2026-09-07T01:37:06+08:00",
            ),
        }),
    )
    latest = _remote_row(
        remote_id=42,
        email="cached@example.com",
        usage_percent_7d=94,
        billed_7d=143.33,
        usage_7d_requests=2049,
        updated_at="2026-09-07T04:07:13+08:00",
        quota_7d_updated_at="2026-09-07T04:06:58+08:00",
    )
    with Session(engine) as session:
        session.add(account)
        session.add(
            CodexInventorySnapshotModel(
                target_id=1,
                remote_id=42,
                summary_json=json.dumps(latest),
                source_updated_at=latest["updated_at"],
                fetched_at=datetime.fromisoformat("2026-09-07T04:08:00+00:00"),
                missing=False,
                error="",
            )
        )
        session.commit()
        result = list_accounts(
            platform="chatgpt",
            page=1,
            page_size=20,
            include_live=True,
            refresh_live=False,
            session=session,
        )

    display = result["items"][0]["chatgpt_display"]
    assert display["quota"]["usage_percent"] == 94
    assert display["quota"]["request_count"] == 2049
    assert display["quota"]["billed_usd"] == 143.33
    assert display["live_updated_at"] == "2026-09-07T04:06:58+08:00"


def test_missing_remote_only_rows_are_hidden_only_for_targets_with_complete_inventory():
    engine = _live_test_engine()
    target_one_row = CodexInventorySnapshotModel(
        target_id=1,
        remote_id=10,
        summary_json=json.dumps({"email": "current@example.com"}),
        missing=False,
        error="",
    )
    target_two_account = AccountModel(
        platform="chatgpt",
        email="target-two@example.com",
        password="",
        status="registered",
        extra_json=json.dumps({
            "remote_only": True,
            "remote_target_id": 2,
            "remote_id": 20,
            "codex_remote_snapshot": {"remote_status": "active"},
        }),
    )
    with Session(engine) as session:
        session.add(target_one_row)
        session.add(target_two_account)
        session.commit()
        visible = _hide_missing_remote_only_accounts([target_two_account], session)

    assert [account.email for account in visible] == ["target-two@example.com"]


def test_empty_inventory_marker_hides_remote_only_rows_on_cached_requests():
    """A successful empty target list is authoritative without snapshot rows."""
    engine = _live_test_engine()
    remote_only = AccountModel(
        platform="chatgpt",
        email="empty-target@example.com",
        password="",
        status="registered",
        extra_json=json.dumps({
            "remote_only": True,
            "remote_target_id": 1,
            "remote_id": 77,
            "codex_remote_snapshot": {"target_id": 1, "remote_id": 77, "remote_status": "active"},
        }),
    )
    with Session(engine) as session:
        session.add(Codex2APITargetModel(
            id=1,
            name="default",
            base_url="https://codex2api.example",
            admin_key_ref="test-key",
            enabled=True,
            inventory_last_sync_at=datetime(2026, 9, 9, tzinfo=timezone.utc),
            inventory_last_error="",
        ))
        session.add(remote_only)
        session.commit()

    with Session(engine) as session:
        result = list_accounts(
            platform="chatgpt",
            page=1,
            page_size=20,
            include_live=True,
            refresh_live=False,
            session=session,
        )

    assert result["items"] == []
    assert result["summary"]["total"] == 0


def test_operational_filter_changes_items_but_keeps_global_summary_counts():
    engine = _live_test_engine()
    rows = [
        AccountModel(
            platform="chatgpt",
            email="normal-filter@example.com",
            password="p",
            status="registered",
            extra_json='{"account_type":"chatgpt_password"}',
        ),
        AccountModel(
            platform="chatgpt",
            email="invalid-filter@example.com",
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
            operational_status="normal",
            page=1,
            page_size=20,
            session=session,
        )

    assert [item["email"] for item in result["items"]] == ["normal-filter@example.com"]
    assert result["total"] == 1
    assert result["summary"]["total"] == 2
    assert result["summary"]["normal"] == 1
    assert result["summary"]["abnormal"] == 1
