"""Phase 2 — pinning policy + memory at dispatch and reconstructing at fetch.

Exercises the durable contract without touching GitHub: the run record pins the
policy version + memory ids, ``build_run_spec`` reconstructs byte-identical,
deterministic, tenant-scoped context, native events are emitted, and a tampered
policy hash refuses to reconstruct.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from _authkit import fresh_sqlite_env  # noqa: E402


def _configure():
    fresh_sqlite_env()
    os.environ["GNSIS_PUBLIC_API_URL"] = "https://api.test"
    from gnsis.service import settings as sm

    sm._settings = None
    from gnsis.service.db import init_db

    init_db()


def _make_job(repo="o/r", workspace_id="ws-A", repository_id="repo-1"):
    from gnsis.orchestration.models import JobSpec
    from gnsis.service.repository import PostgresJobStore

    return PostgresJobStore().create_job(
        JobSpec(
            repo=repo,
            instruction="fix the authentication login flow",
            base_branch="main",
            engine="gnsis",
            workspace_id=workspace_id,
            repository_id=repository_id,
        )
    )


def _pin_run(job, policy, memory_selection):
    from gnsis.service.executor.models import Budgets
    from gnsis.service.executor.store import ExecutionStore

    store = ExecutionStore()
    run = store.create_run(
        job_id=job.id,
        workspace_id=job.workspace_id,
        repository_id=job.repository_id,
        base_branch="main",
        base_sha="a" * 40,
        dispatch_nonce_hash="h",
        executor_owner="ex",
        executor_repository="repo",
        executor_repository_id=1,
        executor_workflow="execute.yml",
        executor_ref="main",
        trusted_workflow_sha="t" * 40,
        budgets=Budgets(5, 1000, 1000, 1.0),
        policy_name=policy.name if policy else None,
        policy_version=policy.version if policy else None,
        policy_hash=policy.content_hash if policy else None,
        memory_ids=memory_selection.memory_ids if memory_selection else None,
    )
    return store, run


class PinAndReconstructTests(unittest.TestCase):
    def setUp(self):
        _configure()
        from gnsis.service import policy_store as ps
        from gnsis.service.codememory import CodeMemory
        from gnsis.service.settings import get_settings

        self.settings = get_settings()
        self.cm = CodeMemory()
        self.job = _make_job()
        self.cm.record_accepted_change(
            repo="o/r", source_job_id="seed", workspace_id="ws-A", repository_id="repo-1",
            content="Refactored the authentication login flow into auth/session.py.",
        )
        self.policy = ps.resolve_active_policy()
        self.selection = self.cm.retrieve_for_task(
            repo="o/r", instruction=self.job.instruction,
            workspace_id="ws-A", repository_id="repo-1", limit=6,
        )

    def test_run_record_pins_policy_and_memory(self):
        store, run = _pin_run(self.job, self.policy, self.selection)
        reloaded = store.get_run(run.id)
        self.assertEqual(reloaded.policy_name, self.policy.name)
        self.assertEqual(reloaded.policy_version, self.policy.version)
        self.assertEqual(reloaded.policy_hash, self.policy.content_hash)
        self.assertEqual(reloaded.memory_ids, self.selection.memory_ids)
        self.assertTrue(reloaded.memory_ids)  # something was selected

    def test_build_run_spec_reconstructs_policy_and_memory(self):
        from gnsis.service.executor.spec import build_run_spec

        _, run = _pin_run(self.job, self.policy, self.selection)
        spec = build_run_spec(self.settings, self.job, run)
        # Policy reconstructed exactly.
        self.assertIsNotNone(spec.policy)
        self.assertEqual(spec.policy["version"], self.policy.version)
        self.assertEqual(spec.policy["content"], self.policy.content)
        self.assertEqual(spec.policy["content_hash"], self.policy.content_hash)
        # Memory reconstructed, scoped, and shaped for the executor.
        self.assertTrue(spec.memory_context)
        first = spec.memory_context[0]
        self.assertEqual(set(first), {"memory_id", "kind", "content", "selection_reason"})
        # to_public_dict carries both, as a SEPARATE field (not in instruction).
        pub = spec.to_public_dict()
        self.assertIn("policy", pub)
        self.assertIn("memory_context", pub)
        self.assertNotIn("auth/session.py", pub["instruction"])

    def test_reconstruction_is_deterministic(self):
        from gnsis.service.executor.spec import build_run_spec

        _, run = _pin_run(self.job, self.policy, self.selection)
        a = build_run_spec(self.settings, self.job, run).to_public_dict()
        b = build_run_spec(self.settings, self.job, run).to_public_dict()
        self.assertEqual(a["policy"], b["policy"])
        self.assertEqual(a["memory_context"], b["memory_context"])

    def test_memory_is_tenant_scoped_on_reconstruction(self):
        from gnsis.service.executor.spec import build_run_spec

        # A run owned by another workspace pins the same ids → resolves to nothing.
        other_job = _make_job(workspace_id="ws-B", repository_id="repo-2")
        _, run = _pin_run(other_job, self.policy, self.selection)
        spec = build_run_spec(self.settings, other_job, run)
        self.assertEqual(spec.memory_context, [])

    def test_tampered_policy_hash_refuses_reconstruction(self):
        from gnsis.service.executor.spec import build_run_spec

        store, run = _pin_run(self.job, self.policy, self.selection)
        # Simulate a stored-policy tamper: pin a wrong hash for the version.
        from gnsis.service import orm
        from gnsis.service.db import session_scope

        with session_scope() as s:
            row = s.get(orm.ExecutionRun, run.id)
            row.policy_hash = "deadbeef"
        reloaded = store.get_run(run.id)
        spec = build_run_spec(self.settings, self.job, reloaded)
        self.assertIsNone(spec.policy)

    def test_native_events_are_emitted(self):
        from gnsis.service.executor.dispatch import _emit_context_events

        store, run = _pin_run(self.job, self.policy, self.selection)
        _emit_context_events(store, run, self.job, self.policy, self.selection)
        kinds = {e["kind"] for e in store.events_for(run.id)}
        self.assertIn("policy_pinned", kinds)
        self.assertIn("memory_selected", kinds)
        mem_event = next(e for e in store.events_for(run.id) if e["kind"] == "memory_selected")
        self.assertEqual(mem_event["payload"]["memory_ids"], self.selection.memory_ids)
        self.assertIn("truncated", mem_event["payload"])


if __name__ == "__main__":
    unittest.main()
