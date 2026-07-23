"""Private-preview selection controls (PR A): repository / branch listing,
model catalog, job model selection, active-only API keys.

Repository availability mirrors GitHub App access exactly — there is no
in-GNSIS enable/disable step. A repository accessible through the App is
usable immediately; one that is no longer accessible becomes unavailable
for new runs while historical rows survive.

Service-layer checks run directly against the test DB; API checks override the
auth dependencies (current_workspace / current_user / get_github_app) so the
routes are exercised without a live JWT or GitHub.
"""

from __future__ import annotations

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from _authkit import fresh_sqlite_env  # noqa: E402


def _prepare():
    os.environ["GNSIS_RUN_ALLOWED_MODELS"] = "anthropic/claude-opus-4.8,openai/gpt-5.4"
    os.environ["GNSIS_MODEL_METADATA"] = '{"openai/gpt-5.4": {"label": "GPT-5.4", "cost_tier": "high"}}'
    # Execution config so create_job reaches model validation.
    os.environ["GNSIS_EXECUTION_PROVIDER"] = "github_actions"
    os.environ["GNSIS_PUBLIC_API_URL"] = "https://api.gnsis.studio"
    os.environ["GNSIS_EXECUTOR_OWNER"] = "aubincorinaldiecooper-bit"
    os.environ["GNSIS_EXECUTOR_REPO"] = "Gnsis-studio-"
    os.environ["GNSIS_EXECUTOR_OIDC_AUDIENCE"] = "https://api.gnsis.studio"
    os.environ["GNSIS_EXECUTOR_TRUSTED_WORKFLOW_SHA"] = "0" * 40
    fresh_sqlite_env()
    from gnsis.service import settings as sm

    sm._settings = None
    from gnsis.service.db import init_db

    init_db()


def _gh_repo(gh_id, full_name, *, default_branch="main", private=False, archived=False):
    owner, name = full_name.split("/")
    return {"id": gh_id, "full_name": full_name, "name": name,
            "owner": {"login": owner}, "default_branch": default_branch,
            "private": private, "archived": archived}


