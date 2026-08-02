import unittest

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from api.accounts import list_accounts
from core.db import AccountModel


class AccountListVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)

    @staticmethod
    def _account(platform: str, email: str, extra_json: str) -> AccountModel:
        return AccountModel(
            platform=platform,
            email=email,
            password="password",
            extra_json=extra_json,
        )

    def test_default_list_hides_only_chatgpt_accounts_without_refresh_token(self):
        with Session(self.engine) as session:
            session.add_all(
                [
                    self._account(
                        "chatgpt",
                        "missing@example.com",
                        '{"access_token":"at","refresh_token":""}',
                    ),
                    self._account(
                        "chatgpt",
                        "blank@example.com",
                        '{"refresh_token":"   "}',
                    ),
                    self._account("chatgpt", "malformed@example.com", "not-json"),
                    self._account(
                        "chatgpt",
                        "valid@example.com",
                        '{"refresh_token":"rt-valid"}',
                    ),
                    self._account(
                        "chatgpt",
                        "legacy-valid@example.com",
                        '{"refreshToken":"rt-legacy"}',
                    ),
                    self._account(
                        "chatgpt",
                        "blank-snake-valid-camel@example.com",
                        '{"refresh_token":"   ","refreshToken":"rt-camel"}',
                    ),
                    self._account("qwen", "qwen@example.com", "{}"),
                ]
            )
            session.commit()

            result = list_accounts(page=1, page_size=20, session=session)

        self.assertEqual(result["total"], 4)
        self.assertEqual(
            {item["email"] for item in result["items"]},
            {
                "valid@example.com",
                "legacy-valid@example.com",
                "blank-snake-valid-camel@example.com",
                "qwen@example.com",
            },
        )

    def test_pagination_is_applied_after_incomplete_chatgpt_rows_are_hidden(self):
        with Session(self.engine) as session:
            session.add_all(
                [
                    self._account(
                        "chatgpt",
                        "hidden-first@example.com",
                        '{"refresh_token":""}',
                    ),
                    self._account(
                        "chatgpt",
                        "visible-first@example.com",
                        '{"refresh_token":"rt-1"}',
                    ),
                    self._account(
                        "chatgpt",
                        "visible-second@example.com",
                        '{"refresh_token":"rt-2"}',
                    ),
                ]
            )
            session.commit()

            result = list_accounts(
                platform="chatgpt",
                page=2,
                page_size=1,
                session=session,
            )

        self.assertEqual(result["total"], 2)
        self.assertEqual(
            [item["email"] for item in result["items"]],
            ["visible-second@example.com"],
        )


if __name__ == "__main__":
    unittest.main()
