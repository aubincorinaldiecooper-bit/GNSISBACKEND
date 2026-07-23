"""FastAPI app — thin HTTP surface over the job store and the queue.

Every endpoint either reads Postgres or enqueues a Celery task. **No evolution or
generation work runs in a request handler** — creating a job returns immediately
with a queued record; the worker does the long work.

Authentication: user-facing routes require a short-lived Better Auth JWT
(verified against the auth service's JWKS). Identity comes only from the verified
token — never from a request body. Every user route is scoped to the caller's
personal workspace, so one user can never read or act on another's data. The
legacy ``GNSIS_API_KEY`` remains only as an internal/emergency path
(``/internal/*``); it is not an end-user identity.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from ..orchestration.models import Approval, JobSpec
from ..orchestration.pipeline import reject_job
from ..orchestration.status import JobStatus, is_terminal
from . import installations as installations_svc
from . import workspaces as ws
from . import webhooks as webhooks_svc
from .auth import AuthedUser, AuthError, JwksCache, JwtVerifier, bearer_token
from .auth_client import AuthServiceClient, InstallationVerificationError
from .executor.api import router as executor_router
from .github_app import app_from_settings
from .repository import PostgresJobStore
from .settings import get_settings
from .ui import INDEX_HTML
from .workspaces import WorkspaceConflictError, WorkspaceRecord


def _cors_origins() -> list:
    try:
        settings = get_settings()
        if settings.frontend_url:
            return [settings.frontend_url]
        return settings.cors_origins
    except Exception:
        return ["*"]


@asynccontextmanager
async def _lifespan(_app: "FastAPI"):
    from .db import init_db

    init_db()
    # In production (Postgres), fail startup if required API-role config —
    # including the public-beta execution settings — is missing. In dev/tests
    # (SQLite) only warn, so the suite can boot without full production config.
    settings = get_settings()
    missing = settings.missing_production_vars(role="api")
    if missing:
        import logging

        message = (
            "GNSIS API missing required settings: " + ", ".join(missing)
        )
        if settings.is_production:
            raise RuntimeError(message)
        logging.getLogger("gnsis").warning(
            "%s (user routes will reject requests until these are set)", message
        )
    yield


app = FastAPI(title="GNSIS", version="0.2.0", lifespan=_lifespan)

_allow_origins = _cors_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allow_origins,
    allow_credentials="*" not in _allow_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# The internal executor + model-gateway surface (OIDC-/run-token-authenticated).
app.include_router(executor_router)

# The internal LiteLLM usage callback (shared-secret authenticated).
from .usage_api import router as usage_router  # noqa: E402

app.include_router(usage_router)

# The Stripe prepaid-refill webhook (signature-verified).
from .stripe_webhook import router as stripe_router  # noqa: E402

app.include_router(stripe_router)

# The public OpenAI-compatible gateway (Genesis virtual-key authenticated).
from .public_gateway import router as public_gateway_router  # noqa: E402

app.include_router(public_gateway_router)


# -- dependency providers (overridable in tests) ------------------------------

_verifier: Optional[JwtVerifier] = None


def get_verifier() -> JwtVerifier:
    settings = get_settings()
    if not settings.user_auth_enabled:
        raise HTTPException(status_code=503, detail="user authentication is not configured")
    global _verifier
    if _verifier is None:
        cache = JwksCache(url=settings.better_auth_jwks_url)
        _verifier = JwtVerifier(
            cache,
            issuer=settings.better_auth_issuer,
            audience=settings.better_auth_audience,
        )
    return _verifier


def get_auth_client() -> AuthServiceClient:
    settings = get_settings()
    if not settings.installation_verification_enabled:
        raise HTTPException(status_code=503, detail="installation verification is not configured")
    return AuthServiceClient(
        base_url=settings.auth_internal_url,
        internal_secret=settings.auth_internal_secret,
    )


def get_github_app():
    settings = get_settings()
    if not (settings.github_app_id and settings.github_app_private_key):
        raise HTTPException(status_code=503, detail="GitHub App credentials are not configured")
    return app_from_settings(settings)


def current_user(
    authorization: Optional[str] = Header(default=None),
    verifier: JwtVerifier = Depends(get_verifier),
) -> AuthedUser:
    try:
        token = bearer_token(authorization)
        return verifier.verify(token)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message)


def current_workspace(user: AuthedUser = Depends(current_user)) -> WorkspaceRecord:
    """The caller's personal workspace, created on first authenticated request."""
    return ws.get_or_create_workspace(user.subject)


