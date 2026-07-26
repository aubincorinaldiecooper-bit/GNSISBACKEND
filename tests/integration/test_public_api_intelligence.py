"""The public API's cross-model intelligence loop, against real Postgres + Redis.

Mirrors the SQLite-backed acceptance test in ``tests/test_public_api.py`` but
exercises the real SQL implementations — the JSON columns, the partial unique
idempotency index, the tenant-scoped memory reads, and the provenance joins that
only behave authentically on PostgreSQL.
"""

import os
import unittest

RUN = bool(os.environ.get("DATABASE_URL")) and bool(os.environ.get("REDIS_URL"))

if RUN:
    from fastapi.testclient import TestClient

    from gnsis.orchestration.models import Diff
    from gnsis.service import api as api_module
    from gnsis.service.auth_client import VerifiedInstallation
    from gnsis.service.codememory import CodeMemory
    from gnsis.service.db import init_db
    from gnsis.service.executor.models import Budgets
    from gnsis.service.executor.spec import build_run_spec
    from gnsis.service.executor.store import ExecutionStore
    from gnsis.service.executor.validation import sha256_text
    from gnsis.service.intelligence_lifecycle import IntelligenceLifecycle
    from gnsis.service.repository import PostgresJobStore
    from gnsis.service.settings import get_settings
    from gnsis.service.virtual_keys import VirtualKeyStore
    from gnsis.service.workspaces import (
        get_or_create_workspace, sync_repositories, upsert_installation,
    )

MODEL_A = "anthropic/claude-opus-4.8"
MODEL_B = "openai/gpt-5.4"
PATCH = "diff --git a/x.txt b/x.txt\n--- a/x.txt\n+++ b/x.txt\n@@ -0,0 +1 @@\n+hi\n"


