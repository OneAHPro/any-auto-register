import unittest
from unittest.mock import patch

from services.mail_imports.schemas import (
    MailImportDetectionRequest,
    MailImportExecuteRequest,
    MailImportProviderDescriptor,
    MailImportResponse,
    MailImportSnapshot,
    MailImportSummary,
)


class FakeStrategy:
    def __init__(self, provider_type):
        self.descriptor = MailImportProviderDescriptor(
            type=provider_type,
            label=provider_type,
            description="",
            content_placeholder="",
        )
        self.requests = []
        self.batch_delete_requests = []
        self.execute_error = None

    def execute(self, request):
        self.requests.append(request)
        if self.execute_error:
            raise self.execute_error
        count = len([line for line in request.content.splitlines() if line.strip()])
        return MailImportResponse(
            type=request.type,
            summary=MailImportSummary(total=count, success=count, failed=0),
            snapshot=MailImportSnapshot(
                type=request.type,
                label=request.type,
                count=count,
                filename="auto-import.json" if request.type == "applemail" else "",
                pool_dir=request.pool_dir if request.type == "applemail" else "",
            ),
            meta={"accounts": [{"email": "one@outlook.com"}]} if request.type == "microsoft" else {},
        )

    def batch_delete(self, request):
        self.batch_delete_requests.append(request)
        return MailImportResponse(
            type=self.descriptor.type,
            summary=MailImportSummary(total=len(request.items), success=len(request.items), failed=0),
            snapshot=MailImportSnapshot(type=self.descriptor.type, label=self.descriptor.type, count=0),
        )


class FakeRegistry:
    def __init__(self):
        self.strategies = {
            "microsoft": FakeStrategy("microsoft"),
            "applemail": FakeStrategy("applemail"),
        }

    def get(self, provider_type):
        return self.strategies[provider_type]


