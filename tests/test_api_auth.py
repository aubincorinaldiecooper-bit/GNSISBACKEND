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
        return repos

    def test_repositories_listed_after_claim(self):
        repos = self._claim_and_get_repo()
        self.assertEqual({r["full_name"] for r in repos}, {"octo/alpha", "octo/beta"})

    def test_create_run_with_repository_id(self):
        repos = self._claim_and_get_repo()
        repo_id = repos[0]["id"]
        r = self.client.post(
            "/jobs",
            json={"repository_id": repo_id, "instruction": "do a thing"},
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
            json={"repository_id": repo_id, "instruction": "x"},
            headers=self.auth("user-2"),
        )
        self.assertEqual(r.status_code, 404)

    def test_disabled_repository_rejected(self):
        repos = self._claim_and_get_repo("user-1")
        # Disable one repo directly, then try to run against it.
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
        self.assertEqual(r.status_code, 409)

    def test_cross_workspace_job_access_is_404(self):
        repos = self._claim_and_get_repo("user-1")
        repo_id = repos[0]["id"]
        job = self.client.post(
            "/jobs",
            json={"repository_id": repo_id, "instruction": "x"},
            headers=self.auth("user-1"),
        ).json()
        # user-2 cannot read user-1's job by id.
        r = self.client.get(f"/jobs/{job['id']}", headers=self.auth("user-2"))
        self.assertEqual(r.status_code, 404)

    def test_list_jobs_scoped_to_workspace(self):
        repos = self._claim_and_get_repo("user-1")
        self.client.post(
            "/jobs",
            json={"repository_id": repos[0]["id"], "instruction": "x"},
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
            json={"repository_id": repos[0]["id"], "instruction": "x"},
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


if __name__ == "__main__":
    unittest.main()
