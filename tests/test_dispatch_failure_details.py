"""Structured, sanitized failure diagnostics for a dispatch-time failure —
one that happens before any ExecutionRun exists, so the usual per-run
lifecycle events never get a chance to explain it. Covers the trusted-
executor SHA mismatch end to end: DispatchError carries the comparison,
run_job()'s handler persists it onto job.context, and both the Activity
projection and the receipt surface it instead of a bare "failed" with no
explanation.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from _authkit import fresh_sqlite_env  # noqa: E402

OWNER = "gnsis"
REPO = "executor"
TRUSTED_SHA = "a" * 40
OBSERVED_SHA = "f" * 40


def _configure():
    fresh_sqlite_env()
    os.environ.update(
        {
            "GITHUB_APP_ID": "12345",
            "GITHUB_APP_PRIVATE_KEY": "key",
            "GITHUB_APP_SLUG": "gnsis-studio",
            "GNSIS_EXECUTION_PROVIDER": "github_actions",
            "GNSIS_PUBLIC_API_URL": "https://api.gnsis.test",
            "GNSIS_EXECUTOR_OWNER": OWNER,
            "GNSIS_EXECUTOR_REPO": REPO,
            "GNSIS_EXECUTOR_OIDC_AUDIENCE": "https://api.gnsis.studio",
            "GNSIS_EXECUTOR_TRUSTED_WORKFLOW_SHA": TRUSTED_SHA,
        }
    )
    from gnsis.service import settings as settings_mod

    settings_mod._settings = None
    from gnsis.service.db import init_db

    init_db()


def _make_job():
    from gnsis.orchestration.models import JobSpec
    from gnsis.service.repository import PostgresJobStore

    return PostgresJobStore().create_job(
        JobSpec(
            repo="octo/repo",
            instruction="fix the bug",
            base_branch="main",
            engine="gnsis",
            workspace_id="ws-A",
            repository_id="repo-1",
        )
    )


class DispatchErrorDetailsTests(unittest.TestCase):
    """The exception class itself: `details` is optional, safe, additive."""

    def test_details_default_to_empty_dict(self):
        from gnsis.service.executor.dispatch import DispatchError

        exc = DispatchError("boom")
        self.assertEqual(exc.details, {})

    def test_details_are_stored_verbatim(self):
        from gnsis.service.executor.dispatch import DispatchError
        from gnsis.service.executor.models import FailureCategory

        exc = DispatchError(
            "sha mismatch",
            category=FailureCategory.SECURITY,
            details={"expected_sha": TRUSTED_SHA, "observed_sha": OBSERVED_SHA},
        )
        self.assertEqual(exc.category, FailureCategory.SECURITY)
        self.assertEqual(
            exc.details, {"expected_sha": TRUSTED_SHA, "observed_sha": OBSERVED_SHA}
        )


class DispatchExecutionShaMismatchTests(unittest.TestCase):
    """dispatch_execution() itself raises with the sanitized comparison."""

    def setUp(self):
        _configure()
        from gnsis.service.executor import installation as inst_mod

        inst_mod._cache.clear()
        self.addCleanup(inst_mod._cache.clear)

    def test_sha_mismatch_carries_expected_and_observed(self):
        from gnsis.service.executor.dispatch import DispatchError, dispatch_execution
        from gnsis.service.executor.models import FailureCategory
        from gnsis.service.settings import get_settings

        settings = get_settings()

        class FakeGitHub:
            def __init__(self, app=None):
                pass

            def repo_installation(self, owner, repo):
                return {
                    "id": 1,
                    "app_id": settings.github_app_id,
                    "permissions": {"actions": "write", "contents": "read"},
                }

            def scoped_installation_token(self, installation_id, *, repositories, permissions):
                return {"token": "ghs_test"}

            def get_repo(self, owner, repo, token):
                return {"id": 1, "private": True}

            def ref_sha(self, owner, repo, ref, token):
                return OBSERVED_SHA

        job = _make_job()
        with self.assertRaises(DispatchError) as ctx:
            dispatch_execution(
                settings, store=None, job=job, base_sha="b" * 40,
                app=object(), github=FakeGitHub(),
            )
        exc = ctx.exception
        self.assertEqual(exc.category, FailureCategory.SECURITY)
        self.assertEqual(
            exc.details, {"expected_sha": TRUSTED_SHA, "observed_sha": OBSERVED_SHA}
        )

    def test_malformed_trusted_sha_is_redacted_not_leaked(self):
        """A misconfigured GNSIS_EXECUTOR_TRUSTED_WORKFLOW_SHA (e.g. an
        accidentally pasted token instead of a real commit sha) must never be
        echoed back verbatim — job.context feeds tenant-visible Activity and
        receipt responses."""
        from gnsis.service import settings as settings_mod
        from gnsis.service.executor.dispatch import DispatchError, dispatch_execution
        from gnsis.service.settings import get_settings

        os.environ["GNSIS_EXECUTOR_TRUSTED_WORKFLOW_SHA"] = "ghp_looksLikeATokenNotASha12345"
        settings_mod._settings = None
        settings = get_settings()
        self.addCleanup(lambda: os.environ.__setitem__(
            "GNSIS_EXECUTOR_TRUSTED_WORKFLOW_SHA", TRUSTED_SHA
        ))

        class FakeGitHub:
            def __init__(self, app=None):
                pass

            def repo_installation(self, owner, repo):
                return {
                    "id": 1,
                    "app_id": settings.github_app_id,
                    "permissions": {"actions": "write", "contents": "read"},
                }

            def scoped_installation_token(self, installation_id, *, repositories, permissions):
                return {"token": "ghs_test"}

            def get_repo(self, owner, repo, token):
                return {"id": 1, "private": True}

            def ref_sha(self, owner, repo, ref, token):
                return OBSERVED_SHA

        job = _make_job()
        with self.assertRaises(DispatchError) as ctx:
            dispatch_execution(
                settings, store=None, job=job, base_sha="b" * 40,
                app=object(), github=FakeGitHub(),
            )
        exc = ctx.exception
        self.assertEqual(exc.details["expected_sha"], "(not a valid commit sha)")
        # The observed side came straight from the GitHub API and is a real
        # sha — it must not be redacted just because the other side was bad.
        self.assertEqual(exc.details["observed_sha"], OBSERVED_SHA)


class ActivityFailureEnrichmentTests(unittest.TestCase):
    """The synthesized run.failed event for a job with no ExecutionRun."""

    def setUp(self):
        _configure()

    def test_recorded_failure_category_enriches_the_synthesized_event(self):
        from gnsis.orchestration.status import JobStatus
        from gnsis.service.activity import build_lifecycle_events
        from gnsis.service.executor.models import FailureCategory
        from gnsis.service.repository import PostgresJobStore

        store = PostgresJobStore()
        job = _make_job()
        store.merge_context(
            job.id,
            {
                "failure_category": FailureCategory.SECURITY,
                "failure_details": {"expected_sha": TRUSTED_SHA, "observed_sha": OBSERVED_SHA},
            },
        )
        store.set_status(job.id, JobStatus.FAILED, error="dispatch failed: sha mismatch")

        events = build_lifecycle_events(job.id)
        failed = [e for e in events if e["type"] == "run.failed"]
        self.assertEqual(len(failed), 1, events)
        payload = failed[0]["payload"]
        self.assertEqual(payload["category"], FailureCategory.SECURITY)
        self.assertIs(payload["execution_started"], False)
        self.assertIs(payload["model_called"], False)
        self.assertEqual(
            payload["technical"],
            {
                "failure_category": FailureCategory.SECURITY,
                "expected_sha": TRUSTED_SHA,
                "observed_sha": OBSERVED_SHA,
            },
        )

    def test_no_recorded_category_leaves_the_bare_fallback_unchanged(self):
        from gnsis.orchestration.status import JobStatus
        from gnsis.service.activity import build_lifecycle_events
        from gnsis.service.repository import PostgresJobStore

        store = PostgresJobStore()
        job = _make_job()
        store.set_status(job.id, JobStatus.FAILED, error="dispatch failed: something else")

        events = build_lifecycle_events(job.id)
        failed = [e for e in events if e["type"] == "run.failed"]
        self.assertEqual(len(failed), 1, events)
        self.assertEqual(failed[0]["payload"], {"status": "failed"})


class ReceiptFailureFieldsTests(unittest.TestCase):
    """build_receipt()'s job-scoped shell (no ExecutionRun yet)."""

    def setUp(self):
        _configure()

    def test_recorded_failure_category_reaches_the_receipt(self):
        from gnsis.orchestration.status import JobStatus
        from gnsis.service.executor.models import FailureCategory
        from gnsis.service.receipts import build_receipt
        from gnsis.service.repository import PostgresJobStore

        store = PostgresJobStore()
        job = _make_job()
        store.merge_context(
            job.id,
            {
                "failure_category": FailureCategory.SECURITY,
                "failure_details": {"expected_sha": TRUSTED_SHA, "observed_sha": OBSERVED_SHA},
            },
        )
        store.set_status(job.id, JobStatus.FAILED, error="dispatch failed: sha mismatch")

        receipt = build_receipt(job.workspace_id, job.id)
        self.assertEqual(receipt["failure_category"], FailureCategory.SECURITY)
        self.assertEqual(
            receipt["failure_details"],
            {"expected_sha": TRUSTED_SHA, "observed_sha": OBSERVED_SHA},
        )
        # job.error is never surfaced here — it isn't sanitized the way the
        # executor-callback failure path's job.error is.
        self.assertIsNone(receipt["failure_message"])

    def test_no_recorded_category_stays_null(self):
        from gnsis.service.receipts import build_receipt

        job = _make_job()
        receipt = build_receipt(job.workspace_id, job.id)
        self.assertIsNone(receipt["failure_category"])
        self.assertIsNone(receipt["failure_details"])
        self.assertIsNone(receipt["failure_message"])

    def test_empty_details_normalize_to_null_not_empty_dict(self):
        """A DispatchError raised without an explicit details= (most dispatch
        failures, e.g. a GitHub API error minting the dispatch token) must
        never surface failure_details as {} — the documented contract is null
        when there is none, and a frontend truthy-check on {} would
        incorrectly try to render technical details that don't exist."""
        from gnsis.orchestration.status import JobStatus
        from gnsis.service.executor.dispatch import DispatchError
        from gnsis.service.executor.models import FailureCategory
        from gnsis.service.receipts import build_receipt
        from gnsis.service.repository import PostgresJobStore

        store = PostgresJobStore()
        job = _make_job()
        exc = DispatchError(
            "could not mint dispatch token: boom", category=FailureCategory.DISPATCH
        )
        self.assertEqual(exc.details, {})
        # Mirrors run_job()'s except DispatchError handler in tasks.py.
        store.merge_context(
            job.id,
            {"failure_category": exc.category, "failure_details": exc.details or None},
        )
        store.set_status(job.id, JobStatus.FAILED, error=f"dispatch failed: {exc}")

        receipt = build_receipt(job.workspace_id, job.id)
        self.assertEqual(receipt["failure_category"], FailureCategory.DISPATCH)
        self.assertIsNone(receipt["failure_details"])


if __name__ == "__main__":
    unittest.main()
