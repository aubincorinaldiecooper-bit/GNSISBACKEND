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

from typing import Optional

from ..engines import get_engine
from ..memory.base import MemoryProvider, NullMemoryProvider
from ..orchestration.engine import PatchEngine
from ..orchestration.models import JobRecord
from ..orchestration.pipeline import JobPipeline, publish
from ..orchestration.status import JobStatus
from . import workspaces as ws
from .github_app import GitHubApp, GitHubPublisher, app_from_settings
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


def resolve_installation_id(job: JobRecord) -> Optional[int]:
    """The GitHub App installation this run must use.

    Resolved per run: job -> repository record -> installation record ->
    installation id. Falls back to the deprecated global installation id only
    for legacy/internal runs that have no repository record.
    """
    if job.repository_id:
        # repository_id -> installation record id -> github installation id.
        # get_repository needs the workspace; the repo row carries it, so look
        # it up directly through the installation record chain.
        repo = _repository_for_job(job)
        if repo is not None:
            inst = ws.get_installation_by_record_id(repo.github_installation_record_id)
            if inst is not None:
                return inst.github_installation_id
    if settings.github_app_installation_id:
        return int(settings.github_app_installation_id)
    return None


def _repository_for_job(job: JobRecord):
    if not (job.workspace_id and job.repository_id):
        return None
    return ws.get_repository(job.workspace_id, job.repository_id)


def _mint_token(installation_id: Optional[int]) -> Optional[str]:
    """Short-lived installation token for this run. Never persisted or logged."""
    if installation_id is None or not (
        settings.github_app_id and settings.github_app_private_key
    ):
        return None
    return app_from_settings(settings).token_for_installation(installation_id)


@celery_app.task(name="gnsis.run_job")
def run_job(job_id: str) -> str:
    """Generate the change for ``job_id`` and park it at the approval gate."""
    store = _store()
    job = store.get_job(job_id)
    if job is None:
        raise KeyError(job_id)

    # Resolve THIS run's installation and mint a token scoped to it. The token
    # is used only to clone here and is never written to the job, logs, or DB.
    token = _mint_token(resolve_installation_id(job))

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


def _publisher_for_job(job: JobRecord) -> GitHubPublisher:
    """A publisher bound to the run's specific installation."""
    installation_id = resolve_installation_id(job)
    if installation_id is None:
        raise RuntimeError(f"no GitHub installation resolvable for job {job.id}")
    app = GitHubApp(
        app_id=settings.github_app_id,
        private_key=settings.github_app_private_key,
        installation_id=str(installation_id),
    )
    return GitHubPublisher(app, settings.workspace_root)


@celery_app.task(name="gnsis.publish_pr")
def publish_pr(job_id: str) -> str:
    """Open the PR for an approved job. Refuses if not approved."""
    store = _store()
    job = store.get_job(job_id)
    if job is None:
        raise KeyError(job_id)
    publisher = _publisher_for_job(job)
    pr = publish(store, publisher, job_id, memory=_memory())
    return pr.url
