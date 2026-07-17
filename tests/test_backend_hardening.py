from __future__ import annotations

import types
import unittest
from dataclasses import dataclass, field

from gnsis.orchestration.models import Approval, Diff, PRMetadata
from gnsis.orchestration.status import JobStatus
from gnsis.service.executor.models import ExecutionStatus


@dataclass
class FakeJob:
    id: str = "job_1"
    repo: str = "octo/repo"
    instruction: str = "fix bug"
    base_branch: str = "main"
    engine: str = "gnsis"
    status: str = JobStatus.APPROVED
    branch: str = "gnsis/job_1"
    context: dict = field(default_factory=dict)
    workspace_id: str = "ws_1"
    repository_id: str = "repo_1"


@dataclass
class FakeRun:
    id: str = "exec_1"
    job_id: str = "job_1"
    base_branch: str = "main"
    base_sha: str = "b" * 40
    patch_sha256: str = "patch-sha"


class FakePublishStore:
    def __init__(self, *, metadata_fails=False, existing_pr=None):
        self.job = FakeJob(context={"approval_binding": {"ok": True}})
        self.statuses = []
        self.logs = []
        self.context_updates = []
        self.metadata_fails = metadata_fails
        self.existing_pr = existing_pr
        self.saved_pr = None

    def get_job(self, job_id):
        return self.job

    def get_latest_approval(self, job_id):
        return Approval(job_id=job_id, decision="approved", actor="tester")

    def get_diff(self, job_id):
        return Diff(job_id=job_id, patch="diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-a\n+b\n")

    def set_status(self, job_id, status, error=None):
        self.job.status = status
        self.job.error = error
        self.statuses.append((status, error))
        return self.job

    def append_log(self, entry):
        self.logs.append(entry)
        return entry

    def get_pr_metadata(self, job_id):
        return self.existing_pr

    def save_pr_metadata(self, meta):
        if self.metadata_fails:
            raise RuntimeError("db write failed token ghs_SECRET")
        self.saved_pr = meta
        return meta

    def merge_context(self, job_id, updates):
        self.context_updates.append(updates)
        self.job.context = {**self.job.context, **updates}
        return self.job