class RepositoryAvailabilityTests(unittest.TestCase):
    """Sync mirrors GitHub App access exactly — no second enablement step."""

    def setUp(self):
        _prepare()
        from gnsis.service import workspaces as ws

        self.ws = ws
        # An installation to hang repos off (FKs are not enforced on SQLite).
        from gnsis.service.db import session_scope
        from gnsis.service import orm

        with session_scope() as s:
            s.add(orm.GitHubInstallation(id="inst-1", workspace_id="ws-1",
                                         github_installation_id=555, status="active"))

    def _sync(self, repos):
        return self.ws.sync_repositories("ws-1", "inst-1", repos)

    def test_newly_synced_repos_are_immediately_available(self):
        # A repository accessible through the GitHub App must appear in the
        # user-facing catalog with no separate approval step.
        self._sync([_gh_repo(10, "octo/alpha"), _gh_repo(11, "octo/beta")])
        repos = self.ws.list_repositories_page("ws-1")
        self.assertEqual({r.full_name for r in repos}, {"octo/alpha", "octo/beta"})

    def test_resync_updates_metadata_and_keeps_access(self):
        self._sync([_gh_repo(10, "octo/alpha"), _gh_repo(11, "octo/beta")])
        # Same repos, updated default branch on alpha — availability unchanged,
        # metadata refreshed. No user-controlled state exists to preserve.
        self._sync([_gh_repo(10, "octo/alpha", default_branch="develop"),
                    _gh_repo(11, "octo/beta")])
        after = {r.full_name: r for r in self.ws.list_repositories_page("ws-1")}
        self.assertEqual(after["octo/alpha"].default_branch, "develop")
        self.assertEqual(set(after.keys()), {"octo/alpha", "octo/beta"})

    def test_repo_removed_from_github_access_disappears_from_catalog(self):
        # First sync: both alpha and beta are accessible.
        self._sync([_gh_repo(10, "octo/alpha"), _gh_repo(11, "octo/beta")])
        # Later sync: beta is no longer part of the installation.
        self._sync([_gh_repo(10, "octo/alpha")])
        catalog = self.ws.list_repositories_page("ws-1")
        self.assertEqual({r.full_name for r in catalog}, {"octo/alpha"})

    def test_repo_removed_from_github_access_row_survives_for_history(self):
        # Sync then remove access.
        self._sync([_gh_repo(10, "octo/alpha")])
        alpha = self.ws.list_repositories_page("ws-1")[0]
        self._sync([])  # installation no longer grants access to any repo.
        # The row still exists so historical jobs / receipts / intelligence
        # continue to resolve. It just isn't in the user-facing catalog.
        self.assertEqual(self.ws.list_repositories_page("ws-1"), [])
        self.assertIsNotNone(self.ws.get_repository("ws-1", alpha.id))

    def test_regranted_access_reappears_without_extra_approval(self):
        # Access → removed → regranted. The repo becomes available again on
        # the next sync with no in-GNSIS step in between.
        self._sync([_gh_repo(10, "octo/alpha")])
        self._sync([])
        self.assertEqual(self.ws.list_repositories_page("ws-1"), [])
        self._sync([_gh_repo(10, "octo/alpha")])
        catalog = self.ws.list_repositories_page("ws-1")
        self.assertEqual({r.full_name for r in catalog}, {"octo/alpha"})

    def test_search_and_pagination(self):
        self._sync([_gh_repo(10, "octo/alpha"), _gh_repo(11, "octo/beta"),
                    _gh_repo(12, "octo/gamma")])
        found = self.ws.list_repositories_page("ws-1", search="beta")
        self.assertEqual([r.full_name for r in found], ["octo/beta"])
        # pagination (ordered by full_name: alpha, beta, gamma)
        page1 = self.ws.list_repositories_page("ws-1", limit=2, offset=0)
        page2 = self.ws.list_repositories_page("ws-1", limit=2, offset=2)
        self.assertEqual([r.full_name for r in page1], ["octo/alpha", "octo/beta"])
        self.assertEqual([r.full_name for r in page2], ["octo/gamma"])


class ModelCatalogServiceTests(unittest.TestCase):
    def setUp(self):
        _prepare()

    def _settings(self):
        from gnsis.service.settings import get_settings

        return get_settings()

    def test_catalog_matches_allowlist(self):
        from gnsis.service.model_catalog import model_catalog

        cat = model_catalog(self._settings())
        self.assertEqual([m["id"] for m in cat], ["anthropic/claude-opus-4.8", "openai/gpt-5.4"])
        self.assertTrue(cat[0]["default"])
        self.assertFalse(cat[1]["default"])
        self.assertEqual(cat[0]["label"], "anthropic/claude-opus-4.8")  # id fallback
        self.assertEqual(cat[1]["label"], "GPT-5.4")  # from metadata
        self.assertEqual(cat[1]["cost_tier"], "high")

    def test_resolve_rejects_unsupported_and_defaults(self):
        from gnsis.service.model_catalog import default_model, resolve_allowed_model

        s = self._settings()
        self.assertEqual(resolve_allowed_model(s, None), "anthropic/claude-opus-4.8")
        self.assertEqual(resolve_allowed_model(s, "openai/gpt-5.4"), "openai/gpt-5.4")
        self.assertIsNone(resolve_allowed_model(s, "evil/model"))
        self.assertEqual(default_model(s), "anthropic/claude-opus-4.8")


