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
            "codex2api_delete_on_account_remove_enabled",
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

    def test_accounts_use_project_owned_codex_import_and_retain_status_projection(self):
        accounts_source = (ROOT / "frontend/src/pages/Accounts.tsx").read_text()

        self.assertIn("syncStatuses.codex2api", accounts_source)
        self.assertIn("codex2apiSync", accounts_source)
        self.assertIn("uploadSyncTitle('Codex2API'", accounts_source)
        self.assertIn("CodexAccountImportModal", accounts_source)
        self.assertNotIn("handleBatchUploadCodex2API", accounts_source)
        self.assertNotIn("导入筛选 Codex2API", accounts_source)
        self.assertNotIn("补传远端未发现", accounts_source)


if __name__ == "__main__":
    unittest.main()
