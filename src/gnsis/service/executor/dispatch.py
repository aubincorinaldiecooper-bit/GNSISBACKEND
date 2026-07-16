"""Dispatch a user job into the fixed executor workflow.

The dispatch step is deliberately thin and leaks nothing: it resolves the
executor installation, refuses to run unless the executor's default branch is
*exactly* the audited trusted commit (so the workflow that runs is the one that
was reviewed), mints a token narrowed to ``Actions: write, Contents: read`` on
the single executor repository, creates the durable run record, and dispatches
``execute.yml`` with **only** ``job_id`` and ``dispatch_nonce``.

Everything the job actually needs — instruction, customer repo, base SHA, model,
budgets — is withheld until the workflow authenticates over OIDC and pulls its
spec. The workflow input carries no instruction, repo, SHA, credential or budget.
"""

from __future__ import annotations

from typing import Optional

from ..github_app import GitHubApp
from .github import ExecutorGitHub, GitHubHTTPError
from .installation import DISPATCH_PERMISSIONS, resolve_executor_installation
from .models import Budgets, ExecutionRunRecord, FailureCategory
from .store import ExecutionStore
from .tokens import hash_secret, new_nonce


class DispatchError(RuntimeError):
    def __init__(self, message: str, category: str = FailureCategory.DISPATCH):
        super().__init__(message)
        self.category = category


def run_name_for(job_id: str) -> str:
    """The ``run-name`` the executor workflow sets; used to locate the run."""
    return f"gnsis-run {job_id}"


def budgets_from_settings(settings) -> Budgets:
    return Budgets(
        max_model_calls=settings.run_max_model_calls,
        max_input_tokens=settings.run_max_input_tokens,
        max_output_tokens=settings.run_max_output_tokens,
        max_cost_usd=settings.run_max_cost_usd,
    )


def resolve_base_sha(
    app: GitHubApp,
    *,
    customer_installation_id: int,
    repo_full_name: str,
    base_branch: str,
) -> str:
    """Resolve the immutable base commit SHA for ``base_branch`` (customer repo).

    Uses a short-lived customer installation token that is never persisted.
    """
    owner, _, name = repo_full_name.partition("/")
    token = app.token_for_installation(customer_installation_id)
    github = ExecutorGitHub(app)
    return github.ref_sha(owner, name, base_branch, token)


def dispatch_execution(
    settings,
    store: ExecutionStore,
    *,
    job,
    base_sha: str,
    app: Optional[GitHubApp] = None,
    github: Optional[ExecutorGitHub] = None,
) -> ExecutionRunRecord:
    """Create the run record and dispatch the fixed workflow. Raises on failure."""
    if not settings.execution_provider_valid:
        raise DispatchError("execution provider is not github_actions")
    missing = settings.missing_execution_vars()
    if missing:
        raise DispatchError(f"execution config incomplete: {', '.join(missing)}")

    app = app or GitHubApp(
        app_id=settings.github_app_id,
        private_key=settings.github_app_private_key,
        installation_id="0",
    )
    github = github or ExecutorGitHub(app)

    owner = settings.executor_owner
    repo = settings.executor_repo
    trusted_sha = settings.executor_trusted_workflow_sha

    # 1) Resolve the executor installation (verifies app/active/perms/private).
    executor_inst = resolve_executor_installation(settings, app, github=github)

    # 2) Mint a scope-narrowed dispatch token.
    try:
        token_data = github.scoped_installation_token(
            executor_inst.installation_id,
            repositories=[repo],
            permissions=DISPATCH_PERMISSIONS,
        )
        dispatch_token = token_data["token"]
    except GitHubHTTPError as exc:
        raise DispatchError(f"could not mint dispatch token: {exc}") from exc

    # 3) Refuse to run unless the executor default branch == the audited commit.
    try:
        head_sha = github.ref_sha(owner, repo, settings.executor_ref, dispatch_token)
    except GitHubHTTPError as exc:
        raise DispatchError(f"could not read executor ref: {exc}") from exc
    if head_sha != trusted_sha:
        raise DispatchError(
            f"executor {owner}/{repo}@{settings.executor_ref} head {head_sha} != "
            f"trusted workflow sha {trusted_sha}; re-audit and update "
            "GNSIS_EXECUTOR_TRUSTED_WORKFLOW_SHA before dispatching",
            category=FailureCategory.SECURITY,
        )

    # 4) Create the durable run record (nonce stored only as a hash).
    nonce = new_nonce()
    run = store.create_run(
        job_id=job.id,
        workspace_id=job.workspace_id,
        repository_id=job.repository_id,
        base_branch=job.base_branch,
        base_sha=base_sha,
        dispatch_nonce_hash=hash_secret(nonce),
        executor_owner=owner,
        executor_repository=repo,
        executor_repository_id=executor_inst.repository_id,
        executor_workflow=settings.executor_workflow,
        executor_ref=settings.executor_ref,
        trusted_workflow_sha=trusted_sha,
        budgets=budgets_from_settings(settings),
    )

    # 5) Dispatch the fixed workflow with ONLY job_id + dispatch_nonce.
    try:
        status = github.dispatch_workflow(
            owner,
            repo,
            settings.executor_workflow,
            settings.executor_ref,
            {"job_id": job.id, "dispatch_nonce": nonce},
            dispatch_token,
        )
    except GitHubHTTPError as exc:
        store.set_status(run.id, "failed", failure_category=FailureCategory.DISPATCH)
        raise DispatchError(f"workflow dispatch failed: {exc}") from exc
    if status != 204:
        store.set_status(run.id, "failed", failure_category=FailureCategory.DISPATCH)
        raise DispatchError(f"workflow dispatch returned {status}, expected 204")

    # 6) Best-effort capture the run id via run-name; reconciliation fills gaps.
    workflow_run_id: Optional[int] = None
    workflow_run_attempt: Optional[int] = None
    workflow_run_url: Optional[str] = None
    try:
        found = github.find_run_by_name(
            owner, repo, settings.executor_workflow, run_name_for(job.id), dispatch_token
        )
        if found:
            workflow_run_id = int(found["id"])
            workflow_run_attempt = int(found.get("run_attempt", 1))
            workflow_run_url = found.get("html_url")
    except GitHubHTTPError:
        pass  # not fatal; reconciliation will resolve the run id

    store.mark_dispatched(
        run.id,
        workflow_run_id=workflow_run_id,
        workflow_run_attempt=workflow_run_attempt,
        workflow_run_url=workflow_run_url,
    )
    return store.get_run(run.id)