class PublishFailureTests(unittest.TestCase):
    def setUp(self):
        import gnsis.service.executor.publish as pub

        self.pub = pub
        self.orig = {
            "ExecutionStore": pub.ExecutionStore,
            "ExecutorGitHub": pub.ExecutorGitHub,
            "_customer_installation": pub._customer_installation,
            "verify_binding": pub.verify_binding,
            "_git": pub._git,
            "_apply_exact_patch": pub._apply_exact_patch,
            "_open_draft_pr": pub._open_draft_pr,
            "_find_existing_open_pr": pub._find_existing_open_pr,
        }
        pub.verify_binding = lambda *a, **k: None
        pub._customer_installation = lambda store, job: (object(), types.SimpleNamespace(github_installation_id=123))
        class ES:
            def get_run_for_job(self, job_id):
                return FakeRun()
        class GH:
            def __init__(self, app):
                pass
            def scoped_installation_token(self, *a, **k):
                return {"token": "ghs_SECRET"}
            def ref_sha(self, *a, **k):
                return "b" * 40
        pub.ExecutionStore = ES
        pub.ExecutorGitHub = GH
        pub._find_existing_open_pr = lambda *a, **k: None
        pub._open_draft_pr = lambda *a, **k: {"number": 7, "html_url": "https://pr", "draft": True}
        pub._apply_exact_patch = lambda *a, **k: None
        self.settings = types.SimpleNamespace(github_app_id="1", github_app_private_key="key")

    def tearDown(self):
        for name, value in self.orig.items():
            setattr(self.pub, name, value)

    def _run_with_git_failure(self, needle):
        store = FakePublishStore()
        calls = []
        def fake_git(args, cwd):
            calls.append(args)
            if needle in " ".join(args):
                raise self.pub.PublishError("boom https://x-access-token:ghs_SECRET@github.com/x/y.git")
            if args[:2] == ["git", "rev-parse"]:
                return "c" * 40
            return ""
        self.pub._git = fake_git
        with self.assertRaises(self.pub.PublishError):
            self.pub.publish_approved(store, self.settings, "job_1")
        self.assertEqual(store.job.status, JobStatus.FAILED)
        self.assertNotIn("ghs_SECRET", store.job.error)
        self.assertTrue(store.logs)
        return calls, store

    def test_fetch_failure_marks_failed(self):
        self._run_with_git_failure("fetch")

    def test_commit_failure_marks_failed(self):
        self._run_with_git_failure("commit")

    def test_push_failure_marks_failed(self):
        self._run_with_git_failure("push -u")

    def test_patch_failure_marks_failed(self):
        store = FakePublishStore()
        self.pub._git = lambda args, cwd: "c" * 40 if args[:2] == ["git", "rev-parse"] else ""
        self.pub._apply_exact_patch = lambda *a, **k: (_ for _ in ()).throw(self.pub.PublishError("patch token ghs_SECRET"))
        with self.assertRaises(self.pub.PublishError):
            self.pub.publish_approved(store, self.settings, "job_1")
        self.assertEqual(store.job.status, JobStatus.FAILED)
        self.assertNotIn("ghs_SECRET", store.job.error)

    def test_pr_creation_failure_deletes_pushed_branch(self):
        store = FakePublishStore()
        calls = []
        def fake_git(args, cwd):
            calls.append(args)
            if args[:2] == ["git", "rev-parse"]:
                return "c" * 40
            return ""
        self.pub._git = fake_git
        self.pub._open_draft_pr = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("pr failed token ghs_SECRET"))
        with self.assertRaises(self.pub.PublishError):
            self.pub.publish_approved(store, self.settings, "job_1")
        self.assertIn(["git", "push", "origin", ":refs/heads/gnsis/job_1"], calls)
        self.assertEqual(store.job.status, JobStatus.FAILED)

    def test_metadata_failure_marks_failed_and_sanitizes(self):
        store = FakePublishStore(metadata_fails=True)
        self.pub._git = lambda args, cwd: "c" * 40 if args[:2] == ["git", "rev-parse"] else ""
        with self.assertRaises(self.pub.PublishError):
            self.pub.publish_approved(store, self.settings, "job_1")
        self.assertEqual(store.job.status, JobStatus.FAILED)
        self.assertNotIn("ghs_SECRET", store.job.error)

    def test_retry_reuses_existing_pr_metadata(self):
        existing = PRMetadata(job_id="job_1", number=3, url="https://existing", branch="gnsis/job_1", head_sha="c" * 40)
        store = FakePublishStore(existing_pr=existing)
        opened = []
        self.pub._git = lambda args, cwd: "c" * 40 if args[:2] == ["git", "rev-parse"] else ""
        self.pub._open_draft_pr = lambda *a, **k: opened.append(True) or {"number": 9, "html_url": "dup"}
        meta = self.pub.publish_approved(store, self.settings, "job_1")
        self.assertEqual(meta.number, 3)
        self.assertFalse(opened)

    def test_retry_reuses_existing_remote_pr_when_metadata_missing(self):
        store = FakePublishStore()
        opened = []
        self.pub._git = lambda args, cwd: "c" * 40 if args[:2] == ["git", "rev-parse"] else ""
        self.pub._find_existing_open_pr = lambda *a, **k: {"number": 4, "html_url": "https://remote", "draft": True}
        self.pub._open_draft_pr = lambda *a, **k: opened.append(True) or {"number": 9, "html_url": "dup"}
        meta = self.pub.publish_approved(store, self.settings, "job_1")
        self.assertEqual(meta.number, 4)
        self.assertFalse(opened)

    def test_token_redaction(self):
        text = "Bearer ghs_SECRET token ghs_SECRET https://x-access-token:ghs_SECRET@github.com/o/r.git"
        clean = self.pub._sanitize(text)
        self.assertNotIn("ghs_SECRET", clean)


