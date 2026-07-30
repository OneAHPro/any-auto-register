import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine

from api.actions import BatchActionRequest, _apply_action_result, _resolve_batch_accounts
from core.db import AccountModel


class Codex2APIActionPersistenceTests(unittest.TestCase):
    def test_manual_action_persists_codex2api_state(self):
        account = AccountModel(
            platform="chatgpt",
            email="demo@example.com",
            password="secret",
            status="registered",
        )
        session = mock.Mock()

        with mock.patch(
            "services.chatgpt_sync.update_account_model_codex2api_sync"
        ) as update_mock:
            _apply_action_result(
                "chatgpt",
                "upload_codex2api",
                account,
                {"ok": True, "data": "uploaded"},
                session,
            )

        update_mock.assert_called_once_with(
            account,
            True,
            "uploaded",
            session=session,
            commit=False,
        )

    def test_failed_action_persists_failure_message(self):
        account = AccountModel(
            platform="chatgpt",
            email="demo@example.com",
            password="secret",
            status="registered",
        )
        session = mock.Mock()

        with mock.patch(
            "services.chatgpt_sync.update_account_model_codex2api_sync"
        ) as update_mock:
            _apply_action_result(
                "chatgpt",
                "upload_codex2api",
                account,
                {"ok": False, "error": "denied"},
                session,
            )

        update_mock.assert_called_once_with(
            account,
            False,
            "denied",
            session=session,
            commit=False,
        )


class Codex2APIBatchFilterTests(unittest.TestCase):
    def test_filtered_batch_honors_created_at_range(self):
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        AccountModel.__table__.create(test_engine)
        now = datetime.now(timezone.utc)

        with Session(test_engine) as session:
            for email, created_at in (
                ("before@example.com", now - timedelta(days=2)),
                ("inside@example.com", now - timedelta(hours=12)),
                ("after@example.com", now + timedelta(days=1)),
            ):
                session.add(
                    AccountModel(
                        platform="chatgpt",
                        email=email,
                        password="secret",
                        status="registered",
                        created_at=created_at,
                    )
                )
            session.commit()

            rows, missing_ids = _resolve_batch_accounts(
                "chatgpt",
                BatchActionRequest(
                    all_filtered=True,
                    created_at_start=now - timedelta(days=1),
                    created_at_end=now,
                ),
                session,
            )

        self.assertEqual(missing_ids, [])
        self.assertEqual([row.email for row in rows], ["inside@example.com"])


if __name__ == "__main__":
    unittest.main()
