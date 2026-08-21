import base64
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlmodel import SQLModel, Session, create_engine, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.db import OutlookAccountModel
from core.applemail_pool import parse_applemail_pool_import_content


def fixture_yisen_jwt(address: str) -> str:
    def encode(payload: dict) -> str:
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return ".".join(
        (
            encode({"alg": "HS256", "typ": "JWT"}),
            encode({"address": address, "address_id": 12345}),
            "fixture-signature",
        )
    )


def load_microsoft_import_rules_module():
    module_path = (
        Path(__file__).resolve().parents[1]
        / "services"
        / "mail_imports"
        / "microsoft_import_rules.py"
    )
    spec = importlib.util.spec_from_file_location("test_microsoft_import_rules", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class MailImportServiceTests(unittest.TestCase):
    def test_outlook_schema_migration_adds_mailapi_token_to_existing_table(self):
        import core.db as db_module

        with tempfile.TemporaryDirectory() as tmp_dir:
            test_engine = create_engine(
                f"sqlite:///{Path(tmp_dir) / 'legacy-mail-imports.db'}"
            )
            try:
                with test_engine.begin() as connection:
                    connection.exec_driver_sql(
                        "CREATE TABLE outlook_accounts ("
                        "id INTEGER PRIMARY KEY, email TEXT, password TEXT, "
                        "account_type TEXT, mailapi_url TEXT)"
                    )
                    connection.exec_driver_sql(
                        "INSERT INTO outlook_accounts "
                        "(email, password, account_type, mailapi_url) VALUES "
                        "('legacy@example.test', 'password', 'mailapi_url', NULL)"
                    )

                with patch.object(db_module, "engine", test_engine):
                    db_module._migrate_outlook_accounts_schema()

                with test_engine.connect() as connection:
                    columns = {
                        str(row[1])
                        for row in connection.exec_driver_sql(
                            "PRAGMA table_info('outlook_accounts')"
                        ).fetchall()
                    }
                    row = connection.exec_driver_sql(
                        "SELECT mailapi_url, mailapi_token "
                        "FROM outlook_accounts WHERE email = 'legacy@example.test'"
                    ).one()
                self.assertIn("mailapi_token", columns)
                self.assertEqual(tuple(row), ("", ""))
            finally:
                test_engine.dispose()

    def test_parse_microsoft_import_line_supports_yisen_password_and_jwt(self):
        rules_module = load_microsoft_import_rules_module()
        parse_microsoft_import_line = rules_module.parse_microsoft_import_line
        token = fixture_yisen_jwt("worker@yisen.uk")

        record = parse_microsoft_import_line(
            1,
            f"worker@yisen.uk----login-password----{token}",
        )

        self.assertEqual(record.email, "worker@yisen.uk")
        self.assertEqual(record.password, "login-password")
        self.assertEqual(record.account_type, "mailapi_url")
        self.assertEqual(record.mailapi_token, token)
        self.assertEqual(
            record.mailapi_url,
            "https://mail.yisen.uk/api/mails"
            "?login=worker%40yisen.uk&limit=20&offset=0",
        )

    def test_parse_yisen_row_restores_markdown_escaped_email_and_jwt(self):
        rules_module = load_microsoft_import_rules_module()
        parse_microsoft_import_line = rules_module.parse_microsoft_import_line
        token = fixture_yisen_jwt("worker@yisen.uk").replace(
            "fixture-signature",
            "fixture_signature",
        )
        escaped_token = token.replace("_", r"\\\_")

        record = parse_microsoft_import_line(
            1,
            rf"worker\\\@yisen.uk----login-password----{escaped_token}",
        )

        self.assertEqual(record.email, "worker@yisen.uk")
        self.assertEqual(record.mailapi_token, token)

    def test_parse_yisen_row_preserves_dash_runs_inside_jwt_signature(self):
        rules_module = load_microsoft_import_rules_module()
        parse_microsoft_import_line = rules_module.parse_microsoft_import_line
        token = fixture_yisen_jwt("worker@yisen.uk").replace(
            "fixture-signature",
            "fixture---signature",
        )

        record = parse_microsoft_import_line(
            1,
            f"worker@yisen.uk----login-password----{token}",
        )

        self.assertEqual(record.mailapi_token, token)

    def test_parse_microsoft_import_line_rejects_mismatched_yisen_jwt(self):
        rules_module = load_microsoft_import_rules_module()
        parse_microsoft_import_line = rules_module.parse_microsoft_import_line
        token = fixture_yisen_jwt("other@yisen.uk")

        with self.assertRaisesRegex(ValueError, "邮箱不匹配"):
            parse_microsoft_import_line(
                1,
                f"worker@yisen.uk----login-password----{token}",
            )

    def test_parse_microsoft_import_line_rejects_malformed_yisen_jwt_safely(self):
        rules_module = load_microsoft_import_rules_module()
        parse_microsoft_import_line = rules_module.parse_microsoft_import_line

        with self.assertRaisesRegex(ValueError, "JWT 格式无效"):
            parse_microsoft_import_line(
                1,
                "worker@yisen.uk----login-password----@@@.%%%.signature",
            )

    def test_microsoft_strategy_persists_yisen_token_without_exposing_it(self):
        from services.mail_imports.providers import MicrosoftMailImportStrategy
        from services.mail_imports.schemas import MailImportExecuteRequest

        token = fixture_yisen_jwt("worker@yisen.uk")
        strategy = MicrosoftMailImportStrategy()
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_engine = create_engine(
                f"sqlite:///{Path(tmp_dir) / 'mail-imports.db'}"
            )
            SQLModel.metadata.create_all(test_engine)
            try:
                with patch("services.mail_imports.providers.engine", test_engine):
                    response = strategy.execute(
                        MailImportExecuteRequest(
                            type="microsoft",
                            content=(
                                "worker@yisen.uk----login-password----"
                                f"{token}"
                            ),
                            bind_to_config=False,
                        )
                    )

                self.assertEqual(response.summary.success, 1)
                self.assertNotIn(token, response.model_dump_json())
                with Session(test_engine) as session:
                    imported = session.exec(
                        select(OutlookAccountModel).where(
                            OutlookAccountModel.email == "worker@yisen.uk"
                        )
                    ).one()
                self.assertEqual(imported.account_type, "mailapi_url")
                self.assertEqual(imported.mailapi_token, token)
            finally:
                test_engine.dispose()

    def test_microsoft_snapshot_only_counts_enabled_accounts(self):
        from services.mail_imports.providers import MicrosoftMailImportStrategy
        from services.mail_imports.schemas import MailImportSnapshotRequest

        strategy = MicrosoftMailImportStrategy()
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_engine = create_engine(
                f"sqlite:///{Path(tmp_dir) / 'mail-imports.db'}"
            )
            SQLModel.metadata.create_all(test_engine)
            try:
                with Session(test_engine) as session:
                    session.add(
                        OutlookAccountModel(
                            email="available@example.com",
                            password="",
                            account_type="mailapi_url",
                            mailapi_url="https://mail.example.test/available",
                            enabled=True,
                        )
                    )
                    session.add(
                        OutlookAccountModel(
                            email="consumed@example.com",
                            password="",
                            account_type="mailapi_url",
                            mailapi_url="https://mail.example.test/consumed",
                            enabled=False,
                        )
                    )
                    session.commit()

                with patch("services.mail_imports.providers.engine", test_engine):
                    snapshot = strategy.get_snapshot(
                        MailImportSnapshotRequest(
                            type="microsoft",
                            preview_limit=10,
                        )
                    )

                self.assertEqual(snapshot.count, 1)
                self.assertEqual(
                    [item.email for item in snapshot.items],
                    ["available@example.com"],
                )
            finally:
                test_engine.dispose()

    def test_parse_microsoft_import_record_requires_oauth_fields(self):
        rules_module = load_microsoft_import_rules_module()
        parse_microsoft_import_record = rules_module.parse_microsoft_import_record

        with self.assertRaisesRegex(ValueError, "缺少 client_id 或 refresh_token"):
            parse_microsoft_import_record(1, "demo@outlook.com----password")

    def test_parse_microsoft_import_line_supports_mailapi_url(self):
        rules_module = load_microsoft_import_rules_module()
        parse_microsoft_import_line = rules_module.parse_microsoft_import_line

        record = parse_microsoft_import_line(
            1,
            "demo@outlook.com----https://mailapi.icu/key?type=html&orderNo=abc123",
        )

        self.assertEqual(record.email, "demo@outlook.com")
        self.assertEqual(record.account_type, "mailapi_url")
        self.assertEqual(record.mailapi_url, "https://mailapi.icu/key?type=html&orderNo=abc123")

    def test_parse_microsoft_import_line_unwraps_markdown_mailapi_url(self):
        rules_module = load_microsoft_import_rules_module()
        parse_microsoft_import_line = rules_module.parse_microsoft_import_line
        secret_url = "https://mail.example.test/messages?token=super-secret"

        record = parse_microsoft_import_line(
            1,
            f"demo@example.com----[{secret_url}]({secret_url})",
        )

        self.assertEqual(record.account_type, "mailapi_url")
        self.assertEqual(record.mailapi_url, secret_url)

    def test_parse_microsoft_import_line_supports_chatgpt_password_and_mailapi_url(self):
        rules_module = load_microsoft_import_rules_module()
        parse_microsoft_import_line = rules_module.parse_microsoft_import_line

        record = parse_microsoft_import_line(
            1,
            "demo@icloud.com----ChatGPT-Password-2026!----https://mail.example.test/messages",
        )

        self.assertEqual(record.email, "demo@icloud.com")
        self.assertEqual(record.password, "ChatGPT-Password-2026!")
        self.assertEqual(record.account_type, "mailapi_url")
        self.assertEqual(
            record.mailapi_url,
            "https://mail.example.test/messages",
        )

    def test_three_dash_delimiter_preserves_single_and_double_hyphens(self):
        rules_module = load_microsoft_import_rules_module()
        parse_microsoft_import_line = rules_module.parse_microsoft_import_line

        record = parse_microsoft_import_line(
            1,
            "demo@outlook.com---Password-2026--safe---client-id---refresh-token",
        )

        self.assertEqual(record.email, "demo@outlook.com")
        self.assertEqual(record.password, "Password-2026--safe")
        self.assertEqual(record.client_id, "client-id")
        self.assertEqual(record.refresh_token, "refresh-token")

    def test_applemail_import_accepts_three_dash_delimiter(self):
        records, errors, total = parse_applemail_pool_import_content(
            "demo@icloud.com---Password-2026--safe---"
            "QM5QPLWGNKZYUQDWSCBDJIJUGXEHIQA3"
        )

        self.assertEqual(total, 1)
        self.assertEqual(errors, [])
        self.assertEqual(records[0]["email"], "demo@icloud.com")
        self.assertEqual(records[0]["password"], "Password-2026--safe")

    def test_rule_engine_returns_first_failure(self):
        rules_module = load_microsoft_import_rules_module()
        MicrosoftMailImportRecord = rules_module.MicrosoftMailImportRecord
        MicrosoftMailImportRuleEngine = rules_module.MicrosoftMailImportRuleEngine

        calls = []

        class FirstRejectRule:
            def evaluate(self, record, context):
                calls.append("first")
                return {"ok": False, "message": f"reject:{record.email}"}

        class SecondRuleMustNotRun:
            def evaluate(self, record, context):
                calls.append("second")
                raise AssertionError("second rule should not be executed after first failure")

        engine = MicrosoftMailImportRuleEngine([FirstRejectRule(), SecondRuleMustNotRun()])
        record = MicrosoftMailImportRecord(
            line_number=1,
            email="demo@outlook.com",
            password="password",
            client_id="client-id",
            refresh_token="refresh-token",
        )

        result = engine.evaluate(record, {})
        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], "reject:demo@outlook.com")
        self.assertEqual(calls, ["first"])

    def test_duplicate_email_rule_rejects_existing_account(self):
        rules_module = load_microsoft_import_rules_module()
        DuplicateMicrosoftMailboxRule = rules_module.DuplicateMicrosoftMailboxRule
        MicrosoftMailImportRecord = rules_module.MicrosoftMailImportRecord

        rule = DuplicateMicrosoftMailboxRule()
        record = MicrosoftMailImportRecord(
            line_number=2,
            email="demo@outlook.com",
            password="password",
            client_id="client-id",
            refresh_token="refresh-token",
        )

        result = rule.evaluate(record, {"existing_emails": {"demo@outlook.com"}})
        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], "行 2: 邮箱已存在: demo@outlook.com")

    def test_microsoft_mailbox_availability_rule_rejects_service_abuse_mode(self):
        rules_module = load_microsoft_import_rules_module()
        MicrosoftMailImportRecord = rules_module.MicrosoftMailImportRecord
        MicrosoftMailboxAvailabilityRule = rules_module.MicrosoftMailboxAvailabilityRule

        class FakeMailbox:
            def probe_oauth_availability(self, **kwargs):
                return {
                    "ok": False,
                    "reason": "service_abuse_mode",
                    "message": "微软邮箱可用性检测未通过，账号处于 service abuse mode",
                }

        rule = MicrosoftMailboxAvailabilityRule(FakeMailbox())
        record = MicrosoftMailImportRecord(
            line_number=5,
            email="demo@hotmail.com",
            password="password",
            client_id="client-id",
            refresh_token="refresh-token",
        )

        result = rule.evaluate(record, {})
        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], "行 5: 微软邮箱可用性检测未通过，账号处于 service abuse mode")

    def test_applemail_strategy_saves_pool_and_returns_snapshot(self):
        from services.mail_imports.providers import AppleMailImportStrategy
        from services.mail_imports.schemas import MailImportExecuteRequest

        strategy = AppleMailImportStrategy()
        with tempfile.TemporaryDirectory() as tmp_dir:
            previous_cwd = os.getcwd()
            os.chdir(tmp_dir)
            try:
                response = strategy.execute(
                    MailImportExecuteRequest(
                        type="applemail",
                        content="demo@example.com----password----client-id----refresh-token",
                        pool_dir="mail",
                        filename="applemail_demo.json",
                        bind_to_config=False,
                    )
                )
            finally:
                os.chdir(previous_cwd)

            saved_path = Path(tmp_dir) / "mail" / "applemail_demo.json"
            self.assertTrue(saved_path.exists())
            self.assertEqual(response.summary.total, 1)
            self.assertEqual(response.summary.success, 1)
            self.assertEqual(response.summary.failed, 0)
            self.assertEqual(response.snapshot.filename, "applemail_demo.json")
            self.assertEqual(response.snapshot.pool_dir, "mail")
            self.assertEqual(response.snapshot.count, 1)
            self.assertEqual(response.snapshot.items[0].email, "demo@example.com")

    def test_applemail_strategy_accepts_chatgpt_password_and_mfa_rows(self):
        from services.mail_imports.providers import AppleMailImportStrategy
        from services.mail_imports.schemas import MailImportExecuteRequest

        strategy = AppleMailImportStrategy()
        with tempfile.TemporaryDirectory() as tmp_dir:
            response = strategy.execute(
                MailImportExecuteRequest(
                    type="applemail",
                    content=(
                        "demo@icloud.com----chatgpt-password----"
                        "JBSWY3DPEHPK3PXP"
                    ),
                    pool_dir=tmp_dir,
                    filename="icloud_mfa.json",
                    bind_to_config=False,
                )
            )

            payload = json.loads(Path(tmp_dir, "icloud_mfa.json").read_text())

        self.assertEqual(response.summary.success, 1)
        self.assertEqual(payload[0]["account_type"], "chatgpt_password_totp")
        self.assertEqual(payload[0]["password"], "chatgpt-password")
        self.assertEqual(payload[0]["totp_secret"], "JBSWY3DPEHPK3PXP")

    def test_applemail_strategy_accepts_google_federated_email_password_rows(self):
        from services.mail_imports.providers import AppleMailImportStrategy
        from services.mail_imports.schemas import MailImportExecuteRequest

        strategy = AppleMailImportStrategy()
        with tempfile.TemporaryDirectory() as tmp_dir:
            response = strategy.execute(
                MailImportExecuteRequest(
                    type="applemail",
                    content="worker@custom-google-domain.example----supplier-password",
                    pool_dir=tmp_dir,
                    filename="google-federated.json",
                    bind_to_config=False,
                )
            )
            payload = json.loads(
                Path(tmp_dir, "google-federated.json").read_text(encoding="utf-8")
            )

        self.assertEqual(response.summary.success, 1)
        self.assertEqual(response.summary.failed, 0)
        self.assertEqual(payload[0]["account_type"], "chatgpt_google_password")
        self.assertEqual(payload[0]["password"], "supplier-password")
        self.assertNotIn("totp_secret", payload[0])

    def test_applemail_strategy_imports_url_and_reset_credentials_after_header(self):
        from services.mail_imports.providers import AppleMailImportStrategy
        from services.mail_imports.schemas import MailImportExecuteRequest

        strategy = AppleMailImportStrategy()
        mail_url = (
            "https://oauth.example.test/mail?email=first%40example.com&token=MAIL_SECRET"
        )
        totp_url = (
            "https://2fa.example.test/view?token=TOTP_SECRET&email=first%40example.com"
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            response = strategy.execute(
                MailImportExecuteRequest(
                    type="applemail",
                    content=(
                        "购买后请按以下格式使用，本说明不属于卡密。\n"
                        "=== 卡密内容 ===\n"
                        f"first@example.com----password-1----{mail_url}----{totp_url}\n"
                        "broken@example.com----登陆请点击忘记密码----"
                        "https://oauth.example.test/mail?token=BROKEN_SECRET\n"
                        "second@example.com----password-2----"
                        "https://oauth.example.test/mail?token=MAIL_TWO----"
                        "https://2fa.example.test/view?token=TOTP_TWO\n"
                        "third@example.com----password-3----"
                        "https://oauth.example.test/mail?token=MAIL_THREE----"
                        "https://2fa.example.test/view?token=TOTP_THREE"
                    ),
                    pool_dir=tmp_dir,
                    filename="url-credentials.json",
                    bind_to_config=False,
                )
            )
            payload = json.loads(
                Path(tmp_dir, "url-credentials.json").read_text(encoding="utf-8")
            )

        self.assertEqual(response.summary.total, 4)
        self.assertEqual(response.summary.success, 4)
        self.assertEqual(response.summary.failed, 0)
        self.assertEqual(response.errors, [])
        self.assertEqual(len(payload), 4)
        first = next(item for item in payload if item["email"] == "first@example.com")
        self.assertEqual(first["account_type"], "chatgpt_password_url_otp")
        self.assertEqual(first["password"], "password-1")
        self.assertEqual(first["mail_api_url"], mail_url)
        self.assertEqual(first["totp_url"], totp_url)
        reset = next(item for item in payload if item["email"] == "broken@example.com")
        self.assertEqual(reset["account_type"], "chatgpt_password_reset_url_mail")
        self.assertEqual(reset["password"], "")
        self.assertEqual(
            reset["mail_api_url"],
            "https://oauth.example.test/mail?token=BROKEN_SECRET",
        )
        self.assertNotIn("totp_url", reset)

    def test_applemail_import_binding_activates_the_imported_pool(self):
        from services.mail_imports.providers import AppleMailImportStrategy
        from services.mail_imports.schemas import MailImportExecuteRequest

        strategy = AppleMailImportStrategy()
        with tempfile.TemporaryDirectory() as tmp_dir, patch(
            "services.mail_imports.providers.config_store.set_many"
        ) as set_many:
            strategy.execute(
                MailImportExecuteRequest(
                    type="applemail",
                    content=(
                        "demo@icloud.com----chatgpt-password----"
                        "JBSWY3DPEHPK3PXP"
                    ),
                    pool_dir=tmp_dir,
                    filename="active-applemail.json",
                    bind_to_config=True,
                )
            )

        set_many.assert_called_once_with(
            {
                "mail_provider": "applemail",
                "applemail_pool_dir": tmp_dir,
                "applemail_pool_file": "active-applemail.json",
            }
        )

    def test_microsoft_import_binding_activates_the_microsoft_pool(self):
        from services.mail_imports.providers import MicrosoftMailImportStrategy
        from services.mail_imports.schemas import MailImportExecuteRequest

        strategy = MicrosoftMailImportStrategy()
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_engine = create_engine(
                f"sqlite:///{Path(tmp_dir) / 'mail-import-bind.db'}"
            )
            SQLModel.metadata.create_all(test_engine)
            try:
                with patch(
                    "services.mail_imports.providers.engine", test_engine
                ), patch(
                    "services.mail_imports.providers.config_store.set_many"
                ) as set_many:
                    response = strategy.execute(
                        MailImportExecuteRequest(
                            type="microsoft",
                            content=(
                                "demo@outlook.com----"
                                "https://mailapi.example.test/inbox/demo"
                            ),
                            bind_to_config=True,
                        )
                    )
            finally:
                test_engine.dispose()

        self.assertEqual(response.summary.success, 1)
        set_many.assert_called_once_with({"mail_provider": "microsoft"})

    def test_microsoft_strategy_rejects_invalid_mailapi_url(self):
        from services.mail_imports.providers import MicrosoftMailImportStrategy
        from services.mail_imports.schemas import MailImportExecuteRequest

        strategy = MicrosoftMailImportStrategy()
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_engine = create_engine(f"sqlite:///{Path(tmp_dir) / 'mail-imports.db'}")
            SQLModel.metadata.create_all(test_engine)

            try:
                with patch("services.mail_imports.providers.engine", test_engine):
                    response = strategy.execute(
                        MailImportExecuteRequest(
                            type="microsoft",
                            content="demo@outlook.com----not-a-url",
                        )
                    )

                    self.assertEqual(response.summary.total, 1)
                    self.assertEqual(response.summary.success, 0)
                    self.assertEqual(response.summary.failed, 1)
                    self.assertIn("无效的 mailapi_url", response.errors[0])
                    self.assertEqual(response.snapshot.count, 0)
            finally:
                test_engine.dispose()

    def test_microsoft_strategy_imports_only_rows_that_pass_rules(self):
        from services.mail_imports.schemas import MailImportExecuteRequest
        from services.mail_imports.providers import MicrosoftMailImportStrategy

        strategy = MicrosoftMailImportStrategy()
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_engine = create_engine(f"sqlite:///{Path(tmp_dir) / 'mail-imports.db'}")
            SQLModel.metadata.create_all(test_engine)

            try:
                with patch("services.mail_imports.providers.engine", test_engine), \
                     patch("services.mail_imports.providers.OutlookMailbox") as mailbox_cls:
                    mailbox = mailbox_cls.return_value
                    def probe_oauth_availability(**kwargs):
                        if kwargs.get("email") == "first@outlook.com":
                            return {
                                "ok": True,
                                "reason": "ok",
                                "message": "微软邮箱可用性检测通过",
                                "access_token": "token-a",
                                "refresh_token": "rotated-refresh-a",
                            }
                        return {
                            "ok": False,
                            "reason": "service_abuse_mode",
                            "message": "微软邮箱可用性检测未通过，账号处于 service abuse mode",
                        }

                    mailbox.probe_oauth_availability.side_effect = (
                        probe_oauth_availability
                    )

                    response = strategy.execute(
                        MailImportExecuteRequest(
                            type="microsoft",
                            content=(
                                "first@outlook.com----password----client-a----refresh-a\n"
                                "second@hotmail.com----password----client-b----refresh-b"
                            ),
                            bind_to_config=False,
                        )
                    )

                    self.assertEqual(response.summary.total, 2)
                    self.assertEqual(response.summary.success, 1)
                    self.assertEqual(response.summary.failed, 1)
                    self.assertEqual(response.snapshot.count, 1)
                    self.assertEqual(response.snapshot.items[0].email, "first@outlook.com")
                    self.assertIn("service abuse mode", response.errors[0])
                    with Session(test_engine) as session:
                        imported = session.exec(
                            select(OutlookAccountModel).where(
                                OutlookAccountModel.email == "first@outlook.com"
                            )
                        ).one()
                    self.assertEqual(imported.refresh_token, "rotated-refresh-a")
            finally:
                test_engine.dispose()

    def test_microsoft_strategy_supports_mixed_oauth_and_mailapi_rows(self):
        from services.mail_imports.schemas import MailImportExecuteRequest
        from services.mail_imports.providers import MicrosoftMailImportStrategy

        strategy = MicrosoftMailImportStrategy()
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_engine = create_engine(f"sqlite:///{Path(tmp_dir) / 'mail-imports.db'}")
            SQLModel.metadata.create_all(test_engine)

            try:
                with patch("services.mail_imports.providers.engine", test_engine), \
                     patch("services.mail_imports.providers.OutlookMailbox") as mailbox_cls:
                    mailbox = mailbox_cls.return_value
                    mailbox.probe_oauth_availability.return_value = {
                        "ok": True,
                        "reason": "ok",
                        "message": "微软邮箱可用性检测通过",
                        "access_token": "token-a",
                    }

                    response = strategy.execute(
                        MailImportExecuteRequest(
                            type="microsoft",
                            content=(
                                "oauth@outlook.com----password----client-a----refresh-a\n"
                                "mailapi@hotmail.com----https://mailapi.icu/key?type=html&orderNo=abc123"
                            ),
                            bind_to_config=False,
                        )
                    )

                    self.assertEqual(response.summary.total, 2)
                    self.assertEqual(response.summary.success, 2)
                    self.assertEqual(response.summary.failed, 0)
                    self.assertEqual(response.snapshot.count, 2)
                    account_types = {item.email: item.account_type for item in response.snapshot.items}
                    self.assertEqual(account_types.get("oauth@outlook.com"), "microsoft_oauth")
                    self.assertEqual(account_types.get("mailapi@hotmail.com"), "mailapi_url")
            finally:
                test_engine.dispose()

    def test_microsoft_strategy_alias_split_generates_alias_emails(self):
        from services.mail_imports.schemas import MailImportExecuteRequest
        from services.mail_imports.providers import MicrosoftMailImportStrategy

        strategy = MicrosoftMailImportStrategy()
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_engine = create_engine(f"sqlite:///{Path(tmp_dir) / 'mail-imports.db'}")
            SQLModel.metadata.create_all(test_engine)

            try:
                with patch("services.mail_imports.providers.engine", test_engine), \
                     patch("services.mail_imports.providers.OutlookMailbox") as mailbox_cls, \
                     patch("services.mail_imports.providers.random.choices") as mock_choices:
                    mailbox = mailbox_cls.return_value
                    mailbox.probe_oauth_availability.return_value = {
                        "ok": True,
                        "reason": "ok",
                        "message": "微软邮箱可用性检测通过",
                        "access_token": "token-a",
                    }
                    mock_choices.side_effect = [
                        list("abcdef"),
                        list("ghijkl"),
                    ]

                    response = strategy.execute(
                        MailImportExecuteRequest(
                            type="microsoft",
                            content="alias@outlook.com----password----client-a----refresh-a",
                            alias_split_enabled=True,
                            alias_split_count=2,
                            alias_include_original=False,
                            bind_to_config=False,
                        )
                    )

                    self.assertEqual(response.summary.total, 2)
                    self.assertEqual(response.summary.success, 2)
                    self.assertEqual(response.summary.failed, 0)
                    imported_emails = sorted(item.email for item in response.snapshot.items)
                    self.assertEqual(
                        imported_emails,
                        sorted(
                            [
                                "alias+abcdef@outlook.com",
                                "alias+ghijkl@outlook.com",
                            ]
                        ),
                    )
            finally:
                test_engine.dispose()


if __name__ == "__main__":
    unittest.main()