@unittest.skipUnless(RUN, "needs DATABASE_URL + REDIS_URL + service extra")
class PublicApiIntelligenceIntegrationTests(unittest.TestCase):
    """The full loop on real Postgres: create -> approve -> reuse cross-model."""

    @classmethod
    def setUpClass(cls):
        os.environ["GNSIS_RUN_ALLOWED_MODELS"] = f"{MODEL_A},{MODEL_B}"
        os.environ["GNSIS_MEMORY_BACKEND"] = "postgres"
        from gnsis.service import settings as settings_mod

        settings_mod._settings = None
        init_db()
        cls.settings = get_settings()
        cls.client = TestClient(api_module.app)

        import gnsis.service.tasks as tasks

        tasks.run_job.delay = lambda *a, **k: None
        tasks.publish_pr.delay = lambda *a, **k: None

        # A distinct workspace per run of this suite keeps it re-runnable.
        import uuid

        cls.ws = get_or_create_workspace(f"papi-int-{uuid.uuid4().hex[:8]}")
        inst = upsert_installation(
            cls.ws.id,
            VerifiedInstallation(installation_id=8100, account_id=2,
                                 account_login="int", account_type="User"),
        )
        cls.repo_full = f"int/papi-{uuid.uuid4().hex[:6]}"
        repos = sync_repositories(
            cls.ws.id, inst.id,
            [{"id": 9100, "full_name": cls.repo_full, "name": cls.repo_full.split("/")[1],
              "owner": {"login": "int"}, "default_branch": "main",
              "private": True, "archived": False}],
        )
        cls.repo_id = repos[0].id
        from gnsis.service import orm
        from gnsis.service.db import session_scope

        with session_scope() as s:
            s.get(orm.Repository, cls.repo_id).enabled = True

        _, cls.secret = VirtualKeyStore().create(
            cls.settings, workspace_id=cls.ws.id, name="integration key"
        )

    def auth(self):
        return {"Authorization": f"Bearer {self.secret}"}

    def _create(self, model, idem=None):
        headers = self.auth()
        if idem:
            headers["Idempotency-Key"] = idem
        return self.client.post(
            "/v1/runs",
            json={"repository_id": self.repo_id, "instruction": "Harden authentication middleware",
                  "model": model},
            headers=headers,
        )

    def _drive_to_gate(self, run_id, *, model, memory_ids=None):
        store = PostgresJobStore()
        store.save_diff(Diff(run_id, PATCH, files_changed=["x.txt"]))
        run = ExecutionStore().create_run(
            job_id=run_id, workspace_id=self.ws.id, repository_id=self.repo_id,
            base_branch="main", base_sha="a" * 40, dispatch_nonce_hash=f"n-{run_id}",
            executor_owner="gnsis", executor_repository="executor",
            executor_repository_id=1, executor_workflow="execute.yml",
            executor_ref="main", trusted_workflow_sha="s",
            budgets=Budgets(50, 500000, 100000, 3.0),
            primary_model=model, memory_ids=memory_ids or None,
        )
        ExecutionStore().set_patch_result(
            run.id, patch_sha256=sha256_text(PATCH), artifact_hashes={},
            security_validation="passed",
            outcome_summary="Authentication middleware now uses the shared verifier",
        )
        store.set_status(run_id, "awaiting_approval")
        return run

    def test_idempotent_create_on_postgres(self):
        """The partial unique index makes a replayed create a true no-op."""
        key = "int-idem-1"
        first = self._create(MODEL_A, idem=key)
        second = self._create(MODEL_A, idem=key)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["id"], second.json()["id"])

    def test_cross_model_intelligence_loop_on_postgres(self):
        run_a = self._create(MODEL_A).json()
        self._drive_to_gate(run_a["id"], model=MODEL_A)

        proposal = self.client.get(
            f"/v1/runs/{run_a['id']}/intelligence-proposals", headers=self.auth()
        ).json()["data"][0]
        approved = self.client.post(
            f"/v1/runs/{run_a['id']}/approve",
            json={"note": "ship", "intelligence": [{"proposal_id": proposal["id"]}]},
            headers=self.auth(),
        )
        self.assertEqual(approved.status_code, 200, approved.text)

        listed = self.client.get(
            f"/v1/repositories/{self.repo_id}/intelligence", headers=self.auth()
        ).json()["data"]
        self.assertTrue(listed, "approval must produce active intelligence on Postgres")
        item = listed[0]
        self.assertEqual(item["source_run_id"], run_a["id"])
        intelligence_id = item["id"]

        prov = IntelligenceLifecycle().provenance_for_memory(intelligence_id)
        self.assertIsNotNone(prov)
        self.assertEqual(prov.outcome_decision, "approved")
        self.assertEqual(prov.source_job_id, run_a["id"])

        # A second run on a DIFFERENT model selects and pins that intelligence.
        run_b = self._create(MODEL_B).json()
        selection = CodeMemory().retrieve_for_task(
            repo=self.repo_full, instruction=run_b["instruction"],
            workspace_id=self.ws.id, repository_id=self.repo_id,
        )
        self.assertIn(intelligence_id, selection.memory_ids)

        run_record_b = self._drive_to_gate(
            run_b["id"], model=MODEL_B, memory_ids=list(selection.memory_ids)
        )
        job_b = PostgresJobStore().get_job(run_b["id"])
        spec = build_run_spec(self.settings, job_b, run_record_b)
        # Delivered as a separate field, never spliced into the instruction.
        self.assertIn(intelligence_id, [m["memory_id"] for m in spec.memory_context])
        self.assertNotIn(intelligence_id, spec.instruction)
        self.assertEqual(spec.model, MODEL_B)

        receipt_b = self.client.get(
            f"/v1/runs/{run_b['id']}/receipt", headers=self.auth()
        ).json()
        self.assertIn(intelligence_id, receipt_b["memory_ids_consumed"])
        self.assertEqual(receipt_b["model"], MODEL_B)
        self.assertEqual(receipt_b["run_id"], run_b["id"])

        # Cross-model: produced under A, consumed under B.
        receipt_a = self.client.get(
            f"/v1/runs/{run_a['id']}/receipt", headers=self.auth()
        ).json()
        self.assertEqual(receipt_a["model"], MODEL_A)
        self.assertNotEqual(receipt_a["model"], receipt_b["model"])

    def test_cross_workspace_intelligence_is_rejected_on_postgres(self):
        other = get_or_create_workspace("papi-int-intruder")
        _, secret = VirtualKeyStore().create(
            self.settings, workspace_id=other.id, name="intruder"
        )
        r = self.client.get(
            f"/v1/repositories/{self.repo_id}/intelligence",
            headers={"Authorization": f"Bearer {secret}"},
        )
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "repository_access_denied")


if __name__ == "__main__":
    unittest.main()
