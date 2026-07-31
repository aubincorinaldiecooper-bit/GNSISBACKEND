"""End-to-end API auth + tenancy tests via FastAPI TestClient.

SQLite stands in for Postgres; the JWT verifier, auth-service client, and GitHub
App are injected via dependency overrides so no network or live services are
needed. Celery's ``.delay`` is monkeypatched to a no-op so job creation doesn't
require a broker.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from _authkit import AUDIENCE, ISSUER, fresh_sqlite_env, make_keypair, mint  # noqa: E402


class FakeAuthClient:
    """Stands in for the auth service: only ``allowed`` installs verify."""

    def __init__(self, allowed):
        self.allowed = allowed

    def verify_installation(self, auth_subject, installation_id):
        from gnsis.service.auth_client import (
            InstallationVerificationError,
            VerifiedInstallation,
        )

        if installation_id not in self.allowed:
            raise InstallationVerificationError("not accessible", status=403)
        return VerifiedInstallation(
            installation_id=installation_id,
            account_id=1000 + installation_id,
            account_login=f"acct-{installation_id}",
            account_type="User",
        )


class FakeGitHubApp:
    """Stands in for the platform GitHub App + repo listing."""

    def __init__(self, repos_by_installation):
        self.repos_by_installation = repos_by_installation
        self.minted = []

    def token_for_installation(self, installation_id):
        self.minted.append(installation_id)
        return f"ghs_faketoken_{installation_id}"


def _repo(repo_id, full_name, default_branch="main", private=False):
    owner, name = full_name.split("/")
    return {
        "id": repo_id,
        "full_name": full_name,
        "name": name,
        "owner": {"login": owner},
        "default_branch": default_branch,
        "private": private,
        "archived": False,
    }


class ApiAuthTestBase(unittest.TestCase):
    def setUp(self):
        fresh_sqlite_env()
        # Configure user-auth + verification + github app so deps don't 503.
        os.environ["BETTER_AUTH_JWKS_URL"] = "https://auth.test/jwks"
        os.environ["BETTER_AUTH_ISSUER"] = ISSUER
        os.environ["BETTER_AUTH_AUDIENCE"] = AUDIENCE
        os.environ["GNSIS_AUTH_INTERNAL_URL"] = "https://auth.test"
        os.environ["GNSIS_AUTH_INTERNAL_SECRET"] = "internal-secret"
        os.environ["GITHUB_APP_ID"] = "12345"
        os.environ["GITHUB_APP_PRIVATE_KEY"] = "key"
        os.environ["GITHUB_APP_SLUG"] = "genesis"
        # Public-beta execution config so job creation is permitted.
        os.environ["GNSIS_EXECUTION_PROVIDER"] = "github_actions"
        os.environ["GNSIS_PUBLIC_API_URL"] = "https://api.gnsis.test"
        os.environ["GNSIS_EXECUTOR_OWNER"] = "aubincorinaldiecooper-bit"
        os.environ["GNSIS_EXECUTOR_REPO"] = "Gnsis-studio-"
        os.environ["GNSIS_EXECUTOR_OIDC_AUDIENCE"] = "https://api.gnsis.studio"
        os.environ["GNSIS_EXECUTOR_TRUSTED_WORKFLOW_SHA"] = "0" * 40
        os.environ["GNSIS_RUN_ALLOWED_MODELS"] = "anthropic/claude-opus-4.8,openai/gpt-5.4"
        from gnsis.service import settings as settings_mod

        settings_mod._settings = None

        from gnsis.service.db import init_db

        init_db()

        # Patch Celery task enqueue so create/approve don't need a broker.
        import gnsis.service.tasks as tasks

        tasks.run_job.delay = lambda *a, **k: None
        tasks.publish_pr.delay = lambda *a, **k: None

        from fastapi.testclient import TestClient

        from gnsis.service import api
        from gnsis.service.auth import JwksCache, JwtVerifier

        self.priv, self.jwks = make_keypair("k1")
        self.api = api
        verifier = JwtVerifier(
            JwksCache(fetcher=lambda: self.jwks), issuer=ISSUER, audience=AUDIENCE
        )
        self.fake_auth = FakeAuthClient(allowed={555})
        self.fake_gh = FakeGitHubApp(
            {555: [_repo(10, "octo/alpha"), _repo(11, "octo/beta", private=True)]}
        )
        # list_installation_repositories is module-level in installations; patch it.
        import gnsis.service.installations as inst_mod

        self._orig_list = inst_mod.list_installation_repositories
        inst_mod.list_installation_repositories = (
            lambda token: self.fake_gh.repos_by_installation.get(
                int(token.split("_")[-1]), []
            )
        )

        api.app.dependency_overrides[api.get_verifier] = lambda: verifier
        api.app.dependency_overrides[api.get_auth_client] = lambda: self.fake_auth
        api.app.dependency_overrides[api.get_github_app] = lambda: self.fake_gh
        self.client = TestClient(api.app)

    def tearDown(self):
        self.api.app.dependency_overrides.clear()
        import gnsis.service.installations as inst_mod

        inst_mod.list_installation_repositories = self._orig_list

    def auth(self, sub, **kw):
        return {"Authorization": f"Bearer {mint(self.priv, 'k1', sub, **kw)}"}


class MeAndClaimTests(ApiAuthTestBase):
    def test_me_requires_auth(self):
        self.assertEqual(self.client.get("/v1/me").status_code, 401)

    def test_me_autocreates_workspace(self):
        r = self.client.get("/v1/me", headers=self.auth("user-1", email="u@x.io"))
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["user"]["id"], "user-1")
        self.assertEqual(body["user"]["email"], "u@x.io")
        self.assertFalse(body["github"]["connected"])
        self.assertTrue(body["workspace"]["id"])

    def test_claim_verifies_ownership_and_syncs(self):
        r = self.client.post(
            "/v1/github/installations/claim",
            json={"installation_id": 555},
            headers=self.auth("user-1"),
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["installation"]["installation_id"], 555)
        self.assertEqual(len(body["repositories"]), 2)
        # /v1/me now reports the connection.
        me = self.client.get("/v1/me", headers=self.auth("user-1")).json()
        self.assertTrue(me["github"]["connected"])
        self.assertEqual(me["github"]["repository_count"], 2)

    def test_spoofed_installation_rejected(self):
        # 999 is not in the fake auth service's allowed set.
        r = self.client.post(
            "/v1/github/installations/claim",
            json={"installation_id": 999},
            headers=self.auth("attacker"),
        )
        self.assertEqual(r.status_code, 403)
        # And nothing was stored for that user.
        me = self.client.get("/v1/me", headers=self.auth("attacker")).json()
        self.assertFalse(me["github"]["connected"])


class RepositoryAndJobScopingTests(ApiAuthTestBase):
    def _claim_and_get_repo(self, sub="user-1"):
        self.client.post(
            "/v1/github/installations/claim",
            json={"installation_id": 555},
            headers=self.auth(sub),
        )
        repos = self.client.get("/v1/repositories", headers=self.auth(sub)).json()
        # Repos now sync as DISABLED by default; enable them so the runnable-repo
        # tests below can create jobs (the toggle route is exercised here too).
        for r in repos:
            self.client.patch(
                f"/v1/repositories/{r['id']}",
                json={"enabled": True},
                headers=self.auth(sub),
            )
        return self.client.get("/v1/repositories", headers=self.auth(sub)).json()

    def test_repositories_listed_after_claim(self):
        repos = self._claim_and_get_repo()
        self.assertEqual({r["full_name"] for r in repos}, {"octo/alpha", "octo/beta"})

    def test_create_run_with_repository_id(self):
        repos = self._claim_and_get_repo()
        repo_id = repos[0]["id"]
        r = self.client.post(
            "/jobs",
            json={"repository_id": repo_id, "instruction": "do a thing", "model": "openai/gpt-5.4", "advisor_model": "anthropic/claude-opus-4.8"},
            headers=self.auth("user-1"),
        )
        self.assertEqual(r.status_code, 200, r.text)
        job = r.json()
        self.assertIn(job["repo"], {"octo/alpha", "octo/beta"})

    def test_create_run_rejects_unknown_repository(self):
        r = self.client.post(
            "/jobs",
            json={"repository_id": "repo_does_not_exist", "instruction": "x"},
            headers=self.auth("user-1"),
        )
        self.assertEqual(r.status_code, 404)

    def test_create_run_rejects_cross_workspace_repository(self):
        # user-1 owns the repo; user-2 must not be able to run against its id.
        repos = self._claim_and_get_repo("user-1")
        repo_id = repos[0]["id"]
        r = self.client.post(
            "/jobs",
            json={"repository_id": repo_id, "instruction": "x", "model": "openai/gpt-5.4", "advisor_model": "anthropic/claude-opus-4.8"},
            headers=self.auth("user-2"),
        )
        self.assertEqual(r.status_code, 404)

    def test_repository_removed_from_github_access_returns_404(self):
        # A repository whose GitHub access was removed (``enabled=False`` after
        # a resync that no longer included it) is a historical row: it must
        # survive for past-job resolution but never accept new runs. From the
        # user's point of view the repo isn't in their catalog, so 404 is the
        # honest response — there is no user-controlled "disabled" toggle to
        # explain.
        repos = self._claim_and_get_repo("user-1")
        from gnsis.service import orm
        from gnsis.service.db import session_scope

        target = repos[0]["id"]
        with session_scope() as s:
            s.get(orm.Repository, target).enabled = False
        r = self.client.post(
            "/jobs",
            json={"repository_id": target, "instruction": "x"},
            headers=self.auth("user-1"),
        )
        self.assertEqual(r.status_code, 404)

    def test_cross_workspace_job_access_is_404(self):
        repos = self._claim_and_get_repo("user-1")
        repo_id = repos[0]["id"]
        job = self.client.post(
            "/jobs",
            json={"repository_id": repo_id, "instruction": "x", "model": "openai/gpt-5.4", "advisor_model": "anthropic/claude-opus-4.8"},
            headers=self.auth("user-1"),
        ).json()
        # user-2 cannot read user-1's job by id.
        r = self.client.get(f"/jobs/{job['id']}", headers=self.auth("user-2"))
        self.assertEqual(r.status_code, 404)

    def test_list_jobs_scoped_to_workspace(self):
        repos = self._claim_and_get_repo("user-1")
        self.client.post(
            "/jobs",
            json={"repository_id": repos[0]["id"], "instruction": "x", "model": "openai/gpt-5.4", "advisor_model": "anthropic/claude-opus-4.8"},
            headers=self.auth("user-1"),
        )
        mine = self.client.get("/jobs", headers=self.auth("user-1")).json()
        theirs = self.client.get("/jobs", headers=self.auth("user-2")).json()
        self.assertEqual(len(mine), 1)
        self.assertEqual(len(theirs), 0)

    def test_approve_another_users_job_rejected(self):
        repos = self._claim_and_get_repo("user-1")
        job = self.client.post(
            "/jobs",
            json={"repository_id": repos[0]["id"], "instruction": "x", "model": "openai/gpt-5.4", "advisor_model": "anthropic/claude-opus-4.8"},
            headers=self.auth("user-1"),
        ).json()
        # Force awaiting_approval with a validated execution run + matching diff,
        # so approve would otherwise be valid (and binds to the exact patch hash).
        from gnsis.orchestration.models import Diff
        from gnsis.service.executor.models import Budgets
        from gnsis.service.executor.store import ExecutionStore
        from gnsis.service.executor.validation import sha256_text
        from gnsis.service.repository import PostgresJobStore

        store = PostgresJobStore()
        patch = "diff --git a/x.txt b/x.txt\n--- a/x.txt\n+++ b/x.txt\n@@ -0,0 +1 @@\n+hi\n"
        store.save_diff(Diff(job["id"], patch, files_changed=["x.txt"]))
        exec_store = ExecutionStore()
        run = exec_store.create_run(
            job_id=job["id"], workspace_id=None, repository_id=None,
            base_branch="main", base_sha="a" * 40, dispatch_nonce_hash="n",
            executor_owner="o", executor_repository="r", executor_repository_id=1,
            executor_workflow="execute.yml", executor_ref="main", trusted_workflow_sha="s",
            budgets=Budgets(50, 500000, 100000, 3.0),
        )
        exec_store.set_patch_result(
            run.id, patch_sha256=sha256_text(patch), artifact_hashes={}, security_validation="passed"
        )
        store.set_status(job["id"], "awaiting_approval")
        r = self.client.post(
            f"/jobs/{job['id']}/approve", json={}, headers=self.auth("user-2")
        )
        self.assertEqual(r.status_code, 404)
        # The real owner can approve.
        ok = self.client.post(
            f"/jobs/{job['id']}/approve", json={}, headers=self.auth("user-1")
        )
        self.assertEqual(ok.status_code, 200, ok.text)


class FollowUpAndThreadTests(ApiAuthTestBase):
    """Conversational run threads: linked immutable runs + thread resolution."""

    def _claim_and_get_repo(self, sub="user-1"):
        self.client.post(
            "/v1/github/installations/claim",
            json={"installation_id": 555},
            headers=self.auth(sub),
        )
        repos = self.client.get("/v1/repositories", headers=self.auth(sub)).json()
        for r in repos:
            self.client.patch(
                f"/v1/repositories/{r['id']}",
                json={"enabled": True},
                headers=self.auth(sub),
            )
        return self.client.get("/v1/repositories", headers=self.auth(sub)).json()

    def _root(self, sub="user-1", advisor="anthropic/claude-opus-4.8", instruction="first"):
        repos = self._claim_and_get_repo(sub)
        body = {"repository_id": repos[0]["id"], "instruction": instruction, "model": "openai/gpt-5.4"}
        if advisor is not None:
            body["advisor_model"] = advisor
        r = self.client.post("/jobs", json=body, headers=self.auth(sub))
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_root_run_is_its_own_thread(self):
        root = self._root()
        # A first run roots its own thread and has no parent.
        self.assertEqual(root["thread_id"], root["id"])
        self.assertIsNone(root["parent_job_id"])

    def test_follow_up_is_a_new_linked_run_inheriting_config(self):
        root = self._root(advisor="anthropic/claude-opus-4.8")
        r = self.client.post(
            f"/jobs/{root['id']}/follow-up",
            json={"instruction": "second"},
            headers=self.auth("user-1"),
        )
        self.assertEqual(r.status_code, 200, r.text)
        fu = r.json()
        # New immutable run — distinct id, same thread, parent = the previous run.
        self.assertNotEqual(fu["id"], root["id"])
        self.assertEqual(fu["thread_id"], root["thread_id"])
        self.assertEqual(fu["parent_job_id"], root["id"])
        self.assertEqual(fu["status"], "queued")
        # Only the instruction is new; repo + models are resolved from the parent.
        self.assertEqual(fu["instruction"], "second")
        self.assertEqual(fu["repo"], root["repo"])
        self.assertEqual(fu["model"], root["model"])
        self.assertEqual(fu["advisor_model"], root["advisor_model"])

    def test_retry_run_again_reuses_parent_instruction(self):
        root = self._root(instruction="do the thing")
        # No instruction supplied — Retry (failed/cancelled) / Run-again (completed).
        r = self.client.post(
            f"/jobs/{root['id']}/follow-up", json={}, headers=self.auth("user-1")
        )
        self.assertEqual(r.status_code, 200, r.text)
        retry = r.json()
        self.assertEqual(retry["instruction"], "do the thing")
        self.assertEqual(retry["parent_job_id"], root["id"])
        self.assertEqual(retry["thread_id"], root["thread_id"])
        self.assertNotEqual(retry["id"], root["id"])

    def test_follow_up_preserves_absent_advisor(self):
        # A parent that pinned no Advisor yields a follow-up with no Advisor.
        root = self._root(advisor=None)
        self.assertIsNone(root["advisor_model"])
        fu = self.client.post(
            f"/jobs/{root['id']}/follow-up",
            json={"instruction": "more"},
            headers=self.auth("user-1"),
        ).json()
        self.assertIsNone(fu["advisor_model"])

    def test_follow_up_blank_instruction_rejected(self):
        root = self._root()
        r = self.client.post(
            f"/jobs/{root['id']}/follow-up",
            json={"instruction": "   "},
            headers=self.auth("user-1"),
        )
        self.assertEqual(r.status_code, 422, r.text)

    def test_follow_up_can_override_model_via_allowlist(self):
        root = self._root()  # model="openai/gpt-5.4"
        r = self.client.post(
            f"/jobs/{root['id']}/follow-up",
            json={"instruction": "switch models", "model": "anthropic/claude-opus-4.8"},
            headers=self.auth("user-1"),
        )
        self.assertEqual(r.status_code, 200, r.text)
        fu = r.json()
        self.assertEqual(fu["model"], "anthropic/claude-opus-4.8")
        self.assertEqual(fu["advisor_model"], root["advisor_model"])
        # The parent's own model is untouched.
        again = self.client.get(f"/jobs/{root['id']}", headers=self.auth("user-1")).json()
        self.assertEqual(again["model"], root["model"])

    def test_follow_up_rejects_model_outside_allowlist(self):
        root = self._root()
        r = self.client.post(
            f"/jobs/{root['id']}/follow-up",
            json={"instruction": "x", "model": "unknown/not-a-real-model"},
            headers=self.auth("user-1"),
        )
        self.assertEqual(r.status_code, 422, r.text)

    def test_follow_up_on_another_workspace_is_404(self):
        root = self._root("user-1")
        # user-2 must not be able to extend user-1's thread by its run id.
        r = self.client.post(
            f"/jobs/{root['id']}/follow-up",
            json={"instruction": "intrude"},
            headers=self.auth("user-2"),
        )
        self.assertEqual(r.status_code, 404)

    def test_follow_up_blocked_when_parent_repo_no_longer_accessible(self):
        root = self._root("user-1")
        # The parent's repository loses installation access (resynced as disabled).
        from gnsis.service import orm
        from gnsis.service.db import session_scope

        with session_scope() as s:
            s.query(orm.Repository).update({orm.Repository.enabled: False})
        r = self.client.post(
            f"/jobs/{root['id']}/follow-up",
            json={"instruction": "again"},
            headers=self.auth("user-1"),
        )
        self.assertEqual(r.status_code, 404)

    def test_thread_lists_all_runs_oldest_first(self):
        root = self._root("user-1")
        fu1 = self.client.post(
            f"/jobs/{root['id']}/follow-up",
            json={"instruction": "second"},
            headers=self.auth("user-1"),
        ).json()
        fu2 = self.client.post(
            f"/jobs/{fu1['id']}/follow-up",
            json={"instruction": "third"},
            headers=self.auth("user-1"),
        ).json()
        # A follow-up of a follow-up stays in the same thread, chained to fu1.
        self.assertEqual(fu2["thread_id"], root["thread_id"])
        self.assertEqual(fu2["parent_job_id"], fu1["id"])

        expected = [root["id"], fu1["id"], fu2["id"]]
        # Opening ANY run in the thread resolves the whole conversation.
        for opened in (root["id"], fu1["id"], fu2["id"]):
            thread = self.client.get(
                f"/jobs/{opened}/thread", headers=self.auth("user-1")
            ).json()
            self.assertEqual([j["id"] for j in thread], expected)

    def test_legacy_job_resolves_to_single_run_thread(self):
        root = self._root("user-1")
        # A run whose thread was never set (legacy row) is a single-run thread.
        from gnsis.service import orm
        from gnsis.service.db import session_scope

        with session_scope() as s:
            s.get(orm.Job, root["id"]).thread_id = None
        thread = self.client.get(
            f"/jobs/{root['id']}/thread", headers=self.auth("user-1")
        ).json()
        self.assertEqual([j["id"] for j in thread], [root["id"]])
        self.assertEqual(thread[0]["thread_id"], root["id"])

    def test_thread_of_another_workspace_is_404(self):
        root = self._root("user-1")
        r = self.client.get(f"/jobs/{root['id']}/thread", headers=self.auth("user-2"))
        self.assertEqual(r.status_code, 404)

    def test_follow_up_requires_execution_configured(self):
        root = self._root("user-1")
        from gnsis.service import settings as settings_mod

        saved = os.environ.pop("GNSIS_EXECUTION_PROVIDER", None)
        settings_mod._settings = None
        try:
            r = self.client.post(
                f"/jobs/{root['id']}/follow-up",
                json={"instruction": "x"},
                headers=self.auth("user-1"),
            )
            self.assertEqual(r.status_code, 503, r.text)
        finally:
            if saved is not None:
                os.environ["GNSIS_EXECUTION_PROVIDER"] = saved
            settings_mod._settings = None


if __name__ == "__main__":
    unittest.main()
