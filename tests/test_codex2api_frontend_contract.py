import unittest
from pathlib import Path

from api.config import CONFIG_KEYS


ROOT = Path(__file__).resolve().parents[1]


class Codex2APIFrontendContractTests(unittest.TestCase):
    def test_settings_expose_independent_codex2api_configuration(self):
        expected_keys = {
            "codex2api_enabled",
            "codex2api_api_url",
            "codex2api_admin_key",
        }
        self.assertTrue(expected_keys.issubset(CONFIG_KEYS))

        settings_source = (ROOT / "frontend/src/pages/Settings.tsx").read_text()
        self.assertIn("key: 'codex2api'", settings_source)
        self.assertIn("label: 'Codex2API'", settings_source)
        self.assertLess(
            settings_source.index("key: 'chatgpt'"),
            settings_source.index("key: 'codex2api'"),
        )
        self.assertIn(
            "data.codex2api_enabled = parseBooleanConfigValue",
            settings_source,
        )
        self.assertIn(
            "values.codex2api_enabled = parseBooleanConfigValue",
            settings_source,
        )

    def test_accounts_expose_codex2api_status_and_batch_upload(self):
        accounts_source = (ROOT / "frontend/src/pages/Accounts.tsx").read_text()

        self.assertIn("syncStatuses.codex2api", accounts_source)
        self.assertIn("codex2apiSync", accounts_source)
        self.assertIn("uploadSyncTitle('Codex2API'", accounts_source)
        self.assertIn("handleBatchUploadCodex2API", accounts_source)
        self.assertIn("codex2apiUploadLoading", accounts_source)
        self.assertIn("/upload_codex2api/batch", accounts_source)
        self.assertIn("导入 Codex2API", accounts_source)
        self.assertIn(
            "if (createdAtStart) body.created_at_start = createdAtStart",
            accounts_source,
        )
        self.assertIn(
            "if (createdAtEnd) body.created_at_end = createdAtEnd",
            accounts_source,
        )


if __name__ == "__main__":
    unittest.main()
