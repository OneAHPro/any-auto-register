import json
import multiprocessing
import tempfile
import threading
import time
import traceback
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import requests

from core.applemail_pool import (
    load_applemail_pool_records,
    load_applemail_pool_snapshot,
    parse_applemail_pool_content,
    requeue_applemail_record,
    save_applemail_pool_json,
    take_next_applemail_record,
)
from core.base_mailbox import AppleMailMailbox, MailApiUrlOtpBackend
from core.icloud_mail import ICloudMailClient, generate_totp


def _claim_pool_record_in_process(pool_dir, pool_file, start, results):
    start.wait(timeout=3)
    try:
        account = AppleMailMailbox(
            pool_dir=pool_dir,
            pool_file=pool_file,
        ).get_email()
        results.put(f"ok:{account.email}")
    except Exception as exc:
        results.put(f"error:{exc}")


class ICloudTotpTests(unittest.TestCase):
    def test_generates_standard_six_digit_totp(self):
        self.assertEqual(
            generate_totp(
                "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ",
                timestamp=59,
            ),
            "287082",
        )

    def test_rejects_invalid_mfa_secret_without_echoing_it(self):
        with self.assertRaisesRegex(ValueError, "MFA 秘钥格式无效") as ctx:
            generate_totp("not-a-base32-secret-0")

        self.assertNotIn("not-a-base32-secret-0", str(ctx.exception))


class _ICloudResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _ICloudSession:
    def __init__(self):
        self.calls = []
        self.proxies = {}

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _ICloudResponse(
            {
                "threadList": [
                    {
                        "threadId": "thread-7",
                        "timestamp": 1785456000,
                        "modseq": 12,
                        "subject": "Your ChatGPT code is 123456",
                        "senders": ["OpenAI <noreply@tm.openai.com>"],
                        "preview": "Use code 123456 to finish signing in.",
                    }
                ],
                "folderStatus": {
                    "undeletedMessages": 1,
                    "unseenUndeletedMessages": 1,
                },
                "sessionHeaders": {
                    "folder": "INBOX",
                    "modseq": 12,
                    "threadmodseq": 4,
                    "condstore": 1,
                    "qresync": 1,
                    "threadmode": 1,
                },
            }
        )


class _ICloudService:
    def __init__(self):
        self.requires_2fa = True
        self.requires_2sa = True
        self.is_trusted_session = False
        self.params = {"clientId": "client-id", "dsid": "dsid-1"}
        self.session = _ICloudSession()
        self.validated_codes = []

    def validate_2fa_code(self, code):
        self.validated_codes.append(code)
        self.is_trusted_session = True
        self.requires_2fa = False
        self.requires_2sa = False
        return True

    def get_webservice_url(self, name):
        if name != "mccgateway":
            raise AssertionError(name)
        return "https://p01-mccgateway.icloud.com"


class _AuthenticatingICloudService:
    auth_proxy_snapshots = []

    def __init__(self, *args, **kwargs):
        self.requires_2fa = False
        self.requires_2sa = False
        self.params = {}
        self.session = _ICloudSession()
        self.authenticate()

    def authenticate(self, *args, **kwargs):
        self.auth_proxy_snapshots.append(dict(self.session.proxies))

    def get_webservice_url(self, name):
        if name != "mccgateway":
            raise AssertionError(name)
        return "https://p01-mccgateway.icloud.com"


class _ICloud2SAService:
    def __init__(self):
        self.requires_2fa = False
        self.requires_2sa = True
        self.params = {}
        self.session = _ICloudSession()
        self.trusted_devices = [{"id": "trusted-device-1", "phoneNumber": "+1•••12"}]
        self.sent_devices = []
        self.validated_codes = []

    def send_verification_code(self, device):
        self.sent_devices.append(dict(device))
        return True

    def validate_verification_code(self, device, code):
        self.validated_codes.append((dict(device), code))
        self.requires_2sa = False
        return True

    def get_webservice_url(self, name):
        if name != "mccgateway":
            raise AssertionError(name)
        return "https://p01-mccgateway.icloud.com"


