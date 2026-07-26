"""Deterministic classification of customer-repository preflight failures.

Before a job's model or execution can run, GNSIS must resolve the customer's
GitHub installation and the exact base commit to check out. When one of those
prerequisites is missing — an empty repository, a branch that doesn't exist, a
GitHub App installation that no longer covers the repo — the run cannot begin
at all. That is a *blocked* attempt (see :class:`ExecutionStatus.BLOCKED`), not
an execution failure: no token is minted, no workflow is dispatched, no model
runs, so no usage is ever consumed.

Classification is purely deterministic — the GitHub HTTP status code, or the
absence of a resolvable installation — never a heuristic or a model call.

A blocked attempt still leaves the same durable evidence trail a dispatched run
would: an ``ExecutionRun`` row (so the receipt reports real, not-null, zero
values) and a ``preflight_blocked`` event (the technical-details / Activity
evidence), via :func:`record_blocked_run`.
"""

from __future__ import annotations

from typing import Optional

from .github import GitHubHTTPError
from .models import Budgets, ExecutionRunRecord, ExecutionStatus, FailureCategory
from .store import ExecutionStore
from .tokens import hash_secret, new_nonce

#: HTTP status GitHub returns from ``GET .../git/ref/heads/{branch}`` for each
#: known prerequisite-missing condition. Any other status (403, 5xx, network
#: error, ...) is not a known blocked condition and is treated as an ordinary
#: (infrastructure) failure by the caller instead.
_STATUS_TO_REASON = {
    409: FailureCategory.BLOCKED_REPOSITORY_EMPTY,  # "Git Repository is empty."
    404: FailureCategory.BLOCKED_BRANCH_NOT_FOUND,
}


def classify_github_ref_error(exc: GitHubHTTPError) -> Optional[str]:
    """Map a base-branch resolution failure to a blocked reason code.

    Returns ``None`` when the error is not a recognized prerequisite-missing
    condition — the caller should classify it as an ordinary failure instead.
    """
    return _STATUS_TO_REASON.get(exc.status)


def _short_repo_name(repo_full_name: str) -> str:
    return (repo_full_name or "").rsplit("/", 1)[-1] or repo_full_name


def blocked_explanation(reason_code: str, *, repo_full_name: str, branch: str) -> str:
    """The clean, GNSIS-voice sentence shown as the primary explanation.

    Matches the application's existing conversational tone. Never includes raw
    provider text — that is attached separately as the technical detail (see
    :func:`record_blocked_run` and how callers compose ``job.error``).
    """
    repo_name = _short_repo_name(repo_full_name)
    if reason_code == FailureCategory.BLOCKED_REPOSITORY_EMPTY:
        return (
            f"GNSIS couldn't start this run because {repo_name} does not have an "
            "initial commit yet. Without a commit, there is no branch or codebase "
            "for GNSIS to check out. Add a README or another file to initialize "
            "the repository, then retry the run. No model was called and no "
            "balance was used."
        )
    if reason_code == FailureCategory.BLOCKED_BRANCH_NOT_FOUND:
        return (
            f'GNSIS couldn\'t start this run because the branch "{branch}" does '
            f"not exist in {repo_name}. Choose an existing branch, or push that "
            "branch to GitHub, then retry the run. No model was called and no "
            "balance was used."
        )
    if reason_code == FailureCategory.BLOCKED_INSTALLATION_INACCESSIBLE:
        return (
            f"GNSIS couldn't start this run because it no longer has GitHub "
            f"access to {repo_name}. Reconnect the repository through GitHub "
            "access, then retry the run. No model was called and no balance "
            "was used."
        )
    return (
        "GNSIS couldn't start this run because a required prerequisite was "
        "missing. No model was called and no balance was used."
    )


def record_blocked_run(
    settings,
    store: ExecutionStore,
    job,
    *,
    reason_code: str,
    provider_detail: str = "",
) -> ExecutionRunRecord:
    """Create and settle a BLOCKED execution run for a preflight failure.

    Reuses the existing :class:`ExecutionRun` schema unchanged (``base_sha``
    already defaults to ``""``) — no new table, no migration. The dispatch
    nonce is generated but never consumed; the executor identity fields mirror
    what a real dispatch would use, purely for consistency, since this run
    never reaches the executor.

    ``provider_detail`` is the raw (already-length-bounded) GitHub response or
    condition text — recorded on the event as the technical-evidence trail,
    never surfaced as the primary user-facing message.
    """
    run = store.create_run(
        job_id=job.id,
        workspace_id=job.workspace_id,
        repository_id=job.repository_id,
        base_branch=job.base_branch,
        base_sha="",
        dispatch_nonce_hash=hash_secret(new_nonce()),
        executor_owner=settings.executor_owner or "",
        executor_repository=settings.executor_repo or "",
        executor_repository_id=None,
        executor_workflow=settings.executor_workflow,
        executor_ref=settings.executor_ref,
        trusted_workflow_sha=settings.executor_trusted_workflow_sha or "",
        budgets=Budgets(0, 0, 0, 0.0),
    )
    store.set_status(run.id, ExecutionStatus.BLOCKED, failure_category=reason_code)
    store.record_event(
        run.id,
        job_id=job.id,
        workflow_run_attempt=None,
        sequence=0,
        idempotency_key=f"preflight-blocked:{run.id}",
        kind="preflight_blocked",
        payload={
            "reason_code": reason_code,
            "base_branch": job.base_branch,
            "provider_detail": provider_detail[:500],
        },
    )
    return store.get_run(run.id)
