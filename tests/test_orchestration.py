"""Offline tests for the orchestration core (no heavy deps, no network).

These exercise the full job lifecycle through the in-memory store and the mock
engine: phases run in order, every phase is checkpointed, the job parks at the
approval gate, and publishing is refused until an approval is recorded.
"""

import unittest

from gnsis.memory import InMemoryMemoryProvider, MemoryRecord
from gnsis.orchestration import (
    APPROVAL_GATE,
    Approval,
    EngineResult,
    InMemoryJobStore,
    JobPipeline,
    JobSpec,
    JobStatus,
    MockEngine,
    Phase,
    PRMetadata,
    publish,
)


class FakePublisher:
    def __init__(self):
        self.calls = 0

    def publish(self, job, diff):
        self.calls += 1
        return PRMetadata(job_id=job.id, number=7, url="https://example/pr/7", branch=job.branch)


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryJobStore()
        self.pipeline = JobPipeline(self.store, MockEngine())
        self.job = self.store.create_job(
            JobSpec(repo="o/r", instruction="add a thing", base_branch="main", engine="mock")
        )

    def test_pipeline_runs_phases_and_parks_at_approval(self):
        result = self.pipeline.run(self.job.id, workspace=None)
        self.assertEqual(result.status, APPROVAL_GATE)

        job = self.store.get_job(self.job.id)
        self.assertEqual(job.status, JobStatus.AWAITING_APPROVAL)

        phases = [c.phase for c in self.store.get_checkpoints(self.job.id)]
        self.assertEqual(phases, list(Phase.ORDER))

        diff = self.store.get_diff(self.job.id)
        self.assertIsNotNone(diff)
        self.assertIn("GNSIS_CHANGE.md", diff.patch)

    def test_default_branch_assigned(self):
        self.assertTrue(self.job.branch.startswith("gnsis/"))

    def test_publish_refused_without_approval(self):
        self.pipeline.run(self.job.id, workspace=None)
        publisher = FakePublisher()
        with self.assertRaises(PermissionError):
            publish(self.store, publisher, self.job.id)
        self.assertEqual(publisher.calls, 0)

    def test_publish_after_approval_opens_pr_and_completes(self):
        self.pipeline.run(self.job.id, workspace=None)
        self.store.save_approval(
            Approval(job_id=self.job.id, decision="approved", actor="me")
        )
        self.store.set_status(self.job.id, JobStatus.APPROVED)

        publisher = FakePublisher()
        pr = publish(self.store, publisher, self.job.id)

        self.assertEqual(pr.number, 7)
        self.assertEqual(publisher.calls, 1)
        self.assertEqual(self.store.get_job(self.job.id).status, JobStatus.COMPLETED)
        self.assertIsNotNone(self.store.get_pr_metadata(self.job.id))

    def test_engine_failure_marks_job_failed(self):
        class BoomEngine:
            name = "boom"

            def generate(self, instruction, workspace, sink):
                raise RuntimeError("kaboom")

        pipeline = JobPipeline(self.store, BoomEngine())
        result = pipeline.run(self.job.id, workspace=None)
        self.assertEqual(result.status, JobStatus.FAILED)
        self.assertEqual(self.store.get_job(self.job.id).error, "kaboom")


class SpyEngine:
    """Captures the instruction it receives and returns a fixed summary."""

    name = "spy"

    def __init__(self):
        self.seen_instruction = None

    def generate(self, instruction, workspace, sink):
        self.seen_instruction = instruction
        sink.begin_phase(Phase.SUMMARY)
        sink.checkpoint(Phase.SUMMARY, "did the thing")
        return EngineResult(
            plan="p", patch="diff --git a/x b/x\n", tests="", summary="did the thing",
            files_changed=["x"], success=True,
        )


class MemoryWiringTests(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryJobStore()
        self.memory = InMemoryMemoryProvider()
        self.job = self.store.create_job(
            JobSpec(repo="o/r", instruction="add feature Z", engine="spy")
        )

    def test_repo_memory_is_injected_into_instruction(self):
        self.memory.write(
            MemoryRecord(repo="o/r", content="always use tabs", approved=True)
        )
        engine = SpyEngine()
        JobPipeline(self.store, engine, memory=self.memory).run(self.job.id)
        self.assertIn("always use tabs", engine.seen_instruction)
        self.assertIn("add feature Z", engine.seen_instruction)

    def test_other_repos_memory_is_not_injected(self):
        self.memory.write(
            MemoryRecord(repo="other/repo", content="secret convention", approved=True)
        )
        engine = SpyEngine()
        JobPipeline(self.store, engine, memory=self.memory).run(self.job.id)
        self.assertNotIn("secret convention", engine.seen_instruction)
        self.assertEqual(engine.seen_instruction, "add feature Z")

    def test_approved_publish_writes_memory(self):
        JobPipeline(self.store, SpyEngine(), memory=self.memory).run(self.job.id)
        self.store.save_approval(
            Approval(job_id=self.job.id, decision="approved", actor="me")
        )
        self.store.set_status(self.job.id, JobStatus.APPROVED)
        publish(self.store, FakePublisher(), self.job.id, memory=self.memory)

        remembered = self.memory.recent("o/r")
        self.assertEqual(len(remembered), 1)
        self.assertEqual(remembered[0].content, "did the thing")
        self.assertEqual(remembered[0].kind, "accepted_change")


if __name__ == "__main__":
    unittest.main()
