import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from _authkit import fresh_sqlite_env  # noqa: E402


def _init():
    from gnsis.service.db import init_db

    init_db()


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        fresh_sqlite_env()
        _init()

    def test_create_is_idempotent(self):
        from gnsis.service.workspaces import get_or_create_workspace

        a = get_or_create_workspace("auth|user-1")
        b = get_or_create_workspace("auth|user-1")
        self.assertEqual(a.id, b.id)
        self.assertEqual(a.owner_auth_subject, "auth|user-1")

    def test_distinct_subjects_get_distinct_workspaces(self):
        from gnsis.service.workspaces import get_or_create_workspace

        a = get_or_create_workspace("user-1")
        b = get_or_create_workspace("user-2")
        self.assertNotEqual(a.id, b.id)

    def test_get_by_subject_returns_none_when_absent(self):
        from gnsis.service.workspaces import get_workspace_by_subject

        self.assertIsNone(get_workspace_by_subject("nobody"))


class InstallationTests(unittest.TestCase):
    def setUp(self):
        fresh_sqlite_env()
        _init()
        from gnsis.service.workspaces import get_or_create_workspace

        self.ws = get_or_create_workspace("user-1")

    def _verified(self, inst_id=555):
        from gnsis.service.auth_client import VerifiedInstallation

        return VerifiedInstallation(
            installation_id=inst_id,
            account_id=99,
            account_login="octo",
            account_type="User",
        )

    def test_upsert_is_idempotent(self):
        from gnsis.service.workspaces import upsert_installation

        a = upsert_installation(self.ws.id, self._verified())
        b = upsert_installation(self.ws.id, self._verified())
        self.assertEqual(a.id, b.id)
        self.assertEqual(a.github_installation_id, 555)

    def test_cross_workspace_reclaim_rejected(self):
        from gnsis.service.workspaces import (
            WorkspaceConflictError,
            get_or_create_workspace,
            upsert_installation,
        )

        upsert_installation(self.ws.id, self._verified())
        other = get_or_create_workspace("user-2")
        with self.assertRaises(WorkspaceConflictError):
            upsert_installation(other.id, self._verified())


class RepositorySyncTests(unittest.TestCase):
    def setUp(self):
        fresh_sqlite_env()
        _init()
        from gnsis.service.auth_client import VerifiedInstallation
        from gnsis.service.workspaces import (
            get_or_create_workspace,
            upsert_installation,
        )

        self.ws = get_or_create_workspace("user-1")
        self.inst = upsert_installation(
            self.ws.id,
            VerifiedInstallation(
                installation_id=1, account_id=1, account_login="o", account_type="User"
            ),
        )

    def _gh(self, repo_id, full_name, **kw):
        owner, name = full_name.split("/")
        base = {
            "id": repo_id,
            "full_name": full_name,
            "name": name,
            "owner": {"login": owner},
            "default_branch": "main",
            "private": False,
            "archived": False,
        }
        base.update(kw)
        return base

    def test_sync_upserts_and_lists(self):
        from gnsis.service.workspaces import list_repositories, sync_repositories

        sync_repositories(
            self.ws.id,
            self.inst.id,
            [self._gh(10, "o/a"), self._gh(11, "o/b", private=True)],
        )
        repos = list_repositories(self.ws.id)
        self.assertEqual({r.full_name for r in repos}, {"o/a", "o/b"})
        b = next(r for r in repos if r.full_name == "o/b")
        self.assertTrue(b.private)

    def test_removed_repo_is_disabled_not_deleted(self):
        from gnsis.service.workspaces import (
            list_repositories,
            sync_repositories,
        )

        sync_repositories(
            self.ws.id, self.inst.id, [self._gh(10, "o/a"), self._gh(11, "o/b")]
        )
        # Second sync drops o/b.
        sync_repositories(self.ws.id, self.inst.id, [self._gh(10, "o/a")])
        enabled = list_repositories(self.ws.id)
        self.assertEqual({r.full_name for r in enabled}, {"o/a"})
        all_repos = list_repositories(self.ws.id, include_disabled=True)
        self.assertEqual({r.full_name for r in all_repos}, {"o/a", "o/b"})
        b = next(r for r in all_repos if r.full_name == "o/b")
        self.assertFalse(b.enabled)

    def test_get_repository_scoped_to_workspace(self):
        from gnsis.service.workspaces import (
            get_or_create_workspace,
            get_repository,
            sync_repositories,
        )

        repos = sync_repositories(self.ws.id, self.inst.id, [self._gh(10, "o/a")])
        repo_id = repos[0].id
        # Same repo id is invisible to a different workspace.
        other = get_or_create_workspace("user-2")
        self.assertIsNone(get_repository(other.id, repo_id))
        self.assertIsNotNone(get_repository(self.ws.id, repo_id))


if __name__ == "__main__":
    unittest.main()