class MailImportAutoServiceTests(unittest.TestCase):
    def test_execute_request_accepts_auto_type(self):
        request = MailImportExecuteRequest(type="auto", content="example")

        self.assertEqual(request.type, "auto")

    def test_detection_request_rejects_empty_content(self):
        with self.assertRaises(ValueError):
            MailImportDetectionRequest(content="")

    def test_mixed_import_delegates_to_both_existing_strategies_without_binding(self):
        from services.mail_imports.auto_import import AutoMailImportService

        registry = FakeRegistry()
        service = AutoMailImportService(registry=registry)
        microsoft_line = "one@outlook.com----https://mail.test/messages/one"
        applemail_line = (
            "two@gmail.com----password----QM5QPLWGNKZYUQDWSCBDJIJUGXEHIQA3"
        )

        with patch("services.mail_imports.auto_import.config_store.set_many"):
            response = service.execute(
                MailImportExecuteRequest(
                    type="auto",
                    content=f"{microsoft_line}\n{applemail_line}",
                    filename="auto-import.json",
                    bind_to_config=True,
                    alias_split_enabled=True,
                    preferred_provider="microsoft",
                )
            )

        microsoft_request = registry.strategies["microsoft"].requests[0]
        applemail_request = registry.strategies["applemail"].requests[0]
        self.assertEqual(microsoft_request.type, "microsoft")
        self.assertEqual(microsoft_request.content, microsoft_line)
        self.assertFalse(microsoft_request.bind_to_config)
        self.assertTrue(microsoft_request.alias_split_enabled)
        self.assertEqual(applemail_request.type, "applemail")
        self.assertEqual(applemail_request.content, applemail_line)
        self.assertFalse(applemail_request.bind_to_config)
        self.assertEqual(applemail_request.filename, "auto-import.json")
        self.assertEqual(response.type, "auto")
        self.assertEqual(response.summary.total, 2)
        self.assertEqual(response.summary.success, 2)
        self.assertEqual(response.meta["providers"], ["microsoft", "applemail"])
        self.assertTrue(response.meta["bound_to_config"])
        self.assertEqual(response.meta["bound_provider"], "microsoft")
        self.assertEqual(response.snapshot.type, "microsoft")

    def test_mixed_import_binds_preferred_provider_and_keeps_applemail_file_discoverable(self):
        from services.mail_imports.auto_import import AutoMailImportService

        registry = FakeRegistry()
        service = AutoMailImportService(registry=registry)

        with patch("services.mail_imports.auto_import.config_store.set_many") as set_many:
            response = service.execute(
                MailImportExecuteRequest(
                    type="auto",
                    content=(
                        "one@outlook.com----https://mail.test/messages/one\n"
                        "two@gmail.com----password----QM5QPLWGNKZYUQDWSCBDJIJUGXEHIQA3"
                    ),
                    pool_dir="mail",
                    preferred_provider="applemail",
                    bind_to_config=True,
                )
            )

        set_many.assert_called_once_with(
            {
                "mail_provider": "applemail",
                "applemail_pool_dir": "mail",
                "applemail_pool_file": "auto-import.json",
            }
        )
        self.assertEqual(response.snapshot.type, "applemail")
        self.assertEqual(response.meta["applemail_pool_file"], "auto-import.json")

    def test_mixed_import_rolls_back_microsoft_rows_when_applemail_write_fails(self):
        from services.mail_imports.auto_import import AutoMailImportService

        registry = FakeRegistry()
        registry.strategies["applemail"].execute_error = RuntimeError("disk failed")
        service = AutoMailImportService(registry=registry)

        with self.assertRaisesRegex(RuntimeError, "disk failed"):
            service.execute(
                MailImportExecuteRequest(
                    type="auto",
                    content=(
                        "one@outlook.com----https://mail.test/messages/one\n"
                        "two@gmail.com----password----QM5QPLWGNKZYUQDWSCBDJIJUGXEHIQA3"
                    ),
                    bind_to_config=True,
                )
            )

        rollback = registry.strategies["microsoft"].batch_delete_requests[0]
        self.assertEqual([item.email for item in rollback.items], ["one@outlook.com"])

    def test_single_provider_auto_import_preserves_config_binding(self):
        from services.mail_imports.auto_import import AutoMailImportService

        registry = FakeRegistry()
        service = AutoMailImportService(registry=registry)

        response = service.execute(
            MailImportExecuteRequest(
                type="auto",
                content="one@outlook.com----https://mail.test/messages/one",
                bind_to_config=True,
            )
        )

        delegated = registry.strategies["microsoft"].requests[0]
        self.assertTrue(delegated.bind_to_config)
        self.assertEqual(response.type, "microsoft")

    def test_unresolved_import_fails_without_exposing_credentials(self):
        from services.mail_imports.auto_import import AutoMailImportService

        registry = FakeRegistry()
        service = AutoMailImportService(registry=registry)
        secret = "refresh-token-value-12345678901234567890"

        with self.assertRaises(ValueError) as raised:
            service.execute(
                MailImportExecuteRequest(
                    type="auto",
                    content=(
                        "one@example.com----password----"
                        f"client-id-value-1234567890----{secret}"
                    ),
                )
            )

        self.assertIn("手动", str(raised.exception))
        self.assertNotIn(secret, str(raised.exception))
        self.assertEqual(registry.strategies["microsoft"].requests, [])
        self.assertEqual(registry.strategies["applemail"].requests, [])

    def test_detect_api_returns_only_redacted_detection_fields(self):
        from api.mail_imports import detect_mail_import

        secret_url = "https://mail.test/messages?token=secret-value"
        response = detect_mail_import(
            MailImportDetectionRequest(
                content=f"one@outlook.com----{secret_url}"
            )
        )
        payload = response.model_dump()

        self.assertEqual(payload["counts"]["microsoft"], 1)
        self.assertNotIn(secret_url, str(payload))
        self.assertNotIn("content", payload["rows"][0])


if __name__ == "__main__":
    unittest.main()