def require_internal_key(authorization: Optional[str] = Header(default=None)) -> None:
    """Guard for internal/emergency routes. Not an end-user identity."""
    settings = get_settings()
    if not settings.api_key:
        raise HTTPException(status_code=503, detail="internal API is not enabled")
    if authorization != f"Bearer {settings.api_key}":
        raise HTTPException(status_code=401, detail="invalid or missing internal API key")


def store() -> PostgresJobStore:
    return PostgresJobStore()


def _memory():
    from ..memory.base import NullMemoryProvider

    if get_settings().memory_backend == "postgres":
        from .repository import PostgresMemoryProvider

        return PostgresMemoryProvider()
    return NullMemoryProvider()


def _require_execution_configured(settings) -> None:
    """Reject job creation unless the fixed GitHub Actions provider is configured."""
    if not settings.execution_provider_valid or settings.missing_execution_vars():
        raise HTTPException(
            status_code=503,
            detail="public-beta execution is not configured (GNSIS_EXECUTION_PROVIDER=github_actions)",
        )


# -- schemas ------------------------------------------------------------------


class CreateJobRequest(BaseModel):
    repository_id: str
    instruction: str
    base_branch: Optional[str] = None
    # The user-selected OpenRouter model. Validated against the server allowlist;
    # an unsupported model is rejected. Omitted → the configured default.
    model: Optional[str] = None
    # Deprecated: internal framework choice, no longer surfaced to users. Ignored
    # for model selection; kept so old clients don't 422.
    engine: Optional[str] = None


class InternalCreateJobRequest(BaseModel):
    repo: str = Field(..., examples=["owner/name"])
    instruction: str
    base_branch: Optional[str] = None
    engine: Optional[str] = None


class JobResponse(BaseModel):
    id: str
    repo: str
    instruction: str
    base_branch: str
    engine: str
    model: Optional[str] = None
    status: str
    branch: Optional[str]
    error: Optional[str]
    created_at: str
    updated_at: str
    usage: dict = Field(default_factory=dict)


class LogResponse(BaseModel):
    phase: str
    level: str
    message: str
    created_at: str


class ApproveRequest(BaseModel):
    note: str = ""


class ClaimRequest(BaseModel):
    installation_id: int


# -- health / meta ------------------------------------------------------------


@app.get("/", include_in_schema=False)
def _root() -> RedirectResponse:
    return RedirectResponse(url="/ui")


@app.get("/ui", response_class=HTMLResponse, include_in_schema=False)
def _ui() -> str:
    return INDEX_HTML


@app.get("/health")
def health() -> dict:
    # Never expose secrets or their values — only whether subsystems are wired.
    settings = get_settings()
    return {
        "status": "ok",
        "user_auth": settings.user_auth_enabled,
        "github_app": bool(settings.github_app_id and settings.github_app_private_key),
    }


AVAILABLE_ENGINES = [
    {"id": "claude", "label": "Claude Agent SDK"},
    {"id": "gnsis", "label": "GNSIS (OpenRouter, native)"},
    {"id": "openhands", "label": "OpenHands"},
]


@app.get("/engines")
def list_engines() -> list:
    return AVAILABLE_ENGINES


# -- identity + workspace -----------------------------------------------------