class ICloudMailClientTests(unittest.TestCase):
    def test_configures_proxy_before_default_service_authentication(self):
        _AuthenticatingICloudService.auth_proxy_snapshots = []
        client = ICloudMailClient(
            email="demo@icloud.com",
            password="apple-password",
            mfa_secret="GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ",
            proxy_url="http://127.0.0.1:7890",
            cookie_directory="/tmp/icloud-test-session",
        )

        with mock.patch.object(
            client,
            "_factory",
            return_value=_AuthenticatingICloudService,
        ):
            client._authenticate()

        self.assertEqual(
            _AuthenticatingICloudService.auth_proxy_snapshots,
            [{"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}],
        )

    def test_uses_trusted_device_flow_for_legacy_two_step_authentication(self):
        service = _ICloud2SAService()
        client = ICloudMailClient(
            email="demo@icloud.com",
            password="apple-password",
            mfa_secret="GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ",
            service_factory=mock.Mock(return_value=service),
            time_fn=lambda: 59,
            cookie_directory="/tmp/icloud-test-session",
        )

        client._authenticate()

        self.assertEqual(service.sent_devices, [service.trusted_devices[0]])
        self.assertEqual(
            service.validated_codes,
            [(service.trusted_devices[0], "287082")],
        )

    def test_authenticates_only_once_when_baseline_and_poll_start_together(self):
        start = threading.Barrier(2)

        def build_service(*args, **kwargs):
            time.sleep(0.05)
            return _AuthenticatingICloudService()

        factory = mock.Mock(side_effect=build_service)
        client = ICloudMailClient(
            email="demo@icloud.com",
            password="apple-password",
            mfa_secret="GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ",
            service_factory=factory,
            cookie_directory="/tmp/icloud-test-session",
        )

        def authenticate():
            start.wait(timeout=1)
            return client._authenticate()

        with ThreadPoolExecutor(max_workers=2) as executor:
            services = list(executor.map(lambda _: authenticate(), range(2)))

        self.assertIs(services[0], services[1])
        self.assertEqual(factory.call_count, 1)

    def test_logs_in_with_generated_mfa_and_lists_recent_threads(self):
        service = _ICloudService()
        factory = mock.Mock(return_value=service)
        client = ICloudMailClient(
            email="demo@icloud.com",
            password="apple-password",
            mfa_secret="GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ",
            service_factory=factory,
            time_fn=lambda: 59,
            cookie_directory="/tmp/icloud-test-session",
        )

        messages = client.list_messages("INBOX", limit=20)

        self.assertEqual(service.validated_codes, ["287082"])
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["subject"], "Your ChatGPT code is 123456")
        self.assertEqual(messages[0]["preview"], "Use code 123456 to finish signing in.")
        self.assertIn("thread-7", messages[0]["id"])
        factory.assert_called_once_with(
            "demo@icloud.com",
            "apple-password",
            cookie_directory="/tmp/icloud-test-session",
            with_family=False,
        )
        url, kwargs = service.session.calls[0]
        self.assertEqual(
            url,
            "https://p01-mccgateway.icloud.com/mailws2/v1/thread/search",
        )
        self.assertEqual(kwargs["json"]["maxResults"], 20)
        self.assertEqual(kwargs["json"]["sessionHeaders"]["folder"], "INBOX")
        self.assertEqual(kwargs["params"], service.params)


class ICloudAppleMailPoolTests(unittest.TestCase):
    @staticmethod
    def _chatgpt_pool_content(count: int) -> str:
        return "\n".join(
            f"user-{index}@icloud.com----password-{index}----JBSWY3DPEHPK3PXP"
            for index in range(count)
        )

    def test_claims_are_persistently_excluded_from_available_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            save_applemail_pool_json(
                self._chatgpt_pool_content(3),
                pool_dir=tmp_dir,
                filename="claim.json",
            )

            first_mailbox = AppleMailMailbox(
                pool_dir=tmp_dir,
                pool_file="claim.json",
            )
            first = first_mailbox.get_email()
            after_claim = load_applemail_pool_snapshot(
                pool_dir=tmp_dir,
                pool_file="claim.json",
            )

            self.assertEqual(first.email, "user-0@icloud.com")
            self.assertEqual(after_claim["count"], 2)
            self.assertNotIn(
                first.email,
                {item["email"] for item in after_claim["items"]},
            )

            # A fresh mailbox instance simulates a process restart: it must read
            # the persisted claim instead of resetting an in-memory cursor.
            restarted_mailbox = AppleMailMailbox(
                pool_dir=tmp_dir,
                pool_file="claim.json",
            )
            second = restarted_mailbox.get_email()
            self.assertEqual(second.email, "user-1@icloud.com")
            self.assertEqual(
                load_applemail_pool_snapshot(
                    pool_dir=tmp_dir,
                    pool_file="claim.json",
                )["count"],
                1,
            )

            _path, all_records = load_applemail_pool_records(
                pool_dir=tmp_dir,
                pool_file="claim.json",
            )
            self.assertEqual(len(all_records), 3)
            self.assertEqual(all_records[0]["pool_state"], "claimed")
            self.assertFalse(all_records[0]["enabled"])

    def test_requeue_restores_only_the_claimed_mailbox_once(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            save_applemail_pool_json(
                self._chatgpt_pool_content(2),
                pool_dir=tmp_dir,
                filename="requeue.json",
            )
            mailbox = AppleMailMailbox(
                pool_dir=tmp_dir,
                pool_file="requeue.json",
            )
            claimed = mailbox.get_email()

            mailbox.requeue_account(claimed)
            mailbox.requeue_account(claimed)

            snapshot = load_applemail_pool_snapshot(
                pool_dir=tmp_dir,
                pool_file="requeue.json",
            )
            self.assertEqual(snapshot["count"], 2)
            self.assertEqual(
                [item["email"] for item in snapshot["items"]].count(claimed.email),
                1,
            )

    def test_claim_excludes_normalized_email_addresses_atomically(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            save_applemail_pool_json(
                self._chatgpt_pool_content(3),
                pool_dir=tmp_dir,
                filename="excluded.json",
            )

            _path, claimed = take_next_applemail_record(
                pool_dir=tmp_dir,
                pool_file="excluded.json",
                exclude_emails={" USER-0@ICLOUD.COM "},
            )

            self.assertEqual(claimed["email"], "user-1@icloud.com")

    def test_concurrent_claims_do_not_consume_unattempted_mailboxes(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            save_applemail_pool_json(
                self._chatgpt_pool_content(10),
                pool_dir=tmp_dir,
                filename="concurrent.json",
            )

            def claim_one(_index: int) -> str:
                return AppleMailMailbox(
                    pool_dir=tmp_dir,
                    pool_file="concurrent.json",
                ).get_email().email

            with ThreadPoolExecutor(max_workers=3) as executor:
                claimed = list(executor.map(claim_one, range(3)))

            self.assertEqual(len(set(claimed)), 3)
            snapshot = load_applemail_pool_snapshot(
                pool_dir=tmp_dir,
                pool_file="concurrent.json",
            )
            self.assertEqual(snapshot["count"], 7)
            self.assertTrue(set(claimed).isdisjoint(
                {item["email"] for item in snapshot["items"]}
            ))

    def test_shared_claim_scope_skips_immediately_requeued_mailboxes_concurrently(self):
        from core.base_mailbox import MailboxClaimScope

        with tempfile.TemporaryDirectory() as tmp_dir:
            save_applemail_pool_json(
                self._chatgpt_pool_content(8),
                pool_dir=tmp_dir,
                filename="claim-scope.json",
            )
            claim_scope = MailboxClaimScope()

            def claim_and_requeue(_index: int) -> str:
                mailbox = AppleMailMailbox(
                    pool_dir=tmp_dir,
                    pool_file="claim-scope.json",
                )
                mailbox.bind_claim_scope(claim_scope)
                account = mailbox.get_email()
                self.assertTrue(mailbox.requeue_account(account))
                return account.email

            with ThreadPoolExecutor(max_workers=4) as executor:
                claimed = list(executor.map(claim_and_requeue, range(8)))

            self.assertEqual(
                set(claimed),
                {f"user-{index}@icloud.com" for index in range(8)},
            )
            self.assertEqual(
                load_applemail_pool_snapshot(
                    pool_dir=tmp_dir,
                    pool_file="claim-scope.json",
                )["count"],
                8,
            )

            next_task_mailbox = AppleMailMailbox(
                pool_dir=tmp_dir,
                pool_file="claim-scope.json",
            )
            next_task_mailbox.bind_claim_scope(MailboxClaimScope())
            self.assertEqual(
                next_task_mailbox.get_email().email,
                "user-0@icloud.com",
            )

    def test_exact_address_claim_bypasses_bound_claim_scope_for_retry(self):
        from core.base_mailbox import MailboxClaimScope

        with tempfile.TemporaryDirectory() as tmp_dir:
            save_applemail_pool_json(
                self._chatgpt_pool_content(2),
                pool_dir=tmp_dir,
                filename="claim-scope-retry.json",
            )
            claim_scope = MailboxClaimScope()
            ordinary_mailbox = AppleMailMailbox(
                pool_dir=tmp_dir,
                pool_file="claim-scope-retry.json",
            )
            ordinary_mailbox.bind_claim_scope(claim_scope)
            ordinary = ordinary_mailbox.get_email()
            self.assertTrue(ordinary_mailbox.requeue_account(ordinary))

            retry_mailbox = AppleMailMailbox(
                pool_dir=tmp_dir,
                pool_file="claim-scope-retry.json",
            )
            retry_mailbox.bind_claim_scope(claim_scope)

            retried = retry_mailbox.get_email_by_address(ordinary.email)

            self.assertEqual(retried.email, ordinary.email)

    def test_claims_are_serialized_across_processes(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            save_applemail_pool_json(
                self._chatgpt_pool_content(1),
                pool_dir=tmp_dir,
                filename="multiprocess.json",
            )
            context = multiprocessing.get_context("spawn")
            start = context.Event()
            results = context.Queue()
            processes = [
                context.Process(
                    target=_claim_pool_record_in_process,
                    args=(tmp_dir, "multiprocess.json", start, results),
                )
                for _ in range(2)
            ]
            for process in processes:
                process.start()
            start.set()
            for process in processes:
                process.join(timeout=5)
                self.assertFalse(process.is_alive())

            outcomes = sorted(results.get(timeout=1) for _ in processes)
            self.assertEqual(
                outcomes,
                ["error:小苹果邮箱账号池没有可用邮箱", "ok:user-0@icloud.com"],
            )

    def test_stale_claim_id_cannot_requeue_a_newer_claim_for_same_email(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            save_applemail_pool_json(
                self._chatgpt_pool_content(1),
                pool_dir=tmp_dir,
                filename="ownership.json",
            )
            _path, stale = take_next_applemail_record(
                pool_dir=tmp_dir,
                pool_file="ownership.json",
            )
            stale_claim_id = stale["pool_claim_id"]
            self.assertTrue(requeue_applemail_record(
                pool_dir=tmp_dir,
                pool_file="ownership.json",
                claim_id=stale_claim_id,
                email=stale["email"],
            ))
            _path, current = take_next_applemail_record(
                pool_dir=tmp_dir,
                pool_file="ownership.json",
            )

            self.assertNotEqual(stale_claim_id, current["pool_claim_id"])
            self.assertFalse(requeue_applemail_record(
                pool_dir=tmp_dir,
                pool_file="ownership.json",
                claim_id=stale_claim_id,
                email=stale["email"],
            ))
            self.assertEqual(
                load_applemail_pool_snapshot(
                    pool_dir=tmp_dir,
                    pool_file="ownership.json",
                )["count"],
                0,
            )

    def test_stale_mailbox_instance_cannot_requeue_another_instances_claim(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            save_applemail_pool_json(
                self._chatgpt_pool_content(1),
                pool_dir=tmp_dir,
                filename="public-ownership.json",
            )
            stale_mailbox = AppleMailMailbox(
                pool_dir=tmp_dir,
                pool_file="public-ownership.json",
            )
            stale_account = stale_mailbox.get_email()
            self.assertTrue(stale_mailbox.requeue_account(stale_account))

            current_mailbox = AppleMailMailbox(
                pool_dir=tmp_dir,
                pool_file="public-ownership.json",
            )
            current_mailbox.get_email()

            self.assertFalse(stale_mailbox.requeue_account(stale_account))
            self.assertEqual(
                load_applemail_pool_snapshot(
                    pool_dir=tmp_dir,
                    pool_file="public-ownership.json",
                )["count"],
                0,
            )

    def test_exact_address_claim_is_persisted_for_retry_binding(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            save_applemail_pool_json(
                self._chatgpt_pool_content(3),
                pool_dir=tmp_dir,
                filename="exact.json",
            )
            mailbox = AppleMailMailbox(
                pool_dir=tmp_dir,
                pool_file="exact.json",
            )

            claimed = mailbox.get_email_by_address("user-2@icloud.com")

            self.assertEqual(claimed.email, "user-2@icloud.com")
            snapshot = load_applemail_pool_snapshot(
                pool_dir=tmp_dir,
                pool_file="exact.json",
            )
            self.assertEqual(snapshot["count"], 2)
            self.assertNotIn(
                "user-2@icloud.com",
                {item["email"] for item in snapshot["items"]},
            )

    def test_empty_json_pool_remains_a_valid_persistent_pool(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir, "empty.json")
            path.write_text("[]", encoding="utf-8")

            loaded_path, records = load_applemail_pool_records(
                pool_dir=tmp_dir,
                pool_file="empty.json",
            )
            snapshot = load_applemail_pool_snapshot(
                pool_dir=tmp_dir,
                pool_file="empty.json",
            )

            self.assertEqual(loaded_path, path)
            self.assertEqual(records, [])
            self.assertEqual(snapshot["count"], 0)
            with self.assertRaisesRegex(RuntimeError, "没有可用邮箱"):
                AppleMailMailbox(
                    pool_dir=tmp_dir,
                    pool_file="empty.json",
                ).get_email()

    def test_used_credentials_are_exactly_readable_but_never_normally_claimed(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            save_applemail_pool_json(
                json.dumps(
                    [
                        {
                            "email": "used@icloud.com",
                            "password": "used-password",
                            "totp_secret": "JBSWY3DPEHPK3PXP",
                            "account_type": "chatgpt_password_totp",
                            "enabled": False,
                            "pool_state": "used",
                        },
                        {
                            "email": "claimed@icloud.com",
                            "password": "claimed-password",
                            "totp_secret": "JBSWY3DPEHPK3PXP",
                            "account_type": "chatgpt_password_totp",
                            "enabled": False,
                            "pool_state": "claimed",
                            "pool_claim_id": "in-flight",
                        },
                        {
                            "email": "available@icloud.com",
                            "password": "available-password",
                            "totp_secret": "JBSWY3DPEHPK3PXP",
                            "account_type": "chatgpt_password_totp",
                        },
                    ]
                ),
                pool_dir=tmp_dir,
                filename="states.json",
            )
            snapshot = load_applemail_pool_snapshot(
                pool_dir=tmp_dir,
                pool_file="states.json",
            )
            self.assertEqual(snapshot["count"], 1)
            self.assertEqual(snapshot["items"][0]["email"], "available@icloud.com")

            exact_mailbox = AppleMailMailbox(
                pool_dir=tmp_dir,
                pool_file="states.json",
            )
            used = exact_mailbox.get_email_by_address("used@icloud.com")
            self.assertEqual(used.email, "used@icloud.com")
            self.assertEqual(used.extra["totp_secret"], "JBSWY3DPEHPK3PXP")
            self.assertTrue(exact_mailbox.mark_account_used(used))
            self.assertEqual(
                load_applemail_pool_snapshot(
                    pool_dir=tmp_dir,
                    pool_file="states.json",
                )["count"],
                1,
            )

            normal = AppleMailMailbox(
                pool_dir=tmp_dir,
                pool_file="states.json",
            ).get_email()
            self.assertEqual(normal.email, "available@icloud.com")
            with self.assertRaisesRegex(RuntimeError, "没有可用邮箱"):
                AppleMailMailbox(
                    pool_dir=tmp_dir,
                    pool_file="states.json",
                ).get_email()

    def test_bulk_used_migration_preserves_credentials_and_is_idempotent(self):
        from core.applemail_pool import mark_applemail_records_used_by_emails

        with tempfile.TemporaryDirectory() as tmp_dir:
            save_applemail_pool_json(
                self._chatgpt_pool_content(3),
                pool_dir=tmp_dir,
                filename="migration.json",
            )

            first_count = mark_applemail_records_used_by_emails(
                pool_dir=tmp_dir,
                pool_file="migration.json",
                emails={"user-0@icloud.com", "user-2@icloud.com"},
            )
            second_count = mark_applemail_records_used_by_emails(
                pool_dir=tmp_dir,
                pool_file="migration.json",
                emails={"user-0@icloud.com", "user-2@icloud.com"},
            )

            self.assertEqual(first_count, 2)
            self.assertEqual(second_count, 2)
            snapshot = load_applemail_pool_snapshot(
                pool_dir=tmp_dir,
                pool_file="migration.json",
            )
            self.assertEqual(snapshot["count"], 1)
            self.assertEqual(snapshot["items"][0]["email"], "user-1@icloud.com")
            _path, records = load_applemail_pool_records(
                pool_dir=tmp_dir,
                pool_file="migration.json",
            )
            self.assertEqual(len(records), 3)
            states = {record["email"]: record.get("pool_state") for record in records}
            self.assertEqual(states["user-0@icloud.com"], "used")
            self.assertEqual(states["user-2@icloud.com"], "used")

    def test_reimport_same_filename_does_not_resurrect_used_credentials(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            save_applemail_pool_json(
                self._chatgpt_pool_content(1),
                pool_dir=tmp_dir,
                filename="reimport.json",
            )
            mailbox = AppleMailMailbox(pool_dir=tmp_dir, pool_file="reimport.json")
            account = mailbox.get_email()
            self.assertTrue(mailbox.mark_account_used(account))

            save_applemail_pool_json(
                self._chatgpt_pool_content(1),
                pool_dir=tmp_dir,
                filename="reimport.json",
            )

            self.assertEqual(
                load_applemail_pool_snapshot(
                    pool_dir=tmp_dir,
                    pool_file="reimport.json",
                )["count"],
                0,
            )
            _path, records = load_applemail_pool_records(
                pool_dir=tmp_dir,
                pool_file="reimport.json",
            )
            self.assertEqual(records[0]["pool_state"], "used")

    def test_saving_empty_json_pool_is_supported(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            result = save_applemail_pool_json(
                "[]",
                pool_dir=tmp_dir,
                filename="empty-save.json",
            )

            self.assertEqual(result["count"], 0)
            self.assertEqual(
                load_applemail_pool_snapshot(
                    pool_dir=tmp_dir,
                    pool_file="empty-save.json",
                )["count"],
                0,
            )

    def test_parses_supplier_email_password_mfa_as_chatgpt_login_credentials(self):
        records = parse_applemail_pool_content(
            "demo@icloud.com----chatgpt-password----JBSWY3DPEHPK3PXP"
        )

        self.assertEqual(
            records,
            [
                {
                    "email": "demo@icloud.com",
                    "password": "chatgpt-password",
                    "totp_secret": "JBSWY3DPEHPK3PXP",
                    "account_type": "chatgpt_password_totp",
                    "mailbox": "INBOX",
                }
            ],
        )

    def test_rejects_invalid_totp_in_three_field_chatgpt_record(self):
        with self.assertRaisesRegex(ValueError, "MFA 秘钥格式无效"):
            parse_applemail_pool_content(
                "demo@icloud.com----chatgpt-password----not-valid-mfa-0"
            )

    def test_preserves_legacy_three_field_oauth_record(self):
        records = parse_applemail_pool_content(
            "legacy@example.com----client-id----refresh-token"
        )

        self.assertEqual(records[0]["email"], "legacy@example.com")
        self.assertEqual(records[0]["client_id"], "client-id")
        self.assertEqual(records[0]["refresh_token"], "refresh-token")

        icloud_records = parse_applemail_pool_content(
            "legacy@icloud.com----9188040d-6c67-4c5b-b112-36a304b66dad----"
            "0.AVeryLongRefreshTokenValueThatExceedsThirtyTwoCharacters"
        )
        self.assertEqual(
            icloud_records[0]["client_id"],
            "9188040d-6c67-4c5b-b112-36a304b66dad",
        )

    def test_parses_json_url_credential_and_preserves_pool_state(self):
        records = parse_applemail_pool_content(json.dumps({
            "email": "url@example.com",
            "password": "password",
            "mail_api_url": "https://mail.example.test/mail?token=secret",
            "totp_url": "https://2fa.example.test/view?token=secret",
            "account_type": "chatgpt_password_url_otp",
            "enabled": False,
            "pool_state": "used",
        }))

        self.assertEqual(records[0]["account_type"], "chatgpt_password_url_otp")
        self.assertEqual(
            records[0]["mail_api_url"],
            "https://mail.example.test/mail?token=secret",
        )
        self.assertEqual(records[0]["pool_state"], "used")
        self.assertFalse(records[0]["enabled"])

    def test_chatgpt_totp_json_preserves_optional_mail_api_url(self):
        mail_url = (
            "https://redeem.example.test/api/internal/oauth/email-history"
            "?email=demo%40icloud.com"
        )
        records = parse_applemail_pool_content(json.dumps({
            "email": "demo@icloud.com",
            "password": "chatgpt-password",
            "totp_secret": "JBSWY3DPEHPK3PXP",
            "account_type": "chatgpt_password_totp",
            "mail_api_url": mail_url,
        }))

        self.assertEqual(records[0]["account_type"], "chatgpt_password_totp")
        self.assertEqual(records[0]["mail_api_url"], mail_url)

    def test_nn_provider_history_selects_newest_verification_message(self):
        parsed = MailApiUrlOtpBackend._parse_mailapi_message(json.dumps({
            "ok": True,
            "email": "demo@icloud.com",
            "messages": [
                {
                    "receivedAt": "2026-08-06T10:00:00Z",
                    "subject": "Your ChatGPT verification code",
                    "verificationCode": "111111",
                },
                {
                    "receivedAt": "2026-08-06T10:01:00Z",
                    "subject": "Your ChatGPT verification code",
                    "verificationCode": "222222",
                },
            ],
        }))

        self.assertIn("222222", parsed["content"])
        self.assertNotIn("111111", parsed["content"])
        self.assertEqual(parsed["status"], True)
        self.assertIsNotNone(parsed["received_at"])
        self.assertTrue(parsed["message_id"].startswith("mailapi_message:"))

    def test_nn_provider_history_ignores_newer_unrelated_verification_mail(self):
        parsed = MailApiUrlOtpBackend._parse_mailapi_message(json.dumps({
            "ok": True,
            "email": "demo@icloud.com",
            "messages": [
                {
                    "receivedAt": "2026-08-06T10:02:00Z",
                    "subject": "Your bank verification code",
                    "verificationCode": "333333",
                },
                {
                    "receivedAt": "2026-08-06T10:01:00Z",
                    "subject": "Your temporary ChatGPT verification code",
                    "verificationCode": "222222",
                },
            ],
        }))

        self.assertIn("222222", parsed["content"])
        self.assertNotIn("333333", parsed["content"])

    def test_nn_provider_history_rejects_unparseable_received_at(self):
        parsed = MailApiUrlOtpBackend._parse_mailapi_message(json.dumps({
            "ok": True,
            "email": "demo@icloud.com",
            "messages": [{
                "receivedAt": "not-a-timestamp",
                "subject": "Your temporary ChatGPT verification code",
                "verificationCode": "222222",
            }],
        }))

        self.assertEqual(parsed["status"], False)
        self.assertEqual(parsed["content"], "")

    def test_parses_merchant_totp_then_mail_url_order(self):
        totp_url = "https://totp.example.test/JBSWY3DPEHPK3PXP"
        mail_api_url = (
            "https://mail.example.test/messages/MAIL_CREDENTIAL/"
            "user%40example.com"
        )

        records = parse_applemail_pool_content(
            f"user@example.com----password----{totp_url}----{mail_api_url}"
        )

        self.assertEqual(records[0]["mail_api_url"], mail_api_url)
        self.assertEqual(records[0]["totp_url"], totp_url)

    def test_parses_merchant_mixed_case_mail_message_path(self):
        totp_url = "https://totp.example.test/JBSWY3DPEHPK3PXP"
        mail_api_url = (
            "https://mail.example.test/Messages/MAIL_CREDENTIAL/"
            "user%40example.com"
        )

        records = parse_applemail_pool_content(
            f"user@example.com----password----{totp_url}----{mail_api_url}"
        )

        self.assertEqual(records[0]["mail_api_url"], mail_api_url)
        self.assertEqual(records[0]["totp_url"], totp_url)

    def test_preserves_current_mail_then_direct_totp_url_order(self):
        mail_api_url = (
            "https://mail.example.test/messages/MAIL_CREDENTIAL/"
            "user%40example.com"
        )
        totp_url = "https://totp.example.test/JBSWY3DPEHPK3PXP"

        records = parse_applemail_pool_content(
            f"user@example.com----password----{mail_api_url}----{totp_url}"
        )

        self.assertEqual(records[0]["mail_api_url"], mail_api_url)
        self.assertEqual(records[0]["totp_url"], totp_url)

    def test_preserves_ambiguous_legacy_four_field_url_order(self):
        mail_api_url = "https://mail.example.test/mail?token=MAIL_SECRET"
        totp_url = "https://totp.example.test/view?token=TOTP_SECRET"

        records = parse_applemail_pool_content(
            f"user@example.com----password----{mail_api_url}----{totp_url}"
        )

        self.assertEqual(records[0]["mail_api_url"], mail_api_url)
        self.assertEqual(records[0]["totp_url"], totp_url)

    def test_chatgpt_login_credentials_never_initialize_icloud_client(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            save_applemail_pool_json(
                "demo@icloud.com----chatgpt-password----JBSWY3DPEHPK3PXP",
                pool_dir=tmp_dir,
                filename="chatgpt_mfa.json",
            )
            mailbox = AppleMailMailbox(
                pool_dir=tmp_dir,
                pool_file="chatgpt_mfa.json",
                mailboxes="INBOX",
                proxy="http://127.0.0.1:7890",
            )

            with mock.patch(
                "core.icloud_mail.ICloudMailClient",
            ) as client_class:
                account = mailbox.get_email()
                current_ids = mailbox.get_current_ids(account)

            self.assertEqual(current_ids, set())
            self.assertEqual(account.extra["account_type"], "chatgpt_password_totp")
            self.assertEqual(account.extra["provider"], "chatgpt_credentials")
            self.assertEqual(account.extra["password"], "chatgpt-password")
            self.assertEqual(
                account.extra["totp_secret"],
                "JBSWY3DPEHPK3PXP",
            )
            client_class.assert_not_called()
            self.assertEqual(
                Path(tmp_dir, "chatgpt_mfa.json").stat().st_mode & 0o777,
                0o600,
            )

    def test_chatgpt_login_credentials_with_mail_url_use_mailapi_backend(self):
        mail_url = (
            "https://redeem.example.test/api/internal/oauth/email-history"
            "?email=demo%40icloud.com"
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            save_applemail_pool_json(
                json.dumps({
                    "email": "demo@icloud.com",
                    "password": "chatgpt-password",
                    "totp_secret": "JBSWY3DPEHPK3PXP",
                    "account_type": "chatgpt_password_totp",
                    "mail_api_url": mail_url,
                }),
                pool_dir=tmp_dir,
                filename="chatgpt_mfa_with_mail.json",
            )
            mailbox = AppleMailMailbox(
                pool_dir=tmp_dir,
                pool_file="chatgpt_mfa_with_mail.json",
            )
            account = mailbox.get_email()
            response = mock.Mock(
                status_code=200,
                text=json.dumps({
                    "ok": True,
                    "email": "demo@icloud.com",
                    "messages": [{
                        "receivedAt": "2026-08-06T10:01:00Z",
                        "subject": "Your ChatGPT verification code",
                        "verificationCode": "222222",
                    }],
                }),
                url=mail_url,
                history=[],
                cookies=None,
            )

            with mock.patch("requests.get", return_value=response) as request_get:
                current_ids = mailbox.get_current_ids(account)

            self.assertTrue(current_ids)
            self.assertEqual(account.extra["mail_api_url"], mail_url)
            self.assertEqual(account.extra["mailapi_url"], mail_url)
            request_get.assert_called_once()

    def test_chatgpt_login_credentials_reject_mail_history_for_other_email(self):
        mail_url = (
            "https://redeem.example.test/api/internal/oauth/email-history"
            "?email=demo%40icloud.com"
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            save_applemail_pool_json(
                json.dumps({
                    "email": "demo@icloud.com",
                    "password": "chatgpt-password",
                    "totp_secret": "JBSWY3DPEHPK3PXP",
                    "account_type": "chatgpt_password_totp",
                    "mail_api_url": mail_url,
                }),
                pool_dir=tmp_dir,
                filename="chatgpt_mfa_wrong_mail.json",
            )
            mailbox = AppleMailMailbox(
                pool_dir=tmp_dir,
                pool_file="chatgpt_mfa_wrong_mail.json",
            )
            account = mailbox.get_email()
            response = mock.Mock(
                status_code=200,
                text=json.dumps({
                    "ok": True,
                    "email": "other@icloud.com",
                    "messages": [{
                        "receivedAt": "2026-08-06T10:01:00Z",
                        "subject": "Your temporary ChatGPT verification code",
                        "verificationCode": "222222",
                    }],
                }),
                url=mail_url,
                history=[],
                cookies=None,
            )

            with mock.patch("requests.get", return_value=response):
                current_ids = mailbox.get_current_ids(account)

            self.assertEqual(current_ids, set())

    def test_remote_totp_accepts_exact_plaintext_code_without_rewriting_url(self):
        mail_url = (
            "https://mail.example.test/messages/MAIL_SECRET/"
            "user%40example.com"
        )
        totp_url = "https://totp.example.test/JBSWY3DPEHPK3PXP"
        with tempfile.TemporaryDirectory() as tmp_dir:
            save_applemail_pool_json(
                f"user@example.com----password----{mail_url}----{totp_url}",
                pool_dir=tmp_dir,
                filename="plaintext-totp.json",
            )
            logs = []
            mailbox = AppleMailMailbox(
                pool_dir=tmp_dir,
                pool_file="plaintext-totp.json",
            )
            mailbox._log_fn = logs.append
            account = mailbox.get_email()
            response = mock.Mock(status_code=200, text="654321\n")

            with mock.patch("requests.get", return_value=response) as request_get:
                code = mailbox.get_totp_code(account)

            self.assertEqual(code, "654321")
            self.assertEqual(request_get.call_args.args[0], totp_url)
            response.json.assert_not_called()
            rendered_logs = "\n".join(logs)
            self.assertNotIn("JBSWY3DPEHPK3PXP", rendered_logs)
            self.assertNotIn("654321", rendered_logs)

    def test_remote_totp_rejects_html_containing_six_digits(self):
        mail_url = (
            "https://mail.example.test/messages/MAIL_SECRET/"
            "user%40example.com"
        )
        totp_url = "https://totp.example.test/JBSWY3DPEHPK3PXP"
        with tempfile.TemporaryDirectory() as tmp_dir:
            save_applemail_pool_json(
                f"user@example.com----password----{mail_url}----{totp_url}",
                pool_dir=tmp_dir,
                filename="html-totp.json",
            )
            mailbox = AppleMailMailbox(
                pool_dir=tmp_dir,
                pool_file="html-totp.json",
            )
            account = mailbox.get_email()
            response = mock.Mock(
                status_code=200,
                text="<html>code 654321</html>",
            )
            response.json.side_effect = ValueError

            with mock.patch("requests.get", return_value=response):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "远程 2FA 获取失败",
                ) as ctx:
                    mailbox.get_totp_code(account)

            self.assertNotIn("JBSWY3DPEHPK3PXP", str(ctx.exception))
            self.assertNotIn("654321", str(ctx.exception))

    def test_remote_totp_rejects_non_ascii_digits(self):
        mail_url = (
            "https://mail.example.test/messages/MAIL_SECRET/"
            "user%40example.com"
        )
        totp_url = "https://totp.example.test/JBSWY3DPEHPK3PXP"
        with tempfile.TemporaryDirectory() as tmp_dir:
            save_applemail_pool_json(
                f"user@example.com----password----{mail_url}----{totp_url}",
                pool_dir=tmp_dir,
                filename="non-ascii-totp.json",
            )
            mailbox = AppleMailMailbox(
                pool_dir=tmp_dir,
                pool_file="non-ascii-totp.json",
            )
            account = mailbox.get_email()
            response = mock.Mock(status_code=200, text="１２３４５６")
            response.json.side_effect = ValueError

            with mock.patch("requests.get", return_value=response):
                with self.assertRaisesRegex(RuntimeError, "远程 2FA 获取失败"):
                    mailbox.get_totp_code(account)

    def test_remote_totp_timeout_traceback_is_redacted(self):
        mail_url = (
            "https://mail.example.test/messages/MAIL_SECRET/"
            "user%40example.com"
        )
        totp_url = "https://totp.example.test/JBSWY3DPEHPK3PXP"
        with tempfile.TemporaryDirectory() as tmp_dir:
            save_applemail_pool_json(
                f"user@example.com----password----{mail_url}----{totp_url}",
                pool_dir=tmp_dir,
                filename="timeout-totp.json",
            )
            mailbox = AppleMailMailbox(
                pool_dir=tmp_dir,
                pool_file="timeout-totp.json",
            )
            account = mailbox.get_email()
            timeout = requests.exceptions.Timeout(f"timeout for {totp_url}")

            with mock.patch("requests.get", side_effect=timeout):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "远程 2FA 获取失败",
                ) as ctx:
                    mailbox.get_totp_code(account)

            rendered_traceback = "".join(traceback.format_exception(
                type(ctx.exception),
                ctx.exception,
                ctx.exception.__traceback__,
            ))
            self.assertNotIn(totp_url, rendered_traceback)
            self.assertNotIn("JBSWY3DPEHPK3PXP", rendered_traceback)

    def test_remote_totp_runtime_fallback_supports_reversed_opaque_urls(self):
        mail_url = (
            "https://mail.example.test/messages/MAIL_SECRET/"
            "user%40example.com"
        )
        totp_url = "https://totp.example.test/JBSWY3DPEHPK3PXP"
        with tempfile.TemporaryDirectory() as tmp_dir:
            save_applemail_pool_json(
                json.dumps({
                    "email": "user@example.com",
                    "password": "password",
                    "mail_api_url": totp_url,
                    "totp_url": mail_url,
                    "account_type": "chatgpt_password_url_otp",
                }),
                pool_dir=tmp_dir,
                filename="reversed-opaque-totp.json",
            )
            logs = []
            mailbox = AppleMailMailbox(
                pool_dir=tmp_dir,
                pool_file="reversed-opaque-totp.json",
            )
            mailbox._log_fn = logs.append
            account = mailbox.get_email()
            mail_response = mock.Mock(
                status_code=200,
                text="<html>mail response</html>",
            )
            mail_response.json.side_effect = ValueError
            totp_response = mock.Mock(status_code=200, text="654321\n")
            totp_response.json.side_effect = ValueError

            with mock.patch(
                "requests.get",
                side_effect=[mail_response, totp_response],
            ) as request_get:
                code = mailbox.get_totp_code(account)

            self.assertEqual(code, "654321")
            self.assertEqual(
                [call.args[0] for call in request_get.call_args_list],
                [mail_url, totp_url],
            )
            self.assertEqual(account.extra["totp_url"], totp_url)
            self.assertEqual(account.extra["mail_api_url"], mail_url)
            self.assertEqual(account.extra["mailapi_url"], mail_url)
            totp_response.json.assert_not_called()
            rendered_logs = "\n".join(logs)
            self.assertIn("自动识别反向 URL 字段顺序", rendered_logs)
            self.assertNotIn("MAIL_SECRET", rendered_logs)
            self.assertNotIn("JBSWY3DPEHPK3PXP", rendered_logs)
            self.assertNotIn("654321", rendered_logs)

    def test_url_credentials_use_mail_api_and_remote_totp_without_leaking_tokens(self):
        mail_url = "https://mail.example.test/mail?token=MAIL_SECRET"
        totp_url = (
            "https://2fa.example.test/view?token=TOTP_SECRET&email=url%40example.com"
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            save_applemail_pool_json(
                f"url@example.com----password----{mail_url}----{totp_url}",
                pool_dir=tmp_dir,
                filename="url.json",
            )
            logs = []
            mailbox = AppleMailMailbox(pool_dir=tmp_dir, pool_file="url.json")
            mailbox._log_fn = logs.append
            account = mailbox.get_email()

            mail_response = mock.Mock(
                status_code=200,
                text=json.dumps({
                    "status": True,
                    "email": "url@example.com",
                    "subject": "Your verification code",
                    "msg": "Code 123456",
                    "received_at": "2026-08-03T12:00:00Z",
                    "request_id": "request-1",
                }),
                url=mail_url,
                history=[],
            )
            mail_response.cookies = None
            totp_response = mock.Mock(status_code=200)
            totp_response.json.return_value = {
                "ok": True,
                "email": "url@example.com",
                "code": "654321",
                "remaining": 20,
            }
            totp_response.raise_for_status.return_value = None

            with mock.patch(
                "requests.get",
                side_effect=[mail_response, totp_response],
            ) as request_get:
                current_ids = mailbox.get_current_ids(account)
                code = mailbox.get_totp_code(account)

            self.assertEqual(account.extra["account_type"], "chatgpt_password_url_otp")
            self.assertEqual(account.extra["mail_api_url"], mail_url)
            self.assertTrue(current_ids)
            self.assertEqual(code, "654321")
            requested_totp_url = request_get.call_args_list[1].args[0]
            self.assertIn("/api/v1/2fa?", requested_totp_url)
            self.assertIn("token=TOTP_SECRET", requested_totp_url)
            rendered_logs = "\n".join(logs)
            self.assertNotIn("MAIL_SECRET", rendered_logs)
            self.assertNotIn("TOTP_SECRET", rendered_logs)
            self.assertNotIn("654321", rendered_logs)

    def test_url_credentials_detect_reversed_mail_and_totp_links(self):
        totp_url = (
            "https://2fa.example.test/view?token=TOTP_SECRET&email=url%40example.com"
        )
        mail_url = (
            "https://oauth.example.test/view?token=MAIL_SECRET&email=url%40example.com"
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            save_applemail_pool_json(
                f"url@example.com----password----{totp_url}----{mail_url}",
                pool_dir=tmp_dir,
                filename="reversed-url.json",
            )
            logs = []
            mailbox = AppleMailMailbox(
                pool_dir=tmp_dir,
                pool_file="reversed-url.json",
            )
            mailbox._log_fn = logs.append
            account = mailbox.get_email()

            wrong_role_response = mock.Mock(status_code=401)
            totp_response = mock.Mock(status_code=200)
            totp_response.json.return_value = {
                "ok": True,
                "email": "url@example.com",
                "code": "654321",
                "remaining": 20,
            }

            with mock.patch(
                "requests.get",
                side_effect=[wrong_role_response, totp_response],
            ) as request_get:
                code = mailbox.get_totp_code(account)

            self.assertEqual(code, "654321")
            self.assertEqual(request_get.call_count, 2)
            self.assertIn(
                "oauth.example.test/api/v1/2fa",
                request_get.call_args_list[0].args[0],
            )
            self.assertIn(
                "2fa.example.test/api/v1/2fa",
                request_get.call_args_list[1].args[0],
            )
            self.assertEqual(account.extra["totp_url"], totp_url)
            self.assertEqual(account.extra["mail_api_url"], mail_url)
            self.assertEqual(account.extra["mailapi_url"], mail_url)
            rendered_logs = "\n".join(logs)
            self.assertIn("自动识别反向 URL 字段顺序", rendered_logs)
            self.assertNotIn("MAIL_SECRET", rendered_logs)
            self.assertNotIn("TOTP_SECRET", rendered_logs)
            self.assertNotIn("654321", rendered_logs)

    def test_remote_totp_failure_is_redacted(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            save_applemail_pool_json(
                "url@example.com----password----"
                "https://mail.example.test/mail?token=MAIL_SECRET----"
                "https://2fa.example.test/view?token=TOTP_SECRET",
                pool_dir=tmp_dir,
                filename="url.json",
            )
            mailbox = AppleMailMailbox(pool_dir=tmp_dir, pool_file="url.json")
            account = mailbox.get_email()
            response = mock.Mock(status_code=403)
            response.raise_for_status.side_effect = RuntimeError(
                "https://2fa.example.test/api/v1/2fa?token=TOTP_SECRET"
            )

            with mock.patch("requests.get", return_value=response):
                with self.assertRaisesRegex(RuntimeError, "远程 2FA 获取失败") as ctx:
                    mailbox.get_totp_code(account)

            self.assertNotIn("TOTP_SECRET", str(ctx.exception))

    def test_reset_url_account_generates_password_and_persists_it_after_success(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            save_applemail_pool_json(
                "reset@example.com----登陆请点击忘记密码----"
                "https://mail.example.test/mail?token=MAIL_SECRET",
                pool_dir=tmp_dir,
                filename="reset.json",
            )
            mailbox = AppleMailMailbox(pool_dir=tmp_dir, pool_file="reset.json")
            account = mailbox.get_email()

            self.assertEqual(
                account.extra["account_type"],
                "chatgpt_password_reset_url_mail",
            )
            self.assertTrue(account.extra["password_reset_required"])
            generated = account.extra["new_password"]
            self.assertGreaterEqual(len(generated), 12)
            self.assertTrue(mailbox.commit_password_reset(account, generated))

            _path, records = load_applemail_pool_records(
                pool_dir=tmp_dir,
                pool_file="reset.json",
            )
            self.assertEqual(records[0]["password"], generated)
            self.assertFalse(records[0]["password_reset_required"])
            self.assertEqual(records[0]["pool_state"], "claimed")
            self.assertNotIn("new_password", records[0])

    def test_url_login_account_can_persist_password_replaced_after_401(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            save_applemail_pool_json(
                "url@example.com----Old-Password-2026----"
                "https://mail.example.test/mail?token=MAIL_SECRET----"
                "https://2fa.example.test/view?token=TOTP_SECRET",
                pool_dir=tmp_dir,
                filename="url-password-replaced.json",
            )
            mailbox = AppleMailMailbox(
                pool_dir=tmp_dir,
                pool_file="url-password-replaced.json",
            )
            account = mailbox.get_email()

            self.assertEqual(
                account.extra["account_type"],
                "chatgpt_password_url_otp",
            )
            self.assertTrue(mailbox.mark_account_used(account))
            replacement = "Replacement-Password-2026!"
            self.assertTrue(mailbox.commit_password_reset(account, replacement))

            _path, records = load_applemail_pool_records(
                pool_dir=tmp_dir,
                pool_file="url-password-replaced.json",
            )
            self.assertEqual(records[0]["password"], replacement)
            self.assertEqual(
                records[0]["account_type"],
                "chatgpt_password_url_otp",
            )
            self.assertFalse(records[0].get("password_reset_required", False))
            self.assertEqual(records[0]["pool_state"], "used")

    def test_totp_login_with_mail_url_can_persist_password_replaced_after_401(self):
        mail_url = (
            "https://mail.example.test/history?email=mfa%40example.com"
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            save_applemail_pool_json(
                json.dumps({
                    "email": "mfa@example.com",
                    "password": "Old-Password-2026",
                    "totp_secret": "JBSWY3DPEHPK3PXP",
                    "mail_api_url": mail_url,
                    "account_type": "chatgpt_password_totp",
                }),
                pool_dir=tmp_dir,
                filename="totp-password-replaced.json",
            )
            mailbox = AppleMailMailbox(
                pool_dir=tmp_dir,
                pool_file="totp-password-replaced.json",
            )
            account = mailbox.get_email()
            replacement = "Replacement-Password-2026!"

            self.assertTrue(mailbox.commit_password_reset(account, replacement))

            _path, records = load_applemail_pool_records(
                pool_dir=tmp_dir,
                pool_file="totp-password-replaced.json",
            )
            self.assertEqual(records[0]["password"], replacement)
            self.assertEqual(records[0]["totp_secret"], "JBSWY3DPEHPK3PXP")
            self.assertEqual(records[0]["mail_api_url"], mail_url)
            self.assertEqual(records[0]["account_type"], "chatgpt_password_totp")

    def test_used_reset_url_account_can_persist_a_replacement_password(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            save_applemail_pool_json(
                "legacy@example.com----登陆请点击忘记密码----"
                "https://mail.example.test/mail?token=MAIL_SECRET",
                pool_dir=tmp_dir,
                filename="legacy-used-reset.json",
            )
            mailbox = AppleMailMailbox(
                pool_dir=tmp_dir,
                pool_file="legacy-used-reset.json",
            )
            account = mailbox.get_email()
            self.assertTrue(mailbox.mark_account_used(account))

            replacement = "Replacement-Password-2026"
            self.assertTrue(mailbox.commit_password_reset(account, replacement))

            _path, records = load_applemail_pool_records(
                pool_dir=tmp_dir,
                pool_file="legacy-used-reset.json",
            )
            self.assertEqual(records[0]["password"], replacement)
            self.assertFalse(records[0]["password_reset_required"])
            self.assertEqual(records[0]["pool_state"], "used")
            self.assertFalse(records[0]["enabled"])

    def test_four_field_reset_marker_is_not_misclassified_as_a_password(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            save_applemail_pool_json(
                "reset@example.com----登陆请点击忘记密码----"
                "https://mail.example.test/mail?token=MAIL_SECRET----"
                "https://2fa.example.test/view?token=TOTP_SECRET",
                pool_dir=tmp_dir,
                filename="reset-four-fields.json",
            )
            mailbox = AppleMailMailbox(
                pool_dir=tmp_dir,
                pool_file="reset-four-fields.json",
            )
            account = mailbox.get_email()

            self.assertEqual(
                account.extra["account_type"],
                "chatgpt_password_reset_url_mail",
            )
            self.assertTrue(account.extra["password_reset_required"])
            self.assertEqual(
                account.extra["totp_url"],
                "https://2fa.example.test/view?token=TOTP_SECRET",
            )
            self.assertNotIn("忘记密码", account.extra["new_password"])

    def test_legacy_json_with_reset_marker_self_heals_on_load(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            save_applemail_pool_json(
                json.dumps(
                    {
                        "email": "reset@example.com",
                        "password": "登陆请点击忘记密码",
                        "mail_api_url": "https://mail.example.test/mail?token=MAIL_SECRET",
                        "totp_url": "https://2fa.example.test/view?token=TOTP_SECRET",
                        "account_type": "chatgpt_password_url_otp",
                    }
                ),
                pool_dir=tmp_dir,
                filename="legacy-reset.json",
            )

            _path, records = load_applemail_pool_records(
                pool_dir=tmp_dir,
                pool_file="legacy-reset.json",
            )

            self.assertEqual(
                records[0]["account_type"],
                "chatgpt_password_reset_url_mail",
            )
            self.assertEqual(records[0]["password"], "")
            self.assertTrue(records[0]["password_reset_required"])

    def test_explicit_icloud_web_json_still_routes_to_direct_client(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            save_applemail_pool_json(
                json.dumps(
                    {
                        "email": "demo@icloud.com",
                        "password": "apple-password",
                        "mfa_secret": "JBSWY3DPEHPK3PXP",
                        "account_type": "icloud_web",
                    }
                ),
                pool_dir=tmp_dir,
                filename="icloud.json",
            )
            client = mock.Mock()
            client.list_messages.return_value = [
                {
                    "id": "INBOX:thread-1:12",
                    "subject": "Your OpenAI code is 654321",
                    "preview": "654321",
                }
            ]
            mailbox = AppleMailMailbox(
                pool_dir=tmp_dir,
                pool_file="icloud.json",
                mailboxes="INBOX",
                proxy="http://127.0.0.1:7890",
            )

            with mock.patch(
                "core.icloud_mail.ICloudMailClient",
                return_value=client,
            ) as client_class:
                account = mailbox.get_email()
                current_ids = mailbox.get_current_ids(account)

            self.assertEqual(current_ids, {"INBOX:thread-1:12"})
            self.assertEqual(account.extra["account_type"], "icloud_web")
            client_class.assert_called_once()


if __name__ == "__main__":
    unittest.main()
