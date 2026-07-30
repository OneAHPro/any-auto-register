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


if __name__ == "__main__":
    unittest.main()