@app.get("/v1/me")
def me(
    user: AuthedUser = Depends(current_user),
    workspace: WorkspaceRecord = Depends(current_workspace),
) -> dict:
    installs = ws.list_installations(workspace.id)
    active = [i for i in installs if i.status == "active"]
    # repository_count = repos GNSIS can *see* (GitHub App access), independent
    # of enablement; enabled_repository_count = repos the user opted into for runs.
    repos = ws.list_repositories(workspace.id, include_disabled=True)
    enabled_repos = [r for r in repos if r.enabled]
    return {
        "user": {
            "id": user.subject,
            "email": user.email,
            "name": user.name,
            "avatar_url": user.avatar_url,
        },
        "workspace": {"id": workspace.id, "name": workspace.name},
        "github": {
            "connected": len(active) > 0,
            "installation_count": len(active),
            "repository_count": len(repos),
            "enabled_repository_count": len(enabled_repos),
        },
    }


# -- virtual keys (Genesis-native scoped inference credentials) ----------------


class CreateVirtualKeyRequest(BaseModel):
    name: str = Field(default="", description="Human label for the key.")
    mode: str = Field(default="live", description="\"live\" or \"test\".")
    project_id: Optional[str] = None
    environment_id: Optional[str] = None
    user_id: Optional[str] = None
    team_id: Optional[str] = None
    allowed_providers: Optional[List[str]] = None
    allowed_models: Optional[List[str]] = None
    soft_limit: Optional[str] = None
    hard_limit: Optional[str] = None
    per_run_limit: Optional[str] = None
    daily_limit: Optional[str] = None
    monthly_limit: Optional[str] = None
    expires_at: Optional[str] = None
    metadata: Optional[dict] = None


@app.post("/v1/virtual-keys")
def create_virtual_key(
    req: CreateVirtualKeyRequest,
    workspace: WorkspaceRecord = Depends(current_workspace),
) -> dict:
    """Issue a Genesis-native virtual key. The secret is returned **once**."""
    from .virtual_keys import VirtualKeyError, VirtualKeyStore

    settings = get_settings()
    try:
        view, secret = VirtualKeyStore().create(
            settings,
            workspace_id=workspace.id,
            name=req.name, mode=req.mode,
            project_id=req.project_id, environment_id=req.environment_id,
            user_id=req.user_id, team_id=req.team_id,
            allowed_providers=req.allowed_providers, allowed_models=req.allowed_models,
            soft_limit=req.soft_limit, hard_limit=req.hard_limit,
            per_run_limit=req.per_run_limit, daily_limit=req.daily_limit,
            monthly_limit=req.monthly_limit, expires_at=req.expires_at, metadata=req.metadata,
        )
    except VirtualKeyError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message)
    return {
        "key": secret,
        "virtual_key": asdict(view),
        "warning": "Store this key now — it will not be shown again.",
    }


@app.get("/v1/virtual-keys")
def list_virtual_keys(
    workspace: WorkspaceRecord = Depends(current_workspace),
) -> dict:
    from .virtual_keys import VirtualKeyStore

    # User-facing list shows ACTIVE keys only. Rotated/disabled rows remain
    # stored for usage attribution, audit, and reconciliation, but are never
    # shown in the normal Settings key list.
    keys = VirtualKeyStore().list_for_workspace(workspace.id, active_only=True)
    return {"items": [asdict(k) for k in keys]}


@app.get("/v1/virtual-keys/{key_id}")
def get_virtual_key(
    key_id: str,
    workspace: WorkspaceRecord = Depends(current_workspace),
) -> dict:
    from .virtual_keys import VirtualKeyStore

    view = VirtualKeyStore().get(workspace.id, key_id)
    if view is None:
        raise HTTPException(status_code=404, detail="virtual key not found")
    return asdict(view)


@app.post("/v1/virtual-keys/{key_id}/disable")
def disable_virtual_key(
    key_id: str,
    workspace: WorkspaceRecord = Depends(current_workspace),
) -> dict:
    from .virtual_keys import VirtualKeyError, VirtualKeyStore

    try:
        view = VirtualKeyStore().disable(workspace.id, key_id)
    except VirtualKeyError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message)
    return asdict(view)