class SourceLifecycleTests(unittest.TestCase):
    def setUp(self):
        import gnsis.service.executor.source as src

        self.src = src
        self.orig_inst = src._customer_installation_id
        self.orig_gh = src.ExecutorGitHub
        self.settings = types.SimpleNamespace(
            executor_source_max_bytes=5,
            github_app_id="1",
            github_app_private_key="key",
        )
        self.run = types.SimpleNamespace(
            id="exec_1",
            job_id="job_1",
            repository_id="repo_1",
            workspace_id="ws_1",
            base_sha="b" * 40,
        )
        class GH:
            def __init__(self, app):
                pass
            def scoped_installation_token(self, *a, **k):
                return {"token": "ghs_SECRET"}
        src.ExecutorGitHub = GH
        src._customer_installation_id = lambda run: 123

    def tearDown(self):
        self.src._customer_installation_id = self.orig_inst
        self.src.ExecutorGitHub = self.orig_gh

    class Resp:
        def __init__(self, chunks=None, *, headers=None, read_error_at=None):
            self.status = 200
            self.headers = headers or {}
            self.chunks = list(chunks or [])
            self.closed = False
            self.reads = 0
            self.read_error_at = read_error_at
        def read(self, n):
            self.reads += 1
            if self.read_error_at == self.reads:
                raise RuntimeError("read failed token ghs_SECRET")
            return self.chunks.pop(0) if self.chunks else b""
        def close(self):
            self.closed = True

    def test_invalid_installation(self):
        self.src._customer_installation_id = lambda run: None
        with self.assertRaises(self.src.SourceError) as cm:
            self.src.prepare_source(self.settings, self.run, "octo/repo")
        self.assertEqual(cm.exception.status, 409)

    def test_archive_open_failure_sanitizes(self):
        with self.assertRaises(self.src.SourceError) as cm:
            self.src.prepare_source(
                self.settings,
                self.run,
                "octo/repo",
                open_archive=lambda *a: (_ for _ in ()).throw(RuntimeError("open ghs_SECRET")),
            )
        self.assertEqual(cm.exception.status, 502)
        self.assertNotIn("ghs_SECRET", str(cm.exception))

    def test_oversized_content_length_closes(self):
        resp = self.Resp(headers={"Content-Length": "6"})
        with self.assertRaises(self.src.SourceError):
            self.src.prepare_source(self.settings, self.run, "octo/repo", open_archive=lambda *a: resp)
        self.assertTrue(resp.closed)

    def test_failure_before_first_byte_closes_and_sanitizes(self):
        resp = self.Resp(read_error_at=1)
        with self.assertRaises(self.src.SourceError) as cm:
            self.src.prepare_source(self.settings, self.run, "octo/repo", open_archive=lambda *a: resp)
        self.assertTrue(resp.closed)
        self.assertNotIn("ghs_SECRET", str(cm.exception))

    def test_oversized_chunked_stream_closes(self):
        resp = self.Resp(chunks=[b"123", b"456"])
        prepared = self.src.prepare_source(self.settings, self.run, "octo/repo", open_archive=lambda *a: resp)
        with self.assertRaises(self.src.SourceError):
            b"".join(prepared.iter_bytes())
        self.assertTrue(resp.closed)

    def test_mid_stream_failure_fails_run_and_revokes_token(self):
        resp = self.Resp(chunks=[b"12"], read_error_at=2)
        prepared = self.src.prepare_source(self.settings, self.run, "octo/repo", open_archive=lambda *a: resp)
        class ExecStore:
            def __init__(self):
                self.status = None; self.revoked = False
            def set_status(self, run_id, status, failure_category=None):
                self.status = status; self.category = failure_category
            def revoke_token(self, run_id):
                self.revoked = True
        class JobStore:
            def __init__(self):
                self.job = types.SimpleNamespace(status="running")
            def get_job(self, job_id):
                return self.job
            def set_status(self, job_id, status, error=None):
                self.job.status = status; self.job.error = error
        es = ExecStore(); js = JobStore()
        with self.assertRaises(self.src.SourceError) as cm:
            for _ in prepared.iter_bytes():
                pass
        self.src.fail_streaming_source(prepared, es, self.run, js, str(cm.exception))
        self.assertEqual(es.status, ExecutionStatus.FAILED)
        self.assertTrue(es.revoked)
        self.assertEqual(js.job.status, JobStatus.FAILED)
        self.assertNotIn("ghs_SECRET", js.job.error)

    def test_parallel_claim_closes_prepared_source(self):
        resp = self.Resp(chunks=[b"ok"])
        prepared = self.src.prepare_source(self.settings, self.run, "octo/repo", open_archive=lambda *a: resp)
        prepared.close()
        self.assertTrue(resp.closed)


