"""Phase 1 — the CodeMemory application layer + agent_memory scoping migration.

Exercises the invariants directly against a fresh SQLite DB (the ORM/JSON paths
are identical to Postgres): approval-gated writes, tenant-strict scoping, bounded
+ deterministic retrieval with selection reasons, and by-id reconstruction.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from _authkit import fresh_sqlite_env  # noqa: E402


def _configure():
    fresh_sqlite_env()
    from gnsis.service import settings as sm

    sm._settings = None
    from gnsis.service.db import init_db

    init_db()


def _cols(table: str) -> set:

    from gnsis.service.db import get_engine

    with get_engine().connect() as conn:
        return {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}


class SchemaMigrationTests(unittest.TestCase):
    def setUp(self):
        _configure()

    def test_agent_memory_has_scoping_columns(self):
        cols = _cols("agent_memory")
        for c in ("workspace_id", "repository_id", "memory_id", "source_job_id"):
            self.assertIn(c, cols, f"agent_memory.{c} missing")

    def test_execution_runs_has_pinned_context_columns(self):
        cols = _cols("execution_runs")
        for c in ("policy_name", "policy_version", "policy_hash", "memory_ids"):
            self.assertIn(c, cols, f"execution_runs.{c} missing")


class CodeMemoryWriteTests(unittest.TestCase):
    def setUp(self):
        _configure()
        from gnsis.service.codememory import CodeMemory

        self.cm = CodeMemory()

    def test_accepted_change_is_written_and_scoped(self):
        item = self.cm.record_accepted_change(
            repo="o/r",
            source_job_id="job-1",
            content="Introduced a shared retry helper in net/retry.py",
            workspace_id="ws-A",
            repository_id="repo-1",
        )
        self.assertIsNotNone(item)
        self.assertTrue(item.memory_id.startswith("mem_"))
        self.assertEqual(item.kind, "accepted_change")
        self.assertEqual(item.workspace_id, "ws-A")
        self.assertEqual(item.repository_id, "repo-1")
        self.assertEqual(item.source_job_id, "job-1")

    def test_rejection_lesson_is_written(self):
        item = self.cm.record_rejection_lesson(
            repo="o/r",
            source_job_id="job-2",
            content="Rejected: don't add a new HTTP client, reuse net/http.py",
            workspace_id="ws-A",
            repository_id="repo-1",
        )
        self.assertIsNotNone(item)
        self.assertEqual(item.kind, "rejection_lesson")

    def test_empty_content_is_ignored(self):
        self.assertIsNone(
            self.cm.record_accepted_change(
                repo="o/r", source_job_id="j", content="   ", workspace_id="ws-A"
            )
        )


class CodeMemoryRetrievalTests(unittest.TestCase):
    def setUp(self):
        _configure()
        from gnsis.service.codememory import CodeMemory, MemoryKind

        self.cm = CodeMemory()
        self.K = MemoryKind
        # A standing security rule (no task-term overlap needed to surface).
        self.cm.record_accepted_change(
            repo="o/r", source_job_id="j1", workspace_id="ws-A", repository_id="repo-1",
            kind=MemoryKind.SECURITY_CONSTRAINT,
            content="Always parameterize SQL; never string-format queries.",
        )
        # An episodic accepted change about authentication.
        self.cm.record_accepted_change(
            repo="o/r", source_job_id="j2", workspace_id="ws-A", repository_id="repo-1",
            content="Refactored the authentication login flow into auth/session.py.",
        )
        # An episodic accepted change about an unrelated area.
        self.cm.record_accepted_change(
            repo="o/r", source_job_id="j3", workspace_id="ws-A", repository_id="repo-1",
            content="Tweaked the invoice PDF export margins.",
        )

    def test_retrieval_is_scoped_bounded_and_reasoned(self):
        sel = self.cm.retrieve_for_task(
            repo="o/r",
            instruction="fix the authentication login bug",
            workspace_id="ws-A",
            repository_id="repo-1",
            limit=2,
        )
        self.assertLessEqual(len(sel.items), 2)
        self.assertTrue(sel.items, "expected at least one relevant memory")
        # The authentication memory must rank first (term overlap), the standing
        # security rule remains eligible; the unrelated invoice change is excluded.
        self.assertIn("authentication", sel.items[0].content.lower())
        for item in sel.items:
            self.assertTrue(item.selection_reason)
            self.assertNotIn("invoice", item.content.lower())

    def test_unrelated_episodic_memory_is_excluded(self):
        sel = self.cm.retrieve_for_task(
            repo="o/r", instruction="authentication", workspace_id="ws-A", limit=10
        )
        contents = " ".join(i.content.lower() for i in sel.items)
        self.assertIn("authentication", contents)
        self.assertNotIn("invoice", contents)

    def test_retrieval_is_deterministic(self):
        a = self.cm.retrieve_for_task(
            repo="o/r", instruction="authentication login", workspace_id="ws-A", limit=5
        )
        b = self.cm.retrieve_for_task(
            repo="o/r", instruction="authentication login", workspace_id="ws-A", limit=5
        )
        self.assertEqual(a.memory_ids, b.memory_ids)

    def test_truncated_flag(self):
        sel = self.cm.retrieve_for_task(
            repo="o/r", instruction="authentication", workspace_id="ws-A", limit=1
        )
        self.assertEqual(len(sel.items), 1)
        self.assertTrue(sel.truncated)


class TenantIsolationTests(unittest.TestCase):
    def setUp(self):
        _configure()
        from gnsis.service.codememory import CodeMemory

        self.cm = CodeMemory()
        self.a = self.cm.record_accepted_change(
            repo="o/r", source_job_id="jA", workspace_id="ws-A", repository_id="repo-1",
            content="Workspace A convention: prefer dataclasses over dicts.",
        )

    def test_other_workspace_cannot_retrieve(self):
        sel = self.cm.retrieve_for_task(
            repo="o/r", instruction="dataclasses convention", workspace_id="ws-B", limit=10
        )
        self.assertEqual(sel.items, [])

    def test_owning_workspace_can_retrieve(self):
        sel = self.cm.retrieve_for_task(
            repo="o/r", instruction="dataclasses convention", workspace_id="ws-A", limit=10
        )
        self.assertTrue(sel.items)

    def test_by_ids_is_workspace_scoped(self):
        mid = self.a.memory_id
        # Owning workspace resolves it; another workspace resolves nothing.
        own = self.cm.get_records_by_ids(memory_ids=[mid], workspace_id="ws-A", repo="o/r")
        other = self.cm.get_records_by_ids(memory_ids=[mid], workspace_id="ws-B", repo="o/r")
        self.assertEqual([i.memory_id for i in own], [mid])
        self.assertEqual(other, [])

    def test_by_ids_preserves_order(self):
        b = self.cm.record_accepted_change(
            repo="o/r", source_job_id="jA2", workspace_id="ws-A", repository_id="repo-1",
            content="Workspace A convention: keep functions under 40 lines.",
        )
        ids = [b.memory_id, self.a.memory_id]
        got = self.cm.get_records_by_ids(memory_ids=ids, workspace_id="ws-A", repo="o/r")
        self.assertEqual([i.memory_id for i in got], ids)


if __name__ == "__main__":
    unittest.main()
