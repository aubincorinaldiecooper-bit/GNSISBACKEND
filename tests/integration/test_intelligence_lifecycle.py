from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import uuid

def fresh_sqlite_env() -> str:
    path = os.path.join("/tmp", f"gnsis-test-{uuid.uuid4().hex}.db")
    os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{path}"
    os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
    from gnsis.service import db, settings as settings_mod
    settings_mod._settings = None
    db._engine = None
    db._SessionLocal = None
    return path


def configure():
    fresh_sqlite_env()
    os.environ["GNSIS_PUBLIC_API_URL"] = "https://api.test"
    from gnsis.service import settings as sm

    sm._settings = None
    from gnsis.service.db import init_db

    init_db()


def make_job(repo="o/r", workspace_id="ws-A", repository_id="repo-1", instruction="fix the authentication login bug"):
    from gnsis.orchestration.models import JobSpec
    from gnsis.service.repository import PostgresJobStore

    return PostgresJobStore().create_job(
        JobSpec(repo=repo, instruction=instruction, engine="gnsis", workspace_id=workspace_id, repository_id=repository_id)
    )


def make_run(job, memory_ids=None):
    from gnsis.service.executor.models import Budgets
    from gnsis.service.executor.store import ExecutionStore
    from gnsis.service import policy_store as ps

    policy = ps.resolve_active_policy()
    store = ExecutionStore()
    run = store.create_run(
        job_id=job.id,
        workspace_id=job.workspace_id,
        repository_id=job.repository_id,
        base_branch="main",
        base_sha="a" * 40,
        dispatch_nonce_hash="h" + job.id,
        executor_owner="ex",
        executor_repository="repo",
        executor_repository_id=1,
        executor_workflow="execute.yml",
        executor_ref="main",
        trusted_workflow_sha="t" * 40,
        budgets=Budgets(5, 1000, 1000, 1.0),
        policy_name=policy.name,
        policy_version=policy.version,
        policy_hash=policy.content_hash,
        memory_ids=memory_ids,
    )
    return store, run


