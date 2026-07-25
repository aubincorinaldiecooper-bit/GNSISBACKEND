"""The public, versioned GNSIS API (``/v1``).

This is the API-first surface: an external coding agent, IDE, CLI or CI system
drives the full GNSIS lifecycle without the web composer, authenticating with a
Genesis virtual key (``gns_live_…`` / ``gns_test_…``).

It is deliberately a *projection layer*, not a second implementation. Every
route delegates to the machinery that already backs the web application:

* run creation, dispatch and thread/follow-up linkage — :mod:`gnsis.service.api`
  helpers, :class:`~gnsis.service.repository.PostgresJobStore`, the Celery
  ``run_job`` task;
* lifecycle evidence — :func:`gnsis.service.activity.build_lifecycle_events`;
* receipts — :func:`gnsis.service.receipts.build_receipt`;
* repository intelligence — :class:`gnsis.service.codememory.CodeMemory` and
  :class:`gnsis.service.intelligence_lifecycle.IntelligenceLifecycle`.

So a run created here is the *same object* the dashboard shows, with the same
evidence, receipt, approval boundary and intelligence provenance.

Internally a run is still a ``job``; the public contract is run-oriented and
stable (``run_id``), which is why the two names coexist.

Authentication accepts either a Genesis virtual key (external clients) or the
dashboard's session JWT (the reference client), so the existing frontend keeps
working unchanged while the same routes become usable by API clients.
"""

from __future__ import annotations

import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import workspaces as ws
from .settings import get_settings

router = APIRouter(prefix="/v1", tags=["public"])


# -- scopes -------------------------------------------------------------------

class Scope:
    REPOSITORIES_READ = "repositories:read"
    RUNS_CREATE = "runs:create"
    RUNS_READ = "runs:read"
    RUNS_APPROVE = "runs:approve"
    RECEIPTS_READ = "receipts:read"
    INTELLIGENCE_READ = "intelligence:read"


#: The complete public-beta scope set. A key issued before scopes existed
#: (``api_scopes`` NULL) is treated as carrying all of these: such keys are
#: already workspace-bound, so this widens nothing across a tenant boundary and
#: keeps existing gateway keys working.
PUBLIC_SCOPES = frozenset(
    {
        Scope.REPOSITORIES_READ,
        Scope.RUNS_CREATE,
        Scope.RUNS_READ,
        Scope.RUNS_APPROVE,
        Scope.RECEIPTS_READ,
        Scope.INTELLIGENCE_READ,
    }
)


# -- error contract -----------------------------------------------------------

