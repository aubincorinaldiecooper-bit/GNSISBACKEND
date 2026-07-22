"""Memory-ID normalization at the execution-run persistence boundary.

A run is pinned to the memory it consumed. If that id list carries null / blank /
duplicate entries, the raw path would write meaningless MemoryConsumption rows
and a duplicate would trip ``uq_memory_consumption_run_memory`` and roll the
whole run creation back. Normalization cleans the list ONCE so the same cleaned
list feeds both ``ExecutionRun.memory_ids`` and the consumption rows.

The pure-function tests need no database. The persistence test drives the real
``ExecutionStore.create_run`` against the test database so the uniqueness
constraint and the run-creation transaction are actually exercised.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from _authkit import fresh_sqlite_env  # noqa: E402


class NormalizeHelperTests(unittest.TestCase):
    def _fn(self):
        from gnsis.service.executor.store import normalize_memory_ids

        return normalize_memory_ids

    def test_spec_example(self):
        norm = self._fn()
        self.assertEqual(
            norm([None, "", "   ", "memory-a", "memory-a", "memory-b"]),
            ["memory-a", "memory-b"],
        )

    def test_drops_null_empty_and_whitespace(self):
        norm = self._fn()
        self.assertEqual(norm([None, "", "  ", "\t", "\n"]), [])

    def test_all_invalid_yields_empty_not_fabricated(self):
        norm = self._fn()
        self.assertEqual(norm([None, "", "   "]), [])
        self.assertEqual(norm(None), [])
        self.assertEqual(norm([]), [])

    def test_trims_surrounding_whitespace(self):
        norm = self._fn()
        self.assertEqual(norm(["  memory-a  ", "\tmemory-b\n"]), ["memory-a", "memory-b"])

    def test_dedup_preserves_first_seen_order(self):
        norm = self._fn()
        self.assertEqual(
            norm(["b", "a", "b", "c", "a"]),
            ["b", "a", "c"],
        )

    def test_one_valid_mixed_with_invalid(self):
        norm = self._fn()
        self.assertEqual(norm([None, "  ", "memory-x", ""]), ["memory-x"])

    def test_ignores_non_string_entries(self):
        norm = self._fn()
        self.assertEqual(norm(["memory-a", 123, None, "memory-b"]), ["memory-a", "memory-b"])


class CreateRunNormalizationTests(unittest.TestCase):
    def setUp(self):
        fresh_sqlite_env()
        from gnsis.service import settings as sm

        sm._settings = None
        from gnsis.service.db import init_db

        init_db()

    def _budgets(self):
        from gnsis.service.executor.models import Budgets

        return Budgets(
            max_model_calls=10,
            max_input_tokens=1000,
            max_output_tokens=1000,
            max_cost_usd=1.0,
        )

    def _create(self, memory_ids):
        from gnsis.service.executor.store import ExecutionStore

        return ExecutionStore().create_run(
            job_id="job_norm_1",
            workspace_id="ws-1",
            repository_id="repo-1",
            base_branch="main",
            base_sha="a" * 40,
            dispatch_nonce_hash="n" * 64,
            executor_owner="owner",
            executor_repository="Gnsis-studio-",
            executor_repository_id=42,
            executor_workflow="execute.yml",
            executor_ref="main",
            trusted_workflow_sha="b" * 40,
            budgets=self._budgets(),
            memory_ids=memory_ids,
        )

    def _consumption_ids(self, run_id):
        from gnsis.service import orm
        from gnsis.service.db import session_scope

        with session_scope() as s:
            rows = (
                s.query(orm.MemoryConsumption)
                .filter(orm.MemoryConsumption.run_id == run_id)
                .order_by(orm.MemoryConsumption.id)
                .all()
            )
            return [r.memory_id for r in rows]

    def test_dirty_ids_persist_without_rollback_and_match(self):
        run = self._create([None, "", "   ", "memory-a", "memory-a", "memory-b"])
        # Run row created (no rollback), memory_ids cleaned.
        self.assertEqual(run.memory_ids, ["memory-a", "memory-b"])
        # The SAME cleaned list is used for the consumption rows — no duplicate
        # uniqueness failure, no blank rows.
        self.assertEqual(self._consumption_ids(run.id), ["memory-a", "memory-b"])

    def test_all_invalid_creates_run_with_no_consumptions(self):
        run = self._create([None, "", "  "])
        self.assertEqual(run.memory_ids, [])
        self.assertEqual(self._consumption_ids(run.id), [])

    def test_none_memory_ids(self):
        run = self._create(None)
        self.assertEqual(run.memory_ids, [])
        self.assertEqual(self._consumption_ids(run.id), [])


if __name__ == "__main__":
    unittest.main()
