import json
import unittest

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from api.accounts import ImportRequest, import_accounts
from core.db import AccountModel


class AccountImportTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)

    def test_import_accepts_four_dash_email_password_rows(self):
        with Session(self.engine) as session:
            response = import_accounts(
                ImportRequest(
                    platform="chatgpt",
                    lines=["user@example.com----ChatGPT-password"],
                ),
                session=session,
            )

            self.assertEqual(response, {"created": 1})
            account = session.exec(select(AccountModel)).one()
            self.assertEqual(account.email, "user@example.com")
            self.assertEqual(account.password, "ChatGPT-password")
            self.assertEqual(account.get_extra(), {})

    def test_import_preserves_json_metadata_after_dash_split(self):
        metadata = {
            "account_type": "chatgpt_password_totp",
            "totp_secret": "BASE32_SECRET",
        }
        with Session(self.engine) as session:
            response = import_accounts(
                ImportRequest(
                    platform="chatgpt",
                    lines=[
                        "user@example.com---ChatGPT-password---"
                        + json.dumps(metadata, separators=(",", ":"))
                    ],
                ),
                session=session,
            )

            self.assertEqual(response, {"created": 1})
            account = session.exec(select(AccountModel)).one()
            self.assertEqual(account.email, "user@example.com")
            self.assertEqual(account.password, "ChatGPT-password")
            self.assertEqual(account.get_extra(), metadata)

    def test_import_keeps_passwords_containing_spaces_when_dash_delimited(self):
        with Session(self.engine) as session:
            response = import_accounts(
                ImportRequest(
                    platform="chatgpt",
                    lines=["user@example.com----password with spaces"],
                ),
                session=session,
            )

            self.assertEqual(response, {"created": 1})
            account = session.exec(select(AccountModel)).one()
            self.assertEqual(account.password, "password with spaces")


if __name__ == "__main__":
    unittest.main()
