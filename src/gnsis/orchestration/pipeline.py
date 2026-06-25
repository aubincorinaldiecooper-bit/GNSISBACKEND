"""The GNSIS job pipeline — the long-running work a worker performs.

This is what the Celery worker invokes (never the HTTP request). It drives the
chosen :class:`~gnsis.orchestration.engine.PatchEngine` through the generation
phases, **checkpointing every phase to the store** so a Railway restart or a
sandbox teardown never loses progress, saves the resulting diff, and then **parks
the job at ``awaiting_approval``**. It performs no GitHub writes — publishing is a
separate, approval-gated step (:func:`publish`).

It depends only on the :class:`~gnsis.orchestration.store.JobStore` protocol, so
it runs identically against the in-memory store (tests) and Postgres (Railway).
"""

from __future__ import annotations

from typing import Any, Optional, Protocol

from ..memory.base import MemoryProvider, MemoryRecord, NullMemoryProvider
from .engine import PatchEngine, PhaseSink, Workspace
from .models import (
    Checkpoint,
    Diff,
    EngineResult,
    LogEntry,
    PRMetadata,
    PipelineResult,
)
from .status import APPROVAL_GATE, PHASE_STATUS, JobStatus, Phase
from .store import JobNotFound, JobStore


class _StoreSink(PhaseSink):
    """Bridges an engine's progress reports to durable store writes."""

    def __init__(self, store: JobStore, job_id: str) -> None:
        self._store = store
        self._job_id = job_id

    def begin_phase(self, phase: str) -> None:
        status = PHASE_STATUS.get(phase)
        if status:
            self._store.set_status(self._job_id, status)
        self._store.append_log(
            LogEntry(self._job_id, phase, "info", f"phase '{phase}' started")
        )

    def checkpoint(self, phase: str, content: Any) -> None:
        self._store.save_checkpoint(Checkpoint(self._job_id, phase, content))
        self._store.append_log(
            LogEntry(self._job_id, phase, "info", f"phase '{phase}' checkpointed")
        )

    def log(self, message: str, level: str = "info", **data: Any) -> None:
        self._store.append_log(
            LogEntry(self._job_id, "", level, message, data=data)
        )


class Publisher(Protocol):
    """The side-effecting GitHub client: opens a PR for an approved job.

    Real implementation lives in :mod:`gnsis.service.github_app`; tests pass a
    fake. The pipeline never calls GitHub directly — it only calls this.
    """

    def publish(self, job: Any, diff: Diff) -> PRMetadata: ...


class JobPipeline:
    """Runs a job from ``queued`` up to the approval gate.

    If a long-term :class:`MemoryProvider` is supplied, the engine receives the
    instruction *augmented* with repo-scoped memory (conventions, prior decisions,
    accepted changes) — this is how GNSIS specializes to a codebase over time.
    The default provider is the no-op, so behavior is unchanged until memory is
    enabled.
    """

    def __init__(
        self,
        store: JobStore,
        engine: PatchEngine,
        memory: Optional[MemoryProvider] = None,
    ) -> None:
        self.store = store
        self.engine = engine
        self.memory = memory or NullMemoryProvider()

    def run(
        self, job_id: str, workspace: Optional[Workspace] = None
    ) -> PipelineResult:
        job = self.store.get_job(job_id)
        if job is None:
            raise JobNotFound(job_id)

        sink = _StoreSink(self.store, job_id)
        instruction = self._augment_with_memory(job)
        try:
            result: EngineResult = self.engine.generate(instruction, workspace, sink)
        except Exception as exc:  # noqa: BLE001 - record and surface every failure
            self.store.append_log(
                LogEntry(job_id, "", "error", f"engine failed: {exc}")
            )
            self.store.set_status(job_id, JobStatus.FAILED, error=str(exc))
            return PipelineResult(job_id, JobStatus.FAILED)

        if not result.success:
            self.store.set_status(
                job_id, JobStatus.FAILED, error="engine reported failure"
            )
            return PipelineResult(job_id, JobStatus.FAILED, result)

        # Persist the proposed change, then halt for a human.
        self.store.save_diff(
            Diff(job_id, result.patch, files_changed=result.files_changed)
        )
        self.store.set_status(job_id, APPROVAL_GATE)
        self.store.append_log(
            LogEntry(
                job_id,
                Phase.SUMMARY,
                "info",
                "awaiting human approval before publishing",
            )
        )
        return PipelineResult(job_id, APPROVAL_GATE, result)

    def _augment_with_memory(self, job) -> str:
        """Prepend repo-scoped memory (if any) to the engine's instruction."""
        records = self.memory.search(job.repo, job.instruction, limit=5)
        if not records:
            records = self.memory.recent(job.repo, limit=5)
        if not records:
            return job.instruction
        bullets = "\n".join(f"- {r.content}" for r in records)
        self.store.append_log(
            LogEntry(
                job.id, Phase.PLAN, "info",
                f"injected {len(records)} memory item(s) for {job.repo}",
            )
        )
        return (
            "Project memory for this repository (learned from prior, approved "
            f"work — honor it):\n{bullets}\n\nTask:\n{job.instruction}"
        )


def publish(
    store: JobStore,
    publisher: "Publisher",
    job_id: str,
    memory: Optional[MemoryProvider] = None,
) -> PRMetadata:
    """Open the PR for an *approved* job. Called by the ``publish_pr`` worker task.

    All GitHub writes happen here, behind the approval gate. The ``publisher``
    is the side-effecting GitHub client (real on Railway, faked in tests); this
    function owns the status transitions and persistence around it.
    """
    job = store.get_job(job_id)
    if job is None:
        raise JobNotFound(job_id)

    approval = store.get_latest_approval(job_id)
    if approval is None or approval.decision != "approved":
        raise PermissionError(f"job {job_id} is not approved for publishing")

    diff = store.get_diff(job_id)
    if diff is None:
        raise ValueError(f"job {job_id} has no diff to publish")

    store.set_status(job_id, JobStatus.PUBLISHING)
    store.append_log(LogEntry(job_id, Phase.PUBLISH, "info", "publishing pull request"))
    try:
        pr = publisher.publish(job, diff)
    except Exception as exc:  # noqa: BLE001
        store.append_log(LogEntry(job_id, Phase.PUBLISH, "error", f"publish failed: {exc}"))
        store.set_status(job_id, JobStatus.FAILED, error=str(exc))
        raise

    store.save_pr_metadata(pr)
    store.set_status(job_id, JobStatus.COMPLETED)
    store.append_log(
        LogEntry(job_id, Phase.PUBLISH, "info", f"opened PR #{pr.number}: {pr.url}")
    )

    # Approval-gated memory write: a published change is a *validated* outcome,
    # so it is the one thing worth remembering for next time. Scoped to the repo.
    if memory is not None:
        summary = _summary_for(store, job_id) or job.instruction
        memory.write(
            MemoryRecord(
                repo=job.repo,
                content=summary,
                kind="accepted_change",
                metadata={"job_id": job_id, "pr": pr.number, "files": diff.files_changed},
                approved=True,
            )
        )
    return pr


def _summary_for(store: JobStore, job_id: str) -> str:
    """The summary-phase checkpoint, if the engine produced one."""
    for cp in reversed(store.get_checkpoints(job_id)):
        if cp.phase == Phase.SUMMARY and isinstance(cp.content, str):
            return cp.content
    return ""
