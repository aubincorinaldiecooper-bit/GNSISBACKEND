"""Integration tests for the service layer against real Postgres + Redis.

These exercise the actual SQL implementations (PostgresJobStore,
PostgresMemoryProvider, PostgresResourceStore) and the FastAPI app — the parts
the offline unit tests can't cover. They run only when DATABASE_URL and REDIS_URL
are set (CI provides them via service containers); otherwise they are skipped.

This directory is intentionally not a package, so the offline
``unittest discover`` never imports it (and never trips over the heavy deps).
"""

import os
import unittest
from unittest import mock

RUN = bool(os.environ.get("DATABASE_URL")) and bool(os.environ.get("REDIS_URL"))

if RUN:  # imports require the `service` extra — only load when we'll run
    import json as _json
    import time as _time

    import jwt as _jwt
    from cryptography.hazmat.primitives.asymmetric import ec as _ec
    from fastapi.testclient import TestClient
    from jwt.algorithms import ECAlgorithm as _ECAlgorithm

    from gnsis.memory.base import MemoryRecord
    from gnsis.orchestration import (
        Approval,
        JobPipeline,
        JobSpec,
        JobStatus,
        MockEngine,
        PRMetadata,
        publish,
    )
    from gnsis.service import api as api_module
    from gnsis.service.auth import JwksCache, JwtVerifier
    from gnsis.service.auth_client import VerifiedInstallation
    from gnsis.service.db import init_db
    from gnsis.service.repository import (
        PostgresJobStore,
        PostgresMemoryProvider,
        PostgresResourceStore,
    )
    from gnsis.service.workspaces import (
        get_or_create_workspace,
        sync_repositories,
        upsert_installation,
    )

    _ISS = "https://auth.integration.test"
    _AUD = "gnsis-integration"


class _FakePublisher:
    def publish(self, job, diff):
        return PRMetadata(job_id=job.id, number=11, url="https://x/pr/11", branch=job.branch)


@unittest.skipUnless(RUN, "needs DATABASE_URL + REDIS_URL + service extra")
class PostgresFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def test_full_pipeline_and_memory_against_postgres(self):
        store = PostgresJobStore()
        memory = PostgresMemoryProvider()
        job = store.create_job(
            JobSpec(repo="o/int", instruction="add widget", engine="mock")
        )

        result = JobPipeline(store, MockEngine(), memory=memory).run(job.id)
        self.assertEqual(result.status, JobStatus.AWAITING_APPROVAL)

        # checkpoints + diff persisted in real SQL
        phases = [c.phase for c in store.get_checkpoints(job.id)]
        self.assertEqual(phases, ["plan", "patch", "tests", "summary"])
        self.assertIsNotNone(store.get_diff(job.id))

        store.save_approval(Approval(job_id=job.id, decision="approved", actor="ci"))
        store.set_status(job.id, JobStatus.APPROVED)
        pr = publish(store, _FakePublisher(), job.id, memory=memory)
        self.assertEqual(pr.number, 11)
        self.assertEqual(store.get_job(job.id).status, JobStatus.COMPLETED)

        # approval-gated memory write landed and is repo-scoped
        self.assertTrue(memory.recent("o/int"))
        self.assertEqual(memory.recent("o/other"), [])

    def test_memory_approval_gate_in_postgres(self):
        memory = PostgresMemoryProvider()
        self.assertIsNone(memory.write(MemoryRecord(repo="o/g", content="nope")))
        self.assertEqual(memory.recent("o/g"), [])
        memory.write(MemoryRecord(repo="o/g", content="yes", approved=True))
        self.assertTrue(any(r.content == "yes" for r in memory.recent("o/g")))

    def test_resource_store_lineage_in_postgres(self):
        rs = PostgresResourceStore()
        rs.commit("prompt", "p_int", "v1", message="seed")
        rs.commit("prompt", "p_int", "v2", message="evolve")
        hist = rs.history("prompt", "p_int")
        self.assertEqual([v.version for v in hist], [1, 2])
        self.assertEqual(rs.head("prompt", "p_int").content, "v2")