class BranchListingServiceTests(unittest.TestCase):
    def setUp(self):
        _prepare()
        from gnsis.service import orm
        from gnsis.service.db import session_scope

        with session_scope() as s:
            s.add(orm.GitHubInstallation(id="inst-1", workspace_id="ws-1",
                                         github_installation_id=555, status="active"))
            s.add(orm.Repository(id="repo-1", workspace_id="ws-1",
                                 github_installation_record_id="inst-1",
                                 github_repository_id=10, owner="octo", name="alpha",
                                 full_name="octo/alpha", default_branch="main", enabled=True))

    def _patch_github(self, monkey_branches):
        import gnsis.service.branches as br

        captured = {}

        class FakeGH:
            def __init__(self, app):
                captured["app"] = app

            def scoped_installation_token(self, installation_id, *, repositories, permissions):
                captured["installation_id"] = installation_id
                captured["permissions"] = permissions
                return {"token": "super-secret-token"}

            def list_branches(self, owner, repo, token, **kw):
                captured["owner_repo"] = (owner, repo)
                captured["token_used"] = token
                return monkey_branches

        br.ExecutorGitHub = FakeGH
        return captured

    def test_lists_branches_default_first_and_hides_token(self):
        captured = self._patch_github([{"name": "dev"}, {"name": "main"}, {"name": "feature/x"}])
        from gnsis.service.branches import list_repository_branches
        from gnsis.service.settings import get_settings

        result = list_repository_branches(get_settings(), object(),
                                          workspace_id="ws-1", repository_id="repo-1")
        names = [b["name"] for b in result["branches"]]
        self.assertEqual(names[0], "main")  # default first
        self.assertEqual(result["default_branch"], "main")
        self.assertTrue(result["branches"][0]["is_default"])
        # Correct installation + least-privilege scope used.
        self.assertEqual(captured["installation_id"], 555)
        # contents:read (not metadata-only) — List branches 403s for a
        # metadata-only token on private repos.
        self.assertEqual(captured["permissions"], {"contents": "read"})
        self.assertEqual(captured["owner_repo"], ("octo", "alpha"))
        # The token is never part of the returned data.
        self.assertNotIn("super-secret-token", str(result))

    def test_unknown_repo_returns_none(self):
        self._patch_github([{"name": "main"}])
        from gnsis.service.branches import list_repository_branches
        from gnsis.service.settings import get_settings

        self.assertIsNone(list_repository_branches(get_settings(), object(),
                                                   workspace_id="ws-1", repository_id="nope"))
        # cross-workspace is also None
        self.assertIsNone(list_repository_branches(get_settings(), object(),
                                                   workspace_id="ws-2", repository_id="repo-1"))


class ActiveKeyListingTests(unittest.TestCase):
    def setUp(self):
        _prepare()

    def test_active_only_excludes_rotated_and_disabled_but_keeps_them_stored(self):
        from gnsis.service.settings import get_settings
        from gnsis.service.virtual_keys import VirtualKeyStore

        settings = get_settings()
        store = VirtualKeyStore()
        k1, _ = store.create(settings, workspace_id="ws-1", name="key1")
        k2, _ = store.create(settings, workspace_id="ws-1", name="key2")
        # Disable k1, rotate k2.
        store.disable("ws-1", k1.id)
        store.rotate(settings, "ws-1", k2.id)

        active = store.list_for_workspace("ws-1", active_only=True)
        active_ids = {k.id for k in active}
        self.assertNotIn(k1.id, active_ids)  # disabled hidden
        self.assertNotIn(k2.id, active_ids)  # rotated hidden
        # But everything is still stored internally (audit/attribution).
        allrows = store.list_for_workspace("ws-1", active_only=False)
        all_ids = {k.id for k in allrows}
        self.assertIn(k1.id, all_ids)
        self.assertIn(k2.id, all_ids)


