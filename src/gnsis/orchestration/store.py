"""The persistence boundary.

:class:`JobStore` is the *only* contract the pipeline knows about for durability.
The in-memory implementation below makes the pipeline fully testable offline; the
Postgres-backed implementation in :mod:`gnsis.service.repository` satisfies the
same protocol so that on Railway "nothing is lost on restart" — every status
change, log line, checkpoint, diff, approval, and PR record lands in Postgres.

Keeping this a Protocol (structural typing) means neither side imports the other:
the core stays dependency-free, the service layer stays swappable.
"""

from __future__ import annotations

from typing import List, Optional, Protocol, runtime_checkable

from .models import (
    Approval,
    Checkpoint,
    Diff,
    JobRecord,
    JobSpec,
    LogEntry,
    PRMetadata,
    _now,
    new_id,
)


@runtime_checkable
class JobStore(Protocol):
    # -- jobs -------------------------------------------------------------
    def create_job(self, spec: JobSpec) -> JobRecord: ...

    def get_job(self, job_id: str) -> Optional[JobRecord]: ...

    def list_jobs(self, limit: int = 50) -> List[JobRecord]: ...

    def set_status(
        self, job_id: str, status: str, error: Optional[str] = None
    ) -> JobRecord: ...

    def set_branch(self, job_id: str, branch: str) -> JobRecord: ...

    def merge_context(self, job_id: str, updates: dict) -> JobRecord: ...

    # -- evolution / phase records ---------------------------------------
    def append_log(self, entry: LogEntry) -> LogEntry: ...

    def get_logs(self, job_id: str) -> List[LogEntry]: ...

    def save_checkpoint(self, checkpoint: Checkpoint) -> Checkpoint: ...

    def get_checkpoints(self, job_id: str) -> List[Checkpoint]: ...

    def save_diff(self, diff: Diff) -> Diff: ...

    def get_diff(self, job_id: str) -> Optional[Diff]: ...

    # -- approvals --------------------------------------------------------
    def save_approval(self, approval: Approval) -> Approval: ...

    def get_latest_approval(self, job_id: str) -> Optional[Approval]: ...

    # -- PR metadata ------------------------------------------------------
    def save_pr_metadata(self, pr: PRMetadata) -> PRMetadata: ...

    def get_pr_metadata(self, job_id: str) -> Optional[PRMetadata]: ...


class JobNotFound(KeyError):
    pass


class InMemoryJobStore:
    """A dict-backed :class:`JobStore` for tests and local single-process runs.

    Deliberately not thread-safe and not durable — its entire purpose is to let
    the pipeline be exercised without Postgres. Anything that must survive a
    restart uses the Postgres implementation instead.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, JobRecord] = {}
        self._logs: dict[str, List[LogEntry]] = {}
        self._checkpoints: dict[str, List[Checkpoint]] = {}
        self._diffs: dict[str, Diff] = {}
        self._approvals: dict[str, List[Approval]] = {}
        self._prs: dict[str, PRMetadata] = {}

    # -- jobs -------------------------------------------------------------
    def create_job(self, spec: JobSpec) -> JobRecord:
        job_id = new_id("job")
        branch = spec.branch or f"gnsis/{job_id}"
        record = JobRecord(
            id=job_id,
            repo=spec.repo,
            instruction=spec.instruction,
            base_branch=spec.base_branch,
            engine=spec.engine,
            status="queued",
            branch=branch,
            context=dict(spec.context),
        )
        self._jobs[job_id] = record
        return record

    def get_job(self, job_id: str) -> Optional[JobRecord]:
        return self._jobs.get(job_id)

    def list_jobs(self, limit: int = 50) -> List[JobRecord]:
        jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
        return jobs[:limit]

    def _require(self, job_id: str) -> JobRecord:
        job = self._jobs.get(job_id)
        if job is None:
            raise JobNotFound(job_id)
        return job

    def set_status(
        self, job_id: str, status: str, error: Optional[str] = None
    ) -> JobRecord:
        job = self._require(job_id)
        job.status = status
        if error is not None:
            job.error = error
        job.updated_at = _now()
        return job

    def set_branch(self, job_id: str, branch: str) -> JobRecord:
        job = self._require(job_id)
        job.branch = branch
        job.updated_at = _now()
        return job

    def merge_context(self, job_id: str, updates: dict) -> JobRecord:
        job = self._require(job_id)
        job.context = {**job.context, **updates}
        job.updated_at = _now()
        return job

    # -- phase records ----------------------------------------------------
    def append_log(self, entry: LogEntry) -> LogEntry:
        self._logs.setdefault(entry.job_id, []).append(entry)
        return entry

    def get_logs(self, job_id: str) -> List[LogEntry]:
        return list(self._logs.get(job_id, []))

    def save_checkpoint(self, checkpoint: Checkpoint) -> Checkpoint:
        self._checkpoints.setdefault(checkpoint.job_id, []).append(checkpoint)
        return checkpoint

    def get_checkpoints(self, job_id: str) -> List[Checkpoint]:
        return list(self._checkpoints.get(job_id, []))

    def save_diff(self, diff: Diff) -> Diff:
        self._diffs[diff.job_id] = diff
        return diff

    def get_diff(self, job_id: str) -> Optional[Diff]:
        return self._diffs.get(job_id)

    # -- approvals --------------------------------------------------------
    def save_approval(self, approval: Approval) -> Approval:
        self._approvals.setdefault(approval.job_id, []).append(approval)
        return approval

    def get_latest_approval(self, job_id: str) -> Optional[Approval]:
        items = self._approvals.get(job_id)
        return items[-1] if items else None

    # -- PR metadata ------------------------------------------------------
    def save_pr_metadata(self, pr: PRMetadata) -> PRMetadata:
        self._prs[pr.job_id] = pr
        return pr

    def get_pr_metadata(self, job_id: str) -> Optional[PRMetadata]:
        return self._prs.get(job_id)