class FakeCIStore:
    def __init__(self):
        self.job = FakeJob()
        self.context = None
        self.logs = []
    def merge_context(self, job_id, updates):
        self.context = updates
        self.job.context = {**self.job.context, **updates}
    def append_log(self, entry):
        self.logs.append(entry)


class FakeExecStore:
    def __init__(self):
        self.receipt = None
    def get_run_for_job(self, job_id):
        return types.SimpleNamespace(id="exec_1")
    def merge_receipt_context(self, run_id, updates):
        self.receipt = updates


class FakeGitHubCI:
    def __init__(self, workflows=None, checks=None, suites=None, statuses=None):
        self.workflows = workflows or []
        self.checks = checks or []
        self.suites = suites or []
        self.statuses = statuses or []
    def commit_workflow_runs(self, *a):
        return {"workflow_runs": self.workflows}
    def commit_check_runs(self, *a):
        return {"check_runs": self.checks}
    def commit_check_suites(self, *a):
        return {"check_suites": self.suites}
    def commit_status(self, *a):
        return {"statuses": self.statuses}


class CIObservationTests(unittest.TestCase):
    def setUp(self):
        import gnsis.service.executor.ci as ci

        self.ci = ci
        self.orig_token = ci._token_for_job
        ci._token_for_job = lambda *a, **k: "token"
        self.settings = types.SimpleNamespace()
        self.pr = PRMetadata(job_id="job_1", number=1, url="https://pr", branch="gnsis/job_1", head_sha="h" * 40)

    def tearDown(self):
        self.ci._token_for_job = self.orig_token

    def observe(self, github):
        store = FakeCIStore(); exec_store = FakeExecStore()
        result = self.ci.observe_job(self.settings, store, exec_store, github, store.job, self.pr)
        self.assertEqual(exec_store.receipt["customer_ci"], result)
        return result

    def test_ci_not_configured(self):
        result = self.observe(FakeGitHubCI())
        self.assertEqual(result["overall"], "not_configured")
        self.assertTrue(result["terminal"])

    def test_ci_pending(self):
        result = self.observe(FakeGitHubCI(checks=[{"name": "build", "status": "queued", "html_url": "u"}]))
        self.assertEqual(result["overall"], "pending")
        self.assertFalse(result["terminal"])

    def test_ci_passed(self):
        result = self.observe(FakeGitHubCI(checks=[{"name": "build", "conclusion": "success"}]))
        self.assertEqual(result["overall"], "passed")

    def test_ci_failed(self):
        result = self.observe(FakeGitHubCI(statuses=[{"context": "ci", "state": "failure"}]))
        self.assertEqual(result["overall"], "failed")

    def test_ci_cancelled(self):
        result = self.observe(FakeGitHubCI(workflows=[{"name": "build", "status": "completed", "conclusion": "cancelled"}]))
        self.assertEqual(result["overall"], "cancelled")


if __name__ == "__main__":
    unittest.main()
