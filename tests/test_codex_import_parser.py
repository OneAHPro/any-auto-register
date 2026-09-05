import json
import unittest

from services.codex_import_parser import (
    ImportFormatError,
    parse_import_content,
    parse_import_files,
)


class CodexImportParserTests(unittest.TestCase):
    def test_txt_is_one_refresh_token_per_line_with_bom_and_dedup(self):
        self.assertEqual(
            parse_import_content("\ufeffrt-1\n\nrt-1\n rt-2 \n", "txt"),
            [{"refresh_token": "rt-1"}, {"refresh_token": "rt-2"}],
        )

    def test_at_txt_is_one_access_token_per_line(self):
        self.assertEqual(
            parse_import_content("at-1\nat-2\nat-1", "at_txt"),
            [{"access_token": "at-1"}, {"access_token": "at-2"}],
        )

    def test_cli_proxy_flat_json_array_preserves_metadata_and_scalar_expiry(self):
        content = json.dumps([
            {"refresh_token": "rt", "access_token": "at", "email": "e@example.com",
             "plan_type": "free", "expires_at": 123, "id_token": "id", "account_id": "acc"},
            {"refresh_token": ""},
        ])
        self.assertEqual(
            parse_import_content(content, "json"),
            [{"refresh_token": "rt", "access_token": "at", "email": "e@example.com",
              "name": "e@example.com", "chatgpt_account_id": "acc", "plan_type": "free", "expires_at": "123",
              "id_token": "id", "account_id": "acc"}],
        )

    def test_sub2api_accounts_credentials_and_camel_case(self):
        content = json.dumps({"accounts": [{"name": "Primary", "credentials": {
            "refresh_token": "rt", "sessionToken": "st", "accessToken": "at",
            "user": {"email": "nested@example.com", "name": "Nested", "id": "uid"},
        }}]})
        row = parse_import_content(content, "json")[0]
        self.assertEqual(row["name"], "Primary")
        self.assertEqual(row["email"], "nested@example.com")
        self.assertEqual(row["session_token"], "st")
        self.assertEqual(row["access_token"], "at")

    def test_session_json_object_and_array(self):
        one = '{"user":{"id":"u","name":"John","email":"john@example.com"},"accessToken":"at","expires":1767225600}'
        rows = parse_import_content(one, "json")
        self.assertEqual(rows[0]["name"], "John")
        self.assertEqual(rows[0]["email"], "john@example.com")
        self.assertEqual(rows[0]["expires_at"], "1767225600")
        self.assertEqual(parse_import_content("[{\"accessToken\":\"at2\"}]", "json"), [{"access_token": "at2"}])

    def test_credentials_wrapped_agent_identity_json_is_unwrapped(self):
        content = json.dumps({"name": "wrapped", "credentials": {
            "auth_mode": "agentIdentity", "agent_runtime_id": "runtime-1",
            "agent_private_key": "private-1", "account_id": "account-1",
            "chatgpt_user_id": "user-1", "email": "wrapped@example.com",
        }})
        self.assertEqual(parse_import_content(content, "json"), [{
            "agent_runtime_id": "runtime-1", "agent_private_key": "private-1",
            "account_id": "account-1", "chatgpt_user_id": "user-1",
            "email": "wrapped@example.com", "name": "wrapped",
        }])

    def test_json_at_ignores_refresh_and_session_tokens(self):
        content = '{"refresh_token":"rt","session_token":"st","access_token":"at"}'
        self.assertEqual(parse_import_content(content, "json_at"), [{"access_token": "at"}])

    def test_invalid_or_unsupported_json_is_explicit(self):
        with self.assertRaises(ImportFormatError):
            parse_import_content("{broken", "json")
        self.assertEqual(parse_import_content('{"hello":"world"}', "json"), [])

    def test_folder_processes_json_then_txt_and_only_supported_extensions(self):
        rows = parse_import_files({
            "z.txt": "rt-z\n",
            "a.json": '{"access_token":"at-a"}',
            "ignored.csv": "rt-no",
        })
        self.assertEqual(rows, [{"access_token": "at-a"}, {"refresh_token": "rt-z"}])

    def test_folder_deduplicates_refresh_tokens_across_txt_files(self):
        rows = parse_import_files({"one.txt": "rt\n", "two.txt": "rt\nrt2\n"})
        self.assertEqual(rows, [{"refresh_token": "rt"}, {"refresh_token": "rt2"}])


if __name__ == "__main__":
    unittest.main()
