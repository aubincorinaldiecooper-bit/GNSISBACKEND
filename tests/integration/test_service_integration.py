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
    from fastapi.testclient import TestClient

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
    from gnsis.service.db import init_db
    from gnsis.service.repository import (
        PostgresJobStore,
        PostgresMemoryProvider,
        PostgresResourceStore,
    )


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
    @classmethod
    def setUpClass(cls):
        init_db()
        cls.client = TestClient(api_module.app)

    def test_create_then_drive_to_approval_and_approve(self):
        store = PostgresJobStore()
        # POST /jobs enqueues run_job; stub the queue so the test is hermetic.
        with mock.patch("gnsis.service.tasks.run_job.delay"):
            resp = self.client.post(
                "/jobs", json={"repo": "o/api", "instruction": "do it", "engine": "mock"}
            )
        self.assertEqual(resp.status_code, 200)
        job_id = resp.json()["id"]
        self.assertEqual(resp.json()["status"], "queued")

        # the worker would run this; do it inline for the test
        JobPipeline(store, MockEngine()).run(job_id)

        self.assertEqual(self.client.get(f"/jobs/{job_id}").json()["status"], "awaiting_approval")
        self.assertTrue(self.client.get(f"/jobs/{job_id}/logs").json())
        self.assertIn("patch", self.client.get(f"/jobs/{job_id}/diff").json())

        with mock.patch("gnsis.service.tasks.publish_pr.delay"):
            ap = self.client.post(f"/jobs/{job_id}/approve", json={"actor": "ci"})
        self.assertEqual(ap.status_code, 200)
        self.assertEqual(ap.json()["status"], "approved")

    def test_health(self):
        self.assertEqual(self.client.get("/health").json()["status"], "ok")


if __name__ == "__main__":
    unittest.main()