@unittest.skipUnless(RUN, "needs DATABASE_URL + REDIS_URL + service extra")
class ApiTests(unittest.TestCase):
    """Drives the user-facing HTTP contract (JWT + workspace + repository_id)."""

    @classmethod
    def setUpClass(cls):
        # Configure + inject a JWT verifier so the authed routes accept our
        # test tokens; seed a workspace/installation/repo the user owns.
        os.environ["BETTER_AUTH_JWKS_URL"] = "https://auth.integration.test/jwks"
        os.environ["BETTER_AUTH_ISSUER"] = _ISS
        os.environ["BETTER_AUTH_AUDIENCE"] = _AUD
        from gnsis.service import settings as settings_mod

        settings_mod._settings = None
        init_db()

        cls.priv = _ec.generate_private_key(_ec.SECP256R1())
        jwk = _json.loads(_ECAlgorithm.to_jwk(cls.priv.public_key()))
        jwk.update({"kid": "itk", "alg": "ES256", "use": "sig"})
        verifier = JwtVerifier(
            JwksCache(fetcher=lambda: {"keys": [jwk]}), issuer=_ISS, audience=_AUD
        )
        api_module.app.dependency_overrides[api_module.get_verifier] = lambda: verifier
        cls.client = TestClient(api_module.app)

        cls.ws = get_or_create_workspace("integration-user")
        inst = upsert_installation(
            cls.ws.id,
            VerifiedInstallation(
                installation_id=7001, account_id=1, account_login="o", account_type="User"
            ),
        )
        repos = sync_repositories(
            cls.ws.id,
            inst.id,
            [
                {
                    "id": 900,
                    "full_name": "o/api",
                    "name": "api",
                    "owner": {"login": "o"},
                    "default_branch": "main",
                    "private": True,
                    "archived": False,
                }
            ],
        )
        cls.repo_id = repos[0].id

    @classmethod
    def tearDownClass(cls):
        api_module.app.dependency_overrides.clear()

    def _hdr(self, sub="integration-user"):
        now = int(_time.time())
        tok = _jwt.encode(
            {"sub": sub, "iss": _ISS, "aud": _AUD, "iat": now, "exp": now + 900},
            self.priv,
            algorithm="ES256",
            headers={"kid": "itk"},
        )
        return {"Authorization": f"Bearer {tok}"}


    def _seed_validated_execution(self, job_id):
        from gnsis.service.executor.models import Budgets, ExecutionStatus
        from gnsis.service.executor.store import ExecutionStore
        from gnsis.service.executor.tokens import hash_secret
        from gnsis.service.executor.validation import sha256_text

        store = PostgresJobStore()
        job = store.get_job(job_id)
        diff = store.get_diff(job_id)
        exec_store = ExecutionStore()
        run = exec_store.create_run(
            job_id=job_id,
            workspace_id=job.workspace_id,
            repository_id=job.repository_id,
            base_branch=job.base_branch,
            base_sha="b" * 40,
            dispatch_nonce_hash=hash_secret("integration-nonce"),
            executor_owner="gnsis-test",
            executor_repository="executor-test",
            executor_repository_id=1,
            executor_workflow="execute.yml",
            executor_ref="main",
            trusted_workflow_sha="a" * 40,
            budgets=Budgets(3, 500000, 1000, 0.10),
        )
        exec_store.set_status(run.id, ExecutionStatus.COMPLETED)
        exec_store.set_patch_result(
            run.id,
            patch_sha256=sha256_text(diff.patch),
            artifact_hashes={},
            security_validation="integration-test",
        )

    def test_create_then_drive_to_approval_and_approve(self):
        store = PostgresJobStore()
        with mock.patch("gnsis.service.tasks.run_job.delay"):
            resp = self.client.post(
                "/jobs",
                json={"repository_id": self.repo_id, "instruction": "do it", "engine": "mock"},
                headers=self._hdr(),
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        job_id = resp.json()["id"]
        self.assertEqual(resp.json()["status"], "queued")
        self.assertEqual(resp.json()["repo"], "o/api")

        # the worker would run this; do it inline for the test
        JobPipeline(store, MockEngine()).run(job_id)

        self.assertEqual(
            self.client.get(f"/jobs/{job_id}", headers=self._hdr()).json()["status"],
            "awaiting_approval",
        )
        self.assertTrue(self.client.get(f"/jobs/{job_id}/logs", headers=self._hdr()).json())
        self.assertIn(
            "patch", self.client.get(f"/jobs/{job_id}/diff", headers=self._hdr()).json()
        )

        # A different user cannot see or approve this job.
        self.assertEqual(
            self.client.get(f"/jobs/{job_id}", headers=self._hdr("intruder")).status_code,
            404,
        )

        self._seed_validated_execution(job_id)
        with mock.patch("gnsis.service.tasks.publish_pr.delay"):
            ap = self.client.post(
                f"/jobs/{job_id}/approve", json={"note": "ci"}, headers=self._hdr()
            )
        self.assertEqual(ap.status_code, 200, ap.text)
        self.assertEqual(ap.json()["status"], "approved")

    def test_health(self):
        self.assertEqual(self.client.get("/health").json()["status"], "ok")

    def test_engines_lists_gnsis(self):
        ids = [e["id"] for e in self.client.get("/engines").json()]
        self.assertIn("gnsis", ids)
        self.assertIn("claude", ids)


    def test_missing_executor_configuration_is_fail_closed(self):
        from gnsis.service import settings as settings_mod

        keys = [
            "GNSIS_EXECUTION_PROVIDER",
            "GNSIS_PUBLIC_API_URL",
            "GNSIS_EXECUTOR_OWNER",
            "GNSIS_EXECUTOR_REPO",
            "GNSIS_EXECUTOR_WORKFLOW",
            "GNSIS_EXECUTOR_REF",
            "GNSIS_EXECUTOR_OIDC_ISSUER",
            "GNSIS_EXECUTOR_OIDC_AUDIENCE",
            "GNSIS_EXECUTOR_TRUSTED_WORKFLOW_SHA",
        ]
        saved = {key: os.environ.get(key) for key in keys}
        before_count = len(PostgresJobStore().list_jobs(limit=500))
        try:
            for key in keys:
                os.environ.pop(key, None)
            settings_mod._settings = None
            with mock.patch("gnsis.service.tasks.run_job.delay") as delay:
                resp = self.client.post(
                    "/jobs",
                    json={
                        "repository_id": self.repo_id,
                        "instruction": "do it",
                        "engine": "mock",
                    },
                    headers=self._hdr(),
                )
            self.assertEqual(resp.status_code, 503, resp.text)
            self.assertIn("public-beta execution is not configured", resp.text)
            delay.assert_not_called()
            after_count = len(PostgresJobStore().list_jobs(limit=500))
            self.assertEqual(after_count, before_count)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            settings_mod._settings = None

    def test_usage_is_returned_once_the_engine_reports_it(self):
        store = PostgresJobStore()
        with mock.patch("gnsis.service.tasks.run_job.delay"):
            resp = self.client.post(
                "/jobs",
                json={"repository_id": self.repo_id, "instruction": "do it", "engine": "usage-spy"},
                headers=self._hdr(),
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        job_id = resp.json()["id"]
        # fresh job: no usage reported yet
        self.assertEqual(
            self.client.get(f"/jobs/{job_id}", headers=self._hdr()).json()["usage"], {}
        )

        class _UsageEngine:
            name = "usage-spy"

            def generate(self, instruction, workspace, sink):
                from gnsis.orchestration import EngineResult, Phase

                sink.begin_phase(Phase.SUMMARY)
                sink.checkpoint(Phase.SUMMARY, "done")
                return EngineResult(
                    plan="p", patch="diff --git a/x b/x\n", tests="", summary="s",
                    files_changed=["x"], success=True,
                    detail={"engine": "usage-spy", "usage": {"total_tokens": 42}},
                )

        JobPipeline(store, _UsageEngine()).run(job_id)
        self.assertEqual(
            self.client.get(f"/jobs/{job_id}", headers=self._hdr()).json()["usage"],
            {"total_tokens": 42},
        )


if __name__ == "__main__":
    unittest.main()
