import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from gnsis.jobs import (  # noqa: E402
    FileJobStore,
    InvalidTransition,
    Job,
    JobState,
    can_transition,
    is_terminal,
    transition,
)


class StateMachineTests(unittest.TestCase):
    def test_happy_path_is_legal(self):
        path = [
            JobState.QUEUED, JobState.PLANNING, JobState.PATCHING, JobState.TESTING,
            JobState.SUMMARIZING, JobState.AWAITING_APPROVAL, JobState.APPROVED,
            JobState.PUBLISHING, JobState.COMPLETED,
        ]
        for current, target in zip(path, path[1:]):
            self.assertTrue(can_transition(current, target), f"{current}->{target}")

    def test_cannot_skip_phases(self):
        job = Job(state=JobState.QUEUED)
        with self.assertRaises(InvalidTransition):
            transition(job, JobState.PUBLISHING)

    def test_approval_gate_blocks_unapproved_publish(self):
        # The only way out of awaiting_approval is approved or rejected.
        self.assertFalse(can_transition(JobState.AWAITING_APPROVAL, JobState.PUBLISHING))
        self.assertTrue(can_transition(JobState.AWAITING_APPROVAL, JobState.APPROVED))
        self.assertTrue(can_transition(JobState.AWAITING_APPROVAL, JobState.REJECTED))
        self.assertTrue(can_transition(JobState.APPROVED, JobState.PUBLISHING))

    def test_terminal_states(self):
        for state in (JobState.COMPLETED, JobState.FAILED, JobState.REJECTED):
            self.assertTrue(is_terminal(state))
        self.assertFalse(is_terminal(JobState.QUEUED))

    def test_transition_mutates_and_touches(self):
        job = Job(state=JobState.QUEUED)
        before = job.updated_at
        transition(job, JobState.PLANNING)
        self.assertEqual(job.state, JobState.PLANNING)
        self.assertGreaterEqual(job.updated_at, before)


class FileJobStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = FileJobStore(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_create_get_round_trip(self):
        job = Job(repo="owner/name", task="add a feature")
        job.log("planning started", phase="plan")
        job.checkpoint("plan", {"steps": ["a", "b"]})
        self.store.create(job)

        loaded = self.store.get(job.id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.repo, "owner/name")
        self.assertEqual(loaded.artifact("plan"), {"steps": ["a", "b"]})
        self.assertEqual(len(loaded.logs), 1)

    def test_create_rejects_duplicates(self):
        job = Job()
        self.store.create(job)
        with self.assertRaises(ValueError):
            self.store.create(job)

    def test_save_persists_state(self):
        job = Job()
        self.store.create(job)
        transition(job, JobState.PLANNING)
        self.store.save(job)
        self.assertEqual(self.store.get(job.id).state, JobState.PLANNING)

    def test_list_sorted(self):
        a = Job(created_at="2026-01-01T00:00:00+00:00")
        b = Job(created_at="2026-02-01T00:00:00+00:00")
        self.store.create(b)
        self.store.create(a)
        ids = [j.id for j in self.store.list()]
        self.assertEqual(ids, [a.id, b.id])


if __name__ == "__main__":
    unittest.main()