class PublicApiError(Exception):
    """A public API failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class ErrorCode:
    AUTHENTICATION_FAILED = "authentication_failed"
    AUTHORIZATION_FAILED = "authorization_failed"
    REPOSITORY_ACCESS_DENIED = "repository_access_denied"
    INVALID_MODEL = "invalid_model"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    SPENDING_LIMIT_EXCEEDED = "spending_limit_exceeded"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    EXECUTOR_UNAVAILABLE = "executor_unavailable"
    DISPATCH_FAILED = "dispatch_failed"
    RUN_NOT_FOUND = "run_not_found"
    INVALID_RUN_STATE = "invalid_run_state"
    RECEIPT_UNAVAILABLE = "receipt_unavailable"
    INTELLIGENCE_UNAVAILABLE = "intelligence_unavailable"
    INVALID_REQUEST = "invalid_request"


def error_response(exc: PublicApiError, request_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={"error": {"code": exc.code, "message": exc.message, "request_id": request_id}},
        headers={"X-Genesis-Request-Id": request_id},
    )


def new_request_id() -> str:
    return f"req_{uuid.uuid4().hex[:24]}"


# -- authenticated principal --------------------------------------------------

class Principal:
    """Who is calling: always a workspace, plus the scopes they carry.

    ``key_id`` is set for virtual-key callers (audit attribution) and ``None``
    for dashboard-session callers.
    """

    def __init__(self, workspace_id: str, scopes: frozenset, *, key_id: Optional[str] = None,
                 actor: str = "", allowed_models: Optional[List[str]] = None):
        self.workspace_id = workspace_id
        self.scopes = scopes
        self.key_id = key_id
        self.actor = actor or (f"key:{key_id}" if key_id else "session")
        self.allowed_models = allowed_models or []

    def require(self, scope: str) -> None:
        if scope not in self.scopes:
            raise PublicApiError(
                ErrorCode.AUTHORIZATION_FAILED,
                f"this credential is missing the required scope: {scope}",
                status=403,
            )


def _principal_from_virtual_key(settings, presented: str) -> Principal:
    from .virtual_keys import VirtualKeyStore

    key = VirtualKeyStore().authenticate(settings, presented)
    if key is None:
        raise PublicApiError(
            ErrorCode.AUTHENTICATION_FAILED,
            "the API key is invalid, disabled, or expired",
            status=401,
        )
    scopes = frozenset(key.api_scopes) if key.api_scopes else PUBLIC_SCOPES
    return Principal(
        key.workspace_id, scopes, key_id=key.id,
        actor=key.user_id or f"key:{key.id}",
        allowed_models=list(key.allowed_models),
    )


def _principal_from_session(request: Request, token: str) -> Principal:
    """Resolve the dashboard's session JWT to the same Principal shape.

    Imported lazily from :mod:`gnsis.service.api` so the dependency-override
    seam the app already uses for the verifier keeps working in tests.
    """
    from .api import get_verifier
    from .auth import AuthError

    verifier = request.app.dependency_overrides.get(get_verifier, get_verifier)()
    try:
        claims = verifier.verify(token)
    except AuthError as exc:
        raise PublicApiError(ErrorCode.AUTHENTICATION_FAILED, exc.message, status=401) from exc
    subject = claims.get("sub") or ""
    if not subject:
        raise PublicApiError(ErrorCode.AUTHENTICATION_FAILED, "token has no subject", status=401)
    workspace = ws.get_or_create_workspace(subject)
    # A signed-in dashboard user holds every public-beta scope for their own
    # workspace; scope narrowing is a property of issued API keys.
    return Principal(workspace.id, PUBLIC_SCOPES, actor=subject)


def current_principal(request: Request, authorization: Optional[str] = Header(default=None)) -> Principal:
    """Authenticate a public-API caller: Genesis virtual key or session JWT."""
    settings = get_settings()
    if not authorization:
        raise PublicApiError(
            ErrorCode.AUTHENTICATION_FAILED,
            "an Authorization header with a Genesis API key is required",
            status=401,
        )
    parts = authorization.split(" ", 1)
    presented = parts[1].strip() if len(parts) == 2 and parts[0].lower() == "bearer" else authorization.strip()
    if presented.startswith("gns_"):
        return _principal_from_virtual_key(settings, presented)
    return _principal_from_session(request, presented)


# -- shared resolution --------------------------------------------------------

def _require_repository(workspace_id: str, repository_id: str):
    """The repository, only if this workspace can currently run against it."""
    repo = ws.get_repository(workspace_id, repository_id)
    if repo is None or not repo.enabled:
        raise PublicApiError(
            ErrorCode.REPOSITORY_ACCESS_DENIED,
            "the API key cannot access this repository.",
            status=404,
        )
    inst = ws.get_installation_by_record_id(repo.github_installation_record_id)
    if inst is None or inst.status == "deleted":
        raise PublicApiError(
            ErrorCode.REPOSITORY_ACCESS_DENIED,
            "the GitHub installation for this repository is unavailable",
            status=409,
        )
    if inst.status == "suspended":
        raise PublicApiError(
            ErrorCode.REPOSITORY_ACCESS_DENIED,
            "the GitHub installation for this repository is suspended",
            status=409,
        )
    return repo


def _require_run(workspace_id: str, run_id: str):
    """The run (job), only if it belongs to this workspace. 404 otherwise."""
    from .repository import PostgresJobStore

    job = PostgresJobStore().get_job(run_id)
    if job is None or job.workspace_id != workspace_id:
        raise PublicApiError(ErrorCode.RUN_NOT_FOUND, "run not found", status=404)
    return job


def _require_execution_configured() -> None:
    settings = get_settings()
    if not settings.execution_provider_valid or settings.missing_execution_vars():
        raise PublicApiError(
            ErrorCode.EXECUTOR_UNAVAILABLE,
            "execution is not configured on this deployment",
            status=503,
        )


def _capture_intelligence(job_store, run_id: str, approval_id) -> None:
    """Derive approved repository intelligence. Never fails the approval.

    Intelligence is an enhancement layered on top of a decision that has already
    been recorded; if extraction breaks, the approval must still stand.
    """
    if approval_id is None:
        return
    try:
        from .intelligence_lifecycle import IntelligenceLifecycle

        IntelligenceLifecycle(jobs=job_store).capture_on_approval(
            job_id=run_id, approval_id=approval_id
        )
    except Exception:  # noqa: BLE001 - approval already committed; never roll it back
        import logging

        logging.getLogger("gnsis.public_api").exception(
            "intelligence capture failed for run %s", run_id
        )


def _resolve_models(principal: Principal, model: Optional[str], advisor_model: Optional[str]):
    """Validate the primary + optional Advisor against the server allowlist.

    The Advisor stays ``None`` when the caller omits it — a run without an
    Advisor is a first-class configuration, never silently defaulted.
    """
    from .model_catalog import resolve_allowed_model

    settings = get_settings()
    if not model:
        raise PublicApiError(ErrorCode.INVALID_MODEL, "model is required", status=422)
    selected = resolve_allowed_model(settings, model)
    if selected is None:
        raise PublicApiError(
            ErrorCode.INVALID_MODEL, f"model '{model}' is not available", status=422
        )
    # A key may further narrow which models it can use.
    if principal.allowed_models and selected not in principal.allowed_models:
        raise PublicApiError(
            ErrorCode.AUTHORIZATION_FAILED,
            f"model not allowed for this key: {selected}",
            status=403,
        )
    selected_advisor = None
    if advisor_model:
        selected_advisor = resolve_allowed_model(settings, advisor_model)
        if selected_advisor is None:
            raise PublicApiError(
                ErrorCode.INVALID_MODEL,
                f"advisor_model '{advisor_model}' is not available",
                status=422,
            )
    return selected, selected_advisor


# -- public shapes ------------------------------------------------------------

class CreateRunRequest(BaseModel):
    repository_id: str
    instruction: str
    base_branch: Optional[str] = None
    model: str
    advisor_model: Optional[str] = None
    metadata: Optional[dict] = None


class FollowUpRequest(BaseModel):
    instruction: Optional[str] = None


class DecisionRequest(BaseModel):
    note: str = ""


class IntelligenceQueryRequest(BaseModel):
    task: str
    limit: int = Field(default=10, ge=1, le=50)


def run_view(job) -> dict:
    """The stable public run object. Never leaks internal credentials."""
    return {
        "id": job.id,
        "object": "run",
        "repository_id": job.repository_id,
        "repository": job.repo,
        "branch": job.base_branch,
        "instruction": job.instruction,
        "model": job.model,
        "advisor_model": job.advisor_model,
        "status": job.status,
        "error": job.error,
        "thread_id": getattr(job, "thread_id", None) or job.id,
        "parent_run_id": getattr(job, "parent_job_id", None),
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


#: Job statuses in which an executor genuinely started doing work. Used to state
#: ``execution_started`` truthfully on the receipt.
_EXECUTION_STARTED_STATUSES = frozenset(
    {"planning", "patching", "testing", "summarizing", "awaiting_approval",
     "approved", "publishing", "completed", "rejected"}
)


def _public_receipt_view(receipt: dict, job) -> dict:
    """Re-shape the internal receipt for the public run-oriented contract.

    Internally ``run_id`` names the *execution* run; publicly a "run" is the job.
    Renaming here (rather than in :mod:`gnsis.service.receipts`) keeps the
    existing dashboard contract byte-for-byte stable while the public API stays
    coherent for external clients.

    Also states plainly whether execution ever started, so a client can tell a
    pre-execution block apart from a mid-execution failure without inferring it.
    """
    view = dict(receipt)
    view["execution_run_id"] = receipt.get("run_id")
    view["run_id"] = receipt.get("job_id")
    view["object"] = "receipt"
    started = job.status in _EXECUTION_STARTED_STATUSES or bool(receipt.get("model_calls"))
    view["execution_started"] = bool(started)
    if not started:
        # Terminal before execution: these are known-zero, never "unavailable".
        view["tests"] = view.get("tests") or "not_run"
    return view


def _page(items: List[dict], limit: int, offset: int) -> dict:
    window = items[offset: offset + limit]
    return {
        "object": "list",
        "data": window,
        "has_more": (offset + len(window)) < len(items),
        "total": len(items),
        "limit": limit,
        "offset": offset,
    }


# -- runs ---------------------------------------------------------------------

@router.post("/runs")
def create_run(
    req: CreateRunRequest,
    principal: Principal = Depends(current_principal),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    """Create and dispatch a run. Repository, model, policy and intelligence are
    all resolved server-side; the client never supplies credentials."""
    from ..orchestration.models import JobSpec
    from .repository import PostgresJobStore

    principal.require(Scope.RUNS_CREATE)
    _require_execution_configured()
    repo = _require_repository(principal.workspace_id, req.repository_id)
    selected_model, selected_advisor = _resolve_models(principal, req.model, req.advisor_model)

    instruction = (req.instruction or "").strip()
    if not instruction:
        raise PublicApiError(ErrorCode.INVALID_REQUEST, "instruction is required", status=422)

    db = PostgresJobStore()
    key = (idempotency_key or "").strip() or None
    if key:
        existing = db.find_by_idempotency_key(principal.workspace_id, key)
        if existing is not None:
            # A replay returns the original run and never bills a second one.
            return run_view(existing)

    settings = get_settings()
    spec = JobSpec(
        repo=repo.full_name,
        instruction=instruction,
        base_branch=req.base_branch or repo.default_branch or settings.default_base_branch,
        engine=settings.default_engine,
        model=selected_model,
        advisor_model=selected_advisor,
        workspace_id=principal.workspace_id,
        repository_id=repo.id,
        idempotency_key=key,
        context={"client_metadata": dict(req.metadata)} if req.metadata else {},
    )
    try:
        job = db.create_job(spec)
    except Exception as exc:  # noqa: BLE001 - unique idempotency index lost a race
        existing = db.find_by_idempotency_key(principal.workspace_id, key) if key else None
        if existing is not None:
            return run_view(existing)
        raise PublicApiError(ErrorCode.DISPATCH_FAILED, f"could not create run: {exc}") from exc

    from .tasks import run_job

    run_job.delay(job.id)
    return run_view(job)


@router.get("/runs")
def list_runs(
    limit: int = 20,
    offset: int = 0,
    principal: Principal = Depends(current_principal),
) -> dict:
    from .repository import PostgresJobStore

    principal.require(Scope.RUNS_READ)
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    jobs = [
        j for j in PostgresJobStore().list_jobs(limit=500)
        if j.workspace_id == principal.workspace_id
    ]
    return _page([run_view(j) for j in jobs], limit, offset)


@router.get("/runs/{run_id}")
def get_run(run_id: str, principal: Principal = Depends(current_principal)) -> dict:
    principal.require(Scope.RUNS_READ)
    return run_view(_require_run(principal.workspace_id, run_id))


@router.get("/runs/{run_id}/events")
def get_run_events(
    run_id: str,
    limit: int = 100,
    offset: int = 0,
    principal: Principal = Depends(current_principal),
) -> dict:
    """The run's ordered lifecycle events, including pre-execution activity."""
    from .activity import build_lifecycle_events

    principal.require(Scope.RUNS_READ)
    _require_run(principal.workspace_id, run_id)
    limit = max(1, min(int(limit), 500))
    return _page(build_lifecycle_events(run_id), limit, max(0, int(offset)))


