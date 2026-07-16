"""Celery tasks — the long work that must never run inside an HTTP request.

Public-beta execution model: the worker **dispatches** user jobs into the fixed
GitHub Actions executor and then reconciles them; it no longer clones or runs any
customer code. GitHub writes remain confined to publishing, behind approval.

* :func:`run_job` — resolve the immutable base commit + the executor installation,
  and dispatch ``execute.yml``. No customer code, model call, or DockerEngine
  runs here. Dispatch/OIDC/timeout failures mark the job failed; there is no
  local, Celery-process, Daytona or DockerEngine fallback.
* :func:`publish_pr` — only after approval: reconstruct the exact base, apply the
  exact approved patch, and open a draft PR through a fresh installation token.
* :func:`reconcile_executions` — periodic source-of-truth reconciliation.

The Celery app uses Redis as both broker and result backend.
"""

from __future__ import annotations

import logging
from typing import Optional

from celery import Celery
from celery.signals import worker_ready

from ..memory.base import MemoryProvider, NullMemoryProvider
from ..orchestration.models import JobRecord, LogEntry
from ..orchestration.status import JobStatus, is_terminal
from . import workspaces as ws
from .github_app import GitHubApp, app_from_settings
from .repository import PostgresJobStore, PostgresMemoryProvider
from .settings import get_settings

logger = logging.getLogger("gnsis.tasks")

settings = get_settings()
celery_app = Celery(
    "gnsis",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)
celery_app.conf.update(
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    beat_schedule={
        "reconcile-executions": {
            "task": "gnsis.reconcile_executions",
            "schedule": 60.0,  # source-of-truth polling every minute
        },
    },
)


@worker_ready.connect
def _ensure_schema(**_: object) -> None:
    """Create tables when the worker boots, and fail loud if misconfigured."""
    from .db import init_db

    init_db()
    s = get_settings()
    if s.is_production:
        missing = s.missing_production_vars(role="worker")
        if missing:
            raise RuntimeError(
                "worker missing required public-beta execution configuration: "
                + ", ".join(missing)
            )


def _store() -> PostgresJobStore:
    return PostgresJobStore()


def _memory() -> MemoryProvider:
    if settings.memory_backend == "postgres":
        return PostgresMemoryProvider()
    return NullMemoryProvider()


def resolve_installation_id(job: JobRecord) -> Optional[int]:
    """The customer GitHub App installation this job's repository belongs to.

    For a user job (one with a ``repository_id``) the installation is resolved
    strictly through the repository record; the deprecated global
    ``GITHUB_APP_INSTALLATION_ID`` is **never** used for user jobs. It remains a
    fallback only for legacy/internal jobs that carry no repository record.
    """
    if job.repository_id:
        repo = _repository_for_job(job)
        if repo is not None:
            inst = ws.get_installation_by_record_id(repo.github_installation_record_id)
            if inst is not None:
                return inst.github_installation_id
        return None  # user job: no global fallback
    if settings.github_app_installation_id:
        return int(settings.github_app_installation_id)
    return None


def _repository_for_job(job: JobRecord):
    if not (job.workspace_id and job.repository_id):
        return None
    return ws.get_repository(job.workspace_id, job.repository_id)


def _app() -> GitHubApp:
    return app_from_settings(settings)


def _mint_token(installation_id: Optional[int]) -> Optional[str]:
    """Short-lived customer installation token. Never persisted or logged.

    Used only in-process (to read a base ref, stream source, or publish). The
    plaintext lives only for the duration of the call and never lands on the job.
    """
    if installation_id is None or not (
        settings.github_app_id and settings.github_app_private_key
    ):
        return None
    return app_from_settings(settings).token_for_installation(installation_id)


@celery_app.task(name="gnsis.run_job")
def run_job(job_id: str) -> str:
    """Dispatch ``job_id`` to the fixed GitHub Actions executor.

    This performs no customer checkout and runs no model or customer command.
    """
    from .executor.dispatch import (
        DispatchError,
        dispatch_execution,
        resolve_base_sha,
    )
    from .executor.store import ExecutionStore

    store = _store()
    job = store.get_job(job_id)
    if job is None:
        raise KeyError(job_id)
    if is_terminal(job.status):
        return job.status

    # The provider is fixed by configuration and never taken from job input.
    if not settings.execution_provider_valid or settings.missing_execution_vars():
        store.set_status(
            job_id, JobStatus.FAILED, error="public-beta execution is not configured"
        )
        raise RuntimeError("execution provider is not configured")

    installation_id = resolve_installation_id(job)
    if installation_id is None:
        store.set_status(
            job_id, JobStatus.FAILED, error="no GitHub installation for repository"
        )
        raise RuntimeError(f"no installation resolvable for job {job_id}")

    try:
        app = _app()
        base_sha = resolve_base_sha(
            app,
            customer_installation_id=installation_id,
            repo_full_name=job.repo,
            base_branch=job.base_branch,
        )
        run = dispatch_execution(
            settings, ExecutionStore(), job=job, base_sha=base_sha, app=app
        )
    except DispatchError as exc:
        store.set_status(job_id, JobStatus.FAILED, error=f"dispatch failed: {exc}")
        store.merge_context(job_id, {"failure_category": exc.category})
        raise
    except Exception as exc:  # noqa: BLE001
        store.set_status(job_id, JobStatus.FAILED, error=f"dispatch failed: {exc}")
        raise

    store.merge_context(
        job_id,
        {
            "execution_run_id": run.id,
            "base_sha": run.base_sha,
            "workflow_run_id": run.workflow_run_id,
            "workflow_run_url": run.workflow_run_url,
        },
    )
    store.append_log(
        LogEntry(job_id, "dispatch", "info", f"dispatched executor run {run.id}")
    )
    return "dispatched"


@celery_app.task(name="gnsis.publish_pr")
def publish_pr(job_id: str) -> str:
    """Open the draft PR for an approved job. Refuses if not approved."""
    from .executor.publish import publish_approved

    pr = publish_approved(_store(), settings, job_id, memory=_memory())
    return pr.url


@celery_app.task(name="gnsis.cancel_execution")
def cancel_execution(job_id: str) -> str:
    """Revoke the run token and cancel the workflow for a cancelled job."""
    from .executor.cancel import cancel_job_execution

    cancel_job_execution(settings, job_id)
    return "cancelled"


@celery_app.task(name="gnsis.reconcile_executions")
def reconcile_executions() -> str:
    """Poll GitHub and repair lost/stale/orphaned runs (source of truth)."""
    from .executor.reconcile import reconcile_all

    repaired = reconcile_all(settings, _store())
    return f"reconciled:{repaired}"