@app.post("/v1/virtual-keys/{key_id}/rotate")
def rotate_virtual_key(
    key_id: str,
    workspace: WorkspaceRecord = Depends(current_workspace),
) -> dict:
    """Retire a key and issue a successor with the same scopes (secret shown once)."""
    from .virtual_keys import VirtualKeyError, VirtualKeyStore

    try:
        view, secret = VirtualKeyStore().rotate(get_settings(), workspace.id, key_id)
    except VirtualKeyError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message)
    return {
        "key": secret,
        "virtual_key": asdict(view),
        "warning": "Store this key now — it will not be shown again.",
    }


# -- usage events (read) -------------------------------------------------------


@app.get("/v1/usage-events")
def list_usage_events(
    limit: int = 50,
    workspace: WorkspaceRecord = Depends(current_workspace),
) -> dict:
    """Recent usage events for the caller's workspace. Provider cost and Genesis
    cost are separate fields; a ``reconciliation_state`` surfaces unpriced rows.
    (Cursor pagination + richer filters land with the REST standardization PR.)"""
    from .usage import UsageStore

    limit = max(1, min(limit, 200))
    items = UsageStore().list_for_workspace(workspace.id, limit=limit)
    return {"items": [asdict(i) for i in items]}


# -- versioned model pricing ---------------------------------------------------


class AddPricingRequest(BaseModel):
    provider: str
    model: str
    input_price: str = Field(..., description="Per-token input price, decimal string.")
    output_price: str = Field(..., description="Per-token output price, decimal string.")
    cached_input_price: Optional[str] = None
    reasoning_price: Optional[str] = None
    currency: str = "USD"
    effective_start: Optional[str] = Field(default=None, description="ISO-8601; default now.")
    source: Optional[str] = None


@app.get("/v1/pricing")
def list_pricing(
    provider: Optional[str] = None,
    user: AuthedUser = Depends(current_user),
) -> dict:
    """The currently-effective rate card (authenticated read; not workspace-scoped)."""
    from .pricing import PricingStore

    return {"items": [asdict(p) for p in PricingStore().list_current(provider)]}


@app.post("/v1/pricing", dependencies=[Depends(require_internal_key)])
def add_pricing(req: AddPricingRequest) -> dict:
    """Publish a new price version (admin, internal key). Closes the prior open
    window for this provider/model so history is preserved, never overwritten."""
    from datetime import datetime

    from .pricing import PricingError, PricingStore

    start = None
    if req.effective_start:
        try:
            start = datetime.fromisoformat(req.effective_start.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=422, detail="effective_start must be ISO-8601")
    try:
        view = PricingStore().add_price(
            provider=req.provider, model=req.model,
            input_price=req.input_price, output_price=req.output_price,
            cached_input_price=req.cached_input_price, reasoning_price=req.reasoning_price,
            currency=req.currency, effective_start=start, source=req.source,
        )
    except PricingError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message)
    return asdict(view)


# -- spending limits + balances ------------------------------------------------


class CreateLimitRequest(BaseModel):
    scope_type: str
    scope_id: str
    limit_type: str
    amount: str
    enforcement_mode: str = "block"
    warning_threshold: Optional[str] = None
    reset_period: Optional[str] = None
    currency: str = "USD"
    effective_at: Optional[str] = None
    expires_at: Optional[str] = None


class UpdateLimitRequest(BaseModel):
    enabled: Optional[bool] = None
    amount: Optional[str] = None
    warning_threshold: Optional[str] = None
    enforcement_mode: Optional[str] = None
    expires_at: Optional[str] = None


def _parse_dt(value: Optional[str]):
    if not value:
        return None
    from datetime import datetime

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail="timestamp must be ISO-8601")


