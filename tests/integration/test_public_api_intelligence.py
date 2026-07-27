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
            VerifiedInstallation(installation_id=int(uuid.uuid4().int % 1_000_000_000),
                                 account_id=2, account_login="int", account_type="User"),
        )
        cls._installation_id = inst.id
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

    def _fresh_repo(self):
        """A new, isolated repository under its OWN installation, in the
        shared workspace.

        Each test that asserts something like "exactly one active intelligence
        item" needs its own repository. Critically, ``sync_repositories``
        *reconciles* an installation's repo set — syncing a second repo list
        under the SAME installation record would mark every previously
        synced repo (including the class's own ``cls.repo_id``, and any
        earlier test's fresh repo) as no-longer-accessible. A fresh
        installation per call keeps each test's repo independent.
        """
        import uuid

        inst = upsert_installation(
            self.ws.id,
            VerifiedInstallation(installation_id=int(uuid.uuid4().int % 1_000_000_000),
                                 account_id=2, account_login="int", account_type="User"),
        )
        full_name = f"int/papi-{uuid.uuid4().hex[:8]}"
        repos = sync_repositories(
            self.ws.id, inst.id,
            [{"id": int(uuid.uuid4().int % 1_000_000_000), "full_name": full_name,
              "name": full_name.split("/")[1], "owner": {"login": "int"},
              "default_branch": "main", "private": True, "archived": False}],
        )
        repo = repos[0]
        from gnsis.service import orm
        from gnsis.service.db import session_scope

        with session_scope() as s:
            s.get(orm.Repository, repo.id).enabled = True
        return repo.id, full_name

    def _create(self, model, idem=None, repository_id=None, extra=None):
        headers = self.auth()
        if idem:
            headers["Idempotency-Key"] = idem
        body = {"repository_id": repository_id or self.repo_id,
                "instruction": "Harden authentication middleware", "model": model}
        body.update(extra or {})
        return self.client.post("/v1/runs", json=body, headers=headers)

    def _drive_to_gate(self, run_id, *, model, memory_ids=None, workspace_id=None,
                        repository_id=None, outcome_summary="Authentication middleware now uses the shared verifier"):
        store = PostgresJobStore()
        store.save_diff(Diff(run_id, PATCH, files_changed=["x.txt"]))
        run = ExecutionStore().create_run(
            job_id=run_id, workspace_id=workspace_id or self.ws.id,
            repository_id=repository_id or self.repo_id,
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
            outcome_summary=outcome_summary,
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
        """The definitive Model A -> approval -> Model B proof, on real Postgres.

        This is the single acceptance criterion: Run A (Model A) proposes,
        a reviewer approves exactly one item, it is stored with durable
        provenance, Run B (Model B) is authoritatively supplied it (never a
        client-supplied id), delivered separately from its instruction, and
        both receipts prove the complete source/approval/destination chain.
        """
        repo_id, repo_full = self._fresh_repo()
        run_a = self._create(MODEL_A, repository_id=repo_id).json()
        self._drive_to_gate(run_a["id"], model=MODEL_A, repository_id=repo_id)

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
            f"/v1/repositories/{repo_id}/intelligence", headers=self.auth()
        ).json()["data"]
        self.assertEqual(len(listed), 1, "exactly one active intelligence item must exist")
        item = listed[0]
        self.assertEqual(item["source_run_id"], run_a["id"])
        self.assertEqual(item["source_model"], MODEL_A)
        self.assertEqual(item["content"], proposal["content"], "final content matches the reviewer-approved text")
        self.assertTrue(item["approved_by"])
        self.assertTrue(item["approved_at"])
        intelligence_id = item["id"]

        prov = IntelligenceLifecycle().provenance_for_memory(intelligence_id)
        self.assertIsNotNone(prov)
        self.assertEqual(prov.outcome_decision, "approved")
        self.assertEqual(prov.source_job_id, run_a["id"])
        self.assertEqual(prov.source_model, MODEL_A)
        approval_id = prov.outcome_id

        # Isolation: a different repository (same workspace) never retrieves it.
        other_repo_id, other_repo_full = self._fresh_repo()
        other_repo_selection = CodeMemory().retrieve_for_task(
            repo=other_repo_full, instruction=run_a["instruction"],
            workspace_id=self.ws.id, repository_id=other_repo_id,
        )
        self.assertNotIn(intelligence_id, other_repo_selection.memory_ids)
        other_repo_listed = self.client.get(
            f"/v1/repositories/{other_repo_id}/intelligence", headers=self.auth()
        ).json()["data"]
        self.assertEqual(other_repo_listed, [])

        # Repeating approval must not duplicate the active item.
        repeat = self.client.post(
            f"/v1/runs/{run_a['id']}/approve",
            json={"note": "ship", "intelligence": [{"proposal_id": proposal["id"]}]},
            headers=self.auth(),
        )
        self.assertEqual(repeat.status_code, 200, repeat.text)
        listed_again = self.client.get(
            f"/v1/repositories/{repo_id}/intelligence", headers=self.auth()
        ).json()["data"]
        self.assertEqual(len(listed_again), 1, "repeating approval must not duplicate active intelligence")
        self.assertEqual(listed_again[0]["id"], intelligence_id)

        # A second run on a DIFFERENT model. The client cannot steer selection —
        # CreateRunRequest has no memory-id field; an extra one is simply ignored.
        run_b = self._create(MODEL_B, repository_id=repo_id, extra={"memory_ids": ["not-a-real-id"]}).json()
        selection = CodeMemory().retrieve_for_task(
            repo=repo_full, instruction=run_b["instruction"],
            workspace_id=self.ws.id, repository_id=repo_id,
        )
        self.assertIn(intelligence_id, selection.memory_ids)
        self.assertNotIn("not-a-real-id", selection.memory_ids)

        run_record_b = self._drive_to_gate(
            run_b["id"], model=MODEL_B, memory_ids=list(selection.memory_ids), repository_id=repo_id,
        )
        # Selected ids are pinned to Run B durably, before/at dispatch.
        self.assertIn(intelligence_id, run_record_b.memory_ids)
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

        supplied = receipt_b["intelligence"]["supplied"]
        self.assertEqual(len(supplied), 1)
        supplied_item = supplied[0]
        self.assertEqual(supplied_item["memory_id"], intelligence_id)
        self.assertTrue(supplied_item["selected"])
        # Truthful: selection alone is not delivery. No executor attestation
        # has been reported for Run B yet, so this must not be fabricated true.
        self.assertFalse(supplied_item["delivered"])
        self.assertNotIn("consumption_reported", supplied_item)
        self.assertEqual(supplied_item["source_run_id"], run_a["id"])
        self.assertEqual(supplied_item["source_model"], MODEL_A)
        self.assertEqual(supplied_item["approval_id"], approval_id)
        self.assertEqual(supplied_item["destination_run_id"], run_b["id"])
        self.assertEqual(supplied_item["destination_model"], MODEL_B)

        # Once the executor's own harness-authored attestation arrives (via
        # the same authenticated events callback real runs use), "delivered"
        # becomes truthfully true — never claiming semantic use, only that
        # the executor attached it to a real outbound model request.
        from gnsis.service.executor.callbacks import record_run_event

        record_run_event(self.settings, ExecutionStore(), run_record_b, {
            "run_id": run_record_b.workflow_run_id, "run_attempt": run_record_b.workflow_run_attempt,
            "sequence": 1, "idempotency_key": f"{run_record_b.id}:intel-delivered",
            "kind": "intelligence_context_delivered",
            "data": {"memory_ids": [intelligence_id, "not-a-real-pinned-id"],
                     "destination_model": MODEL_B, "delivery_state": "delivered",
                     "model_request_started": True},
        })
        receipt_b_after = self.client.get(
            f"/v1/runs/{run_b['id']}/receipt", headers=self.auth()
        ).json()
        supplied_after = receipt_b_after["intelligence"]["supplied"][0]
        self.assertTrue(supplied_after["delivered"])

        # Cross-model: produced under A, consumed under B.
        receipt_a = self.client.get(
            f"/v1/runs/{run_a['id']}/receipt", headers=self.auth()
        ).json()
        self.assertEqual(receipt_a["model"], MODEL_A)
        self.assertNotEqual(receipt_a["model"], receipt_b["model"])
        approved_from_a = receipt_a["intelligence"]["approved"]
        self.assertEqual(len(approved_from_a), 1)
        self.assertEqual(approved_from_a[0]["memory_id"], intelligence_id)
        self.assertEqual(approved_from_a[0]["source_model"], MODEL_A)

    def test_concurrent_approval_produces_one_active_intelligence_item_on_postgres(self):
        """Two truly concurrent approve requests for the same run must
        collapse to exactly one active intelligence item — proving the
        database-backed uniqueness (job_approvals.job_id), not merely an
        in-memory/application-level guard."""
        import threading

        repo_id, repo_full = self._fresh_repo()
        run = self._create(MODEL_A, repository_id=repo_id).json()
        self._drive_to_gate(run["id"], model=MODEL_A, repository_id=repo_id)
        proposal = self.client.get(
            f"/v1/runs/{run['id']}/intelligence-proposals", headers=self.auth()
        ).json()["data"][0]

        results = []

        def _approve():
            r = self.client.post(
                f"/v1/runs/{run['id']}/approve",
                json={"intelligence": [{"proposal_id": proposal["id"]}]},
                headers=self.auth(),
            )
            results.append(r)

        threads = [threading.Thread(target=_approve) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertTrue(all(r.status_code == 200 for r in results), [r.text for r in results])
        listed = self.client.get(
            f"/v1/repositories/{repo_id}/intelligence", headers=self.auth()
        ).json()["data"]
        self.assertEqual(len(listed), 1, "concurrent approval must not duplicate active intelligence")

        from gnsis.service import orm
        from gnsis.service.db import session_scope

        with session_scope() as s:
            approval_count = (
                s.query(orm.JobApproval).filter(orm.JobApproval.job_id == run["id"]).count()
            )
        self.assertEqual(approval_count, 1, "a job's decision is recorded exactly once, ever")

    def test_duplicate_memory_consumption_is_rejected_by_database_on_postgres(self):
        """Database-backed (not merely in-memory) protection against a
        duplicate consumption record for the same run+memory item."""
        from sqlalchemy.exc import IntegrityError

        from gnsis.service import orm
        from gnsis.service.db import session_scope

        repo_id, repo_full = self._fresh_repo()
        run = self._create(MODEL_A, repository_id=repo_id).json()
        run_record = self._drive_to_gate(run["id"], model=MODEL_A, repository_id=repo_id)

        with session_scope() as s:
            s.add(orm.MemoryConsumption(
                run_id=run_record.id, job_id=run["id"], memory_id="mem-dup-test",
                workspace_id=self.ws.id, repository_id=repo_id,
            ))
        with self.assertRaises(IntegrityError):
            with session_scope() as s:
                s.add(orm.MemoryConsumption(
                    run_id=run_record.id, job_id=run["id"], memory_id="mem-dup-test",
                    workspace_id=self.ws.id, repository_id=repo_id,
                ))

    def test_unapproved_and_rejected_proposals_are_never_retrieved_on_postgres(self):
        """Isolation: an unapproved proposal and a rejected run's proposal must
        never surface as active intelligence for later retrieval."""
        repo_id, repo_full = self._fresh_repo()

        # Never approved.
        pending = self._create(MODEL_A, repository_id=repo_id).json()
        self._drive_to_gate(
            pending["id"], model=MODEL_A, repository_id=repo_id,
            outcome_summary="Introduced a caching layer for tokens",
        )

        # Explicitly rejected.
        rejected = self._create(MODEL_A, repository_id=repo_id).json()
        self._drive_to_gate(
            rejected["id"], model=MODEL_A, repository_id=repo_id,
            outcome_summary="Rewrote the token refresh logic",
        )
        rej = self.client.post(
            f"/v1/runs/{rejected['id']}/reject", json={"note": "not this approach"}, headers=self.auth()
        )
        self.assertEqual(rej.status_code, 200, rej.text)

        listed = self.client.get(
            f"/v1/repositories/{repo_id}/intelligence", headers=self.auth()
        ).json()["data"]
        self.assertEqual(listed, [], "neither an unapproved nor a rejected run may supply active intelligence")

        selection = CodeMemory().retrieve_for_task(
            repo=repo_full, instruction="tokens", workspace_id=self.ws.id, repository_id=repo_id,
        )
        self.assertEqual(selection.items, [])

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
