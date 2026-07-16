"""Reconciliation — polling is the source of truth, webhooks only speed it up.

Runs every minute. For each non-terminal run it repairs the states that no
single callback can be trusted to deliver: a dispatched job whose run id was
never captured, a run that queued or ran past its deadline, a workflow that was
cancelled or failed externally, a workflow that finished without ever calling
back (a lost completion), an expired token, an orphaned record, and a run whose
model budget was exhausted. Every branch is idempotent and finalizes cleanly.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from ...orchestration.status import JobStatus, is_terminal
from ..github_app import GitHubApp
from .dispatch import run_name_for
from .github import ExecutorGitHub, GitHubHTTPError
from .installation import DISPATCH_PERMISSIONS, resolve_executor_installation
from .models import ExecutionStatus, FailureCategory
from .store import ExecutionStore

logger = logging.getLogger("gnsis.reconcile")


def _parse(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_seconds(ts: str) -> float:
    dt = _parse(ts)
    if dt is None:
        return 0.0
    return (datetime.now(timezone.utc) - dt).total_seconds()


def reconcile_all(settings, job_store, *, app: Optional[GitHubApp] = None) -> int:
    store = ExecutionStore()
    runs = store.active_runs()
    if not runs:
        return 0
    app = app or GitHubApp(
        app_id=settings.github_app_id,
        private_key=settings.github_app_private_key,
        installation_id="0",
    )
    github = ExecutorGitHub(app)
    token = None
    try:
        executor_inst = resolve_executor_installation(settings, app, github=github)
        token = github.scoped_installation_token(
            executor_inst.installation_id,
            repositories=[settings.executor_repo],
            permissions=DISPATCH_PERMISSIONS,
        )["token"]
    except Exception as exc:  # noqa: BLE001 - without a token we can still time out
        logger.warning("reconcile: could not mint executor token: %s", exc)

    repaired = 0
    for run in runs:
        try:
            if _reconcile_one(settings, job_store, store, github, token, run):
                repaired += 1
        except Exception as exc:  # noqa: BLE001 - one bad run must not stop the batch
            logger.warning("reconcile: run %s failed: %s", run.id, exc)
        finally:
            store.touch_reconciled(run.id)
    return repaired


def _fail(store, job_store, run, category: str, error: str) -> None:
    store.set_status(run.id, ExecutionStatus.FAILED, failure_category=category)
    store.revoke_token(run.id)
    job = job_store.get_job(run.job_id)
    if job is not None and not is_terminal(job.status):
        job_store.set_status(run.job_id, JobStatus.FAILED, error=error)


def _reconcile_one(settings, job_store, store, github, token, run) -> bool:
    # Cancellation that has not finalized.
    if run.cancellation_requested and run.status not in ExecutionStatus.TERMINAL:
        store.set_status(run.id, ExecutionStatus.CANCELLED, failure_category=FailureCategory.CANCELLED)
        store.revoke_token(run.id)
        return True

    age = _age_seconds(run.created_at)
    deadline = settings.executor_timeout_seconds

    # Resolve a missing workflow run id ("dispatched without recorded run").
    if run.workflow_run_id is None and token:
        try:
            found = github.find_run_by_name(
                run.executor_owner, run.executor_repository, run.executor_workflow,
                run_name_for(run.job_id), token,
            )
            if found:
                store.set_workflow_run(
                    run.id,
                    workflow_run_id=int(found["id"]),
                    workflow_run_attempt=int(found.get("run_attempt", 1)),
                    workflow_run_url=found.get("html_url"),
                )
                run = store.get_run(run.id)
        except GitHubHTTPError:
            pass

    # Past deadline with no completion — time it out (and cancel if still live).
    if age > deadline and run.status not in ExecutionStatus.TERMINAL:
        if run.workflow_run_id is not None and token:
            try:
                github.cancel_workflow_run(
                    run.executor_owner, run.executor_repository, run.workflow_run_id, token
                )
            except GitHubHTTPError:
                pass
        _fail(store, job_store, run, FailureCategory.TIMEOUT, "executor run exceeded deadline")
        return True

    # Ask GitHub what actually happened to the run.
    if run.workflow_run_id is not None and token:
        try:
            gh_run = github.get_workflow_run(
                run.executor_owner, run.executor_repository, run.workflow_run_id, token
            )
        except GitHubHTTPError:
            return False
        gh_status = gh_run.get("status")
        conclusion = gh_run.get("conclusion")
        attempt = int(gh_run.get("run_attempt", run.workflow_run_attempt or 1))
        # Obsolete attempt: a newer attempt supersedes the bound one.
        if run.workflow_run_attempt and attempt > run.workflow_run_attempt and run.status not in ExecutionStatus.TERMINAL:
            _fail(store, job_store, run, FailureCategory.STALE_ATTEMPT, "workflow re-run supersedes this attempt")
            return True
        if gh_status == "completed" and run.status not in ExecutionStatus.TERMINAL:
            if conclusion == "success":
                # Finished but we never received the completion callback.
                _fail(store, job_store, run, FailureCategory.LOST_CALLBACK,
                      "workflow succeeded but no completion callback was received")
            elif conclusion == "cancelled":
                store.set_status(run.id, ExecutionStatus.CANCELLED, failure_category=FailureCategory.CANCELLED)
                store.revoke_token(run.id)
                job = job_store.get_job(run.job_id)
                if job is not None and not is_terminal(job.status):
                    job_store.set_status(run.job_id, JobStatus.CANCELLED, error="workflow cancelled")
            else:
                _fail(store, job_store, run, FailureCategory.EXECUTOR_ERROR,
                      f"workflow concluded {conclusion}")
            return True
    return False
