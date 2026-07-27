"""The public ``/v1`` API-first surface, including the beta acceptance proof.

The headline test is :meth:`BetaAcceptanceTests.test_cross_model_intelligence_loop`,
which drives the complete product loop through the public HTTP API only:

    key -> repositories -> Run A (model A) -> events -> receipt -> approve
        -> provenance-backed intelligence -> Run B (model B)
        -> intelligence pinned + consumed -> Run B receipt proves it

Everything else here pins the authorization, idempotency, tenancy and truthful
receipt guarantees that make that loop trustworthy.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from _authkit import AUDIENCE, ISSUER, fresh_sqlite_env, make_keypair, mint  # noqa: E402

MODEL_A = "anthropic/claude-opus-4.8"
MODEL_B = "openai/gpt-5.4"

PATCH = (
    "diff --git a/x.txt b/x.txt\n--- a/x.txt\n+++ b/x.txt\n@@ -0,0 +1 @@\n+hi\n"
)


def _repo(repo_id, full_name):
    owner, name = full_name.split("/")
    return {
        "id": repo_id, "full_name": full_name, "name": name,
        "owner": {"login": owner}, "default_branch": "main",
        "private": False, "archived": False,
    }


class PublicApiTestBase(unittest.TestCase):
    """A workspace with one accessible repository and a fully-scoped API key."""

    def setUp(self):
        fresh_sqlite_env()
        os.environ["BETTER_AUTH_JWKS_URL"] = "https://auth.test/jwks"
        os.environ["BETTER_AUTH_ISSUER"] = ISSUER
        os.environ["BETTER_AUTH_AUDIENCE"] = AUDIENCE
        os.environ["GNSIS_AUTH_INTERNAL_URL"] = "https://auth.test"
        os.environ["GNSIS_AUTH_INTERNAL_SECRET"] = "internal-secret"
        os.environ["GITHUB_APP_ID"] = "12345"
        os.environ["GITHUB_APP_PRIVATE_KEY"] = "key"
        os.environ["GITHUB_APP_SLUG"] = "genesis"
        os.environ["GNSIS_EXECUTION_PROVIDER"] = "github_actions"
        os.environ["GNSIS_PUBLIC_API_URL"] = "https://api.gnsis.test"
        os.environ["GNSIS_EXECUTOR_OWNER"] = "gnsis"
        os.environ["GNSIS_EXECUTOR_REPO"] = "executor"
        os.environ["GNSIS_EXECUTOR_OIDC_AUDIENCE"] = "https://api.gnsis.studio"
        os.environ["GNSIS_EXECUTOR_TRUSTED_WORKFLOW_SHA"] = "0" * 40
        os.environ["GNSIS_RUN_ALLOWED_MODELS"] = f"{MODEL_A},{MODEL_B}"
        os.environ["GNSIS_MEMORY_BACKEND"] = "postgres"  # SQLite-backed here
        from gnsis.service import settings as settings_mod

        settings_mod._settings = None
        from gnsis.service.db import init_db

        init_db()

        import gnsis.service.tasks as tasks

        tasks.run_job.delay = lambda *a, **k: None
        tasks.publish_pr.delay = lambda *a, **k: None

        from fastapi.testclient import TestClient

        from gnsis.service import api
        from gnsis.service.auth import JwksCache, JwtVerifier
        from gnsis.service.auth_client import VerifiedInstallation
        from gnsis.service.workspaces import (
            get_or_create_workspace, sync_repositories, upsert_installation,
        )

        self.api = api
        self.priv, self.jwks = make_keypair("k1")
        verifier = JwtVerifier(JwksCache(fetcher=lambda: self.jwks), issuer=ISSUER, audience=AUDIENCE)
        api.app.dependency_overrides[api.get_verifier] = lambda: verifier
        self.client = TestClient(api.app)

        self.ws = get_or_create_workspace("owner-user")
        inst = upsert_installation(
            self.ws.id,
            VerifiedInstallation(installation_id=555, account_id=1,
                                 account_login="octo", account_type="User"),
        )
        repos = sync_repositories(self.ws.id, inst.id, [_repo(10, "octo/alpha")])
        self.repo_id = repos[0].id
        from gnsis.service import orm
        from gnsis.service.db import session_scope

        with session_scope() as s:
            s.get(orm.Repository, self.repo_id).enabled = True

        self.settings = settings_mod.get_settings()
        self.secret = self._mint_key()

    def tearDown(self):
        self.api.app.dependency_overrides.clear()

    def _mint_key(self, workspace_id=None, scopes=None):
        from gnsis.service.virtual_keys import VirtualKeyStore

        _, secret = VirtualKeyStore().create(
            self.settings,
            workspace_id=workspace_id or self.ws.id,
            name="test key",
            api_scopes=scopes,
        )
        return secret

    def auth(self, secret=None):
        return {"Authorization": f"Bearer {secret or self.secret}"}

    def create_run(self, model=MODEL_A, advisor=None, idem=None, secret=None, **over):
        body = {"repository_id": self.repo_id, "instruction": "Refactor authentication middleware",
                "model": model, **over}
        if advisor:
            body["advisor_model"] = advisor
        headers = self.auth(secret)
        if idem:
            headers["Idempotency-Key"] = idem
        return self.client.post("/v1/runs", json=body, headers=headers)

    def drive_to_awaiting_approval(self, run_id, *, model=MODEL_A, memory_ids=None,
                                   outcome_summary="Authentication middleware now uses the shared verifier"):
        """Seed the validated execution a real executor run would have produced."""
        from gnsis.orchestration.models import Diff
        from gnsis.service.executor.models import Budgets
        from gnsis.service.executor.store import ExecutionStore
        from gnsis.service.executor.validation import sha256_text
        from gnsis.service.repository import PostgresJobStore

        store = PostgresJobStore()
        store.save_diff(Diff(run_id, PATCH, files_changed=["x.txt"]))
        run = ExecutionStore().create_run(
            job_id=run_id, workspace_id=self.ws.id, repository_id=self.repo_id,
            base_branch="main", base_sha="a" * 40, dispatch_nonce_hash="n",
            executor_owner="gnsis", executor_repository="executor",
            executor_repository_id=1, executor_workflow="execute.yml",
            executor_ref="main", trusted_workflow_sha="s",
            budgets=Budgets(50, 500000, 100000, 3.0),
            primary_model=model, memory_ids=memory_ids or None,
        )
        ExecutionStore().set_patch_result(
            run.id, patch_sha256=sha256_text(PATCH), artifact_hashes={},
            security_validation="passed",
            outcome_summary=outcome_summary,
        )
        store.set_status(run_id, "awaiting_approval")
        return run


class AuthAndScopeTests(PublicApiTestBase):
    def test_api_key_can_create_a_run(self):
        r = self.create_run()
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["id"])
        self.assertEqual(body["object"], "run")
        self.assertEqual(body["model"], MODEL_A)
        self.assertEqual(body["status"], "queued")
        # The public object never leaks internal tenancy or credentials.
        self.assertNotIn("workspace_id", body)

    def test_missing_credential_is_a_stable_error_envelope(self):
        r = self.client.post("/v1/runs", json={})
        self.assertEqual(r.status_code, 401)
        err = r.json()["error"]
        self.assertEqual(err["code"], "authentication_failed")
        self.assertTrue(err["request_id"].startswith("req_"))

    def test_scopes_are_enforced(self):
        # A key without runs:create can read but not create.
        read_only = self._mint_key(scopes=["runs:read", "repositories:read"])
        r = self.create_run(secret=read_only)
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json()["error"]["code"], "authorization_failed")
        self.assertEqual(self.client.get("/v1/runs", headers=self.auth(read_only)).status_code, 200)

    def test_legacy_key_without_scopes_retains_full_beta_access(self):
        legacy = self._mint_key(scopes=None)
        self.assertEqual(self.create_run(secret=legacy).status_code, 200)

    def test_repository_access_is_workspace_scoped(self):
        from gnsis.service.workspaces import get_or_create_workspace

        other = get_or_create_workspace("intruder")
        intruder_key = self._mint_key(workspace_id=other.id)
        r = self.create_run(secret=intruder_key)
        self.assertEqual(r.status_code, 404, r.text)
        self.assertEqual(r.json()["error"]["code"], "repository_access_denied")

    def test_invalid_model_is_rejected(self):
        r = self.create_run(model="evil/not-allowed")
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "invalid_model")

    def test_advisor_stays_null_when_omitted(self):
        self.assertIsNone(self.create_run().json()["advisor_model"])
        self.assertEqual(self.create_run(advisor=MODEL_B).json()["advisor_model"], MODEL_B)

    def test_cross_workspace_run_read_is_404(self):
        from gnsis.service.workspaces import get_or_create_workspace

        run_id = self.create_run().json()["id"]
        other = get_or_create_workspace("intruder2")
        key = self._mint_key(workspace_id=other.id)
        self.assertEqual(self.client.get(f"/v1/runs/{run_id}", headers=self.auth(key)).status_code, 404)


class SessionAuthTests(PublicApiTestBase):
    """A signed-in dashboard session (Better Auth JWT, not a Genesis key) must
    reach the same /v1 routes a virtual key does — this is the reference
    client's authentication path, not a hypothetical.

    Regression coverage for the bug where the session branch treated the
    typed ``AuthedUser`` returned by ``JwtVerifier.verify()`` as a dict
    (``claims.get("sub")``), which raised an unhandled ``AttributeError`` (a
    500) on every dashboard-session call into ``/v1/*``. These tests mint a
    real, signed JWT and let it flow through the real (non-mocked)
    ``JwtVerifier`` — never a dict standing in for ``AuthedUser`` — so they
    fail the same way production would if the bug reappeared.
    """

    def _session_auth(self, subject="owner-user"):
        return {"Authorization": f"Bearer {mint(self.priv, 'k1', subject)}"}

    def test_session_can_create_and_read_a_run(self):
        # The session subject "owner-user" resolves (via get_or_create_workspace)
        # to the exact same workspace the virtual key in setUp belongs to.
        r = self.client.post(
            "/v1/runs",
            json={"repository_id": self.repo_id, "instruction": "Add a health check",
                  "model": MODEL_A},
            headers=self._session_auth(),
        )
        self.assertEqual(r.status_code, 200, r.text)
        run_id = r.json()["id"]

        # Run details.
        got = self.client.get(f"/v1/runs/{run_id}", headers=self._session_auth())
        self.assertEqual(got.status_code, 200, got.text)
        self.assertEqual(got.json()["id"], run_id)

        # Run events.
        events = self.client.get(f"/v1/runs/{run_id}/events", headers=self._session_auth())
        self.assertEqual(events.status_code, 200, events.text)
        self.assertIn("run.created", [e["type"] for e in events.json()["data"]])

        self.drive_to_awaiting_approval(run_id, model=MODEL_A)

        # Run receipt.
        receipt = self.client.get(f"/v1/runs/{run_id}/receipt", headers=self._session_auth())
        self.assertEqual(receipt.status_code, 200, receipt.text)
        self.assertEqual(receipt.json()["run_id"], run_id)

        # Intelligence proposals.
        proposals = self.client.get(
            f"/v1/runs/{run_id}/intelligence-proposals", headers=self._session_auth()
        )
        self.assertEqual(proposals.status_code, 200, proposals.text)

        # Repository intelligence.
        intel = self.client.get(
            f"/v1/repositories/{self.repo_id}/intelligence", headers=self._session_auth()
        )
        self.assertEqual(intel.status_code, 200, intel.text)
        self.assertEqual(intel.json()["object"], "list")

    def test_session_approve_activates_intelligence(self):
        """The full approval boundary works over a session, not just a key."""
        run_id = self.create_run().json()["id"]
        self.drive_to_awaiting_approval(run_id, outcome_summary="Added the health check route")
        proposal = self.client.get(
            f"/v1/runs/{run_id}/intelligence-proposals", headers=self._session_auth()
        ).json()["data"][0]
        approved = self.client.post(
            f"/v1/runs/{run_id}/approve",
            json={"intelligence": [{"proposal_id": proposal["id"]}]},
            headers=self._session_auth(),
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["status"], "approved")

    def test_a_different_workspaces_session_cannot_read_this_run(self):
        run_id = self.create_run().json()["id"]
        r = self.client.get(f"/v1/runs/{run_id}", headers=self._session_auth(subject="someone-else"))
        self.assertEqual(r.status_code, 404)

    def test_invalid_session_token_is_rejected_not_a_server_error(self):
        # Malformed token entirely.
        r = self.client.get("/v1/runs", headers={"Authorization": "Bearer not-a-jwt-at-all"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["error"]["code"], "authentication_failed")

        # Well-formed but signed by a key that isn't in the JWKS.
        other_priv, _ = make_keypair("other-key")
        forged = mint(other_priv, "other-key", "owner-user")
        r2 = self.client.get("/v1/runs", headers={"Authorization": f"Bearer {forged}"})
        self.assertEqual(r2.status_code, 401)
        self.assertEqual(r2.json()["error"]["code"], "authentication_failed")

        # Wrong audience.
        wrong_aud = mint(self.priv, "k1", "owner-user", audience="someone-elses-api")
        r3 = self.client.get("/v1/runs", headers={"Authorization": f"Bearer {wrong_aud}"})
        self.assertEqual(r3.status_code, 401)


class IdempotencyTests(PublicApiTestBase):
    def test_same_idempotency_key_returns_the_original_run(self):
        first = self.create_run(idem="abc-123")
        second = self.create_run(idem="abc-123")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["id"], second.json()["id"])

        from gnsis.service.repository import PostgresJobStore

        mine = [j for j in PostgresJobStore().list_jobs(limit=100) if j.workspace_id == self.ws.id]
        self.assertEqual(len(mine), 1, "a replayed request must not create a second run")

    def test_different_idempotency_keys_create_distinct_runs(self):
        a = self.create_run(idem="k1").json()["id"]
        b = self.create_run(idem="k2").json()["id"]
        self.assertNotEqual(a, b)

    def test_idempotency_key_is_workspace_scoped(self):
        """One tenant's key must never resolve another tenant's run."""
        from gnsis.service.repository import PostgresJobStore

        mine = self.create_run(idem="shared").json()["id"]
        other = PostgresJobStore().find_by_idempotency_key("ws_someone_else", "shared")
        self.assertIsNone(other)
        self.assertIsNotNone(PostgresJobStore().find_by_idempotency_key(self.ws.id, "shared"))
        self.assertTrue(mine)


class LifecycleAndReceiptTests(PublicApiTestBase):
    def test_events_include_pre_execution_activity(self):
        run_id = self.create_run().json()["id"]
        events = self.client.get(f"/v1/runs/{run_id}/events", headers=self.auth()).json()
        types = [e["type"] for e in events["data"]]
        self.assertIn("run.created", types)
        self.assertIn("run.queued", types)
        # Stable identity + ordering contract.
        self.assertTrue(all(e["id"] and e["run_id"] == run_id for e in events["data"]))
        self.assertEqual([e["sequence"] for e in events["data"]], list(range(len(events["data"]))))

    def test_blocked_run_reports_preflight_activity_and_truthful_zero_receipt(self):
        """A run stopped before execution still has activity and a real receipt."""
        from gnsis.service.executor.models import FailureCategory
        from gnsis.service.executor.preflight import record_blocked_run
        from gnsis.service.repository import PostgresJobStore

        run_id = self.create_run().json()["id"]
        store = PostgresJobStore()
        job = store.get_job(run_id)
        from gnsis.service.executor.store import ExecutionStore

        record_blocked_run(
            self.settings, ExecutionStore(), job,
            reason_code=FailureCategory.BLOCKED_REPOSITORY_EMPTY,
            provider_detail="GitHub GET .../git/ref/heads/main -> 409: Git Repository is empty.",
        )
        store.set_status(run_id, "blocked", error="GNSIS couldn't start this run…")

        events = self.client.get(f"/v1/runs/{run_id}/events", headers=self.auth()).json()["data"]
        types = [e["type"] for e in events]
        self.assertIn("run.dispatch_started", types)
        self.assertIn("executor.installation_lookup_started", types)
        self.assertIn("run.blocked", types)
        self.assertIn("receipt.ready", types)

        receipt = self.client.get(f"/v1/runs/{run_id}/receipt", headers=self.auth()).json()
        # Conclusively zero, never "not tracked yet"; and it says plainly that
        # execution never started, so a client can tell this apart from a
        # mid-execution failure.
        self.assertFalse(receipt["execution_started"])
        self.assertEqual(receipt["tests"], "not_run")
        self.assertEqual(receipt["model_calls"], 0)
        self.assertEqual(receipt["tokens"], {"input": 0, "output": 0, "cached": 0, "reasoning": 0})
        self.assertEqual(receipt["cost"]["provider_cost"], "0")
        self.assertEqual(receipt["cost"]["total_billed"], "0")
        self.assertEqual(receipt["files_changed"], [])
        self.assertEqual(receipt["failure_category"], FailureCategory.BLOCKED_REPOSITORY_EMPTY)
        self.assertTrue(receipt["failure_message"])

    def test_receipt_requires_receipts_scope(self):
        run_id = self.create_run().json()["id"]
        key = self._mint_key(scopes=["runs:read"])
        r = self.client.get(f"/v1/runs/{run_id}/receipt", headers=self.auth(key))
        self.assertEqual(r.status_code, 403)


class ApprovalTests(PublicApiTestBase):
    def test_reviewer_selects_edits_excludes_and_publish_is_separate(self):
        run_id = self.create_run(instruction="DO NOT STORE THIS TASK").json()["id"]
        self.drive_to_awaiting_approval(
            run_id, outcome_summary="- Shared verifier enforces issuer checks\n- Added replay protection"
        )
        proposals = self.client.get(
            f"/v1/runs/{run_id}/intelligence-proposals", headers=self.auth()
        ).json()["data"]
        self.assertEqual(len(proposals), 2)
        self.assertNotIn("DO NOT STORE", str(proposals))
        response = self.client.post(
            f"/v1/runs/{run_id}/approve",
            json={"intelligence": [
                {"proposal_id": proposals[0]["id"], "content": "Verifier must enforce issuer checks"},
                {"proposal_id": proposals[1]["id"], "selected": False},
            ]}, headers=self.auth(),
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "approved")
        active = self.client.get(
            f"/v1/repositories/{self.repo_id}/intelligence", headers=self.auth()
        ).json()["data"]
        self.assertEqual([x["content"] for x in active], ["Verifier must enforce issuer checks"])
        # Approval does not enqueue publication; publication remains compatible
        # through its own explicit route.
        import gnsis.service.tasks as tasks
        from unittest.mock import Mock
        tasks.publish_pr.delay = Mock()
        published = self.client.post(f"/v1/runs/{run_id}/publish", headers=self.auth())
        self.assertEqual(published.status_code, 200)
        tasks.publish_pr.delay.assert_called_once_with(run_id)

    def test_zero_proposals_and_zero_selection_approve(self):
        run_id = self.create_run().json()["id"]
        self.drive_to_awaiting_approval(run_id, outcome_summary=None)
        proposals = self.client.get(
            f"/v1/runs/{run_id}/intelligence-proposals", headers=self.auth()
        ).json()
        self.assertEqual(proposals["data"], [])
        approved = self.client.post(
            f"/v1/runs/{run_id}/approve", json={"intelligence": []}, headers=self.auth()
        )
        self.assertEqual(approved.status_code, 200)

    def test_approval_is_authorized_and_idempotent(self):
        run_id = self.create_run().json()["id"]
        self.drive_to_awaiting_approval(run_id)

        no_approve = self._mint_key(scopes=["runs:read"])
        denied = self.client.post(f"/v1/runs/{run_id}/approve", json={}, headers=self.auth(no_approve))
        self.assertEqual(denied.status_code, 403)

        first = self.client.post(f"/v1/runs/{run_id}/approve", json={"note": "ok"}, headers=self.auth())
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["status"], "approved")
        # Idempotent: a replayed approval returns the run, never re-decides.
        second = self.client.post(f"/v1/runs/{run_id}/approve", json={}, headers=self.auth())
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["status"], "approved")

    def test_approving_a_run_that_is_not_awaiting_is_a_conflict(self):
        run_id = self.create_run().json()["id"]
        r = self.client.post(f"/v1/runs/{run_id}/approve", json={}, headers=self.auth())
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "invalid_run_state")

    def test_rejected_run_produces_no_active_accepted_intelligence(self):
        run_id = self.create_run().json()["id"]
        self.drive_to_awaiting_approval(run_id)
        r = self.client.post(f"/v1/runs/{run_id}/reject", json={"note": "no"}, headers=self.auth())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "rejected")

        listed = self.client.get(
            f"/v1/repositories/{self.repo_id}/intelligence", headers=self.auth()
        ).json()["data"]
        self.assertFalse(
            [i for i in listed if i["type"] == "accepted_change"],
            "a rejected run must never yield accepted-change intelligence",
        )


class FollowUpTests(PublicApiTestBase):
    def test_follow_up_creates_a_new_linked_immutable_run(self):
        parent = self.create_run(advisor=MODEL_B).json()
        r = self.client.post(
            f"/v1/runs/{parent['id']}/follow-ups",
            json={"instruction": "now add tests"}, headers=self.auth(),
        )
        self.assertEqual(r.status_code, 200, r.text)
        child = r.json()
        self.assertNotEqual(child["id"], parent["id"])
        self.assertEqual(child["thread_id"], parent["thread_id"])
        self.assertEqual(child["parent_run_id"], parent["id"])
        self.assertEqual(child["instruction"], "now add tests")
        # Inherited authoritatively from the parent, not from the client.
        self.assertEqual(child["repository_id"], parent["repository_id"])
        self.assertEqual(child["model"], parent["model"])
        self.assertEqual(child["advisor_model"], parent["advisor_model"])
        self.assertEqual(child["branch"], parent["branch"])
        # The parent is untouched.
        again = self.client.get(f"/v1/runs/{parent['id']}", headers=self.auth()).json()
        self.assertEqual(again["status"], parent["status"])
        self.assertEqual(again["instruction"], parent["instruction"])

    def test_cross_workspace_follow_up_is_rejected(self):
        from gnsis.service.workspaces import get_or_create_workspace

        parent = self.create_run().json()
        other = get_or_create_workspace("intruder3")
        key = self._mint_key(workspace_id=other.id)
        r = self.client.post(
            f"/v1/runs/{parent['id']}/follow-ups", json={"instruction": "x"}, headers=self.auth(key)
        )
        self.assertEqual(r.status_code, 404)


class IntelligenceScopeTests(PublicApiTestBase):
    def test_intelligence_is_repository_and_workspace_scoped(self):
        from gnsis.service.workspaces import get_or_create_workspace

        other = get_or_create_workspace("intruder4")
        key = self._mint_key(workspace_id=other.id)
        r = self.client.get(f"/v1/repositories/{self.repo_id}/intelligence", headers=self.auth(key))
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "repository_access_denied")

    def test_query_enforces_scope_regardless_of_input(self):
        r = self.client.post(
            f"/v1/repositories/{self.repo_id}/intelligence/query",
            json={"task": "authentication middleware", "limit": 5}, headers=self.auth(),
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["object"], "list")
        self.assertIsInstance(body["data"], list)

    def test_intelligence_requires_scope(self):
        key = self._mint_key(scopes=["runs:read"])
        r = self.client.get(f"/v1/repositories/{self.repo_id}/intelligence", headers=self.auth(key))
        self.assertEqual(r.status_code, 403)


class BetaAcceptanceTests(PublicApiTestBase):
    """The primary completion criterion: the cross-model intelligence loop."""

    def test_cross_model_intelligence_loop(self):
        # 1-3. Authenticate with a scoped key and create Run A using Model A.
        run_a = self.create_run(model=MODEL_A).json()
        self.assertEqual(run_a["model"], MODEL_A)

        # 4. Observe lifecycle events.
        events_a = self.client.get(f"/v1/runs/{run_a['id']}/events", headers=self.auth()).json()["data"]
        self.assertIn("run.created", [e["type"] for e in events_a])

        # Drive to the approval gate the way a real executor completion would.
        self.drive_to_awaiting_approval(run_a["id"], model=MODEL_A)

        # 5. Retrieve Run A's receipt.
        receipt_a = self.client.get(f"/v1/runs/{run_a['id']}/receipt", headers=self.auth()).json()
        # The public contract is run-oriented: run_id is the run the client knows.
        self.assertEqual(receipt_a["run_id"], run_a["id"])
        self.assertTrue(receipt_a["execution_run_id"])
        self.assertEqual(receipt_a["model"], MODEL_A)
        self.assertTrue(receipt_a["execution_started"])

        # 6. Select the outcome-derived proposal and approve Run A.
        proposal = self.client.get(
            f"/v1/runs/{run_a['id']}/intelligence-proposals", headers=self.auth()
        ).json()["data"][0]
        approved = self.client.post(
            f"/v1/runs/{run_a['id']}/approve",
            json={"note": "looks right", "intelligence": [{"proposal_id": proposal["id"]}]},
            headers=self.auth()
        )
        self.assertEqual(approved.status_code, 200, approved.text)

        # 7. At least one provenance-backed intelligence item is now active.
        listed = self.client.get(
            f"/v1/repositories/{self.repo_id}/intelligence", headers=self.auth()
        ).json()["data"]
        self.assertTrue(listed, "approval must produce active repository intelligence")
        produced = listed[0]
        self.assertEqual(produced["source_run_id"], run_a["id"])
        self.assertEqual(produced["repository_id"], self.repo_id)
        self.assertEqual(produced["status"], "active")
        intelligence_id = produced["id"]

        # Provenance traces back to the run AND the approval that authorized it.
        from gnsis.service.intelligence_lifecycle import IntelligenceLifecycle

        prov = IntelligenceLifecycle().provenance_for_memory(intelligence_id)
        self.assertIsNotNone(prov, "intelligence must carry queryable provenance")
        self.assertEqual(prov.source_job_id, run_a["id"])
        self.assertEqual(prov.outcome_decision, "approved")
        self.assertIsNotNone(prov.outcome_id)

        # 8. Create Run B on the same repository with a DIFFERENT model.
        run_b = self.create_run(model=MODEL_B).json()
        self.assertEqual(run_b["model"], MODEL_B)
        self.assertNotEqual(run_b["model"], run_a["model"])

        # 9-10. The approved intelligence is selected and pinned to Run B, and is
        # what the executor would receive — supplied SEPARATELY from the
        # instruction, never concatenated into it.
        from gnsis.service.codememory import CodeMemory

        selection = CodeMemory().retrieve_for_task(
            repo="octo/alpha", instruction=run_b["instruction"],
            workspace_id=self.ws.id, repository_id=self.repo_id,
        )
        self.assertIn(intelligence_id, selection.memory_ids,
                      "approved intelligence must be selectable for a later run")

        run_record_b = self.drive_to_awaiting_approval(
            run_b["id"], model=MODEL_B, memory_ids=list(selection.memory_ids)
        )
        self.assertIn(intelligence_id, run_record_b.memory_ids)

        from gnsis.service.executor.spec import build_run_spec
        from gnsis.service.repository import PostgresJobStore

        job_b = PostgresJobStore().get_job(run_b["id"])
        spec = build_run_spec(self.settings, job_b, run_record_b)
        self.assertNotIn(intelligence_id, spec.instruction,
                         "intelligence must never be spliced into the instruction")
        self.assertIn(intelligence_id, [m["memory_id"] for m in spec.memory_context])
        self.assertEqual(spec.model, MODEL_B)

        # 11-13. Run B's receipt identifies the consumed intelligence, its source
        # Run A, its approval provenance, and the cross-model relationship.
        receipt_b = self.client.get(f"/v1/runs/{run_b['id']}/receipt", headers=self.auth()).json()
        self.assertIn(intelligence_id, receipt_b["memory_ids_consumed"])
        self.assertEqual(receipt_b["model"], MODEL_B)

        prov_b = IntelligenceLifecycle().provenance_for_memory(intelligence_id)
        self.assertEqual(prov_b.source_job_id, run_a["id"])
        # The model that PRODUCED it differs from the model that CONSUMED it.
        self.assertEqual(receipt_a["model"], MODEL_A)
        self.assertNotEqual(receipt_a["model"], receipt_b["model"])

        # Every run remains independently auditable.
        self.assertNotEqual(receipt_a["run_id"], receipt_b["run_id"])
        self.assertNotEqual(receipt_a["execution_run_id"], receipt_b["execution_run_id"])


if __name__ == "__main__":
    unittest.main()
