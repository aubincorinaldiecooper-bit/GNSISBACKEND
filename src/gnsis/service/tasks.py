"""Celery tasks — the long work that must never run inside an HTTP request.

Two tasks, mirroring the two halves of the job lifecycle:

* :func:`run_job` — clone the repo, drive the engine through the phases (each
  checkpointed to Postgres), save the diff, and stop at ``awaiting_approval``.
* :func:`publish_pr` — only after approval: mint a scoped token, push the branch,
  open the PR. GitHub writes live here and nowhere else.

The Celery app uses Redis as both broker and result backend.
"""

from __future__ import annotations

from celery import Celery
from celery.signals import worker_ready

from ..engines import get_engine
from ..memory.base import MemoryProvider, NullMemoryProvider
from ..orchestration.engine import PatchEngine
from ..orchestration.pipeline import JobPipeline, publish
from ..orchestration.status import JobStatus
from .github_app import publisher_from_env
from .repository import PostgresJobStore, PostgresMemoryProvider
from .sandbox import DockerEngine
from .settings import get_settings
from .workspace import cleanup_workspace, prepare_workspace

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
)


@worker_ready.connect
def _ensure_schema(**_: object) -> None:
    """Create tables when the worker boots, so it never races the API."""
    from .db import init_db

    init_db()


def _store() -> PostgresJobStore:
    return PostgresJobStore()


def _memory() -> MemoryProvider:
    if settings.memory_backend == "postgres":
        return PostgresMemoryProvider()
    return NullMemoryProvider()


def _build_engine(engine_name: str) -> PatchEngine:
    """The job's engine, wrapped in the Docker sandbox if one is configured."""
    if settings.sandbox == "docker":
        return DockerEngine(
            inner_engine=engine_name,
            image=settings.sandbox_image,
            network=settings.sandbox_network,
            memory=settings.sandbox_memory,
            cpus=settings.sandbox_cpus,
            timeout_seconds=settings.sandbox_timeout,
        )
    return get_engine(engine_name)


@celery_app.task(name="gnsis.run_job")
def run_job(job_id: str) -> str:
    """Generate the change for ``job_id`` and park it at the approval gate."""
    store = _store()
    job = store.get_job(job_id)
    if job is None:
        raise KeyError(job_id)

    token = None
    if settings.github_app_id and settings.github_app_installation_id:
        token = publisher_from_env(settings).app.installation_token()

    workspace = None
    try:
        workspace = prepare_workspace(
            repo=job.repo,
            base_branch=job.base_branch,
            token=token,
            root=settings.workspace_root,
            job_id=job_id,
        )
        pipeline = JobPipeline(store, _build_engine(job.engine), memory=_memory())
        result = pipeline.run(job_id, workspace)
        return result.status
    except Exception as exc:  # noqa: BLE001
        store.set_status(job_id, JobStatus.FAILED, error=str(exc))
        raise
    finally:
        if workspace is not None:
            cleanup_workspace(workspace)


@celery_app.task(name="gnsis.publish_pr")
def publish_pr(job_id: str) -> str:
    """Open the PR for an approved job. Refuses if not approved."""
    store = _store()
    publisher = publisher_from_env(settings)
    pr = publish(store, publisher, job_id, memory=_memory())
    return pr.url