@app.post("/v1/limits")
def create_limit(
    req: CreateLimitRequest,
    workspace: WorkspaceRecord = Depends(current_workspace),
) -> dict:
    from .limits import LimitError, LimitStore

    try:
        view = LimitStore().create(
            workspace_id=workspace.id, scope_type=req.scope_type, scope_id=req.scope_id,
            limit_type=req.limit_type, amount=req.amount, enforcement_mode=req.enforcement_mode,
            warning_threshold=req.warning_threshold, reset_period=req.reset_period,
            currency=req.currency, effective_at=_parse_dt(req.effective_at),
            expires_at=_parse_dt(req.expires_at),
        )
    except LimitError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message)
    return asdict(view)


@app.get("/v1/limits")
def list_limits(
    workspace: WorkspaceRecord = Depends(current_workspace),
) -> dict:
    from .limits import LimitStore

    return {"items": [asdict(p) for p in LimitStore().list_for_workspace(workspace.id)]}


@app.patch("/v1/limits/{limit_id}")
def update_limit(
    limit_id: str,
    req: UpdateLimitRequest,
    workspace: WorkspaceRecord = Depends(current_workspace),
) -> dict:
    from .limits import LimitError, LimitStore

    try:
        view = LimitStore().update(
            workspace.id, limit_id, enabled=req.enabled, amount=req.amount,
            warning_threshold=req.warning_threshold, enforcement_mode=req.enforcement_mode,
            expires_at=_parse_dt(req.expires_at),
        )
    except LimitError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message)
    return asdict(view)


@app.get("/v1/balances")
def get_balances(
    workspace: WorkspaceRecord = Depends(current_workspace),
) -> dict:
    from .billing import BillingStore
    from .rates import to_money_str

    b = BillingStore()
    bal = b.balance(workspace.id)
    avail = b.available(workspace.id)
    return {
        "workspace_id": workspace.id,
        "currency": get_settings().default_currency or "USD",
        "balance": to_money_str(bal),
        "available": to_money_str(avail),
        "reserved": to_money_str(bal - avail),
    }


# -- GitHub installations -----------------------------------------------------


def _installation_dict(inst) -> dict:
    return {
        "installation_id": inst.github_installation_id,
        "status": inst.status,
        "account": {
            "id": inst.github_account_id,
            "login": inst.github_account_login,
            "type": inst.github_account_type,
        },
    }


def _repository_dict(repo) -> dict:
    return {
        "id": repo.id,
        "github_repository_id": repo.github_repository_id,
        "owner": repo.owner,
        "name": repo.name,
        "full_name": repo.full_name,
        "default_branch": repo.default_branch,
        "private": repo.private,
        "enabled": repo.enabled,
        "archived": repo.archived,
    }


@app.post("/v1/github/installations/claim")
def claim_installation(
    req: ClaimRequest,
    user: AuthedUser = Depends(current_user),
    auth_client: AuthServiceClient = Depends(get_auth_client),
    github_app=Depends(get_github_app),
) -> dict:
    try:
        result = installations_svc.claim_installation(
            auth_subject=user.subject,
            installation_id=req.installation_id,
            auth_client=auth_client,
            github_app=github_app,
        )
    except InstallationVerificationError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message)
    except WorkspaceConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {
        "installation": _installation_dict(result.installation),
        "repositories": [_repository_dict(r) for r in result.repositories],
    }


@app.get("/v1/github/installations")
def list_installations_route(
    workspace: WorkspaceRecord = Depends(current_workspace),
) -> list:
    return [_installation_dict(i) for i in ws.list_installations(workspace.id)]


@app.post("/v1/github/installations/{installation_id}/sync")
def sync_installation_route(
    installation_id: int,
    workspace: WorkspaceRecord = Depends(current_workspace),
    github_app=Depends(get_github_app),
) -> dict:
    inst = ws.get_installation_for_workspace(workspace.id, installation_id)
    if inst is None:
        raise HTTPException(status_code=404, detail="installation not found")
    repos = installations_svc.sync_installation(
        workspace_id=workspace.id, installation=inst, github_app=github_app
    )
    return {
        "installation": _installation_dict(inst),
        "repositories": [_repository_dict(r) for r in repos],
    }


