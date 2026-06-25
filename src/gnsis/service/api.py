"""FastAPI app — thin HTTP surface over the job store and the queue.

Every endpoint either reads Postgres or enqueues a Celery task. **No evolution or
generation work runs in a request handler** — creating a job returns immediately
with a queued record; the worker does the long work. Approval simply records the
human decision and enqueues ``publish_pr``.
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from ..orchestration.models import Approval, JobSpec
from ..orchestration.status import JobStatus
from .repository import PostgresJobStore
from .settings import get_settings
from .ui import INDEX_HTML

def _cors_origins() -> list:
    try:
        return get_settings().cors_origins
    except Exception:
        return ["*"]


app = FastAPI(title="GNSIS", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    # Idempotent: ensures tables exist before the first request.
    from .db import init_db

    init_db()


@app.get("/", include_in_schema=False)
def _root() -> RedirectResponse:
    return RedirectResponse(url="/ui")


@app.get("/ui", response_class=HTMLResponse, include_in_schema=False)
def _ui() -> str:
    return INDEX_HTML


def store() -> PostgresJobStore:
    return PostgresJobStore()


def require_api_key(authorization: Optional[str] = Header(default=None)) -> None:
    """Optional shared-secret gate. Disabled unless GNSIS_API_KEY is set."""
    settings = get_settings()
    if not settings.api_key:
        return
    expected = f"Bearer {settings.api_key}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="invalid or missing API key")


# -- schemas ------------------------------------------------------------------


class CreateJobRequest(BaseModel):
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
    status: str
    branch: Optional[str]
    error: Optional[str]
    created_at: str
    updated_at: str


class LogResponse(BaseModel):
    phase: str
    level: str
    message: str
    created_at: str


class ApproveRequest(BaseModel):
    actor: str = "human"
    note: str = ""


# -- routes -------------------------------------------------------------------


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/jobs", response_model=JobResponse, dependencies=[Depends(require_api_key)])
def create_job(req: CreateJobRequest, db: PostgresJobStore = Depends(store)) -> JobResponse:
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

    # Enqueue the long work; never run it here.
    from .tasks import run_job

    run_job.delay(job.id)
    return _to_response(job)


@app.get("/jobs", response_model=List[JobResponse], dependencies=[Depends(require_api_key)])
def list_jobs(limit: int = 50, db: PostgresJobStore = Depends(store)) -> List[JobResponse]:
    return [_to_response(j) for j in db.list_jobs(limit=limit)]


@app.get("/jobs/{job_id}", response_model=JobResponse, dependencies=[Depends(require_api_key)])
def get_job(job_id: str, db: PostgresJobStore = Depends(store)) -> JobResponse:
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _to_response(job)


@app.get(
    "/jobs/{job_id}/logs",
    response_model=List[LogResponse],
    dependencies=[Depends(require_api_key)],
)
def get_logs(job_id: str, db: PostgresJobStore = Depends(store)) -> List[LogResponse]:
    if db.get_job(job_id) is None:
        raise HTTPException(status_code=404, detail="job not found")
    return [
        LogResponse(
            phase=e.phase, level=e.level, message=e.message, created_at=e.created_at
        )
        for e in db.get_logs(job_id)
    ]


@app.get("/jobs/{job_id}/diff", dependencies=[Depends(require_api_key)])
def get_diff(job_id: str, db: PostgresJobStore = Depends(store)) -> dict:
    diff = db.get_diff(job_id)
    if diff is None:
        raise HTTPException(status_code=404, detail="no diff yet")
    return {"patch": diff.patch, "files_changed": diff.files_changed}


@app.post(
    "/jobs/{job_id}/approve",
    response_model=JobResponse,
    dependencies=[Depends(require_api_key)],
)
def approve(
    job_id: str, req: ApproveRequest, db: PostgresJobStore = Depends(store)
) -> JobResponse:
    job = _require_awaiting(db, job_id)
    db.save_approval(
        Approval(job_id=job_id, decision="approved", actor=req.actor, note=req.note)
    )
    job = db.set_status(job_id, JobStatus.APPROVED)

    from .tasks import publish_pr

    publish_pr.delay(job_id)
    return _to_response(job)


@app.post(
    "/jobs/{job_id}/reject",
    response_model=JobResponse,
    dependencies=[Depends(require_api_key)],
)
def reject(
    job_id: str, req: ApproveRequest, db: PostgresJobStore = Depends(store)
) -> JobResponse:
    _require_awaiting(db, job_id)
    db.save_approval(
        Approval(job_id=job_id, decision="rejected", actor=req.actor, note=req.note)
    )
    job = db.set_status(job_id, JobStatus.REJECTED)
    return _to_response(job)


def _require_awaiting(db: PostgresJobStore, job_id: str):
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status != JobStatus.AWAITING_APPROVAL:
        raise HTTPException(
            status_code=409,
            detail=f"job is '{job.status}', not awaiting approval",
        )
    return job


def _to_response(job) -> JobResponse:
    return JobResponse(
        id=job.id,
        repo=job.repo,
        instruction=job.instruction,
        base_branch=job.base_branch,
        engine=job.engine,
        status=job.status,
        branch=job.branch,
        error=job.error,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )
