"""The internal executor + model-gateway HTTP surface.

Mounted on the main FastAPI app. Two authentication regimes:

* ``/internal/executor/oidc/exchange`` authenticates with a GitHub Actions OIDC
  identity + the single-use dispatch nonce — the VM has no token yet.
* every other route authenticates with the short-lived, hashed run token issued
  by that exchange, bound to exactly one run/attempt/job/repo/base SHA.

Nothing here returns a control-plane secret or a GitHub credential.
"""

from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ...orchestration.status import is_terminal
from ..settings import get_settings
from . import callbacks as cb
from . import gateway as gw
from . import source as src
from .models import ExecutionStatus, FailureCategory
from .oidc import GithubOidcVerifier, OidcError, check_execution_claims
from .spec import build_run_spec, model_gateway_url
from .store import ExecutionStore
from .tokens import hash_secret, new_run_token

router = APIRouter()

# Lazily-built OIDC verifier; overridable in tests.
_oidc_verifier: Optional[GithubOidcVerifier] = None


def set_oidc_verifier(verifier: Optional[GithubOidcVerifier]) -> None:
    global _oidc_verifier
    _oidc_verifier = verifier


def _get_oidc_verifier() -> GithubOidcVerifier:
    global _oidc_verifier
    if _oidc_verifier is None:
        s = get_settings()
        if not s.executor_oidc_audience:
            raise HTTPException(status_code=503, detail="executor OIDC is not configured")
        _oidc_verifier = GithubOidcVerifier.default(
            audience=s.executor_oidc_audience, issuer=s.executor_oidc_issuer
        )
    return _oidc_verifier


def _job_store():
    from ..repository import PostgresJobStore

    return PostgresJobStore()


async def _read_json(request: Request, max_bytes: int) -> dict:
    body = await request.body()
    if len(body) > max_bytes:
        raise HTTPException(status_code=413, detail="request body too large")
    if not body:
        return {}
    try:
        data = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="invalid JSON body")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    return data


def _bearer(authorization: Optional[str]) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="missing Authorization")
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(status_code=401, detail="malformed Authorization")
    return parts[1].strip()


def _authenticate_run(authorization: Optional[str], store: ExecutionStore, job_id: Optional[str] = None):
    token = _bearer(authorization)
    run = store.get_run_by_token_hash(hash_secret(token))
    if run is None:
        raise HTTPException(status_code=401, detail="invalid run token")
    if run.token_revoked:
        raise HTTPException(status_code=401, detail="run token revoked")
    if run.token_expired:
        raise HTTPException(status_code=401, detail="run token expired")
    if run.status not in ExecutionStatus.TOKEN_ACTIVE:
        raise HTTPException(status_code=409, detail=f"run is {run.status}")
    if job_id is not None and run.job_id != job_id:
        raise HTTPException(status_code=403, detail="token not bound to this job")
    return run


# -- OIDC exchange ------------------------------------------------------------
@router.post("/internal/executor/oidc/exchange")
async def oidc_exchange(request: Request):
    settings = get_settings()
    body = await _read_json(request, settings.executor_callback_max_bytes)
    job_id = body.get("job_id")
    nonce = body.get("dispatch_nonce")
    oidc_token = body.get("oidc_token") or body.get("token")
    if not (job_id and nonce and oidc_token):
        raise HTTPException(status_code=400, detail="job_id, dispatch_nonce and oidc_token are required")

    store = ExecutionStore()
    job_store = _job_store()
    run = store.get_run_for_job(job_id)
    if run is None or run.is_terminal:
        raise HTTPException(status_code=401, detail="no active run for job")

    # The nonce proves the caller is the dispatched workflow. Only then does an
    # OIDC failure count against the job (an unauthenticated caller cannot).
    nonce_ok = store.nonce_matches(run.id, hash_secret(nonce))
    if not nonce_ok:
        raise HTTPException(status_code=401, detail="invalid dispatch nonce")

    def _fail_job(reason: str, category: str):
        store.set_status(run.id, ExecutionStatus.FAILED, failure_category=category)
        store.revoke_token(run.id)
        job = job_store.get_job(job_id)
        if job is not None and not is_terminal(job.status):
            from ...orchestration.status import JobStatus

            job_store.set_status(job_id, JobStatus.FAILED, error=f"OIDC exchange failed: {reason}")

    verifier = _get_oidc_verifier()
    try:
        claims = verifier.verify(oidc_token)
    except OidcError as exc:
        _fail_job(exc.reason, FailureCategory.OIDC)
        raise HTTPException(status_code=401, detail=f"oidc verification failed: {exc.reason}")

    try:
        check_execution_claims(
            claims,
            expected_repository=settings.executor_full_name,
            expected_owner=settings.executor_owner,
            expected_repository_id=run.executor_repository_id,
            expected_workflow_ref=settings.expected_workflow_ref,
            trusted_workflow_sha=run.trusted_workflow_sha,
            expected_run_id=run.workflow_run_id,
            expected_run_attempt=run.workflow_run_attempt,
        )
    except OidcError as exc:
        _fail_job(exc.reason, exc.category)
        raise HTTPException(status_code=401, detail=f"oidc claim rejected: {exc.reason}")

    # Consume the nonce exactly once (replays/concurrent exchanges lose here).
    if not store.consume_nonce(run.id, hash_secret(nonce)):
        raise HTTPException(status_code=401, detail="dispatch nonce already used")

    # Bind the run to the exact workflow run/attempt if not already captured.
    if run.workflow_run_id is None:
        try:
            store.set_workflow_run(
                run.id,
                workflow_run_id=int(claims["run_id"]),
                workflow_run_attempt=int(claims.get("run_attempt", 1)),
            )
        except (KeyError, TypeError, ValueError):
            raise HTTPException(status_code=401, detail="token missing run id")

    from datetime import datetime, timezone, timedelta

    token = new_run_token()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=settings.run_token_ttl_seconds)
    store.bind_token(run.id, token_hash=hash_secret(token), expires_at=expires_at)

    base = (settings.public_api_url or "").rstrip("/")
    return {
        "run_token": token,
        "token_type": "Bearer",
        "expires_in": settings.run_token_ttl_seconds,
        "spec_url": f"{base}/internal/executor/runs/{job_id}/spec",
        "source_url": f"{base}/internal/executor/runs/{job_id}/source",
        "events_url": f"{base}/internal/executor/runs/{job_id}/events",
        "complete_url": f"{base}/internal/executor/runs/{job_id}/complete",
        "failed_url": f"{base}/internal/executor/runs/{job_id}/failed",
        "model_gateway_url": model_gateway_url(settings),
    }


