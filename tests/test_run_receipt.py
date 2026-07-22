"""Run receipt (G6, minimal): assemble-on-read from immutable records.

Exercises the assembler directly against the test database — inserting the
immutable source rows a real run leaves behind, then asserting the receipt
reflects them, is tenant-scoped, stays historically accurate, and degrades
gracefully when related records are missing.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from _authkit import fresh_sqlite_env  # noqa: E402


def _reset_settings():
    from gnsis.service import settings as sm

    sm._settings = None


class ReceiptTestBase(unittest.TestCase):
    def setUp(self):
        fresh_sqlite_env()
        _reset_settings()
        from gnsis.service.db import init_db

        init_db()

    def _add(self, *rows):
        from gnsis.service.db import session_scope

        with session_scope() as s:
            for r in rows:
                s.add(r)

    def _job(self, job_id="job-1", workspace_id="ws-1", **kw):
        from gnsis.service import orm

        params = dict(
            id=job_id, repo="owner/repo", instruction="add a hello() function",
            base_branch="main", engine="gnsis", status="awaiting_approval",
            workspace_id=workspace_id, repository_id="repo-1",
        )
        params.update(kw)
        return orm.Job(**params)

    def _run(self, run_id="exec-1", job_id="job-1", workspace_id="ws-1", **kw):
        from gnsis.service import orm

        params = dict(
            id=run_id, job_id=job_id, workspace_id=workspace_id, repository_id="repo-1",
            provider="github_actions", base_branch="main", base_sha="a" * 40,
            dispatch_nonce_hash="n" * 64, status="completed",
            patch_sha256="p" * 64, input_tokens=1200, output_tokens=340, model_calls=3,
            policy_name="genesis-coding-policy", policy_version=1, policy_hash="h" * 64,
            memory_ids=["mem-a", "mem-b"],
            tests_summary={"runner": "pytest", "status": "passed", "passed": 4, "failed": 0, "skipped": 0},
        )
        params.update(kw)
        return orm.ExecutionRun(**params)

    def _receipt(self, workspace_id="ws-1", job_id="job-1"):
        from gnsis.service.receipts import build_receipt

        return build_receipt(workspace_id, job_id)


class TenantScopingTests(ReceiptTestBase):
    def test_job_not_owned_returns_none(self):
        self._add(self._job(workspace_id="ws-1"))
        self.assertIsNone(self._receipt(workspace_id="ws-2"))

    def test_missing_job_returns_none(self):
        self.assertIsNone(self._receipt(job_id="nope"))


class MissingRecordsTests(ReceiptTestBase):
    def test_job_without_run_returns_shell(self):
        self._add(self._job(status="queued"))
        r = self._receipt()
        self.assertIsNotNone(r)
        self.assertIsNone(r["run_id"])
        self.assertEqual(r["status"], "queued")
        self.assertEqual(r["memory_ids_consumed"], [])
        self.assertEqual(r["files_changed"], [])
        self.assertIsNone(r["cost"])

    def test_run_without_charges_reports_zero_cost(self):
        self._add(self._job(), self._run())
        r = self._receipt()
        self.assertEqual(r["cost"]["provider_cost"], "0")
        self.assertEqual(r["cost"]["total_billed"], "0")
        self.assertEqual(r["cost"]["reconciliation_state"], "resolved")


class FullAssemblyTests(ReceiptTestBase):
    def _seed_full(self):
        from gnsis.service import orm

        self._add(
            self._job(),
            self._run(),
            orm.ExecutionModelCall(run_id="exec-1", job_id="job-1",
                                   model="anthropic/claude-opus-4.8",
                                   input_tokens=1200, output_tokens=340, cost_usd=0.02),
            orm.ExecutionEvent(run_id="exec-1", job_id="job-1", sequence=1,
                               idempotency_key="k1", kind="tool_call",
                               payload={"name": "read_file", "step": 1}),
            orm.ExecutionEvent(run_id="exec-1", job_id="job-1", sequence=2,
                               idempotency_key="k2", kind="tool_call",
                               payload={"name": "write_file", "step": 2}),
            orm.ExecutionEvent(run_id="exec-1", job_id="job-1", sequence=3,
                               idempotency_key="k3", kind="model_response",
                               payload={"step": 3}),
            orm.UsageRecord(id="usage-1", litellm_request_id="lr-1", run_id="exec-1",
                            workspace_id="ws-1", user_id="u-1",
                            provider="anthropic", model="anthropic/claude-opus-4.8",
                            input_tokens=1200, output_tokens=340,
                            cached_tokens=200, reasoning_tokens=50,
                            upstream_cost="0.0200", pricing_version_id="pv-2026-07",
                            reconciliation_state="resolved"),
            orm.UsageCharge(id="charge-1", usage_record_id="usage-1", litellm_request_id="lr-1",
                            workspace_id="ws-1", user_id="u-1", run_id="exec-1",
                            upstream_cost="0.0200", markup_rate="0.05",
                            service_fee="0.0010", retail_cost="0.0210",
                            currency="USD", rate_card_version="beta-2026-07"),
            orm.JobApproval(job_id="job-1", decision="approved", actor="user@example.com", note=""),
            orm.PullRequest(job_id="job-1", number=42, url="https://github.com/owner/repo/pull/42",
                            branch="gnsis/job-1", head_sha="c" * 40),
            orm.JobDiff(job_id="job-1", patch="--- a\n+++ b\n", files_changed=["hello.py", "test_hello.py"]),
            orm.MemoryConsumption(run_id="exec-1", job_id="job-1", memory_id="mem-a",
                                  workspace_id="ws-1", repository_id="repo-1"),
            orm.MemoryConsumption(run_id="exec-1", job_id="job-1", memory_id="mem-b",
                                  workspace_id="ws-1", repository_id="repo-1"),
        )
        # Provenance references a real approval id; fetch it after insert.
        from gnsis.service.db import session_scope

        with session_scope() as s:
            appr = s.query(orm.JobApproval).filter(orm.JobApproval.job_id == "job-1").one()
            s.add(orm.MemoryProvenance(
                source_run_id="exec-1", source_job_id="job-1", outcome_id=appr.id,
                item_key="accepted_change", memory_id="mem-new-1", kind="accepted_change",
                outcome_decision="approved", workspace_id="ws-1", repository_id="repo-1",
            ))

    def test_full_receipt(self):
        self._seed_full()
        r = self._receipt()
        self.assertEqual(r["run_id"], "exec-1")
        self.assertEqual(r["task"], "add a hello() function")
        self.assertEqual(r["repository"], "owner/repo")
        self.assertEqual(r["model"], "anthropic/claude-opus-4.8")
        self.assertEqual(r["base_sha"], "a" * 40)
        self.assertEqual(r["patch_hash"], "p" * 64)
        self.assertEqual(r["policy"], {"name": "genesis-coding-policy", "version": 1, "hash": "h" * 64})
        self.assertEqual(r["memory_ids_consumed"], ["mem-a", "mem-b"])
        self.assertEqual(r["reviewed_intelligence_created"],
                         [{"memory_id": "mem-new-1", "item_key": "accepted_change", "kind": "accepted_change"}])
        self.assertEqual(r["tokens"], {"input": 1200, "output": 340, "cached": 200, "reasoning": 50})
        self.assertEqual(r["model_calls"], 3)
        self.assertEqual(r["tool_calls"], 2)
        self.assertEqual(r["files_read"], 1)
        self.assertEqual(r["files_changed"], ["hello.py", "test_hello.py"])
        self.assertEqual(r["tests"], {"runner": "pytest", "status": "passed", "passed": 4, "failed": 0, "skipped": 0})
        self.assertEqual(r["approval"]["decision"], "approved")
        self.assertEqual(r["approval"]["approver"], "user@example.com")
        self.assertEqual(r["pull_request"]["url"], "https://github.com/owner/repo/pull/42")
        self.assertEqual(r["cost"]["provider_cost"], "0.0200")
        self.assertEqual(r["cost"]["gnsis_service_fee"], "0.0010")
        self.assertEqual(r["cost"]["total_billed"], "0.0210")
        self.assertEqual(r["cost"]["pricing_version"], "pv-2026-07")
        self.assertEqual(r["cost"]["rate_card_version"], "beta-2026-07")

    def test_historical_accuracy_reads_stored_charge_not_recomputed(self):
        self._seed_full()
        first = self._receipt()
        # Simulate a later price/markup change: add a NEW current-pricing row.
        # The receipt reads the immutable usage_charges row, so it must not move.
        from gnsis.service import orm

        self._add(orm.ModelPricing(id="pv-2099", provider="anthropic",
                                   model="anthropic/claude-opus-4.8",
                                   input_price="9.99", output_price="9.99"))
        second = self._receipt()
        self.assertEqual(first["cost"], second["cost"])
        self.assertEqual(second["cost"]["total_billed"], "0.0210")


class FailureAndReconTests(ReceiptTestBase):
    def test_failed_run_surfaces_safe_message(self):
        self._add(
            self._job(status="failed", error="dispatch failed: executor unreachable"),
            self._run(status="failed", failure_category="executor_error",
                      patch_sha256=None, tests_summary=None),
        )
        r = self._receipt()
        self.assertEqual(r["status"], "failed")
        self.assertEqual(r["failure_category"], "executor_error")
        self.assertEqual(r["failure_message"], "dispatch failed: executor unreachable")
        self.assertIsNone(r["tests"])

    def test_needs_reconciliation_dominates(self):
        from gnsis.service import orm

        self._add(
            self._job(), self._run(),
            orm.UsageRecord(id="u-2", litellm_request_id="lr-2", run_id="exec-1",
                            workspace_id="ws-1", user_id="u-1", provider="anthropic",
                            model="m", upstream_cost="0", reconciliation_state="needs_reconciliation"),
        )
        r = self._receipt()
        self.assertEqual(r["cost"]["reconciliation_state"], "needs_reconciliation")


class CompactTestsSummaryTests(unittest.TestCase):
    def _fn(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
        from gnsis.service.executor.callbacks import _compact_tests_summary

        return _compact_tests_summary

    def test_extracts_outcome_fields(self):
        fn = self._fn()
        out = fn('{"runner":"pytest","status":"passed","passed":4,"failed":0,"skipped":1,"output":"...big blob..."}')
        self.assertEqual(out, {"runner": "pytest", "status": "passed", "passed": 4, "failed": 0, "skipped": 1})
        self.assertNotIn("output", out)  # never carry the raw blob

    def test_bad_json_returns_none(self):
        fn = self._fn()
        self.assertIsNone(fn("not json"))
        self.assertIsNone(fn("[1,2,3]"))

    def test_non_int_counts_coerce_to_zero(self):
        fn = self._fn()
        out = fn('{"runner":"npm","passed":"lots","failed":true}')
        self.assertEqual(out["passed"], 0)
        self.assertEqual(out["failed"], 0)  # bool is not counted as int


if __name__ == "__main__":
    unittest.main()