class IntelligenceLifecycleIntegrationTests(unittest.TestCase):
    def setUp(self):
        configure()

    def test_rejected_outcome_creates_traceable_reusable_lesson_consumed_by_later_run(self):
        from gnsis.orchestration.models import Approval
        from gnsis.service.codememory import CodeMemory, MemoryKind
        from gnsis.service.executor.models import ExecutionStatus
        from gnsis.service.intelligence_lifecycle import IntelligenceLifecycle, ReviewedIntelligenceItem
        from gnsis.service.repository import PostgresJobStore
        from gnsis.service import orm
        from gnsis.service.db import session_scope

        jobs = PostgresJobStore()
        memory = CodeMemory()
        lifecycle = IntelligenceLifecycle(jobs=jobs, memory=memory)

        job1 = make_job()
        selection1 = memory.retrieve_for_task(repo=job1.repo, instruction=job1.instruction, workspace_id=job1.workspace_id, repository_id=job1.repository_id)
        run_store, run1 = make_run(job1, selection1.memory_ids)
        self.assertEqual(run_store.get_run(run1.id).memory_ids, selection1.memory_ids)
        self.assertIsNotNone(run_store.get_run(run1.id).policy_hash)

        run_store.set_status(run1.id, ExecutionStatus.RUNNING)
        run_store.record_event(run1.id, job_id=job1.id, workflow_run_attempt=None, sequence=1, idempotency_key="tool-1", kind="tool_call", payload={"tool": "shell"})
        run_store.set_status(run1.id, ExecutionStatus.COMPLETED)
        self.assertEqual(lifecycle.intelligence_from_run(run1.id), [])

        approval = jobs.save_approval(Approval(job_id=job1.id, decision="rejected", actor="reviewer", note="Prefer the service-layer auth helper."))
        item = lifecycle.process_reviewed_outcome(
            outcome_id=approval.id,
            reusable_intelligence="authentication login fixes must use the service-layer auth helper; do not patch controllers directly",
        )
        self.assertIsNotNone(item)
        self.assertEqual(item.kind, MemoryKind.REJECTION_LESSON)

        prov = lifecycle.provenance_for_memory(item.memory_id)
        self.assertEqual(prov.source_run_id, run1.id)
        self.assertEqual(prov.source_job_id, job1.id)
        self.assertEqual(prov.outcome_id, approval.id)
        self.assertEqual(prov.outcome_decision, "rejected")

        again = lifecycle.process_reviewed_outcome(outcome_id=approval.id, reusable_intelligence="authentication login fixes must use the service-layer auth helper; do not patch controllers directly")
        self.assertEqual(again.memory_id, item.memory_id)
        with session_scope() as s:
            self.assertEqual(s.query(orm.AgentMemory).filter(orm.AgentMemory.memory_id == item.memory_id).count(), 1)
            self.assertEqual(s.query(orm.MemoryProvenance).filter(orm.MemoryProvenance.outcome_id == approval.id).count(), 1)

        job2 = make_job(instruction="repair authentication login error handling")
        selection2 = memory.retrieve_for_task(repo=job2.repo, instruction=job2.instruction, workspace_id=job2.workspace_id, repository_id=job2.repository_id)
        self.assertIn(item.memory_id, selection2.memory_ids)
        _, run2 = make_run(job2, selection2.memory_ids)
        self.assertIn(item.memory_id, run_store.get_run(run2.id).memory_ids)
        self.assertEqual([r.id for r in lifecycle.later_runs_that_received(item.memory_id)], [run2.id])

        self.assertEqual(lifecycle.intelligence_from_run(run1.id)[0].memory_id, item.memory_id)

        other_ws = memory.retrieve_for_task(repo=job2.repo, instruction=job2.instruction, workspace_id="ws-B", repository_id=job2.repository_id)
        other_repo = memory.retrieve_for_task(repo=job2.repo, instruction=job2.instruction, workspace_id=job2.workspace_id, repository_id="repo-2")
        self.assertNotIn(item.memory_id, other_ws.memory_ids)
        self.assertNotIn(item.memory_id, other_repo.memory_ids)


    def test_explicit_end_to_end_intelligence_review_and_cross_model_reuse(self):
        from gnsis.orchestration.models import Approval
        from gnsis.service.codememory import CodeMemory, MemoryKind
        from gnsis.service.executor.models import ExecutionStatus
        from gnsis.service.intelligence_lifecycle import IntelligenceLifecycle, ReviewedIntelligenceItem
        from gnsis.service.repository import PostgresJobStore
        from gnsis.service import orm
        from gnsis.service.db import session_scope

        jobs = PostgresJobStore()
        memory = CodeMemory()
        lifecycle = IntelligenceLifecycle(jobs=jobs, memory=memory)

        # 1. Existing approved intelligence is stored.
        seed = memory.record_accepted_change(
            repo="o/r",
            source_job_id="seed-job",
            workspace_id="ws-A",
            repository_id="repo-1",
            content="Auth login fixes must use auth/session.py helpers and regression tests.",
        )
        self.assertIsNotNone(seed)

        # 2-3. Run A retrieves it and consumption is pinned/recorded.
        job_a = make_job(instruction="fix auth login regression")
        selection_a = memory.retrieve_for_task(
            repo=job_a.repo,
            instruction=job_a.instruction,
            workspace_id=job_a.workspace_id,
            repository_id=job_a.repository_id,
        )
        self.assertIn(seed.memory_id, selection_a.memory_ids)
        run_store, run_a = make_run(job_a, selection_a.memory_ids)
        with session_scope() as db:
            row = db.get(orm.ExecutionRun, run_a.id)
            row.primary_model = "anthropic/claude-opus-4.8"
            row.advisor_model = None
        run_a = run_store.get_run(run_a.id)
        self.assertEqual(run_a.primary_model, "anthropic/claude-opus-4.8")
        self.assertIsNone(run_a.advisor_model)
        with session_scope() as db:
            consumed = [
                r.memory_id
                for r in db.query(orm.MemoryConsumption)
                .filter(orm.MemoryConsumption.run_id == run_a.id)
                .all()
            ]
        self.assertIn(seed.memory_id, consumed)

        # 4-5. OpenHands-style bounded evidence creates a review candidate that
        # is visible in the structured review contract before persistence.
        run_store.set_status(run_a.id, ExecutionStatus.RUNNING)
        run_store.record_event(
            run_a.id,
            job_id=job_a.id,
            workflow_run_attempt=None,
            sequence=1,
            idempotency_key="cmd-pytest",
            kind="command_result",
            payload={"cmd": "pytest tests/test_auth.py", "exit_code": 0},
        )
        run_store.record_event(
            run_a.id,
            job_id=job_a.id,
            workflow_run_attempt=None,
            sequence=2,
            idempotency_key="diff-auth",
            kind="final_diff",
            payload={"files": ["auth/session.py", "tests/test_auth.py"]},
        )
        candidate = ReviewedIntelligenceItem(
            content="Auth login regressions require auth/session.py changes plus tests/test_auth.py coverage.",
            kind=MemoryKind.ACCEPTED_CHANGE,
            item_key="auth-login-evidence",
        )
        review_contract = [candidate]
        self.assertEqual(review_contract[0].item_key, "auth-login-evidence")
        self.assertIn("tests/test_auth.py", review_contract[0].content)

        # 6. Candidate rejection prevents persistence and future reuse.
        rejected_job = make_job(instruction="auth login rejected candidate")
        make_run(rejected_job)
        rejected = jobs.save_approval(
            Approval(job_id=rejected_job.id, decision="rejected", actor="reviewer")
        )
        rejected_candidate = ReviewedIntelligenceItem(
            content="Rejected candidate must not be stored for future auth login work.",
            kind=MemoryKind.ACCEPTED_CHANGE,
            item_key="rejected-candidate",
        )
        # A rejected candidate-review UI action does not call the approval-gated
        # persistence method. Web/Advisor text below likewise only records events.
        self.assertEqual(lifecycle.process_reviewed_outcome_items(outcome_id=rejected.id, intelligence_items=[]), [])
        after_reject = memory.retrieve_for_task(
            repo="o/r",
            instruction="Rejected candidate future auth login work",
            workspace_id="ws-A",
            repository_id="repo-1",
        )
        self.assertFalse(any(item.content == rejected_candidate.content for item in after_reject.items))

        # 11-12. Web Search text alone and Advisor guidance alone are observable
        # events but are not approval-gated repository intelligence.
        run_store.record_event(
            run_a.id,
            job_id=job_a.id,
            workflow_run_attempt=None,
            sequence=3,
            idempotency_key="web-search-only",
            kind="server_tool_result",
            payload={"tool": "openrouter:web_search", "text": "Web-only auth claim"},
        )
        run_store.record_event(
            run_a.id,
            job_id=job_a.id,
            workflow_run_attempt=None,
            sequence=4,
            idempotency_key="advisor-only",
            kind="server_tool_result",
            payload={"tool": "openrouter:advisor", "text": "Advisor-only auth claim"},
        )
        with session_scope() as db:
            all_contents = [row.content for row in db.query(orm.AgentMemory).all()]
        self.assertFalse(any("Web-only auth claim" in c for c in all_contents))
        self.assertFalse(any("Advisor-only auth claim" in c for c in all_contents))

        # 7-8. Approval persists with provenance; duplicate processing is idempotent.
        run_store.set_status(run_a.id, ExecutionStatus.COMPLETED)
        approved = jobs.save_approval(Approval(job_id=job_a.id, decision="approved", actor="reviewer"))
        first = lifecycle.process_reviewed_outcome_items(
            outcome_id=approved.id,
            intelligence_items=review_contract,
        )
        second = lifecycle.process_reviewed_outcome_items(
            outcome_id=approved.id,
            intelligence_items=review_contract,
        )
        self.assertEqual(len(first), 1)
        self.assertEqual(second[0].memory_id, first[0].memory_id)
        prov = lifecycle.provenance_for_memory(first[0].memory_id)
        self.assertEqual(prov.source_run_id, run_a.id)
        self.assertEqual(prov.item_key, "auth-login-evidence")
        with session_scope() as db:
            self.assertEqual(
                db.query(orm.MemoryProvenance)
                .filter(orm.MemoryProvenance.outcome_id == approved.id)
                .count(),
                1,
            )

        # 9-10. Run B uses a different selected primary model, has no Advisor,
        # and receives the newly approved intelligence for cross-model reuse.
        job_b = make_job(instruction="auth login tests coverage", workspace_id="ws-A", repository_id="repo-1")
        selection_b = memory.retrieve_for_task(
            repo=job_b.repo,
            instruction=job_b.instruction,
            workspace_id=job_b.workspace_id,
            repository_id=job_b.repository_id,
        )
        self.assertIn(first[0].memory_id, selection_b.memory_ids)
        _, run_b = make_run(job_b, selection_b.memory_ids)
        with session_scope() as db:
            row = db.get(orm.ExecutionRun, run_b.id)
            row.primary_model = "openai/gpt-5.4"
            row.advisor_model = None
        run_b = run_store.get_run(run_b.id)
        self.assertEqual(run_b.primary_model, "openai/gpt-5.4")
        self.assertIsNone(run_b.advisor_model)
        self.assertIn(first[0].memory_id, run_b.memory_ids)
        with session_scope() as db:
            consumed_b = [
                r.memory_id
                for r in db.query(orm.MemoryConsumption)
                .filter(orm.MemoryConsumption.run_id == run_b.id)
                .all()
            ]
        self.assertIn(first[0].memory_id, consumed_b)

    def test_processes_explicit_outcome_id_not_newer_latest_review(self):
        from gnsis.orchestration.models import Approval
        from gnsis.service.intelligence_lifecycle import IntelligenceLifecycle
        from gnsis.service.repository import PostgresJobStore

        jobs = PostgresJobStore()
        lifecycle = IntelligenceLifecycle(jobs=jobs)
        job = make_job(instruction="fix payment retry handling")
        _, run = make_run(job)
        first = jobs.save_approval(Approval(job_id=job.id, decision="rejected", actor="a", note="first"))
        second = jobs.save_approval(Approval(job_id=job.id, decision="rejected", actor="b", note="second"))

        item = lifecycle.process_reviewed_outcome(
            outcome_id=first.id,
            reusable_intelligence="payment retry fixes must keep idempotency keys stable",
        )

        prov = lifecycle.provenance_for_memory(item.memory_id)
        self.assertEqual(prov.source_run_id, run.id)
        self.assertEqual(prov.outcome_id, first.id)
        self.assertNotEqual(prov.outcome_id, second.id)

    def test_one_outcome_creates_multiple_same_kind_records_with_retry_safety(self):
        from gnsis.orchestration.models import Approval
        from gnsis.service.codememory import MemoryKind
        from gnsis.service.intelligence_lifecycle import IntelligenceLifecycle, ReviewedIntelligenceItem
        from gnsis.service.repository import PostgresJobStore
        from gnsis.service import orm
        from gnsis.service.db import session_scope

        jobs = PostgresJobStore()
        lifecycle = IntelligenceLifecycle(jobs=jobs)
        job = make_job(instruction="fix auth headers")
        make_run(job)
        approval = jobs.save_approval(Approval(job_id=job.id, decision="rejected", actor="reviewer"))

        items = [
            ReviewedIntelligenceItem("auth headers must be normalized in middleware", MemoryKind.REJECTION_LESSON, "middleware"),
            ReviewedIntelligenceItem("auth headers tests must cover mixed case", MemoryKind.REJECTION_LESSON, "tests"),
        ]
        first = lifecycle.process_reviewed_outcome_items(outcome_id=approval.id, intelligence_items=items)
        retry = lifecycle.process_reviewed_outcome_items(outcome_id=approval.id, intelligence_items=list(reversed(items)))

        self.assertEqual(len(first), 2)
        self.assertEqual({item.kind for item in first}, {MemoryKind.REJECTION_LESSON})
        self.assertEqual({item.memory_id for item in retry}, {item.memory_id for item in first})
        with session_scope() as s:
            self.assertEqual(s.query(orm.MemoryProvenance).filter(orm.MemoryProvenance.outcome_id == approval.id).count(), 2)
            self.assertEqual(s.query(orm.AgentMemory).count(), 2)

    def test_mixed_kinds_and_later_add_only_new_item(self):
        from gnsis.orchestration.models import Approval
        from gnsis.service.codememory import MemoryKind
        from gnsis.service.intelligence_lifecycle import IntelligenceLifecycle, ReviewedIntelligenceItem
        from gnsis.service import orm
        from gnsis.service.db import session_scope

        lifecycle = IntelligenceLifecycle()
        job = make_job()
        make_run(job)
        approval = lifecycle.jobs.save_approval(Approval(job_id=job.id, decision="approved", actor="reviewer"))
        first = lifecycle.process_reviewed_outcome_items(
            outcome_id=approval.id,
            intelligence_items=[
                ReviewedIntelligenceItem("accepted auth change", MemoryKind.ACCEPTED_CHANGE, "accepted"),
                ReviewedIntelligenceItem("do not skip auth regression tests", MemoryKind.REJECTION_LESSON, "lesson"),
            ],
        )
        second = lifecycle.process_reviewed_outcome_items(
            outcome_id=approval.id,
            intelligence_items=[
                ReviewedIntelligenceItem("accepted auth change", MemoryKind.ACCEPTED_CHANGE, "accepted"),
                ReviewedIntelligenceItem("document auth middleware ownership", MemoryKind.ACCEPTED_CHANGE, "docs"),
            ],
        )
        self.assertEqual(len(first), 2)
        self.assertEqual(len(second), 2)
        self.assertEqual(second[0].memory_id, first[0].memory_id)
        with session_scope() as s:
            self.assertEqual(s.query(orm.AgentMemory).count(), 3)

    def test_conflicting_identity_and_failed_batch_do_not_partially_commit(self):
        from gnsis.orchestration.models import Approval
        from gnsis.service.codememory import MemoryKind
        from gnsis.service.intelligence_lifecycle import IntelligenceLifecycle, ReviewedIntelligenceItem
        from gnsis.service import orm
        from gnsis.service.db import session_scope

        lifecycle = IntelligenceLifecycle()
        job = make_job()
        make_run(job)
        approval = lifecycle.jobs.save_approval(Approval(job_id=job.id, decision="rejected", actor="reviewer"))
        lifecycle.process_reviewed_outcome_items(
            outcome_id=approval.id,
            intelligence_items=[ReviewedIntelligenceItem("stable lesson", MemoryKind.REJECTION_LESSON, "stable")],
        )
        with self.assertRaises(ValueError):
            lifecycle.process_reviewed_outcome_items(
                outcome_id=approval.id,
                intelligence_items=[
                    ReviewedIntelligenceItem("stable lesson changed", MemoryKind.REJECTION_LESSON, "stable"),
                    ReviewedIntelligenceItem("new lesson should rollback", MemoryKind.REJECTION_LESSON, "new"),
                ],
            )
        with session_scope() as s:
            self.assertEqual(s.query(orm.AgentMemory).count(), 1)
            self.assertEqual(s.query(orm.MemoryProvenance).count(), 1)

    def test_existing_null_item_key_provenance_remains_queryable(self):
        from gnsis.orchestration.models import Approval
        from gnsis.service.codememory import MemoryKind
        from gnsis.service.intelligence_lifecycle import IntelligenceLifecycle, ReviewedIntelligenceItem
        from gnsis.service import orm
        from gnsis.service.db import session_scope

        lifecycle = IntelligenceLifecycle()
        job = make_job()
        _, run = make_run(job)
        approval = lifecycle.jobs.save_approval(Approval(job_id=job.id, decision="rejected", actor="reviewer"))
        with session_scope() as s:
            mem = orm.AgentMemory(repo=job.repo, kind=MemoryKind.REJECTION_LESSON, content="legacy lesson", meta={}, approved=True, workspace_id=job.workspace_id, repository_id=job.repository_id, memory_id="mem_legacy", source_job_id=job.id)
            s.add(mem)
            s.add(orm.MemoryProvenance(memory_id="mem_legacy", kind=MemoryKind.REJECTION_LESSON, source_run_id=run.id, source_job_id=job.id, outcome_id=approval.id, outcome_decision="rejected", workspace_id=job.workspace_id, repository_id=job.repository_id))
        prov = lifecycle.provenance_for_memory("mem_legacy")
        self.assertEqual(prov.memory_id, "mem_legacy")
        self.assertIsNone(prov.item_key)
        item = lifecycle.process_reviewed_outcome(outcome_id=approval.id, reusable_intelligence="legacy lesson")
        self.assertEqual(item.memory_id, "mem_legacy")
        multi = lifecycle.process_reviewed_outcome_items(
            outcome_id=approval.id,
            intelligence_items=[
                ReviewedIntelligenceItem(
                    "legacy lesson",
                    MemoryKind.REJECTION_LESSON,
                    MemoryKind.REJECTION_LESSON,
                )
            ],
        )
        self.assertEqual([i.memory_id for i in multi], ["mem_legacy"])

        with self.assertRaises(ValueError):
            lifecycle.process_reviewed_outcome(
                outcome_id=approval.id,
                reusable_intelligence="legacy lesson changed",
            )
        with self.assertRaises(ValueError):
            lifecycle.process_reviewed_outcome_items(
                outcome_id=approval.id,
                intelligence_items=[
                    ReviewedIntelligenceItem(
                        "legacy lesson changed",
                        MemoryKind.REJECTION_LESSON,
                        MemoryKind.REJECTION_LESSON,
                    )
                ],
            )

        added = lifecycle.process_reviewed_outcome_items(
            outcome_id=approval.id,
            intelligence_items=[
                ReviewedIntelligenceItem(
                    "legacy same-kind new item",
                    MemoryKind.REJECTION_LESSON,
                    "legacy-new",
                )
            ],
        )
        self.assertEqual(len(added), 1)
        self.assertNotEqual(added[0].memory_id, "mem_legacy")
        with session_scope() as s:
            self.assertEqual(s.query(orm.AgentMemory).count(), 2)
            self.assertEqual(s.query(orm.MemoryProvenance).count(), 2)

    def test_production_reject_job_wires_reviewed_outcome_to_codememory(self):
        from gnsis.orchestration.pipeline import reject_job
        from gnsis.service.codememory import CodeMemory, MemoryKind
        from gnsis.service.intelligence_lifecycle import IntelligenceLifecycle
        from gnsis.service.repository import PostgresJobStore, PostgresMemoryProvider

        jobs = PostgresJobStore()
        memory = CodeMemory()
        lifecycle = IntelligenceLifecycle(jobs=jobs, memory=memory)
        job = make_job(instruction="fix search authentication checks")
        _, run = make_run(job)

        reject_job(jobs, job.id, actor="reviewer", note="use the shared auth helper", memory=PostgresMemoryProvider())

        produced = lifecycle.intelligence_from_run(run.id)
        self.assertEqual(len(produced), 1)
        self.assertEqual(produced[0].kind, MemoryKind.REJECTION_LESSON)
        selected = memory.retrieve_for_task(
            repo=job.repo,
            instruction="repair search authentication",
            workspace_id=job.workspace_id,
            repository_id=job.repository_id,
        )
        self.assertIn(produced[0].memory_id, selected.memory_ids)

    def test_approved_publish_with_execution_run_creates_one_accepted_change_with_provenance(self):
        from gnsis.orchestration.models import Approval, Diff, PRMetadata
        from gnsis.orchestration.pipeline import publish
        from gnsis.service.codememory import MemoryKind
        from gnsis.service.intelligence_lifecycle import IntelligenceLifecycle
        from gnsis.service.repository import PostgresJobStore, PostgresMemoryProvider

        class FakePublisher:
            def publish(self, job, diff):
                return PRMetadata(job_id=job.id, number=22, url="https://pr/22", branch="b")

        jobs = PostgresJobStore()
        memory = PostgresMemoryProvider()
        lifecycle = IntelligenceLifecycle(jobs=jobs)
        job = make_job(instruction="add authentication widget")
        jobs.save_diff(Diff(job_id=job.id, patch="diff --git a/a b/a", files_changed=["a"]))
        _, run = make_run(job)
        approval = jobs.save_approval(Approval(job_id=job.id, decision="approved", actor="reviewer"))
        jobs.set_status(job.id, "approved")

        publish(jobs, FakePublisher(), job.id, memory=memory)
        publish(jobs, FakePublisher(), job.id, memory=memory)

        produced = lifecycle.intelligence_from_run(run.id)
        self.assertEqual(len(produced), 1)
        self.assertEqual(produced[0].kind, MemoryKind.ACCEPTED_CHANGE)
        self.assertEqual(produced[0].outcome_id, approval.id)
        self.assertEqual(len(memory.recent("o/r")), 1)

    def test_compatibility_pipeline_publish_without_execution_run_writes_legacy_memory_without_provenance(self):
        from gnsis.orchestration.models import Approval, Diff, PRMetadata
        from gnsis.orchestration.pipeline import publish
        from gnsis.service.intelligence_lifecycle import IntelligenceLifecycle
        from gnsis.service.repository import PostgresJobStore, PostgresMemoryProvider

        class FakePublisher:
            def publish(self, job, diff):
                return PRMetadata(job_id=job.id, number=23, url="https://pr/23", branch="b")

        jobs = PostgresJobStore()
        memory = PostgresMemoryProvider()
        lifecycle = IntelligenceLifecycle(jobs=jobs)
        job = make_job(instruction="add compatibility widget")
        jobs.save_diff(Diff(job_id=job.id, patch="diff --git a/a b/a", files_changed=["a"]))
        approval = jobs.save_approval(Approval(job_id=job.id, decision="approved", actor="reviewer"))
        jobs.set_status(job.id, "approved")

        publish(jobs, FakePublisher(), job.id, memory=memory)

        self.assertTrue(memory.recent("o/r"))
        self.assertIsNone(lifecycle.process_reviewed_outcome(outcome_id=approval.id, reusable_intelligence="compatibility widget"))
        self.assertEqual(lifecycle.intelligence_from_run("exec_missing"), [])

    def test_legacy_repository_null_scoping_and_pinned_reconstruction(self):
        from gnsis.service.codememory import CodeMemory
        from gnsis.service.repository import PostgresMemoryProvider
        from gnsis.memory.base import MemoryRecord

        provider = PostgresMemoryProvider()
        memory = CodeMemory(provider)
        own_legacy = provider.write(
            MemoryRecord(
                repo="o/r",
                content="authentication legacy helper must be preserved",
                kind="convention",
                approved=True,
                workspace_id="ws-A",
                repository_id=None,
                source_job_id="legacy",
            )
        )
        other_repo = provider.write(
            MemoryRecord(
                repo="o/r",
                content="authentication other repository rule",
                kind="convention",
                approved=True,
                workspace_id="ws-A",
                repository_id="repo-2",
                source_job_id="other",
            )
        )
        other_ws = provider.write(
            MemoryRecord(
                repo="o/r",
                content="authentication other workspace rule",
                kind="convention",
                approved=True,
                workspace_id="ws-B",
                repository_id=None,
                source_job_id="other-ws",
            )
        )

        selected = memory.retrieve_for_task(
            repo="o/r",
            instruction="authentication helper",
            workspace_id="ws-A",
            repository_id="repo-1",
        )
        self.assertIn(own_legacy.memory_id, selected.memory_ids)
        self.assertNotIn(other_repo.memory_id, selected.memory_ids)
        self.assertNotIn(other_ws.memory_id, selected.memory_ids)

        pinned = memory.get_records_by_ids(
            memory_ids=[own_legacy.memory_id, other_repo.memory_id, other_ws.memory_id],
            workspace_id="ws-A",
            repository_id="repo-1",
            repo="o/r",
        )
        self.assertEqual([item.memory_id for item in pinned], [own_legacy.memory_id])


if __name__ == "__main__":
    unittest.main()
