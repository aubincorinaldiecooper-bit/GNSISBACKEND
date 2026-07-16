"""Cancellation — stop a run and make its token useless, idempotently.

User cancellation records the request, revokes the executor token (so no further
model call or callback can succeed), and cancels the exact workflow run. It is
safe to call more than once and finalizes even if the workflow is already gone.
"""

from __future__ import annotations

from typing import Optional

from ..github_app import GitHubApp
from .github import ExecutorGitHub, GitHubHTTPError
from .installation import DISPATCH_PERMISSIONS, resolve_executor_installation
from .models import ExecutionStatus, FailureCategory
from .store import ExecutionStore


def cancel_job_execution(
    settings, job_id: str, *, app: Optional[GitHubApp] = None
) -> bool:
    """Revoke the token and cancel the workflow for ``job_id``'s run."""
    store = ExecutionStore()
    run = store.get_run_for_job(job_id)
    if run is None:
        return False

    store.request_cancellation(run.id)
    store.revoke_token(run.id)

    if run.status not in ExecutionStatus.TERMINAL:
        # Best-effort cancel of the exact workflow run.
        if run.workflow_run_id is not None:
            try:
                app = app or GitHubApp(
                    app_id=settings.github_app_id,
                    private_key=settings.github_app_private_key,
                    installation_id="0",
                )
                github = ExecutorGitHub(app)
                executor_inst = resolve_executor_installation(settings, app, github=github)
                token = github.scoped_installation_token(
                    executor_inst.installation_id,
                    repositories=[run.executor_repository],
                    permissions=DISPATCH_PERMISSIONS,
                )["token"]
                github.cancel_workflow_run(
                    run.executor_owner, run.executor_repository, run.workflow_run_id, token
                )
            except (GitHubHTTPError, Exception):  # noqa: BLE001 - cancellation is best-effort
                pass
        store.set_status(run.id, ExecutionStatus.CANCELLED, failure_category=FailureCategory.CANCELLED)
    return True