class SetRepositoryEnabledRequest(BaseModel):
    enabled: bool


@app.get("/v1/repositories")
def list_repositories_route(
    enabled_only: bool = False,
    q: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    workspace: WorkspaceRecord = Depends(current_workspace),
) -> list:
    """Searchable, paginated repositories for the caller's workspace.

    Default returns all repos (enabled + disabled) so Settings can toggle them;
    the New Run workflow passes ``enabled_only=true`` to source only enabled repos.
    """
    repos = ws.list_repositories_page(
        workspace.id, enabled_only=enabled_only, search=q, limit=limit, offset=offset
    )
    return [_repository_dict(r) for r in repos]


@app.patch("/v1/repositories/{repository_id}")
def set_repository_enabled_route(
    repository_id: str,
    req: SetRepositoryEnabledRequest,
    workspace: WorkspaceRecord = Depends(current_workspace),
) -> dict:
    """Enable/disable a repository for new runs. 404 for unknown/cross-workspace."""
    updated = ws.set_repository_enabled(workspace.id, repository_id, req.enabled)
    if updated is None:
        raise HTTPException(status_code=404, detail="repository not found")
    return _repository_dict(updated)


@app.get("/v1/repositories/{repository_id}/branches")
def list_repository_branches_route(
    repository_id: str,
    q: Optional[str] = None,
    limit: int = 100,
    workspace: WorkspaceRecord = Depends(current_workspace),
    app_gh=Depends(get_github_app),
) -> dict:
    """Branches for a selected repository (server-side; token never exposed)."""
    from .branches import BranchListError, list_repository_branches

    try:
        result = list_repository_branches(
            get_settings(), app_gh,
            workspace_id=workspace.id, repository_id=repository_id,
            search=q, limit=limit,
        )
    except BranchListError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message)
    if result is None:
        raise HTTPException(status_code=404, detail="repository not found")
    return result


# -- models (user-facing catalog from the server allowlist) -------------------


@app.get("/v1/models")
def list_models_route(user: AuthedUser = Depends(current_user)) -> dict:
    """The offerable OpenRouter models, derived from the server allowlist."""
    from .model_catalog import model_catalog

    return {"items": model_catalog(get_settings())}


# -- jobs (user-facing, workspace-scoped) -------------------------------------


@app.post("/jobs", response_model=JobResponse)
def create_job(
    req: CreateJobRequest,
    workspace: WorkspaceRecord = Depends(current_workspace),
    db: PostgresJobStore = Depends(store),
) -> JobResponse:
    settings = get_settings()
    # The execution provider is fixed by server configuration and is NEVER read
    # from job input. If it is missing or invalid, no job can be created — there
    # is no local/Celery/Docker fallback to fall back to.
    _require_execution_configured(settings)
    repo = ws.get_repository(workspace.id, req.repository_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="repository not found")
    if not repo.enabled:
        raise HTTPException(status_code=409, detail="repository is disabled")
    inst = ws.get_installation_by_record_id(repo.github_installation_record_id)
    if inst is None or inst.status == "deleted":
        raise HTTPException(status_code=409, detail="repository installation is unavailable")
    if inst.status == "suspended":
        raise HTTPException(status_code=409, detail="repository installation is suspended")

    # Validate the selected model against the server allowlist. An explicit
    # unsupported model is rejected (422); an omitted model uses the default.
    from .model_catalog import resolve_allowed_model

    selected_model = resolve_allowed_model(settings, req.model)
    if req.model and selected_model is None:
        raise HTTPException(status_code=422, detail=f"model '{req.model}' is not available")

    spec = JobSpec(
        repo=repo.full_name,
        instruction=req.instruction,
        base_branch=req.base_branch or repo.default_branch or settings.default_base_branch,
        engine=req.engine or settings.default_engine,
        model=selected_model,
        workspace_id=workspace.id,
        repository_id=repo.id,
    )
    job = db.create_job(spec)

    from .tasks import run_job

    run_job.delay(job.id)
    return _to_response(job)