class SelectionApiTests(unittest.TestCase):
    def setUp(self):
        _prepare()
        from gnsis.service import orm
        from gnsis.service.db import session_scope

        with session_scope() as s:
            s.add(orm.GitHubInstallation(id="inst-1", workspace_id="ws-1",
                                         github_installation_id=555, status="active"))
            # Both repos are currently accessible through GitHub — no user
            # enablement step exists to withhold either.
            s.add(orm.Repository(id="repo-1", workspace_id="ws-1",
                                 github_installation_record_id="inst-1",
                                 github_repository_id=10, owner="octo", name="alpha",
                                 full_name="octo/alpha", default_branch="main", enabled=True))
            s.add(orm.Repository(id="repo-2", workspace_id="ws-1",
                                 github_installation_record_id="inst-1",
                                 github_repository_id=11, owner="octo", name="beta",
                                 full_name="octo/beta", default_branch="main", enabled=True))
            # A historical row for a repo that lost GitHub access — must
            # survive for past-job resolution but never appear in the catalog
            # or accept new runs.
            s.add(orm.Repository(id="repo-gone", workspace_id="ws-1",
                                 github_installation_record_id="inst-1",
                                 github_repository_id=99, owner="octo", name="gone",
                                 full_name="octo/gone", default_branch="main", enabled=False))

        from fastapi.testclient import TestClient
        from gnsis.service import api

        self.api = api
        api.app.dependency_overrides[api.current_workspace] = lambda: types.SimpleNamespace(id="ws-1")
        api.app.dependency_overrides[api.current_user] = lambda: types.SimpleNamespace(subject="u-1")
        api.app.dependency_overrides[api.get_github_app] = lambda: object()
        self.client = TestClient(api.app)

    def tearDown(self):
        self.api.app.dependency_overrides.clear()

    def test_models_endpoint(self):
        r = self.client.get("/v1/models")
        self.assertEqual(r.status_code, 200)
        ids = [m["id"] for m in r.json()["items"]]
        self.assertEqual(ids, ["anthropic/claude-opus-4.8", "openai/gpt-5.4"])

    def test_repositories_listing_returns_only_currently_accessible(self):
        allr = self.client.get("/v1/repositories").json()
        # Only the two currently-accessible repos appear; the "gone" row that
        # lost GitHub access is filtered out of the user-facing catalog.
        self.assertEqual({r["full_name"] for r in allr}, {"octo/alpha", "octo/beta"})
        found = self.client.get("/v1/repositories", params={"q": "beta"}).json()
        self.assertEqual([r["full_name"] for r in found], ["octo/beta"])

    def test_repositories_listing_ignores_legacy_enabled_only_param(self):
        # Older frontends may still pass this — it is accepted and returns the
        # exact same result as the default listing, never a smaller set.
        default = self.client.get("/v1/repositories").json()
        with_flag = self.client.get(
            "/v1/repositories", params={"enabled_only": True}
        ).json()
        self.assertEqual([r["full_name"] for r in default],
                         [r["full_name"] for r in with_flag])

    def test_no_user_toggle_route_exists(self):
        # The user-facing enable/disable route is gone from the product
        # contract. Any PATCH to a repository is a 405 or 404.
        r = self.client.patch("/v1/repositories/repo-1", json={"enabled": False})
        self.assertIn(r.status_code, (404, 405))

    def test_branches_route_hides_token(self):
        import gnsis.service.branches as br

        class FakeGH:
            def __init__(self, app): pass
            def scoped_installation_token(self, iid, *, repositories, permissions):
                return {"token": "super-secret-token"}
            def list_branches(self, owner, repo, token, **kw):
                return [{"name": "main"}, {"name": "dev"}]

        br.ExecutorGitHub = FakeGH
        r = self.client.get("/v1/repositories/repo-1/branches")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["branches"][0]["name"], "main")
        self.assertNotIn("super-secret-token", r.text)

    def test_create_job_rejects_unsupported_model(self):
        r = self.client.post("/jobs", json={
            "repository_id": "repo-1", "instruction": "do it", "model": "evil/model"})
        self.assertEqual(r.status_code, 422)

    def test_create_job_accepts_any_currently_accessible_repository(self):
        # Both repo-1 and repo-2 are currently accessible through GitHub.
        # The API must accept them for job creation without any separate
        # enable step. Regression guard for the removed 409-disabled path.
        import gnsis.service.tasks as tasks

        tasks.run_job.delay = lambda *a, **k: None
        for repo_id in ("repo-1", "repo-2"):
            r = self.client.post("/jobs", json={
                "repository_id": repo_id, "instruction": "do it",
                "model": "openai/gpt-5.4",
                "advisor_model": "anthropic/claude-opus-4.8"})
            self.assertEqual(r.status_code, 200, f"{repo_id}: {r.text}")

    def test_create_job_rejects_repo_that_lost_github_access(self):
        # ``repo-gone`` is a historical row whose GitHub access was removed
        # (``enabled=False``). It must not be usable for new runs — from the
        # user's point of view it isn't in their accessible list at all.
        r = self.client.post("/jobs", json={
            "repository_id": "repo-gone", "instruction": "do it",
            "model": "openai/gpt-5.4",
            "advisor_model": "anthropic/claude-opus-4.8"})
        self.assertEqual(r.status_code, 404)

    def test_create_job_persists_selected_model(self):
        import gnsis.service.tasks as tasks

        tasks.run_job.delay = lambda *a, **k: None
        r = self.client.post("/jobs", json={
            "repository_id": "repo-1", "instruction": "add hello", "model": "openai/gpt-5.4",
            "advisor_model": "anthropic/claude-opus-4.8"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["model"], "openai/gpt-5.4")

    def test_create_job_rejects_unsupported_advisor_model(self):
        # The Advisor is validated against the SAME allowlist as the primary,
        # so an off-list Advisor is rejected at the API boundary — the gateway
        # cannot be tricked into using an arbitrary model as a subsidised
        # second seat.
        r = self.client.post("/jobs", json={
            "repository_id": "repo-1", "instruction": "do it",
            "model": "openai/gpt-5.4", "advisor_model": "attacker/model"})
        self.assertEqual(r.status_code, 422, r.text)
        self.assertIn("advisor_model", r.text)

    def test_create_job_persists_both_selected_and_advisor_model(self):
        import gnsis.service.tasks as tasks

        tasks.run_job.delay = lambda *a, **k: None
        r = self.client.post("/jobs", json={
            "repository_id": "repo-1", "instruction": "add hello",
            "model": "anthropic/claude-opus-4.8",
            "advisor_model": "openai/gpt-5.4"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        # Both fields survive round-trip on JobResponse.
        self.assertEqual(body["model"], "anthropic/claude-opus-4.8")
        self.assertEqual(body["advisor_model"], "openai/gpt-5.4")

    def test_create_job_without_advisor_persists_null(self):
        import gnsis.service.tasks as tasks

        tasks.run_job.delay = lambda *a, **k: None
        r = self.client.post("/jobs", json={
            "repository_id": "repo-1", "instruction": "add hello",
            "model": "openai/gpt-5.4"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["model"], "openai/gpt-5.4")
        self.assertIsNone(r.json()["advisor_model"])

    def test_create_job_accepts_equal_primary_and_advisor_model_ids(self):
        import gnsis.service.tasks as tasks

        tasks.run_job.delay = lambda *a, **k: None
        r = self.client.post("/jobs", json={
            "repository_id": "repo-1", "instruction": "add hello",
            "model": "openai/gpt-5.4",
            "advisor_model": "openai/gpt-5.4"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["model"], "openai/gpt-5.4")
        self.assertEqual(r.json()["advisor_model"], "openai/gpt-5.4")

    def test_single_model_allowlist_can_create_job_with_same_role_model(self):
        import gnsis.service.settings as settings_mod
        import gnsis.service.tasks as tasks

        tasks.run_job.delay = lambda *a, **k: None
        settings_mod._settings = None
        import os
        os.environ["GNSIS_RUN_ALLOWED_MODELS"] = "anthropic/claude-opus-4.8"
        r = self.client.post("/jobs", json={
            "repository_id": "repo-1", "instruction": "add hello",
            "model": "anthropic/claude-opus-4.8",
            "advisor_model": "anthropic/claude-opus-4.8"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["model"], "anthropic/claude-opus-4.8")
        self.assertEqual(r.json()["advisor_model"], "anthropic/claude-opus-4.8")


if __name__ == "__main__":
    unittest.main()
