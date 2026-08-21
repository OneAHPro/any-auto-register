import base64
import imaplib
import json
import unittest
from datetime import datetime
from urllib.parse import quote
from unittest import mock

from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select

from core import base_mailbox
from core.base_mailbox import MailboxAccount, OutlookMailbox, create_mailbox
from core.db import OutlookAccountModel


class _FakeResponse:
    def __init__(self, status_code, payload=None, text="", json_error=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or ""
        self.content = b"{}" if payload is not None or json_error is not None else b""
        self._json_error = json_error

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return dict(self._payload)


class OutlookMailboxOAuthTests(unittest.TestCase):
    @mock.patch("requests.get")
    def test_yisen_mailapi_request_uses_jwt_and_browser_headers(self, mock_get):
        token = "header.payload.fixture-signature"
        mailbox = OutlookMailbox()
        account = MailboxAccount(
            email="worker@yisen.uk",
            extra={
                "account_type": "mailapi_url",
                "mailapi_url": (
                    "https://mail.yisen.uk/api/mails"
                    "?login=worker%40yisen.uk&limit=20&offset=0"
                ),
                "mailapi_token": token,
            },
        )
        mock_get.return_value = _FakeResponse(
            200,
            payload={"count": 0, "results": []},
            text='{"count":0,"results":[]}',
        )

        mailbox._backends["mailapi_url"]._fetch_mailapi_text(account)

        request_kwargs = mock_get.call_args.kwargs
        self.assertEqual(
            request_kwargs["headers"]["Authorization"],
            f"Bearer {token}",
        )
        self.assertEqual(
            request_kwargs["headers"]["Accept"],
            "application/json, text/plain, */*",
        )
        self.assertEqual(request_kwargs["headers"]["x-lang"], "zh-CN")
        self.assertIn("Mozilla/5.0", request_kwargs["headers"]["User-Agent"])

    def test_yisen_mailapi_results_parse_newest_openai_code(self):
        mailbox = OutlookMailbox()
        backend = mailbox._backends["mailapi_url"]
        payload = {
            "count": 3,
            "results": [
                {
                    "id": 7,
                    "message_id": "mail-7",
                    "address": "worker@yisen.uk",
                    "metadata": json.dumps(
                        {"subject": "Your OpenAI verification code"}
                    ),
                    "source": "Your verification code is 246810",
                    "created_at": "2026-08-21T06:30:00Z",
                },
                {
                    "id": 8,
                    "message_id": "mail-8",
                    "address": "worker@yisen.uk",
                    "metadata": json.dumps(
                        {"subject": "Your ChatGPT authentication code"}
                    ),
                    "source": "Your authentication code is 975310",
                    "created_at": "2026-08-21T06:31:00Z",
                },
                {
                    "id": 9,
                    "message_id": "mail-9",
                    "address": "worker@yisen.uk",
                    "metadata": json.dumps({"subject": "Unrelated receipt"}),
                    "source": "Receipt number 111111",
                    "created_at": "2026-08-21T06:32:00Z",
                },
            ],
        }

        parsed = backend._parse_mailapi_message(json.dumps(payload), yisen=True)

        self.assertEqual(parsed["content"], "Your ChatGPT authentication code 975310")
        self.assertEqual(
            parsed["received_at"],
            datetime.fromisoformat("2026-08-21T06:31:00+00:00").timestamp(),
        )
        self.assertEqual(parsed["message_id"], "mailapi_message:mail-8")
        self.assertTrue(parsed["mailapi_history"])

    def test_yisen_mailapi_results_decode_base64_raw_mime(self):
        mailbox = OutlookMailbox()
        backend = mailbox._backends["mailapi_url"]
        raw_mime = (
            "From: no-reply@openai.com\r\n"
            "Subject: Your temporary ChatGPT verification code\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "Content-Transfer-Encoding: base64\r\n\r\n"
            "WW91ciB2ZXJpZmljYXRpb24gY29kZSBpcyAyNDY4MTA="
        )
        payload = {
            "count": 1,
            "results": [
                {
                    "id": 7,
                    "message_id": "mail-7",
                    "address": "worker@yisen.uk",
                    "metadata": json.dumps(
                        {"subject": "Your temporary ChatGPT verification code"}
                    ),
                    "source": "bounces@example.test",
                    "raw": raw_mime,
                    "created_at": "2026-08-21T06:30:00Z",
                }
            ],
        }

        parsed = backend._parse_mailapi_message(json.dumps(payload), yisen=True)
        code = backend._extract_message_code(parsed, json.dumps(payload), None)

        self.assertEqual(code, "246810")
        self.assertTrue(parsed["status"])
        self.assertEqual(parsed["message_id"], "mailapi_message:mail-7")

    def test_yisen_mailapi_results_extract_code_from_html_mime(self):
        mailbox = OutlookMailbox()
        backend = mailbox._backends["mailapi_url"]
        long_attributes = "x" * 220
        raw_mime = (
            "From: no-reply@openai.com\r\n"
            "Subject: Your temporary ChatGPT verification code\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            "Content-Transfer-Encoding: quoted-printable\r\n\r\n"
            "<h1>Your verification code</h1>"
            f"<div data-layout='{long_attributes}'>246810</div>"
        )
        payload = {
            "count": 1,
            "results": [
                {
                    "id": 7,
                    "message_id": "mail-7",
                    "address": "worker@yisen.uk",
                    "metadata": json.dumps(
                        {"subject": "Your temporary ChatGPT verification code"}
                    ),
                    "source": "bounces@example.test",
                    "raw": raw_mime,
                    "created_at": "2026-08-21T06:30:00Z",
                }
            ],
        }

        parsed = backend._parse_mailapi_message(json.dumps(payload), yisen=True)
        code = backend._extract_message_code(parsed, json.dumps(payload), None)

        self.assertEqual(code, "246810")
        self.assertTrue(parsed["status"])
        self.assertEqual(parsed["message_id"], "mailapi_message:mail-7")

    @mock.patch("requests.get")
    def test_yisen_wait_accepts_latest_message_marked_by_baseline(self, mock_get):
        mailbox = OutlookMailbox()
        backend = mailbox._backends["mailapi_url"]
        token = "header.payload.fixture-signature"
        account = MailboxAccount(
            email="worker@yisen.uk",
            extra={
                "account_type": "mailapi_url",
                "mailapi_url": (
                    "https://mail.yisen.uk/api/mails"
                    "?login=worker%40yisen.uk&limit=20&offset=0"
                ),
                "mailapi_token": token,
            },
        )
        payload = {
            "count": 1,
            "results": [
                {
                    "id": 7,
                    "message_id": "mail-7",
                    "address": "worker@yisen.uk",
                    "metadata": json.dumps(
                        {"subject": "Your temporary ChatGPT login code"}
                    ),
                    "source": "Your login code is 246810",
                    "created_at": "2026-08-21T06:30:00Z",
                }
            ],
        }
        mock_get.return_value = _FakeResponse(
            200,
            payload=payload,
            text=json.dumps(payload),
        )
        baseline = backend.get_current_ids(account)
        with mock.patch.object(
            mailbox,
            "_run_polling_wait",
            side_effect=lambda **kwargs: kwargs["poll_once"](),
        ):
            code = backend.wait_for_code(
                account,
                timeout=5,
                before_ids=baseline,
            )

        self.assertEqual(code, "246810")

    def test_generic_mailapi_results_key_preserves_top_level_content(self):
        mailbox = OutlookMailbox()
        backend = mailbox._backends["mailapi_url"]
        payload = {
            "results": [],
            "content": "Your verification code is 246810",
        }

        parsed = backend._parse_mailapi_message(json.dumps(payload))

        self.assertIn("246810", parsed["content"])
        self.assertIsNot(parsed.get("status"), False)

    @mock.patch("requests.get")
    def test_yisen_mailapi_403_does_not_expose_token_or_email(self, mock_get):
        logs = []
        mailbox = OutlookMailbox()
        mailbox._log_fn = logs.append
        token = "header.payload.private-signature"
        email = "private@yisen.uk"
        account = MailboxAccount(
            email=email,
            extra={
                "account_type": "mailapi_url",
                "mailapi_url": (
                    "https://mail.yisen.uk/api/mails"
                    "?login=private%40yisen.uk&limit=20&offset=0"
                ),
                "mailapi_token": token,
            },
        )
        mock_get.return_value = _FakeResponse(403, text="forbidden")

        with mock.patch.object(
            mailbox,
            "_run_polling_wait",
            side_effect=lambda **kwargs: kwargs["poll_once"](),
        ):
            code = mailbox.wait_for_code(account, timeout=5)

        self.assertIsNone(code)
        rendered = "\n".join(logs)
        self.assertIn("[MailAPI] 拉取失败", rendered)
        self.assertNotIn(token, rendered)
        self.assertNotIn(email, rendered)

    def test_requeue_account_restores_removed_outlook_account(self):
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)
        mailbox = OutlookMailbox()
        account = MailboxAccount(
            email="restore@example.com",
            account_id="42",
            extra={
                "password": "mail-password",
                "client_id": "mail-client",
                "refresh_token": "mail-refresh",
                "account_type": "microsoft_oauth",
                "mailapi_token": "mailapi-secret-token",
            },
        )

        with mock.patch("core.db.engine", test_engine):
            mailbox.requeue_account(account)

        with Session(test_engine) as session:
            restored = session.exec(
                select(OutlookAccountModel).where(
                    OutlookAccountModel.email == "restore@example.com"
                )
            ).one()

        self.assertEqual(restored.password, "mail-password")
        self.assertEqual(restored.client_id, "mail-client")
        self.assertEqual(restored.refresh_token, "mail-refresh")
        self.assertEqual(restored.mailapi_token, "mailapi-secret-token")
        self.assertTrue(restored.enabled)

    def test_mailapi_account_can_persist_chatgpt_password_after_reset(self):
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)
        with Session(test_engine) as session:
            session.add(
                OutlookAccountModel(
                    email="reset@example.com",
                    password="",
                    account_type="mailapi_url",
                    mailapi_url="https://mail.example.test/messages",
                )
            )
            session.commit()

        mailbox = OutlookMailbox()
        account = MailboxAccount(
            email="reset@example.com",
            extra={
                "password": "",
                "account_type": "mailapi_url",
                "mailapi_url": "https://mail.example.test/messages",
            },
        )
        with mock.patch("core.db.engine", test_engine):
            committed = mailbox.commit_password_reset(
                account,
                "Replacement-Password-2026!",
            )

        self.assertTrue(committed)
        self.assertEqual(account.extra["password"], "Replacement-Password-2026!")
        with Session(test_engine) as session:
            saved = session.exec(
                select(OutlookAccountModel).where(
                    OutlookAccountModel.email == "reset@example.com"
                )
            ).one()
        self.assertEqual(saved.password, "Replacement-Password-2026!")
        self.assertFalse(saved.enabled)

    def test_claimed_mailapi_account_is_restored_after_password_reset(self):
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)
        with Session(test_engine) as session:
            session.add(
                OutlookAccountModel(
                    email="claimed-reset@example.com",
                    password="",
                    client_id="mail-client",
                    refresh_token="mail-refresh",
                    account_type="mailapi_url",
                    mailapi_url="https://mail.example.test/claimed",
                    mailapi_token="mailapi-secret-token",
                )
            )
            session.commit()

        mailbox = OutlookMailbox()
        with mock.patch("core.db.engine", test_engine):
            claimed = mailbox.get_email_by_address(
                "claimed-reset@example.com"
            )
            claimed.extra.update({
                "account_type": "chatgpt_password_reset_url_mail",
                "password_reset_required": True,
                "new_password": "Replacement-Password-2026!",
            })
            committed = mailbox.commit_password_reset(
                claimed,
                "Replacement-Password-2026!",
            )

        self.assertTrue(committed)
        self.assertEqual(
            claimed.extra["password"],
            "Replacement-Password-2026!",
        )
        self.assertFalse(claimed.extra["password_reset_required"])
        self.assertNotIn("new_password", claimed.extra)
        with Session(test_engine) as session:
            saved = session.exec(
                select(OutlookAccountModel).where(
                    OutlookAccountModel.email
                    == "claimed-reset@example.com"
                )
            ).one()
        self.assertEqual(saved.password, "Replacement-Password-2026!")
        self.assertEqual(saved.client_id, "mail-client")
        self.assertEqual(saved.refresh_token, "mail-refresh")
        self.assertEqual(saved.account_type, "mailapi_url")
        self.assertEqual(
            saved.mailapi_url,
            "https://mail.example.test/claimed",
        )
        self.assertEqual(claimed.extra["mailapi_token"], "mailapi-secret-token")
        self.assertEqual(saved.mailapi_token, "mailapi-secret-token")
        self.assertFalse(saved.enabled)

    def test_get_email_by_address_preserves_retry_binding_order(self):
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)
        with Session(test_engine) as session:
            session.add(
                OutlookAccountModel(
                    email="first@example.com",
                    password="first-password",
                    account_type="mailapi_url",
                    mailapi_url="https://mail.example.test/first",
                )
            )
            session.add(
                OutlookAccountModel(
                    email="second@example.com",
                    password="second-password",
                    account_type="mailapi_url",
                    mailapi_url="https://mail.example.test/second",
                )
            )
            session.commit()

        mailbox = OutlookMailbox()
        with mock.patch("core.db.engine", test_engine):
            selected = mailbox.get_email_by_address("second@example.com")

        self.assertEqual(selected.email, "second@example.com")
        self.assertEqual(selected.extra["mailapi_url"], "https://mail.example.test/second")
        with Session(test_engine) as session:
            remaining = session.exec(select(OutlookAccountModel)).all()
        self.assertEqual([row.email for row in remaining], ["first@example.com"])

    def test_create_mailbox_outlook_defaults_to_graph_backend(self):
        mailbox = create_mailbox("outlook", extra={})

        self.assertIsInstance(mailbox, OutlookMailbox)
        self.assertEqual(mailbox._backend_name, "graph")

    def test_get_current_ids_keeps_inbox_ids_when_junk_folder_is_unsupported(self):
        mailbox = OutlookMailbox(backend="imap")
        account = MailboxAccount(email="demo@outlook.com")
        imap_conn = mock.Mock()

        def select_folder(folder, readonly=False):
            self.assertTrue(readonly)
            if folder == "Junk":
                raise imaplib.IMAP4.error("EXAMINE command error: BAD")
            if folder == "Trash":
                return "NO", [b""]
            return "OK", [b""]

        imap_conn.select.side_effect = select_folder
        imap_conn.uid.side_effect = [
            ("OK", [b"7 8"]),
            ("OK", [b"9"]),
        ]

        with mock.patch.object(mailbox, "_open_imap", return_value=imap_conn):
            ids = mailbox.get_current_ids(account)

        self.assertEqual(ids, {"INBOX:7", "INBOX:8", "Deleted Items:9"})
        imap_conn.logout.assert_called_once_with()

    def test_wait_for_code_reuses_one_imap_connection_for_all_folders(self):
        mailbox = OutlookMailbox(backend="imap")
        account = MailboxAccount(email="demo@outlook.com")
        imap_conn = mock.Mock()
        imap_conn.select.return_value = ("OK", [b""])
        imap_conn.uid.return_value = ("OK", [b""])

        with (
            mock.patch.object(mailbox, "_open_imap", return_value=imap_conn) as open_imap,
            mock.patch.object(
                mailbox,
                "_run_polling_wait",
                side_effect=lambda **kwargs: kwargs["poll_once"](),
            ),
        ):
            code = mailbox.wait_for_code(account, timeout=5)

        self.assertIsNone(code)
        open_imap.assert_called_once_with(account)
        self.assertEqual(imap_conn.select.call_count, len(mailbox._imap_folder_names))
        imap_conn.logout.assert_called_once_with()

    def test_wait_for_code_logs_one_clear_auth_failure_per_poll(self):
        mailbox = OutlookMailbox(backend="imap")
        account = MailboxAccount(email="demo@outlook.com")
        logs = []
        mailbox._log_fn = logs.append
        auth_error = getattr(
            base_mailbox,
            "MailboxAuthenticationError",
            RuntimeError,
        )("微软邮箱 IMAP 鉴权失败")

        with (
            mock.patch.object(mailbox, "_open_imap", side_effect=auth_error) as open_imap,
            mock.patch.object(
                mailbox,
                "_run_polling_wait",
                side_effect=lambda **kwargs: kwargs["poll_once"](),
            ),
            self.assertRaises(type(auth_error)),
        ):
            mailbox.wait_for_code(account, timeout=5)

        open_imap.assert_called_once_with(account)
        auth_logs = [line for line in logs if "鉴权失败" in line]
        self.assertEqual(len(auth_logs), 1)
        self.assertNotIn("查询异常", auth_logs[0])

    def test_wait_for_code_stops_after_three_consecutive_auth_failures(self):
        mailbox = OutlookMailbox(backend="imap")
        account = MailboxAccount(email="demo@outlook.com")
        auth_error_type = getattr(
            base_mailbox,
            "MailboxAuthenticationError",
            RuntimeError,
        )

        def run_three_polls(**kwargs):
            for _ in range(3):
                kwargs["poll_once"]()

        with (
            mock.patch.object(
                mailbox,
                "_open_imap",
                side_effect=auth_error_type("微软邮箱 IMAP 鉴权失败"),
            ),
            mock.patch.object(
                mailbox,
                "_run_polling_wait",
                side_effect=run_three_polls,
            ),
            self.assertRaises(auth_error_type),
        ):
            mailbox.wait_for_code(account, timeout=30)

    def test_imap_oauth_auth_failure_refreshes_cached_token_once(self):
        mailbox = OutlookMailbox(
            backend="imap",
            imap_server="imap.example.test",
        )
        account = MailboxAccount(
            email="demo@outlook.com",
            extra={
                "client_id": "client-id",
                "refresh_token": "refresh-token",
                "_oauth_token_cache": {
                    "imap": {
                        "access_token": "cached-token",
                        "expires_at": 9_999_999_999,
                    }
                },
            },
        )
        first_connection = mock.Mock()
        second_connection = mock.Mock()

        def reject_cached_token(_mechanism, callback):
            self.assertIn(b"cached-token", callback(None))
            raise imaplib.IMAP4.error("AUTHENTICATE failed")

        def accept_fresh_token(_mechanism, callback):
            self.assertIn(b"fresh-token", callback(None))

        first_connection.authenticate.side_effect = reject_cached_token
        second_connection.authenticate.side_effect = accept_fresh_token

        with (
            mock.patch(
                "imaplib.IMAP4_SSL",
                side_effect=[first_connection, second_connection],
            ) as connect,
            mock.patch.object(
                mailbox,
                "_fetch_oauth_token_bundle",
                return_value={
                    "access_token": "fresh-token",
                    "expires_in": 3600,
                    "scope_label": "imap_new",
                },
            ) as fetch_token,
        ):
            connection = mailbox._open_imap(account)

        self.assertIs(connection, second_connection)
        self.assertEqual(connect.call_count, 2)
        fetch_token.assert_called_once()
        self.assertEqual(
            account.extra["_oauth_token_cache"]["imap"]["access_token"],
            "fresh-token",
        )
        first_connection.logout.assert_called_once_with()

    def test_imap_oauth_failure_after_refresh_raises_terminal_auth_error(self):
        mailbox = OutlookMailbox(
            backend="imap",
            imap_server="imap.example.test",
        )
        logs = []
        mailbox._log_fn = logs.append
        account = MailboxAccount(
            email="demo@outlook.com",
            extra={
                "client_id": "client-id",
                "refresh_token": "refresh-token",
                "_oauth_token_cache": {
                    "imap": {
                        "access_token": "cached-token",
                        "expires_at": 9_999_999_999,
                    }
                },
            },
        )
        connections = [mock.Mock(), mock.Mock()]
        for connection in connections:
            connection.authenticate.side_effect = imaplib.IMAP4.error(
                "AUTHENTICATE failed"
            )

        with (
            mock.patch("imaplib.IMAP4_SSL", side_effect=connections) as connect,
            mock.patch.object(
                mailbox,
                "_fetch_oauth_token_bundle",
                return_value={
                    "access_token": "fresh-token",
                    "expires_in": 3600,
                    "scope_label": "imap_new",
                },
            ) as fetch_token,
            self.assertRaises(base_mailbox.MailboxAuthenticationError) as captured,
        ):
            mailbox._open_imap(account)

        self.assertIn("刷新令牌并重试后仍被服务端拒绝", str(captured.exception))
        self.assertEqual(connect.call_count, 2)
        fetch_token.assert_called_once()
        rendered_logs = "\n".join(logs)
        self.assertNotIn("cached-token", rendered_logs)
        self.assertNotIn("fresh-token", rendered_logs)
        self.assertNotIn("refresh-token", rendered_logs)

    def test_imap_oauth_refresh_timeout_remains_retryable(self):
        mailbox = OutlookMailbox(
            backend="imap",
            imap_server="imap.example.test",
        )
        account = MailboxAccount(
            email="demo@outlook.com",
            extra={
                "client_id": "client-id",
                "refresh_token": "refresh-token",
                "_oauth_token_cache": {
                    "imap": {
                        "access_token": "cached-token",
                        "expires_at": 9_999_999_999,
                    }
                },
            },
        )
        connection = mock.Mock()
        connection.authenticate.side_effect = imaplib.IMAP4.error(
            "AUTHENTICATE failed"
        )

        with (
            mock.patch("imaplib.IMAP4_SSL", return_value=connection),
            mock.patch.object(
                mailbox,
                "_fetch_oauth_token_bundle",
                side_effect=TimeoutError("token endpoint timed out"),
            ),
            self.assertRaises(RuntimeError) as captured,
        ):
            mailbox._open_imap(account)

        self.assertNotIsInstance(
            captured.exception,
            base_mailbox.MailboxAuthenticationError,
        )
        self.assertIn("token endpoint timed out", str(captured.exception))

    def test_imap_oauth_connection_abort_after_refresh_remains_retryable(self):
        mailbox = OutlookMailbox(
            backend="imap",
            imap_server="imap.example.test",
        )
        account = MailboxAccount(
            email="demo@outlook.com",
            extra={
                "client_id": "client-id",
                "refresh_token": "refresh-token",
                "_oauth_token_cache": {
                    "imap": {
                        "access_token": "cached-token",
                        "expires_at": 9_999_999_999,
                    }
                },
            },
        )
        cached_connection = mock.Mock()
        fresh_connection = mock.Mock()
        cached_connection.authenticate.side_effect = imaplib.IMAP4.error(
            "AUTHENTICATE failed"
        )
        fresh_connection.authenticate.side_effect = imaplib.IMAP4.abort(
            "connection reset"
        )

        with (
            mock.patch(
                "imaplib.IMAP4_SSL",
                side_effect=[cached_connection, fresh_connection],
            ),
            mock.patch.object(
                mailbox,
                "_fetch_oauth_token_bundle",
                return_value={
                    "access_token": "fresh-token",
                    "expires_in": 3600,
                    "scope_label": "imap_new",
                },
            ),
            self.assertRaises(RuntimeError) as captured,
        ):
            mailbox._open_imap(account)

        self.assertNotIsInstance(
            captured.exception,
            base_mailbox.MailboxAuthenticationError,
        )
        self.assertIn("connection reset", str(captured.exception))

    @mock.patch("requests.post")
    def test_fetch_oauth_token_graph_backend_prefers_graph_scope(self, mock_post):
        mailbox = OutlookMailbox(token_endpoint="https://token.example.test")

        responses = [
            _FakeResponse(
                400,
                text='{"error":"invalid_grant","error_description":"scopes requested are unauthorized"}',
            ),
            _FakeResponse(
                200,
                payload={"access_token": "access-token-demo"},
                text='{"access_token":"access-token-demo"}',
            ),
        ]
        mock_post.side_effect = responses

        token = mailbox._fetch_oauth_token(
            email="demo@outlook.com",
            client_id="client-id",
            refresh_token="refresh-token",
        )

        self.assertEqual(token, "access-token-demo")
        self.assertEqual(mock_post.call_count, 2)

        first_scope = mock_post.call_args_list[0].kwargs["data"].get("scope", "")
        second_scope = mock_post.call_args_list[1].kwargs["data"].get("scope", "")
        self.assertEqual(
            first_scope,
            "https://graph.microsoft.com/.default",
        )
        self.assertEqual(
            second_scope,
            "https://outlook.office.com/.default offline_access",
        )

    @mock.patch("requests.post")
    def test_fetch_oauth_token_imap_backend_prefers_imap_scope(self, mock_post):
        mailbox = OutlookMailbox(
            token_endpoint="https://token.example.test",
            backend="imap",
        )
        mock_post.side_effect = [
            _FakeResponse(
                200,
                payload={"access_token": "imap-token"},
                text='{"access_token":"imap-token"}',
            ),
        ]

        token = mailbox._fetch_oauth_token(
            email="demo@outlook.com",
            client_id="client-id",
            refresh_token="refresh-token",
        )

        self.assertEqual(token, "imap-token")
        self.assertEqual(
            mock_post.call_args.kwargs["data"].get("scope", ""),
            "https://outlook.office.com/IMAP.AccessAsUser.All offline_access",
        )

    @mock.patch("requests.post")
    def test_probe_oauth_availability_detects_service_abuse_mode(self, mock_post):
        mailbox = OutlookMailbox(token_endpoint="https://token.example.test")
        mock_post.return_value = _FakeResponse(
            400,
            text='{"error":"invalid_grant","error_description":"User account is found to be in service abuse mode."}',
        )

        result = mailbox.probe_oauth_availability(
            email="demo@hotmail.com",
            client_id="client-id",
            refresh_token="refresh-token",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "service_abuse_mode")
        self.assertIn("service abuse mode", result["message"])

    @mock.patch("requests.post")
    def test_probe_oauth_availability_redacts_refresh_token_from_failure(self, mock_post):
        mailbox = OutlookMailbox(token_endpoint="https://token.example.test")
        logs = []
        mailbox._log_fn = logs.append
        secret = "sensitive-refresh-token"
        mock_post.return_value = _FakeResponse(
            400,
            payload={
                "error": "invalid_grant",
                "error_description": (
                    f"Rejected refresh_token={secret} "
                    "access_token=server-access-secret "
                    "Bearer server-bearer-secret"
                ),
            },
            text=(
                '{"error":"invalid_grant","error_description":'
                f'"Rejected refresh_token={secret} '
                'access_token=server-access-secret '
                'Bearer server-bearer-secret"}'
            ),
        )

        result = mailbox.probe_oauth_availability(
            email="demo@hotmail.com",
            client_id="client-id",
            refresh_token=secret,
        )

        rendered = "\n".join(logs) + json.dumps(result)
        self.assertFalse(result["ok"])
        self.assertIn("invalid_grant", rendered)
        for sensitive_value in (
            secret,
            "server-access-secret",
            "server-bearer-secret",
        ):
            self.assertNotIn(sensitive_value, rendered)

    @mock.patch("requests.post")
    def test_probe_oauth_availability_returns_ok_when_access_token_is_obtained(self, mock_post):
        mailbox = OutlookMailbox(token_endpoint="https://token.example.test")
        mock_post.return_value = _FakeResponse(
            200,
            payload={
                "access_token": "ok-token",
                "refresh_token": "rotated-refresh-token",
                "expires_in": 3600,
            },
            text='{"access_token":"ok-token","refresh_token":"rotated-refresh-token","expires_in":3600}',
        )

        result = mailbox.probe_oauth_availability(
            email="demo@outlook.com",
            client_id="client-id",
            refresh_token="refresh-token",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "ok")
        self.assertEqual(result["access_token"], "ok-token")
        self.assertEqual(result["refresh_token"], "rotated-refresh-token")

    @mock.patch("requests.post")
    def test_probe_oauth_availability_keeps_original_refresh_token_when_not_rotated(
        self,
        mock_post,
    ):
        mailbox = OutlookMailbox(token_endpoint="https://token.example.test")
        mock_post.return_value = _FakeResponse(
            200,
            payload={"access_token": "ok-token", "expires_in": 3600},
            text='{"access_token":"ok-token","expires_in":3600}',
        )

        result = mailbox.probe_oauth_availability(
            email="demo@outlook.com",
            client_id="client-id",
            refresh_token="original-refresh-token",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["refresh_token"], "original-refresh-token")

    def test_get_oauth_access_token_keeps_rotated_refresh_token_on_account(self):
        mailbox = OutlookMailbox(token_endpoint="https://token.example.test")
        account = MailboxAccount(
            email="demo@outlook.com",
            extra={
                "client_id": "client-id",
                "refresh_token": "original-refresh-token",
            },
        )

        with mock.patch.object(
            mailbox,
            "_fetch_oauth_token_bundle",
            return_value={
                "access_token": "access-token",
                "refresh_token": "rotated-refresh-token",
                "expires_in": 3600,
                "scope_label": "graph_default",
            },
        ):
            token = mailbox._get_oauth_access_token(account)

        self.assertEqual(token, "access-token")
        self.assertEqual(
            account.extra["refresh_token"],
            "rotated-refresh-token",
        )
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)
        with mock.patch("core.db.engine", test_engine):
            mailbox.requeue_account(account)
        with Session(test_engine) as session:
            persisted = session.exec(
                select(OutlookAccountModel).where(
                    OutlookAccountModel.email == "demo@outlook.com"
                )
            ).one()
        self.assertEqual(persisted.refresh_token, "rotated-refresh-token")

    @mock.patch("requests.post")
    @mock.patch("requests.request")
    def test_wait_for_code_uses_graph_backend_by_default(self, mock_request, mock_post):
        mailbox = OutlookMailbox(token_endpoint="https://token.example.test")
        account = MailboxAccount(
            email="demo@outlook.com",
            extra={
                "client_id": "client-id",
                "refresh_token": "refresh-token",
            },
        )
        mock_post.return_value = _FakeResponse(
            200,
            payload={"access_token": "graph-token", "expires_in": 3600},
            text='{"access_token":"graph-token","expires_in":3600}',
        )
        mock_request.return_value = _FakeResponse(
            200,
            payload={
                "value": [
                    {
                        "id": "message-1",
                        "subject": "OpenAI verification code",
                        "bodyPreview": "Your verification code is 123456",
                    }
                ]
            },
        )

        code = mailbox.wait_for_code(account, timeout=5)

        self.assertEqual(code, "123456")
        self.assertIn(
            "/me/mailFolders/inbox/messages",
            str(mock_request.call_args.args[1]),
        )

    @mock.patch("requests.get")
    def test_wait_for_code_uses_mailapi_backend_for_mailapi_account(self, mock_get):
        mailbox = OutlookMailbox()
        account = MailboxAccount(
            email="demo@outlook.com",
            extra={
                "account_type": "mailapi_url",
                "mailapi_url": "https://mailapi.icu/key?type=html&orderNo=abc123",
            },
        )
        mock_get.return_value = _FakeResponse(
            200,
            text="<html><body>Your OpenAI verification code is 246810</body></html>",
        )

        code = mailbox.wait_for_code(
            account,
            timeout=5,
            before_ids=set(),
        )

        self.assertEqual(code, "246810")
        self.assertTrue(mock_get.call_count >= 1)
        self.assertEqual(
            mock_get.call_args.kwargs.get("timeout"),
            15,
        )

    @mock.patch("requests.get")
    def test_mailapi_naturalflower_shell_follows_public_mailbox_api(self, mock_get):
        mailbox = OutlookMailbox()
        token = "pickup-token.with-punctuation"
        account = MailboxAccount(
            email="demo@post.com",
            extra={
                "account_type": "mailapi_url",
                "mailapi_url": (
                    "https://pickup.naturalflower.cn/?token=" + token
                ),
            },
        )
        shell = """
        <!doctype html>
        <html><body>
          <strong id="pickup-email">正在验证取件链接...</strong>
          <aside id="pickup-list">正在获取邮件...</aside>
          <script src="/static/pickup.js"></script>
        </body></html>
        """
        mailbox_payload = {
            "email": "demo@post.com",
            "messages": [
                {
                    "id": 36759760,
                    "subject": "Your temporary ChatGPT login code",
                    "sender_address": "noreply@tm.openai.com",
                    "received_at": "2026-08-20T23:45:34+08:00",
                    "preview": (
                        "Enter this temporary verification code to continue: "
                        "292882"
                    ),
                }
            ],
        }
        mock_get.side_effect = [
            _FakeResponse(200, text=shell),
            _FakeResponse(200, text=json.dumps(mailbox_payload)),
        ]

        with mock.patch.object(
            mailbox,
            "_run_polling_wait",
            side_effect=lambda **kwargs: kwargs["poll_once"](),
        ):
            code = mailbox.wait_for_code(
                account,
                timeout=5,
                before_ids=set(),
                otp_sent_at=datetime.fromisoformat(
                    "2026-08-20T23:45:33+08:00"
                ).timestamp(),
            )

        self.assertEqual(code, "292882")
        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(
            mock_get.call_args_list[1].args[0],
            (
                "https://pickup.naturalflower.cn/api/public/mailbox/"
                "pickup-token.with-punctuation"
            ),
        )

    def test_mailapi_parses_single_message_panel_with_timestamp_and_code(self):
        mailbox = OutlookMailbox()
        backend = mailbox._backends["mailapi_url"]
        page = """
        <!doctype html>
        <html lang="zh-CN">
          <head>
            <title>Your temporary ChatGPT login code - 最新邮件</title>
            <style>
              .content{padding:20px;line-height:1.65}
              .content table{max-width:100%;border-collapse:collapse}
            </style>
          </head>
          <body>
            <main class="wrap">
              <div class="top">
                <h1>最新邮件</h1>
                <div class="mailbox">
                  <a href="/cdn-cgi/l/email-protection" class="__cf_email__">
                    [email protected]
                  </a>
                </div>
              </div>
              <section class="panel">
                <div class="meta">
                  <div class="subject">Your temporary ChatGPT login code</div>
                  <div class="row">
                    <div class="label">时间</div>
                    <div class="value">2026-08-12 00:23:22</div>
                  </div>
                </div>
                <div class="content">
                  <table><tr><td>
                    <p>Enter this temporary verification code to continue:</p>
                    <p style="font-family: Menlo">326097</p>
                  </td></tr></table>
                </div>
              </section>
            </main>
          </body>
        </html>
        """

        message = backend._parse_mailapi_message(page)
        code = backend._extract_message_code(message, page, None)

        self.assertEqual(code, "326097")
        self.assertEqual(
            message["received_at"],
            datetime.fromisoformat("2026-08-12T00:23:22").timestamp(),
        )
        self.assertTrue(message["message_id"].startswith("mailapi_message:"))
        self.assertNotEqual(message["content"], page)

    def test_mailapi_panel_content_supports_nested_divs(self):
        mailbox = OutlookMailbox()
        backend = mailbox._backends["mailapi_url"]
        page = """
        <section class="panel">
          <div class="content">
            <div class="top"><img alt="ChatGPT"></div>
            <div class="message-body">
              <div class="copy">Your temporary verification code:</div>\r\n\r\n
              <div class="code">326097</div>
            </div>
          </div>
        </section>
        """

        message = backend._parse_mailapi_message(page)

        self.assertEqual(backend._extract_message_code(message, page, None), "326097")

    def test_mailapi_parses_latest_code_mailbox_page(self):
        mailbox = OutlookMailbox()
        backend = mailbox._backends["mailapi_url"]
        page = """
        <main class="page-shell">
          <section class="latest-view" id="latest-view">
            <div class="code-band">
              <span class="section-label">最新验证码</span>
              <output class="latest-code" id="latest-code">030979</output>
            </div>
            <article class="latest-mail">
              <div class="mail-heading">
                <h2>Your temporary ChatGPT login code</h2>
                <span class="inline-code">030979</span>
              </div>
              <dl class="mail-meta">
                <dt>时间</dt>
                <dd>Wed, 19 Aug 2026 18:34:10 +0000</dd>
              </dl>
              <div class="mail-content">
                Enter this temporary verification code to continue: 030979
              </div>
            </article>
          </section>
        </main>
        """

        message = backend._parse_mailapi_message(page)

        self.assertEqual(
            backend._extract_message_code(message, page, None),
            "030979",
        )
        self.assertTrue(message["message_id"].startswith("mailapi_message:"))
        self.assertEqual(
            message["received_at"],
            datetime.fromisoformat("2026-08-19T18:34:10+00:00").timestamp(),
        )

    def test_mailapi_panel_does_not_scan_page_chrome_for_otp(self):
        mailbox = OutlookMailbox()
        backend = mailbox._backends["mailapi_url"]
        page = """
        <html><body>
          <section class="panel">
            <div class="subject">Mailbox status</div>
            <div class="content">No new message</div>
          </section>
          <footer>For help, use verification code 111111.</footer>
        </body></html>
        """

        message = backend._parse_mailapi_message(page)

        self.assertEqual(backend._extract_message_code(message, page, None), "")

    def test_mailapi_detail_discovery_ignores_cloudflare_email_protection(self):
        mailbox = OutlookMailbox()
        backend = mailbox._backends["mailapi_url"]
        page = """
        <html><body>
          <section class="panel">
            <a href="/cdn-cgi/l/email-protection" class="__cf_email__">
              [email protected]
            </a>
          </section>
        </body></html>
        """

        urls = backend._find_mailapi_detail_urls(
            page,
            "https://mail.example.test/mailbox/TOKEN",
        )

        self.assertEqual(urls, [])

    @mock.patch("requests.get")
    def test_mailapi_detail_without_code_falls_back_to_original_page(self, mock_get):
        mailbox = OutlookMailbox()
        account = MailboxAccount(
            email="demo@icloud.com",
            extra={
                "account_type": "mailapi_url",
                "mailapi_url": "https://mail.example.test/mailbox/TOKEN",
            },
        )
        list_page = """
        <html><body>
          <a href="/message/latest">View message</a>
          <div>Enter this temporary verification code to continue: 326097</div>
        </body></html>
        """

        def fake_get(url, **_kwargs):
            if url == account.extra["mailapi_url"]:
                return _FakeResponse(200, text=list_page)
            self.assertEqual(url, "https://mail.example.test/message/latest")
            return _FakeResponse(200, text="<html><body>Message unavailable</body></html>")

        mock_get.side_effect = fake_get

        with mock.patch.object(
            mailbox,
            "_run_polling_wait",
            side_effect=lambda **kwargs: kwargs["poll_once"](),
        ):
            code = mailbox.wait_for_code(account, timeout=5)

        self.assertEqual(code, "326097")
        self.assertEqual(mock_get.call_count, 2)

    @mock.patch("requests.get")
    def test_mailapi_detail_discovery_log_is_deduplicated(self, mock_get):
        logs = []
        mailbox = OutlookMailbox()
        mailbox._log_fn = logs.append
        backend = mailbox._backends["mailapi_url"]
        account = MailboxAccount(
            email="demo@icloud.com",
            extra={
                "account_type": "mailapi_url",
                "mailapi_url": "https://mail.example.test/mailbox/TOKEN",
            },
        )
        list_page = "<html><body><a href=\"/message/latest\">View</a></body></html>"
        mock_get.side_effect = [
            _FakeResponse(200, text=list_page),
            _FakeResponse(200, text="<html><body>Notice</body></html>"),
            _FakeResponse(200, text=list_page),
            _FakeResponse(200, text="<html><body>Notice</body></html>"),
        ]

        backend._fetch_mailapi_text(account)
        backend._fetch_mailapi_text(account)

        self.assertEqual(
            logs.count("[MailAPI] 入口为邮件列表，自动发现并跟进邮件详情"),
            1,
        )

    @mock.patch("requests.get")
    def test_mailapi_otp_log_redacts_the_code(self, mock_get):
        logs = []
        mailbox = OutlookMailbox()
        mailbox._log_fn = logs.append
        account = MailboxAccount(
            email="demo@icloud.com",
            extra={
                "account_type": "mailapi_url",
                "mailapi_url": "https://mail.example.test/latest",
            },
        )
        mock_get.return_value = _FakeResponse(
            200,
            text=(
                "<section class=\"panel\"><div class=\"meta\">"
                "<div class=\"subject\">Your temporary ChatGPT login code</div>"
                "<div class=\"label\">时间</div><div class=\"value\">"
                "2026-08-12 00:23:22</div></div>"
                "<div class=\"content\">Enter this temporary verification code "
                "to continue: 326097</div></section>"
            ),
        )

        with mock.patch.object(
            mailbox,
            "_run_polling_wait",
            side_effect=lambda **kwargs: kwargs["poll_once"](),
        ):
            code = mailbox.wait_for_code(account, timeout=5)
            skipped = mailbox.wait_for_code(
                account,
                timeout=5,
                exclude_codes={"326097"},
            )

        self.assertEqual(code, "326097")
        self.assertIsNone(skipped)
        rendered = "\n".join(logs)
        self.assertIn("[验证码已隐藏]", rendered)
        self.assertNotIn("326097", rendered)
        self.assertEqual(
            len([line for line in logs if "跳过已尝试验证码" in line]),
            1,
        )

    def test_mailapi_detail_discovery_skips_destructive_links(self):
        mailbox = OutlookMailbox()
        backend = mailbox._backends["mailapi_url"]
        page = """
        <html><body>
          <a href="/delete">Delete all messages</a>
          <a href="/logout">Sign out</a>
          <a href="/messages/latest">Latest message</a>
        </body></html>
        """

        urls = backend._find_mailapi_detail_urls(
            page,
            "https://mail.example.test/mailbox/TOKEN",
        )

        self.assertEqual(urls, ["https://mail.example.test/messages/latest"])

    @mock.patch("requests.get")
    def test_mailapi_html_list_follows_latest_verification_message_detail(
        self,
        mock_get,
    ):
        mailbox = OutlookMailbox()
        account = MailboxAccount(
            email="demo@outlook.com",
            extra={
                "account_type": "mailapi_url",
                "mailapi_url": "http://yangyang.website/messages/TOKEN/EMAIL",
            },
        )
        list_html = """
        <a class="item" href="#mail-111111" data-id="111111">
          <div class="subject">Verification code</div>
        </a>
        <div id="message-list">
          <a class="item active" href="#mail-509187" data-id="509187">
            <div class="subject">New login detected - change your password</div>
            <div class="time">2026-07-31 18:09:45</div>
          </a>
          <a class="item" href="#mail-459469" data-id="459469">
            <div class="subject">ChatGPT の一時的な認証コード</div>
          </a>
        </div>
        <script>
          var detailBase='/message/';
          var detailSuffix='/DETAIL_TOKEN/EMAIL';
        </script>
        """
        detail_payload = {
            "subject": "ChatGPT の一時的な認証コード",
            "fromAddress": "noreply@example.test",
            "receivedAt": "2026-07-31 18:09:45",
            "html": True,
            "body": (
                "data:text/html;charset=utf-8;base64,"
                + quote(
                    base64.b64encode(
                        b"<html><body>Your verification code is 246810</body></html>"
                    ).decode("ascii"),
                    safe="",
                )
            ),
        }

        redirect_response = _FakeResponse(302)
        redirect_response.cookies = {"mail_session": "redirect-cookie"}
        list_response = _FakeResponse(200, text=list_html)
        list_response.cookies = {}
        list_response.history = [redirect_response]

        def fake_get(url, **_kwargs):
            if url == account.extra["mailapi_url"]:
                return list_response
            if "/message/459469/" in url:
                return _FakeResponse(200, text=json.dumps(detail_payload))
            return _FakeResponse(
                200,
                text=json.dumps(
                    {
                        "subject": "New login detected - change your password",
                        "body": "Your subscription was updated.",
                    }
                ),
            )

        mock_get.side_effect = fake_get

        with mock.patch.object(
            mailbox,
            "_run_polling_wait",
            side_effect=lambda **kwargs: kwargs["poll_once"](),
        ):
            code = mailbox.wait_for_code(account, timeout=5)

        self.assertEqual(code, "246810")
        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(
            mock_get.call_args_list[1].args[0],
            "http://yangyang.website/message/459469/DETAIL_TOKEN/EMAIL",
        )
        self.assertEqual(
            mock_get.call_args_list[1].kwargs["cookies"].get("mail_session"),
            "redirect-cookie",
        )
        self.assertEqual(mock_get.call_args_list[1].kwargs["timeout"], 5)

    @mock.patch("requests.get")
    def test_mailapi_legacy_list_checks_chinese_login_code_after_newer_notice(
        self,
        mock_get,
    ):
        mailbox = OutlookMailbox()
        account = MailboxAccount(
            email="demo@icloud.com",
            extra={
                "account_type": "mailapi_url",
                "mailapi_url": "https://mail.example.test/messages/TOKEN/EMAIL",
            },
        )
        list_html = """
        <div id="message-list">
          <a class="item active" href="#mail-notice" data-id="notice">
            <div class="subject">New sign-in to your OpenAI account</div>
          </a>
          <a class="item" href="#mail-otp" data-id="otp">
            <div class="subject">你的临时 ChatGPT 登录代码</div>
          </a>
        </div>
        <script>
          var detailBase='/message/';
          var detailSuffix='/DETAIL_TOKEN/EMAIL';
        </script>
        """

        def fake_get(url, **_kwargs):
            if url == account.extra["mailapi_url"]:
                return _FakeResponse(200, text=list_html)
            if "/message/notice/" in url:
                return _FakeResponse(
                    200,
                    text=json.dumps(
                        {
                            "subject": "New sign-in to your OpenAI account",
                            "body": "A new sign-in was detected.",
                        }
                    ),
                )
            if "/message/otp/" in url:
                return _FakeResponse(
                    200,
                    text=json.dumps(
                        {
                            "subject": "你的临时 ChatGPT 登录代码",
                            "body": "输入此临时验证码以继续：975310",
                        }
                    ),
                )
            raise AssertionError(f"unexpected URL: {url}")

        mock_get.side_effect = fake_get

        with mock.patch.object(
            mailbox,
            "_run_polling_wait",
            side_effect=lambda **kwargs: kwargs["poll_once"](),
        ):
            code = mailbox.wait_for_code(account, timeout=5)

        self.assertEqual(code, "975310")
        self.assertEqual(mock_get.call_count, 2)

    @mock.patch("requests.get")
    def test_mailapi_html_notice_does_not_extract_six_digits_from_markup(
        self,
        mock_get,
    ):
        mailbox = OutlookMailbox()
        account = MailboxAccount(
            email="demo@icloud.com",
            extra={
                "account_type": "mailapi_url",
                "mailapi_url": "https://mail.example.test/latest",
            },
        )
        mock_get.return_value = _FakeResponse(
            200,
            text="""
            <html><body>
              <article class="mail-card" data-message-id="654321">
                <span class="subject">New sign-in to your OpenAI account</span>
                <span class="date">2026-08-06 03:14:56</span>
                <p>A new sign-in was detected on your account. Reference 654321.</p>
              </article>
            </body></html>
            """,
        )

        with mock.patch.object(
            mailbox,
            "_run_polling_wait",
            side_effect=lambda **kwargs: kwargs["poll_once"](),
        ):
            code = mailbox.wait_for_code(account, timeout=5)

        self.assertIsNone(code)

    @mock.patch("requests.get")
    def test_mailapi_list_poll_limits_slow_detail_fanout(self, mock_get):
        mailbox = OutlookMailbox()
        account = MailboxAccount(
            email="demo@icloud.com",
            extra={
                "account_type": "mailapi_url",
                "mailapi_url": "https://mail.example.test/inbox/TOKEN",
            },
        )
        links = "".join(
            f'<a href="/message/{index}">Message {index}</a>'
            for index in range(1, 7)
        )
        mock_get.side_effect = [
            _FakeResponse(200, text=f"<html><body>{links}</body></html>"),
            *[
                _FakeResponse(200, text="<html><body>No verification code</body></html>")
                for _ in range(6)
            ],
        ]

        with mock.patch.object(
            mailbox,
            "_run_polling_wait",
            side_effect=lambda **kwargs: kwargs["poll_once"](),
        ):
            code = mailbox.wait_for_code(account, timeout=5)

        self.assertIsNone(code)
        self.assertEqual(mock_get.call_count, 4)

    @mock.patch("requests.get")
    def test_mailapi_json_falls_back_to_nested_unknown_content(self, mock_get):
        mailbox = OutlookMailbox()
        account = MailboxAccount(
            email="demo@outlook.com",
            extra={
                "account_type": "mailapi_url",
                "mailapi_url": "https://mail.example.test/messages",
            },
        )
        mock_get.return_value = _FakeResponse(
            200,
            text=json.dumps(
                {
                    "subject": "ChatGPT login",
                    "data": {
                        "body": "Your verification code is 135790",
                    },
                }
            ),
        )

        with mock.patch.object(
            mailbox,
            "_run_polling_wait",
            side_effect=lambda **kwargs: kwargs["poll_once"](),
        ):
            code = mailbox.wait_for_code(account, timeout=5)

        self.assertEqual(code, "135790")

    @mock.patch("requests.get")
    def test_mailapi_html_follows_arbitrary_same_origin_message_link(
        self,
        mock_get,
    ):
        mailbox = OutlookMailbox()
        account = MailboxAccount(
            email="demo@icloud.com",
            extra={
                "account_type": "mailapi_url",
                "mailapi_url": "https://mail.example.test/inbox/TOKEN",
            },
        )
        list_response = _FakeResponse(
            200,
            text="""
            <html><body>
              <article class="message-row" data-message-id="old">
                <a href="/v1/messages/old/read">Old notice</a>
              </article>
              <article class="message-row" data-message-id="new">
                <a href="/api/v3/mail/detail/new?view=full">
                  OpenAI login verification code
                </a>
              </article>
            </body></html>
            """,
        )
        detail_response = _FakeResponse(
            200,
            text=json.dumps(
                {
                    "subject": "OpenAI login verification code",
                    "body": "Your verification code is 975310",
                    "message_id": "new",
                }
            ),
        )

        def fake_get(url, **kwargs):
            if url == account.extra["mailapi_url"]:
                return list_response
            self.assertEqual(
                url,
                "https://mail.example.test/api/v3/mail/detail/new?view=full",
            )
            self.assertNotIn("mail.example.test", kwargs.get("cookies", {}))
            return detail_response

        mock_get.side_effect = fake_get

        with mock.patch.object(
            mailbox,
            "_run_polling_wait",
            side_effect=lambda **kwargs: kwargs["poll_once"](),
        ):
            code = mailbox.wait_for_code(account, timeout=5)

        self.assertEqual(code, "975310")
        self.assertEqual(mock_get.call_count, 2)

    @mock.patch("requests.get")
    def test_mailapi_html_card_with_ambiguous_code_still_follows_detail(
        self,
        mock_get,
    ):
        mailbox = OutlookMailbox()
        account = MailboxAccount(
            email="demo@icloud.com",
            extra={
                "account_type": "mailapi_url",
                "mailapi_url": "https://mail.example.test/inbox/TOKEN",
            },
        )
        list_response = _FakeResponse(
            200,
            text="""
            <html><body>
              <article class="mail-card">
                <span class="subject">OpenAI login verification code</span>

                <span class="preview">246810</span>
                <a href="/api/messages/latest">View message</a>
                <span class="date">2026-08-10 13:00:03</span>
              </article>
            </body></html>
            """,
        )
        detail_response = _FakeResponse(
            200,
            text=json.dumps(
                {
                    "subject": "OpenAI login verification code",
                    "body": "Your verification code is 246810",
                    "received_at": "2026-08-10T13:00:03+08:00",
                    "message_id": "latest",
                }
            ),
        )

        def fake_get(url, **_kwargs):
            if url == account.extra["mailapi_url"]:
                return list_response
            self.assertEqual(
                url,
                "https://mail.example.test/api/messages/latest",
            )
            return detail_response

        mock_get.side_effect = fake_get

        with mock.patch.object(
            mailbox,
            "_run_polling_wait",
            side_effect=lambda **kwargs: kwargs["poll_once"](),
        ):
            code = mailbox.wait_for_code(account, timeout=5)

        self.assertEqual(code, "246810")
        self.assertEqual(mock_get.call_count, 2)

    @mock.patch("requests.get")
    def test_mailapi_direct_json_code_wins_over_unrelated_url_field(
        self,
        mock_get,
    ):
        mailbox = OutlookMailbox()
        account = MailboxAccount(
            email="demo@icloud.com",
            extra={
                "account_type": "mailapi_url",
                "mailapi_url": "https://mail.example.test/inbox/TOKEN",
            },
        )
        mock_get.return_value = _FakeResponse(
            200,
            text=json.dumps(
                {
                    "body": "Your verification code is 246810",
                    "url": "/account/settings",
                }
            ),
        )

        with mock.patch.object(
            mailbox,
            "_run_polling_wait",
            side_effect=lambda **kwargs: kwargs["poll_once"](),
        ):
            code = mailbox.wait_for_code(account, timeout=5)

        self.assertEqual(code, "246810")
        self.assertEqual(mock_get.call_count, 1)

    @mock.patch("requests.get")
    def test_mailapi_json_list_does_not_treat_numeric_message_id_as_code(
        self,
        mock_get,
    ):
        mailbox = OutlookMailbox()
        account = MailboxAccount(
            email="demo@icloud.com",
            extra={
                "account_type": "mailapi_url",
                "mailapi_url": "https://mail.example.test/inbox/TOKEN",
            },
        )
        list_response = _FakeResponse(
            200,
            text=json.dumps(
                {
                    "messages": [
                        {
                            "id": "123456",
                            "subject": "OpenAI login verification code",
                            "detail": "/api/messages/123456",
                        }
                    ]
                }
            ),
        )
        detail_response = _FakeResponse(
            200,
            text=json.dumps(
                {"body": "Your verification code is 864209"}
            ),
        )
        mock_get.side_effect = [list_response, detail_response]

        with mock.patch.object(
            mailbox,
            "_run_polling_wait",
            side_effect=lambda **kwargs: kwargs["poll_once"](),
        ):
            code = mailbox.wait_for_code(account, timeout=5)

        self.assertEqual(code, "864209")
        self.assertEqual(mock_get.call_count, 2)

    def test_mailapi_decodes_percent_encoded_base64_data_uri(self):
        mailbox = OutlookMailbox()
        backend = mailbox._backends["mailapi_url"]
        encoded_body = quote(
            base64.b64encode(b"Your verification code is 864209").decode("ascii"),
            safe="",
        )

        decoded = backend._decode_mailapi_data_uri(
            "data:text/html;charset=utf-8;base64," + encoded_body
        )

        self.assertEqual(decoded, "Your verification code is 864209")

    @mock.patch("requests.get")
    def test_mailapi_json_rejects_message_received_before_otp_was_sent(self, mock_get):
        mailbox = OutlookMailbox()
        account = MailboxAccount(
            email="demo@icloud.com",
            extra={
                "account_type": "mailapi_url",
                "mailapi_url": "https://mail.example.test/messages",
            },
        )
        mock_get.return_value = _FakeResponse(
            200,
            text=json.dumps(
                {
                    "email": "demo@icloud.com",
                    "msg": "Enter this temporary verification code to continue:\n246810",
                    "received_at": "2026-07-31T03:28:01Z",
                    "request_id": "message-old",
                    "status": True,
                    "subject": "Your temporary ChatGPT login code",
                }
            ),
        )
        otp_sent_at = datetime.fromisoformat(
            "2026-07-31T03:29:01+00:00"
        ).timestamp()

        with mock.patch.object(
            mailbox,
            "_run_polling_wait",
            side_effect=lambda **kwargs: kwargs["poll_once"](),
        ):
            code = mailbox.wait_for_code(
                account,
                timeout=5,
                before_ids=set(),
                otp_sent_at=otp_sent_at,
            )

        self.assertIsNone(code)

    @mock.patch("requests.get")
    def test_mailapi_logs_the_same_old_message_only_once_per_challenge(self, mock_get):
        logs = []
        mailbox = OutlookMailbox()
        mailbox._log_fn = logs.append
        account = MailboxAccount(
            email="demo@icloud.com",
            extra={
                "account_type": "mailapi_url",
                "mailapi_url": "https://mail.example.test/messages",
            },
        )
        mock_get.return_value = _FakeResponse(
            200,
            text=json.dumps(
                {
                    "email": "demo@icloud.com",
                    "msg": "Enter this temporary verification code: 246810",
                    "received_at": "2026-08-21T00:03:30+08:00",
                    "request_id": "message-old",
                    "status": True,
                    "subject": "Your temporary ChatGPT login code",
                }
            ),
        )
        otp_sent_at = datetime.fromisoformat(
            "2026-08-21T00:04:06+08:00"
        ).timestamp()

        def poll_twice(**kwargs):
            self.assertIsNone(kwargs["poll_once"]())
            return kwargs["poll_once"]()

        with mock.patch.object(
            mailbox,
            "_run_polling_wait",
            side_effect=poll_twice,
        ):
            code = mailbox.wait_for_code(
                account,
                timeout=5,
                before_ids=set(),
                otp_sent_at=otp_sent_at,
            )

        self.assertIsNone(code)
        self.assertEqual(
            logs.count(
                "[MailAPI] 当前只有发送前的旧验证码，继续等待新邮件"
            ),
            1,
        )

    @mock.patch("requests.get")
    def test_mailapi_json_accepts_new_message_even_if_baseline_raced(self, mock_get):
        mailbox = OutlookMailbox()
        account = MailboxAccount(
            email="demo@icloud.com",
            extra={
                "account_type": "mailapi_url",
                "mailapi_url": "https://mail.example.test/messages",
            },
        )
        mock_get.return_value = _FakeResponse(
            200,
            text=json.dumps(
                {
                    "email": "demo@icloud.com",
                    "msg": "Enter this temporary verification code to continue:\n246810",
                    "received_at": "2026-07-31T03:28:01Z",
                    "request_id": "message-new",
                    "status": True,
                    "subject": "Your temporary ChatGPT login code",
                }
            ),
        )
        otp_sent_at = datetime.fromisoformat(
            "2026-07-31T03:28:00+00:00"
        ).timestamp()
        before_ids = mailbox.get_current_ids(account)

        with mock.patch.object(
            mailbox,
            "_run_polling_wait",
            side_effect=lambda **kwargs: kwargs["poll_once"](),
        ):
            code = mailbox.wait_for_code(
                account,
                timeout=5,
                before_ids=before_ids,
                otp_sent_at=otp_sent_at,
            )

        self.assertEqual(code, "246810")

    @mock.patch("requests.get")
    def test_mailapi_accepts_reused_code_from_new_challenge_email(self, mock_get):
        mailbox = OutlookMailbox()
        account = MailboxAccount(
            email="demo@icloud.com",
            extra={
                "account_type": "mailapi_url",
                "mailapi_url": "https://mail.example.test/messages",
            },
        )
        mock_get.return_value = _FakeResponse(
            200,
            text=json.dumps(
                {
                    "email": "demo@icloud.com",
                    "msg": "Enter this temporary verification code to continue:\n246810",
                    "received_at": "2026-07-31T03:28:01Z",
                    "request_id": "mfa-message-new",
                    "status": True,
                    "subject": "Your authentication code",
                }
            ),
        )
        otp_sent_at = datetime.fromisoformat(
            "2026-07-31T03:28:00.900+00:00"
        ).timestamp()

        with mock.patch.object(
            mailbox,
            "_run_polling_wait",
            side_effect=lambda **kwargs: kwargs["poll_once"](),
        ):
            code = mailbox.wait_for_code(
                account,
                timeout=5,
                before_ids=set(),
                otp_sent_at=otp_sent_at,
                exclude_codes={"246810"},
            )

        self.assertEqual(code, "246810")

    @mock.patch("requests.get")
    def test_mailapi_accepts_reused_code_with_chinese_beijing_timestamp(self, mock_get):
        mailbox = OutlookMailbox()
        account = MailboxAccount(
            email="demo@icloud.com",
            extra={
                "account_type": "mailapi_url",
                "mailapi_url": "https://mail.example.test/latest",
            },
        )
        mock_get.return_value = _FakeResponse(
            200,
            text=(
                '<section class="panel">'
                '<div class="label">接收时间：</div>'
                '<div class="value">2026年08月12日 23:52:49（北京时间）</div>'
                '<div class="subject">Your authentication code</div>'
                '<div class="content">Your authentication code is 574876</div>'
                '</section>'
            ),
        )
        otp_sent_at = datetime.fromisoformat(
            "2026-08-12T23:52:48+08:00"
        ).timestamp()

        with mock.patch.object(
            mailbox,
            "_run_polling_wait",
            side_effect=lambda **kwargs: kwargs["poll_once"](),
        ):
            code = mailbox.wait_for_code(
                account,
                timeout=5,
                before_ids=set(),
                otp_sent_at=otp_sent_at,
                exclude_codes={"574876"},
            )

        self.assertEqual(code, "574876")

    @mock.patch("requests.get")
    def test_mailapi_accepts_latest_iframe_email_with_chinese_time(self, mock_get):
        mailbox = OutlookMailbox()
        account = MailboxAccount(
            email="demo@icloud.com",
            extra={
                "account_type": "mailapi_url",
                "mailapi_url": "https://mail.example.test/latest",
            },
        )
        mock_get.return_value = _FakeResponse(
            200,
            text=(
                '<div class="container"><h1>最新邮件信息</h1>'
                '<div class="info"><div class="label">接收时间：</div>'
                '<div class="time">2026年08月13日 00:19:55 (北京时间)</div></div>'
                '<div class="info"><div class="label">邮件主题：</div>'
                '<div class="subject">Your authentication code</div></div>'
                '<iframe class="email-frame" id="emailFrame"></iframe></div>'
                '<script>var htmlContent = "Your authentication code is 417024";'
                '</script>'
            ),
        )
        otp_sent_at = datetime.fromisoformat(
            "2026-08-13T00:19:54+08:00"
        ).timestamp()

        with mock.patch.object(
            mailbox,
            "_run_polling_wait",
            side_effect=lambda **kwargs: kwargs["poll_once"](),
        ):
            code = mailbox.wait_for_code(
                account,
                timeout=5,
                before_ids=set(),
                otp_sent_at=otp_sent_at,
                exclude_codes={"417024"},
            )

        self.assertEqual(code, "417024")

    @mock.patch("requests.get")
    def test_mailapi_latest_view_uses_current_card_before_stale_detail(
        self,
        mock_get,
    ):
        mailbox = OutlookMailbox()
        account = MailboxAccount(
            email="demo@outlook.com",
            extra={
                "account_type": "mailapi_url",
                "mailapi_url": "https://mail.example.test/latest",
            },
        )
        mock_get.side_effect = [
            _FakeResponse(
                200,
                text="""
                <html><body>
                  <main class="mailbox" data-view="latest">
                    <nav><a href="/history">查看全部</a></nav>
                    <div class="card">
                      <div class="code" aria-label="验证码">654321</div>
                      <div class="su">Your temporary ChatGPT login code</div>
                      <time class="dt">2026-08-18T09:16:18+00:00</time>
                      <div class="bd content">
                        Enter this temporary verification code to continue:
                        654321
                      </div>
                    </div>
                  </main>
                </body></html>
                """,
            ),
            _FakeResponse(
                200,
                text=(
                    '<section class="panel">'
                    '<div class="subject">Your authentication code</div>'
                    '<div class="content">Your authentication code is 111111</div>'
                    '</section>'
                ),
            ),
        ]
        otp_sent_at = datetime.fromisoformat(
            "2026-08-18T09:16:17+00:00"
        ).timestamp()

        with mock.patch.object(
            mailbox,
            "_run_polling_wait",
            side_effect=lambda **kwargs: kwargs["poll_once"](),
        ):
            code = mailbox.wait_for_code(
                account,
                timeout=5,
                before_ids=set(),
                otp_sent_at=otp_sent_at,
            )

        self.assertEqual(code, "654321")
        self.assertEqual(mock_get.call_count, 1)

    @mock.patch("requests.get")
    def test_mailapi_html_rejects_old_latest_card(self, mock_get):
        mailbox = OutlookMailbox()
        account = MailboxAccount(
            email="demo@icloud.com",
            extra={
                "account_type": "mailapi_url",
                "mailapi_url": "https://mail.example.test/latest",
            },
        )
        mock_get.return_value = _FakeResponse(
            200,
            text="""
            <html><body>
              <article class="mail-card">
                <details>
                  <summary>
                    <span class="subject">Your temporary ChatGPT login code</span>
                    <span class="date">2026-08-05 19:25:00</span>
                  </summary>
                  <pre class="body">Your verification code is 246810</pre>
                </details>
              </article>
              <article class="mail-card">
                <details>
                  <summary>
                    <span class="subject">Your temporary ChatGPT login code</span>
                    <span class="date">2026-08-05 19:00:00</span>
                  </summary>
                  <pre class="body">Your verification code is 135790</pre>
                </details>
              </article>
            </body></html>
            """,
        )
        before_ids = mailbox.get_current_ids(account)
        otp_sent_at = datetime.fromisoformat("2026-08-05T19:25:30").timestamp()

        with mock.patch.object(
            mailbox,
            "_run_polling_wait",
            side_effect=lambda **kwargs: kwargs["poll_once"](),
        ):
            code = mailbox.wait_for_code(
                account,
                timeout=5,
                before_ids=before_ids,
                otp_sent_at=otp_sent_at,
            )

        self.assertEqual(len(before_ids), 1)
        self.assertTrue(next(iter(before_ids)).startswith("mailapi_message:"))
        self.assertIsNone(code)

    @mock.patch("requests.get")
    def test_mailapi_html_accepts_new_card_with_second_precision(self, mock_get):
        mailbox = OutlookMailbox()
        account = MailboxAccount(
            email="demo@icloud.com",
            extra={
                "account_type": "mailapi_url",
                "mailapi_url": "https://mail.example.test/latest",
            },
        )
        old_html = """
        <html><body>
          <article class="mail-card">
            <details>
              <summary>
                <span class="subject">Your temporary ChatGPT login code</span>
                <span class="date">2026-08-05 19:25:00</span>
              </summary>
              <pre class="body">Your verification code is 246810</pre>
            </details>
          </article>
        </body></html>
        """
        new_html = """
        <html><body>
          <article class="mail-card">
            <details>
              <summary>
                <span class="subject">Your temporary ChatGPT login code</span>
                <span class="date">2026-08-05 19:25:44</span>
              </summary>
              <pre class="body">Your verification code is 975310</pre>
            </details>
          </article>
          <article class="mail-card">
            <details>
              <summary>
                <span class="subject">Your temporary ChatGPT login code</span>
                <span class="date">2026-08-05 19:25:00</span>
              </summary>
              <pre class="body">Your verification code is 246810</pre>
            </details>
          </article>
        </body></html>
        """
        mock_get.side_effect = [
            _FakeResponse(200, text=old_html),
            _FakeResponse(200, text=new_html),
        ]
        before_ids = mailbox.get_current_ids(account)
        otp_sent_at = datetime.fromisoformat(
            "2026-08-05T19:25:44.900"
        ).timestamp()

        with mock.patch.object(
            mailbox,
            "_run_polling_wait",
            side_effect=lambda **kwargs: kwargs["poll_once"](),
        ):
            code = mailbox.wait_for_code(
                account,
                timeout=5,
                before_ids=before_ids,
                otp_sent_at=otp_sent_at,
            )

        self.assertEqual(code, "975310")
        self.assertEqual(len(before_ids), 1)
        self.assertTrue(next(iter(before_ids)).startswith("mailapi_message:"))

    @mock.patch("requests.get")
    def test_mailapi_html_does_not_permanently_suppress_raced_baseline_code(
        self,
        mock_get,
    ):
        mailbox = OutlookMailbox()
        account = MailboxAccount(
            email="demo@icloud.com",
            extra={
                "account_type": "mailapi_url",
                "mailapi_url": "https://mail.example.test/latest",
            },
        )
        mock_get.return_value = _FakeResponse(
            200,
            text=(
                "<html><body>"
                "Your temporary ChatGPT login code is 246810"
                "</body></html>"
            ),
        )
        raced_baseline = mailbox.get_current_ids(account)

        with mock.patch.object(
            mailbox,
            "_run_polling_wait",
            side_effect=lambda **kwargs: kwargs["poll_once"](),
        ):
            code = mailbox.wait_for_code(
                account,
                timeout=5,
                before_ids=raced_baseline,
                otp_sent_at=datetime.now().timestamp(),
            )

        self.assertEqual(raced_baseline, {"mailapi_code:246810"})
        self.assertEqual(code, "246810")

    @mock.patch("requests.get")
    def test_mailapi_poll_error_log_does_not_expose_url_email_or_token(self, mock_get):
        logs = []
        mailbox = OutlookMailbox()
        mailbox._log_fn = logs.append
        email = "private-user@example.com"
        token = "mailapi-secret-token"
        mailapi_url = (
            "https://mail.example.test/messages"
            f"?email={email}&token={token}"
        )
        account = MailboxAccount(
            email=email,
            extra={
                "account_type": "mailapi_url",
                "mailapi_url": mailapi_url,
            },
        )
        mock_get.side_effect = RuntimeError(
            f"GET {mailapi_url} failed for {email}; token={token}"
        )

        with mock.patch.object(
            mailbox,
            "_run_polling_wait",
            side_effect=lambda **kwargs: kwargs["poll_once"](),
        ):
            code = mailbox.wait_for_code(account, timeout=5)

        self.assertIsNone(code)
        rendered_logs = "\n".join(logs)
        self.assertIn("[MailAPI] 拉取失败", rendered_logs)
        self.assertIn("RuntimeError", rendered_logs)
        self.assertNotIn(mailapi_url, rendered_logs)
        self.assertNotIn(email, rendered_logs)
        self.assertNotIn(token, rendered_logs)
        self.assertNotIn("?", rendered_logs)

    @mock.patch("requests.post")
    @mock.patch("requests.request")
    def test_wait_for_code_reads_deleteditems_folder_when_inbox_has_no_new_code(self, mock_request, mock_post):
        mailbox = OutlookMailbox(token_endpoint="https://token.example.test")
        account = MailboxAccount(
            email="demo@outlook.com",
            extra={
                "client_id": "client-id",
                "refresh_token": "refresh-token",
            },
        )
        mock_post.return_value = _FakeResponse(
            200,
            payload={"access_token": "graph-token", "expires_in": 3600},
            text='{"access_token":"graph-token","expires_in":3600}',
        )
        mock_request.side_effect = [
            _FakeResponse(200, payload={"value": []}),
            _FakeResponse(200, payload={"value": []}),
            _FakeResponse(
                200,
                payload={
                    "value": [
                        {
                            "id": "deleted-message-1",
                            "subject": "OpenAI verification code",
                            "bodyPreview": "Your verification code is 654321",
                        }
                    ]
                },
            ),
        ]

        code = mailbox.wait_for_code(account, timeout=5)

        self.assertEqual(code, "654321")
        requested_urls = [str(call.args[1]) for call in mock_request.call_args_list]
        self.assertTrue(any("/me/mailFolders/deleteditems/messages" in url for url in requested_urls))

    @mock.patch("requests.post")
    def test_fetch_oauth_token_returns_empty_when_probe_gets_malformed_json_on_2xx(self, mock_post):
        mailbox = OutlookMailbox(token_endpoint="https://token.example.test")
        mock_post.side_effect = [
            _FakeResponse(
                200,
                text="not-json",
                json_error=ValueError("malformed json"),
            ),
            _FakeResponse(
                200,
                text="still-not-json",
                json_error=ValueError("malformed json again"),
            ),
        ]

        token = mailbox._fetch_oauth_token(
            email="demo@outlook.com",
            client_id="client-id",
            refresh_token="refresh-token",
        )

        self.assertEqual(token, "")
        attempted_scopes = [
            call.kwargs["data"].get("scope", "")
            for call in mock_post.call_args_list
        ]
        self.assertIn(
            "https://graph.microsoft.com/.default",
            attempted_scopes,
        )
        self.assertIn(
            "https://outlook.office.com/.default offline_access",
            attempted_scopes,
        )


if __name__ == "__main__":
    unittest.main()