@app.get("/jobs", response_model=List[JobResponse])
def list_jobs(
    limit: int = 50,
    workspace: WorkspaceRecord = Depends(current_workspace),
    db: PostgresJobStore = Depends(store),
) -> List[JobResponse]:
    jobs = [j for j in db.list_jobs(limit=500) if j.workspace_id == workspace.id]
    return [_to_response(j) for j in jobs[:limit]]


@app.get("/jobs/{job_id}", response_model=JobResponse)
def get_job(
    job_id: str,
    workspace: WorkspaceRecord = Depends(current_workspace),
    db: PostgresJobStore = Depends(store),
) -> JobResponse:
    return _to_response(_require_owned_job(db, workspace, job_id))


@app.get("/jobs/{job_id}/logs", response_model=List[LogResponse])
def get_logs(
    job_id: str,
    workspace: WorkspaceRecord = Depends(current_workspace),
    db: PostgresJobStore = Depends(store),
) -> List[LogResponse]:
    _require_owned_job(db, workspace, job_id)
    return [
        LogResponse(phase=e.phase, level=e.level, message=e.message, created_at=e.created_at)
        for e in db.get_logs(job_id)
    ]


@app.get("/jobs/{job_id}/diff")
def get_diff(
    job_id: str,
    workspace: WorkspaceRecord = Depends(current_workspace),
    db: PostgresJobStore = Depends(store),
) -> dict:
    _require_owned_job(db, workspace, job_id)
    diff = db.get_diff(job_id)
    if diff is None:
        raise HTTPException(status_code=404, detail="no diff yet")
    return {"patch": diff.patch, "files_changed": diff.files_changed}


@app.post("/jobs/{job_id}/approve", response_model=JobResponse)
def approve(
    job_id: str,
    req: ApproveRequest,
    user: AuthedUser = Depends(current_user),
    workspace: WorkspaceRecord = Depends(current_workspace),
    db: PostgresJobStore = Depends(store),
) -> JobResponse:
    job = _require_owned_job(db, workspace, job_id)
    _require_awaiting(db, job_id)

    # Bind the approval to the exact base SHA + patch hash of the validated run.
    from .executor.approval import build_binding
    from .executor.store import ExecutionStore

    run = ExecutionStore().get_run_for_job(job_id)
    if run is None or not run.patch_sha256:
        raise HTTPException(status_code=409, detail="no validated execution to approve")
    diff = db.get_diff(job_id)
    from .executor.validation import sha256_text

    if diff is None or sha256_text(diff.patch) != run.patch_sha256:
        raise HTTPException(status_code=409, detail="stored patch does not match validated run")

    repo = ws.get_repository(workspace.id, job.repository_id) if job.repository_id else None
    binding = build_binding(
        job=job,
        run=run,
        repo=repo,
        installation_record_id=repo.github_installation_record_id if repo else None,
        actor=user.subject,
        verification=run.security_validation or "passed",
        ttl_seconds=get_settings().executor_token_ttl_seconds * 4,
        patch_sha256=run.patch_sha256,
    )
    approval = db.save_approval(
        Approval(job_id=job_id, decision="approved", actor=user.subject, note=req.note)
    )
    db.merge_context(job_id, {"approval_binding": binding, "approval_id": approval.id})
    job = db.set_status(job_id, JobStatus.APPROVED)

    from .tasks import publish_pr

    publish_pr.delay(job_id)
    return _to_response(job)


@app.post("/jobs/{job_id}/reject", response_model=JobResponse)
def reject(
    job_id: str,
    req: ApproveRequest,
    user: AuthedUser = Depends(current_user),
    workspace: WorkspaceRecord = Depends(current_workspace),
    db: PostgresJobStore = Depends(store),
) -> JobResponse:
    _require_owned_job(db, workspace, job_id)
    _require_awaiting(db, job_id)
    reject_job(db, job_id, actor=user.subject, note=req.note, memory=_memory())
    return _to_response(db.get_job(job_id))


