import unittest
from types import SimpleNamespace
from unittest import mock

from fastapi import BackgroundTasks, FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select

import api.tasks as tasks_module
from api.tasks import (
    ChatGPTRetryFailedTaskRequest,
    RegisterTaskRequest,
    _build_chatgpt_retry_request,
    get_retryable_task_bindings,
    retry_failed_task_bindings,
    _retryable_chatgpt_bindings,
    _upsert_chatgpt_attempt_binding,
)
from core.db import ChatGPTAttemptBindingModel, _recover_chatgpt_attempt_bindings


class ChatGPTRetryBindingTests(unittest.TestCase):
    def test_build_retry_request_preserves_email_card_order(self):
        bindings = [
            {
                "id": 41,
                "email": "first@example.com",
                "leadbee_code": "bei-sms-FIRST",
                "mail_provider": "microsoft",
                "status": "failed",
            },
            {
                "id": 42,
                "email": "second@example.com",
                "leadbee_code": "bei-sms-SECOND",
                "status": "failed",
            },
        ]

        with mock.patch(
            "core.config_store.config_store.get_all",
            return_value={
                "default_executor": "headless",
                "default_captcha_solver": "yescaptcha",
            },
        ):
            request = _build_chatgpt_retry_request(bindings)

        self.assertEqual(request.platform, "chatgpt")
        self.assertEqual(request.count, 2)
        self.assertEqual(request.concurrency, 1)
        self.assertEqual(
            request.extra["chatgpt_existing_account_leadbee_codes"],
            ["bei-sms-FIRST", "bei-sms-SECOND"],
        )
        self.assertEqual(
            request.extra["chatgpt_retry_bindings"],
            [
                {
                    "id": 41,
                    "email": "first@example.com",
                    "leadbee_code": "bei-sms-FIRST",
                    "mail_provider": "microsoft",
                },
                {"id": 42, "email": "second@example.com", "leadbee_code": "bei-sms-SECOND"},
            ],
        )

    def test_build_retry_request_uses_requested_concurrency_bounded_by_count(self):
        bindings = [
            {
                "id": 41,
                "email": "first@example.com",
                "leadbee_code": "bei-sms-FIRST",
            },
            {
                "id": 42,
                "email": "second@example.com",
                "leadbee_code": "bei-sms-SECOND",
            },
        ]

        with mock.patch(
            "core.config_store.config_store.get_all",
            return_value={"default_executor": "headless"},
        ):
            request = _build_chatgpt_retry_request(bindings, concurrency=10)

        self.assertEqual(request.concurrency, 2)

    def test_retry_route_keeps_empty_body_compatible_and_accepts_concurrency(self):
        app = FastAPI()
        app.include_router(tasks_module.router)
        client = TestClient(app)
        row = SimpleNamespace(id=41)

        def build_request(_rows, concurrency=1):
            return RegisterTaskRequest(
                platform="chatgpt",
                count=1,
                concurrency=concurrency,
            )

        with (
            mock.patch(
                "api.tasks._get_task_snapshot",
                return_value={"platform": "chatgpt", "status": "done"},
            ),
            mock.patch(
                "api.tasks._retryable_chatgpt_bindings",
                return_value=[row],
            ),
            mock.patch(
                "api.tasks._build_chatgpt_retry_request",
                side_effect=build_request,
            ) as build,
            mock.patch("api.tasks.Session") as session,
            mock.patch(
                "api.tasks.enqueue_register_task",
                side_effect=["task-default", "task-concurrent"],
            ),
        ):
            session.return_value.__enter__.return_value.get.return_value = None
            default_response = client.post("/tasks/task-failed/retry-failed")
            concurrent_response = client.post(
                "/tasks/task-failed/retry-failed",
                json={"concurrency": 4},
            )

        self.assertEqual(default_response.status_code, 200)
        self.assertEqual(default_response.json()["concurrency"], 1)
        self.assertEqual(concurrent_response.status_code, 200)
        self.assertEqual(concurrent_response.json()["concurrency"], 4)
        self.assertEqual(
            [call.kwargs["concurrency"] for call in build.call_args_list],
            [1, 4],
        )

    def test_retry_request_rejects_non_integer_concurrency(self):
        for invalid in (True, 1.0, "1"):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                ChatGPTRetryFailedTaskRequest(concurrency=invalid)

    def test_failed_binding_is_persisted_with_email_and_card_for_later_retry(self):
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)

        with mock.patch("api.tasks.engine", test_engine):
            _upsert_chatgpt_attempt_binding(
                task_id="task-original",
                attempt_index=3,
                email="bound@example.com",
                leadbee_code="bei-sms-BOUND",
                stage="phone",
                status="failed",
                error="temporary phone failure",
                mailbox_context={"provider": "microsoft"},
            )
            rows = _retryable_chatgpt_bindings("task-original")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].email, "bound@example.com")
        self.assertEqual(rows[0].leadbee_code, "bei-sms-BOUND")
        self.assertEqual(rows[0].attempt_index, 3)

    def test_service_restart_returns_interrupted_bindings_to_failed_state(self):
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)
        with Session(test_engine) as session:
            session.add(
                ChatGPTAttemptBindingModel(
                    task_id="task-interrupted",
                    attempt_index=0,
                    email="retry@example.com",
                    leadbee_code="bei-sms-RETRY",
                    status="retrying",
                )
            )
            session.commit()

        with mock.patch("core.db.engine", test_engine):
            _recover_chatgpt_attempt_bindings()

        with Session(test_engine) as session:
            row = session.exec(select(ChatGPTAttemptBindingModel)).one()
            self.assertEqual(row.status, "failed")
            self.assertIn("服务重启", row.error)

    def test_retry_endpoint_hides_card_and_queues_the_bound_retry(self):
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)
        with Session(test_engine) as session:
            session.add(
                ChatGPTAttemptBindingModel(
                    task_id="task-failed",
                    attempt_index=0,
                    email="bound@example.com",
                    leadbee_code="bei-sms-SECRET-CODE",
                    status="failed",
                    mailbox_context_json='{"provider":"microsoft"}',
                )
            )
            session.commit()

        snapshot = {
            "id": "task-failed",
            "platform": "chatgpt",
            "status": "done",
        }
        with (
            mock.patch("api.tasks.engine", test_engine),
            mock.patch("api.tasks._get_task_snapshot", return_value=snapshot),
            mock.patch(
                "core.config_store.config_store.get_all",
                return_value={"default_executor": "headless"},
            ),
            mock.patch(
                "api.tasks.enqueue_register_task",
                return_value="task-retry",
            ) as enqueue,
        ):
            public = get_retryable_task_bindings("task-failed")
            result = retry_failed_task_bindings(
                "task-failed",
                BackgroundTasks(),
            )

        self.assertEqual(public["count"], 1)
        self.assertNotIn("leadbee_code", public["items"][0])
        self.assertEqual(result["task_id"], "task-retry")
        queued_request = enqueue.call_args.args[0]
        self.assertEqual(
            queued_request.extra["chatgpt_retry_bindings"][0],
            {
                "id": 1,
                "email": "bound@example.com",
                "leadbee_code": "bei-sms-SECRET-CODE",
                "mail_provider": "microsoft",
            },
        )
        with Session(test_engine) as session:
            row = session.exec(select(ChatGPTAttemptBindingModel)).one()
            self.assertEqual(row.status, "retrying")


if __name__ == "__main__":
    unittest.main()
