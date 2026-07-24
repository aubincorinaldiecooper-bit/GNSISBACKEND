import hashlib
import hmac
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from _authkit import fresh_sqlite_env  # noqa: E402


def _sign(secret, body):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class SignatureTests(unittest.TestCase):
    def test_valid_signature_accepted(self):
        from gnsis.service.webhooks import verify_signature

        body = b'{"a":1}'
        verify_signature("secret", body, _sign("secret", body))  # no raise

    def test_invalid_signature_rejected(self):
        from gnsis.service.webhooks import WebhookError, verify_signature

        body = b'{"a":1}'
        with self.assertRaises(WebhookError):
            verify_signature("secret", body, _sign("wrong", body))

    def test_missing_signature_rejected(self):
        from gnsis.service.webhooks import WebhookError, verify_signature

        with self.assertRaises(WebhookError):
            verify_signature("secret", b"{}", "")


class HandlerTests(unittest.TestCase):
    def setUp(self):
        fresh_sqlite_env()
        from gnsis.service.db import init_db

        init_db()
        from gnsis.service.auth_client import VerifiedInstallation
        from gnsis.service.workspaces import (
            get_or_create_workspace,
            sync_repositories,
            upsert_installation,
        )

        self.ws = get_or_create_workspace("user-1")
        self.inst = upsert_installation(
            self.ws.id,
            VerifiedInstallation(
                installation_id=42, account_id=1, account_login="o", account_type="User"
            ),
        )
        sync_repositories(
            self.ws.id,
            self.inst.id,
            [
                {
                    "id": 10,
                    "full_name": "o/a",
                    "name": "a",
                    "owner": {"login": "o"},
                    "default_branch": "main",
                    "private": False,
                    "archived": False,
                }
            ],
        )

    def test_duplicate_delivery_is_idempotent(self):
        from gnsis.service.webhooks import handle_event

        payload = {"action": "suspend", "installation": {"id": 42}}
        first = handle_event("installation", "delivery-1", payload)
        second = handle_event("installation", "delivery-1", payload)
        self.assertEqual(first["status"], "applied")
        self.assertEqual(second["status"], "duplicate")

    def test_installation_deleted_disables_repositories(self):
        from gnsis.service.webhooks import handle_event
        from gnsis.service.workspaces import list_repositories

        handle_event(
            "installation", "d-1", {"action": "deleted", "installation": {"id": 42}}
        )
        self.assertEqual(list_repositories(self.ws.id), [])  # none enabled

    def test_suspend_and_unsuspend(self):
        from gnsis.service.webhooks import handle_event
        from gnsis.service.workspaces import get_installation_for_workspace

        handle_event(
            "installation", "s-1", {"action": "suspend", "installation": {"id": 42}}
        )
        inst = get_installation_for_workspace(self.ws.id, 42)
        self.assertEqual(inst.status, "suspended")
        handle_event(
            "installation", "s-2", {"action": "unsuspend", "installation": {"id": 42}}
        )
        inst = get_installation_for_workspace(self.ws.id, 42)
        self.assertEqual(inst.status, "active")

    def test_repositories_removed_disables_only_those(self):
        from gnsis.service.webhooks import handle_event
        from gnsis.service.workspaces import list_repositories

        handle_event(
            "installation_repositories",
            "r-1",
            {
                "action": "removed",
                "installation": {"id": 42},
                "repositories_removed": [{"id": 10}],
            },
        )
        self.assertEqual(list_repositories(self.ws.id), [])

    def test_event_for_unclaimed_installation_is_safe(self):
        from gnsis.service.webhooks import handle_event

        # Installation 999 was never claimed — must not error.
        res = handle_event(
            "installation", "u-1", {"action": "suspend", "installation": {"id": 999}}
        )
        self.assertEqual(res["status"], "applied")


if __name__ == "__main__":
    unittest.main()