# -- job spec -----------------------------------------------------------------
@router.get("/internal/executor/runs/{job_id}/spec")
def get_spec(job_id: str, authorization: Optional[str] = Header(default=None)):
    settings = get_settings()
    store = ExecutionStore()
    run = _authenticate_run(authorization, store, job_id)
    job = _job_store().get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    store.mark_started(run.id)
    return build_run_spec(settings, job, run).to_public_dict()


# -- immutable source ---------------------------------------------------------
@router.get("/internal/executor/runs/{job_id}/source")
def get_source(job_id: str, authorization: Optional[str] = Header(default=None)):
    settings = get_settings()
    store = ExecutionStore()
    run = _authenticate_run(authorization, store, job_id)
    job = _job_store().get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    # Prepare upstream, validate headers and first byte before HTTP response start.
    try:
        prepared = src.prepare_source(settings, run, job.repo)
    except src.SourceError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    # Single use is claimed only after preparation succeeds; close on races.
    if not store.claim_source_download(run.id):
        prepared.close()
        raise HTTPException(status_code=409, detail="source already delivered")

    def generator():
        try:
            yield from prepared.iter_bytes()
        except src.SourceError as exc:
            src.fail_streaming_source(prepared, store, run, _job_store(), str(exc))
            raise
        except Exception:  # noqa: BLE001
            src.fail_streaming_source(prepared, store, run, _job_store(), "source stream failed")
            raise
    headers = {
        "Content-Type": "application/gzip",
        "X-GNSIS-Base-SHA": run.base_sha,
        "Content-Disposition": f'attachment; filename="{job_id}-source.tar.gz"',
    }
    return StreamingResponse(generator, headers=headers, media_type="application/gzip")


# -- restricted model gateway -------------------------------------------------
@router.post("/internal/model/v1/chat/completions")
async def model_chat_completions(request: Request, authorization: Optional[str] = Header(default=None)):
    settings = get_settings()
    store = ExecutionStore()
    run = _authenticate_run(authorization, store)
    body = await _read_json(request, settings.executor_callback_max_bytes)
    try:
        status, data = gw.handle_chat_completion(settings, store, run, body)
    except gw.GatewayError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message)
    return JSONResponse(status_code=status, content=data)


# -- events / completion / failure --------------------------------------------
@router.post("/internal/executor/runs/{job_id}/events")
async def post_events(job_id: str, request: Request, authorization: Optional[str] = Header(default=None)):
    settings = get_settings()
    store = ExecutionStore()
    run = _authenticate_run(authorization, store, job_id)
    body = await _read_json(request, settings.executor_event_max_bytes)
    try:
        return cb.record_run_event(settings, store, run, body)
    except cb.CallbackError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message)


@router.post("/internal/executor/runs/{job_id}/complete")
async def post_complete(job_id: str, request: Request, authorization: Optional[str] = Header(default=None)):
    settings = get_settings()
    store = ExecutionStore()
    run = _authenticate_run(authorization, store, job_id)
    body = await _read_json(request, settings.executor_callback_max_bytes)
    try:
        return cb.handle_complete(settings, _job_store(), store, run, body)
    except cb.CallbackError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message)


@router.post("/internal/executor/runs/{job_id}/failed")
async def post_failed(job_id: str, request: Request, authorization: Optional[str] = Header(default=None)):
    settings = get_settings()
    store = ExecutionStore()
    run = _authenticate_run(authorization, store, job_id)
    body = await _read_json(request, settings.executor_event_max_bytes)
    try:
        return cb.handle_failed(settings, _job_store(), store, run, body)
    except cb.CallbackError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message)
