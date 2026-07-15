"""The worker resolves each run's own installation, and never persists a token."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from _authkit import fresh_sqlite_env  # noqa: E402


class WorkerInstallationTests(unittest.TestCase):
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
        repos = sync_repositories(
            self.ws.id,
            self.inst.id,
            [
                {
                    "id": 10,
                    "full_name": "o/a",
                    "name": "a",
                    "owner": {"login": "o"},
                    "default_branch": "main",
                    "private": True,
                    "archived": False,
                }
            ],
        )
        self.repo = repos[0]

        from gnsis.orchestration.models import JobSpec
        from gnsis.service.repository import PostgresJobStore

        self.store = PostgresJobStore()
        self.job = self.store.create_job(
            JobSpec(
                repo="o/a",
                instruction="do it",
                base_branch="main",
                engine="gnsis",
                workspace_id=self.ws.id,
                repository_id=self.repo.id,
            )
        )

    def test_resolves_jobs_own_installation(self):
        import gnsis.service.tasks as tasks

        self.assertEqual(tasks.resolve_installation_id(self.job), 42)

    def test_falls_back_to_global_when_no_repository(self):
        import gnsis.service.tasks as tasks
        from gnsis.orchestration.models import JobSpec

        legacy = self.store.create_job(
            JobSpec(repo="o/legacy", instruction="x", base_branch="main", engine="mock")
        )
        original = tasks.settings.github_app_installation_id
        try:
            tasks.settings.github_app_installation_id = "7"
            self.assertEqual(tasks.resolve_installation_id(legacy), 7)
        finally:
            tasks.settings.github_app_installation_id = original

    def test_token_is_never_persisted_on_the_job(self):
        # The job row / context must never contain an installation token. The
        # token only flows to prepare_workspace at clone time and is discarded.
        fetched = self.store.get_job(self.job.id)
        serialized = str(fetched.context)
        self.assertNotIn("ghs_", serialized)
        self.assertNotIn("token", serialized.lower())

    def test_mint_token_is_separate_from_persistence(self):
        import gnsis.service.tasks as tasks

        class FakeApp:
            def __init__(self):
                self.calls = []

            def token_for_installation(self, iid):
                self.calls.append(iid)
                return f"ghs_secret_{iid}"

        fake = FakeApp()
        orig = tasks.app_from_settings
        tasks.settings.github_app_id = "1"
        tasks.settings.github_app_private_key = "k"
        try:
            tasks.app_from_settings = lambda s: fake
            token = tasks._mint_token(42)
        finally:
            tasks.app_from_settings = orig
        self.assertEqual(token, "ghs_secret_42")
        self.assertEqual(fake.calls, [42])
        # Still not on the persisted job.
        self.assertNotIn("ghs_secret", str(self.store.get_job(self.job.id).context))


if __name__ == "__main__":
    unittest.main()