@router.get("/runs/{run_id}/receipt")
def get_run_receipt(run_id: str, principal: Principal = Depends(current_principal)) -> dict:
    """The settled receipt. Every terminal attempt has one, including a run that
    was blocked before execution began."""
    from .receipts import build_receipt

    principal.require(Scope.RECEIPTS_READ)
    job = _require_run(principal.workspace_id, run_id)
    receipt = build_receipt(principal.workspace_id, run_id)
    if receipt is None:
        raise PublicApiError(ErrorCode.RECEIPT_UNAVAILABLE, "receipt unavailable", status=404)
    return _public_receipt_view(receipt, job)


@router.post("/runs/{run_id}/approve")
def approve_run(
    run_id: str,
    req: DecisionRequest,
    principal: Principal = Depends(current_principal),
) -> dict:
    """Approve a run. Idempotent: approving an already-approved run returns it
    unchanged rather than re-deciding or re-publishing.

    Approval is the trust boundary for reusable repository intelligence: the
    intelligence extraction runs from the publish path this triggers.
    """
    from ..orchestration.models import Approval
    from ..orchestration.status import JobStatus
    from .repository import PostgresJobStore

    principal.require(Scope.RUNS_APPROVE)
    job = _require_run(principal.workspace_id, run_id)
    db = PostgresJobStore()

    if job.status in (JobStatus.APPROVED, JobStatus.PUBLISHING, JobStatus.COMPLETED):
        return run_view(job)
    if job.status != JobStatus.AWAITING_APPROVAL:
        raise PublicApiError(
            ErrorCode.INVALID_RUN_STATE,
            f"run is '{job.status}', not awaiting approval",
            status=409,
        )

    from .executor.approval import build_binding
    from .executor.store import ExecutionStore
    from .executor.validation import sha256_text

    run = ExecutionStore().get_run_for_job(run_id)
    if run is None or not run.patch_sha256:
        raise PublicApiError(
            ErrorCode.INVALID_RUN_STATE, "no validated execution to approve", status=409
        )
    diff = db.get_diff(run_id)
    if diff is None or sha256_text(diff.patch) != run.patch_sha256:
        raise PublicApiError(
            ErrorCode.INVALID_RUN_STATE, "stored patch does not match validated run", status=409
        )

    repo = ws.get_repository(principal.workspace_id, job.repository_id) if job.repository_id else None
    binding = build_binding(
        job=job, run=run, repo=repo,
        installation_record_id=repo.github_installation_record_id if repo else None,
        actor=principal.actor,
        verification=run.security_validation or "passed",
        ttl_seconds=get_settings().executor_token_ttl_seconds * 4,
        patch_sha256=run.patch_sha256,
    )
    approval = db.save_approval(
        Approval(job_id=run_id, decision="approved", actor=principal.actor, note=req.note)
    )
    db.merge_context(run_id, {"approval_binding": binding, "approval_id": approval.id})
    job = db.set_status(run_id, JobStatus.APPROVED)

    # Approval is the trust boundary for reusable intelligence: capture it here
    # rather than waiting on the publish task. Idempotent with the publish-time
    # extraction, so the two can never double-write.
    _capture_intelligence(db, run_id, approval.id)

    from .tasks import publish_pr

    publish_pr.delay(run_id)
    return run_view(job)