@app.post("/jobs/{job_id}/cancel", response_model=JobResponse)
def cancel(
    job_id: str,
    workspace: WorkspaceRecord = Depends(current_workspace),
    db: PostgresJobStore = Depends(store),
) -> JobResponse:
    job = _require_owned_job(db, workspace, job_id)
    if is_terminal(job.status):
        raise HTTPException(status_code=409, detail=f"job is already '{job.status}'")

    # Immediately mark cancellation + revoke the run token so no further model
    # call or callback can succeed; the workflow itself is cancelled in the
    # worker (network I/O) and reconciliation finalizes idempotently.
    from .executor.store import ExecutionStore

    run = ExecutionStore().get_run_for_job(job_id)
    if run is not None:
        store_ex = ExecutionStore()
        store_ex.request_cancellation(run.id)
        store_ex.revoke_token(run.id)
        try:
            from .tasks import cancel_execution

            cancel_execution.delay(job_id)
        except Exception:  # noqa: BLE001 - queue optional; reconciliation covers it
            pass

    job = db.set_status(job_id, JobStatus.CANCELLED, error="cancelled by user")
    return _to_response(job)


# -- internal / emergency (raw repo, behind the internal key) -----------------


@app.post(
    "/internal/jobs",
    response_model=JobResponse,
    dependencies=[Depends(require_internal_key)],
)
def internal_create_job(
    req: InternalCreateJobRequest, db: PostgresJobStore = Depends(store)
) -> JobResponse:
    settings = get_settings()
    if settings.allowed_repos and req.repo not in settings.allowed_repos:
        raise HTTPException(status_code=403, detail=f"repo not allowed: {req.repo}")
    spec = JobSpec(
        repo=req.repo,
        instruction=req.instruction,
        base_branch=req.base_branch or settings.default_base_branch,
        engine=req.engine or settings.default_engine,
    )
    job = db.create_job(spec)
    from .tasks import run_job

    run_job.delay(job.id)
    return _to_response(job)


# -- GitHub webhooks ----------------------------------------------------------


@app.post("/github/webhooks")
async def github_webhooks(request: Request) -> dict:
    settings = get_settings()
    if not settings.github_webhook_secret:
        raise HTTPException(status_code=503, detail="webhooks are not configured")
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")
    try:
        webhooks_svc.verify_signature(settings.github_webhook_secret, body, signature)
    except webhooks_svc.WebhookError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message)

    event = request.headers.get("X-GitHub-Event", "")
    delivery = request.headers.get("X-GitHub-Delivery", "")
    import json

    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    return webhooks_svc.handle_event(event, delivery, payload)


# -- helpers ------------------------------------------------------------------


def _require_owned_job(db: PostgresJobStore, workspace: WorkspaceRecord, job_id: str):
    """Fetch a job only if it belongs to the caller's workspace.

    Cross-workspace (or unknown) ids return 404 — never confirm another user's
    job exists, so ids can't be enumerated.
    """
    job = db.get_job(job_id)
    if job is None or job.workspace_id != workspace.id:
        raise HTTPException(status_code=404, detail="job not found")
    return job


def _require_awaiting(db: PostgresJobStore, job_id: str):
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status != JobStatus.AWAITING_APPROVAL:
        raise HTTPException(
            status_code=409, detail=f"job is '{job.status}', not awaiting approval"
        )
    return job


def _to_response(job) -> JobResponse:
    return JobResponse(
        id=job.id,
        repo=job.repo,
        instruction=job.instruction,
        base_branch=job.base_branch,
        engine=job.engine,
        model=getattr(job, "model", None),
        status=job.status,
        branch=job.branch,
        error=job.error,
        created_at=job.created_at,
        updated_at=job.updated_at,
        usage=(job.context or {}).get("usage", {}),
    )