@router.post("/runs/{run_id}/reject")
def reject_run(
    run_id: str,
    req: DecisionRequest,
    principal: Principal = Depends(current_principal),
) -> dict:
    """Reject a run. Idempotent. A rejected run never yields active
    intelligence — its lesson is recorded as a rejection lesson instead."""
    from ..orchestration.pipeline import reject_job
    from ..orchestration.status import JobStatus
    from .repository import PostgresJobStore

    principal.require(Scope.RUNS_APPROVE)
    job = _require_run(principal.workspace_id, run_id)
    db = PostgresJobStore()

    if job.status == JobStatus.REJECTED:
        return run_view(job)
    if job.status != JobStatus.AWAITING_APPROVAL:
        raise PublicApiError(
            ErrorCode.INVALID_RUN_STATE,
            f"run is '{job.status}', not awaiting approval",
            status=409,
        )
    reject_job(db, run_id, actor=principal.actor, note=req.note)
    return run_view(db.get_job(run_id))


@router.post("/runs/{run_id}/follow-ups")
def create_follow_up_run(
    run_id: str,
    req: FollowUpRequest,
    principal: Principal = Depends(current_principal),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    """Create a new immutable run linked into the parent's thread.

    The client supplies only the new instruction; workspace, repository, branch,
    models, policy and thread identity are resolved authoritatively from the
    parent. The parent is never mutated, resumed, or appended to.
    """
    from ..orchestration.models import JobSpec
    from .repository import PostgresJobStore

    principal.require(Scope.RUNS_CREATE)
    _require_execution_configured()
    parent = _require_run(principal.workspace_id, run_id)
    repo = _require_repository(principal.workspace_id, parent.repository_id)

    raw = req.instruction if req.instruction is not None else parent.instruction
    instruction = (raw or "").strip()
    if not instruction:
        raise PublicApiError(ErrorCode.INVALID_REQUEST, "instruction is required", status=422)

    db = PostgresJobStore()
    key = (idempotency_key or "").strip() or None
    if key:
        existing = db.find_by_idempotency_key(principal.workspace_id, key)
        if existing is not None:
            return run_view(existing)

    spec = JobSpec(
        repo=repo.full_name,
        instruction=instruction,
        base_branch=parent.base_branch,
        engine=parent.engine,
        model=parent.model,
        advisor_model=parent.advisor_model,
        workspace_id=principal.workspace_id,
        repository_id=repo.id,
        thread_id=parent.thread_id,
        parent_job_id=parent.id,
        idempotency_key=key,
        context={
            "thread": {
                "thread_id": parent.thread_id,
                "parent_job_id": parent.id,
                "parent_status": parent.status,
            }
        },
    )
    job = db.create_job(spec)

    from .tasks import run_job

    run_job.delay(job.id)
    return run_view(job)


# -- repository intelligence --------------------------------------------------

def _intelligence_view(record) -> dict:
    """The public intelligence object. Workspace id is deliberately omitted —
    it is internal tenancy, not client-facing information."""
    return {
        "id": record.memory_id,
        "object": "intelligence",
        "repository_id": record.repository_id,
        "content": record.content,
        "type": record.kind,
        "status": "active",
        "source_run_id": record.source_job_id,
        "created_at": record.created_at,
    }


@router.get("/repositories/{repository_id}/intelligence")
def list_repository_intelligence(
    repository_id: str,
    limit: int = 20,
    offset: int = 0,
    principal: Principal = Depends(current_principal),
) -> dict:
    """Active, approved intelligence for one repository, newest first."""
    from .repository import PostgresMemoryProvider

    principal.require(Scope.INTELLIGENCE_READ)
    repo = _require_repository(principal.workspace_id, repository_id)
    limit = max(1, min(int(limit), 100))

    # Tenant-strict read: scoped to this workspace's rows for this repository.
    records = PostgresMemoryProvider().recent_scoped(
        repo=repo.full_name,
        workspace_id=principal.workspace_id,
        repository_id=repo.id,
        limit=500,
    )
    return _page([_intelligence_view(r) for r in records], limit, max(0, int(offset)))


@router.post("/repositories/{repository_id}/intelligence/query")
def query_repository_intelligence(
    repository_id: str,
    req: IntelligenceQueryRequest,
    principal: Principal = Depends(current_principal),
) -> dict:
    """Candidate intelligence for a task, using the same deterministic ranker
    that selects context at dispatch — so a client can preview exactly what a
    run would receive. Scope is enforced server-side regardless of the query."""
    from .codememory import CodeMemory

    principal.require(Scope.INTELLIGENCE_READ)
    repo = _require_repository(principal.workspace_id, repository_id)

    selection = CodeMemory().retrieve_for_task(
        repo=repo.full_name,
        instruction=req.task,
        workspace_id=principal.workspace_id,
        repository_id=repo.id,
        limit=req.limit,
    )
    return {
        "object": "list",
        "data": selection.to_public_dicts(),
        "total_available": selection.total_available,
        "truncated": selection.truncated,
    }
