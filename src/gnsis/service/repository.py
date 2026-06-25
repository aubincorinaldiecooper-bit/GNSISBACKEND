"""Postgres-backed implementations of the core persistence contracts.

:class:`PostgresJobStore` satisfies :class:`gnsis.orchestration.store.JobStore`,
so the same pipeline that runs against the in-memory store in tests runs against
Postgres on Railway with no code change. :class:`PostgresResourceStore` mirrors
the file-backed :class:`gnsis.resources.store.ResourceStore` API so prompts and
their lineage become durable too.
"""

from __future__ import annotations

from typing import List, Optional

from ..memory.base import MemoryProvider, MemoryRecord
from ..orchestration.models import (
    Approval,
    Checkpoint,
    Diff,
    JobRecord,
    JobSpec,
    LogEntry,
    PRMetadata,
    new_id,
)
from ..resources.resource import Resource, ResourceVersion
from . import orm
from .db import session_scope


def _job_to_record(row: orm.Job) -> JobRecord:
    return JobRecord(
        id=row.id,
        repo=row.repo,
        instruction=row.instruction,
        base_branch=row.base_branch,
        engine=row.engine,
        status=row.status,
        branch=row.branch,
        error=row.error,
        created_at=row.created_at.isoformat() if row.created_at else "",
        updated_at=row.updated_at.isoformat() if row.updated_at else "",
        context=dict(row.context or {}),
    )


class PostgresJobStore:
    """Durable :class:`JobStore`. Each call is its own transaction."""

    # -- jobs -------------------------------------------------------------
    def create_job(self, spec: JobSpec) -> JobRecord:
        job_id = new_id("job")
        branch = spec.branch or f"gnsis/{job_id}"
        with session_scope() as s:
            row = orm.Job(
                id=job_id,
                repo=spec.repo,
                instruction=spec.instruction,
                base_branch=spec.base_branch,
                engine=spec.engine,
                status="queued",
                branch=branch,
                context=dict(spec.context),
            )
            s.add(row)
            s.flush()
            return _job_to_record(row)

    def get_job(self, job_id: str) -> Optional[JobRecord]:
        with session_scope() as s:
            row = s.get(orm.Job, job_id)
            return _job_to_record(row) if row else None

    def list_jobs(self, limit: int = 50) -> List[JobRecord]:
        with session_scope() as s:
            rows = (
                s.query(orm.Job)
                .order_by(orm.Job.created_at.desc())
                .limit(limit)
                .all()
            )
            return [_job_to_record(r) for r in rows]

    def set_status(
        self, job_id: str, status: str, error: Optional[str] = None
    ) -> JobRecord:
        with session_scope() as s:
            row = s.get(orm.Job, job_id)
            if row is None:
                raise KeyError(job_id)
            row.status = status
            if error is not None:
                row.error = error
            s.flush()
            return _job_to_record(row)

    def set_branch(self, job_id: str, branch: str) -> JobRecord:
        with session_scope() as s:
            row = s.get(orm.Job, job_id)
            if row is None:
                raise KeyError(job_id)
            row.branch = branch
            s.flush()
            return _job_to_record(row)

    # -- phase records ----------------------------------------------------
    def append_log(self, entry: LogEntry) -> LogEntry:
        with session_scope() as s:
            s.add(
                orm.JobLog(
                    job_id=entry.job_id,
                    phase=entry.phase,
                    level=entry.level,
                    message=entry.message,
                    data=dict(entry.data),
                )
            )
        return entry

    def get_logs(self, job_id: str) -> List[LogEntry]:
        with session_scope() as s:
            rows = (
                s.query(orm.JobLog)
                .filter(orm.JobLog.job_id == job_id)
                .order_by(orm.JobLog.id)
                .all()
            )
            return [
                LogEntry(
                    job_id=r.job_id,
                    phase=r.phase,
                    level=r.level,
                    message=r.message,
                    data=dict(r.data or {}),
                    created_at=r.created_at.isoformat() if r.created_at else "",
                )
                for r in rows
            ]

    def save_checkpoint(self, checkpoint: Checkpoint) -> Checkpoint:
        with session_scope() as s:
            s.add(
                orm.JobCheckpoint(
                    job_id=checkpoint.job_id,
                    phase=checkpoint.phase,
                    content=checkpoint.content,
                )
            )
        return checkpoint

    def get_checkpoints(self, job_id: str) -> List[Checkpoint]:
        with session_scope() as s:
            rows = (
                s.query(orm.JobCheckpoint)
                .filter(orm.JobCheckpoint.job_id == job_id)
                .order_by(orm.JobCheckpoint.id)
                .all()
            )
            return [
                Checkpoint(
                    job_id=r.job_id,
                    phase=r.phase,
                    content=r.content,
                    created_at=r.created_at.isoformat() if r.created_at else "",
                )
                for r in rows
            ]

    def save_diff(self, diff: Diff) -> Diff:
        with session_scope() as s:
            row = s.get(orm.JobDiff, diff.job_id)
            if row is None:
                s.add(
                    orm.JobDiff(
                        job_id=diff.job_id,
                        patch=diff.patch,
                        files_changed=list(diff.files_changed),
                    )
                )
            else:
                row.patch = diff.patch
                row.files_changed = list(diff.files_changed)
        return diff

    def get_diff(self, job_id: str) -> Optional[Diff]:
        with session_scope() as s:
            row = s.get(orm.JobDiff, job_id)
            if row is None:
                return None
            return Diff(
                job_id=row.job_id,
                patch=row.patch,
                files_changed=list(row.files_changed or []),
                created_at=row.created_at.isoformat() if row.created_at else "",
            )

    # -- approvals --------------------------------------------------------
    def save_approval(self, approval: Approval) -> Approval:
        with session_scope() as s:
            s.add(
                orm.JobApproval(
                    job_id=approval.job_id,
                    decision=approval.decision,
                    actor=approval.actor,
                    note=approval.note,
                )
            )
        return approval

    def get_latest_approval(self, job_id: str) -> Optional[Approval]:
        with session_scope() as s:
            row = (
                s.query(orm.JobApproval)
                .filter(orm.JobApproval.job_id == job_id)
                .order_by(orm.JobApproval.id.desc())
                .first()
            )
            if row is None:
                return None
            return Approval(
                job_id=row.job_id,
                decision=row.decision,
                actor=row.actor,
                note=row.note,
                created_at=row.created_at.isoformat() if row.created_at else "",
            )

    # -- PR metadata ------------------------------------------------------
    def save_pr_metadata(self, pr: PRMetadata) -> PRMetadata:
        with session_scope() as s:
            row = s.get(orm.PullRequest, pr.job_id)
            if row is None:
                s.add(
                    orm.PullRequest(
                        job_id=pr.job_id,
                        number=pr.number,
                        url=pr.url,
                        branch=pr.branch,
                        head_sha=pr.head_sha,
                    )
                )
            else:
                row.number, row.url, row.branch, row.head_sha = (
                    pr.number,
                    pr.url,
                    pr.branch,
                    pr.head_sha,
                )
        return pr

    def get_pr_metadata(self, job_id: str) -> Optional[PRMetadata]:
        with session_scope() as s:
            row = s.get(orm.PullRequest, job_id)
            if row is None:
                return None
            return PRMetadata(
                job_id=row.job_id,
                number=row.number,
                url=row.url,
                branch=row.branch,
                head_sha=row.head_sha,
                created_at=row.created_at.isoformat() if row.created_at else "",
            )


class PostgresResourceStore:
    """Durable RSPL: mirrors :class:`gnsis.resources.store.ResourceStore`.

    Only the methods the runtime relies on are implemented; the storage moves
    from JSON files to Postgres so prompt lineage survives Railway restarts.
    """

    def _version_to_dataclass(self, row: orm.ResourceVersionRecord) -> ResourceVersion:
        return ResourceVersion(
            resource_id=row.resource_id,
            kind=row.kind,
            name=row.name,
            version=row.version,
            content=row.content,
            content_hash=row.content_hash,
            parent_version=row.parent_version,
            message=row.message,
            created_at=row.created_at,
        )

    def exists(self, kind: str, name: str) -> bool:
        with session_scope() as s:
            return s.get(orm.ResourceRecord, Resource.make_id(kind, name)) is not None

    def commit(
        self,
        kind: str,
        name: str,
        content,
        message: str = "",
        parent_version: Optional[int] = None,
    ) -> ResourceVersion:
        resource_id = Resource.make_id(kind, name)
        with session_scope() as s:
            rec = s.get(orm.ResourceRecord, resource_id)
            if rec is None:
                rec = orm.ResourceRecord(resource_id=resource_id, kind=kind, name=name)
                s.add(rec)
                s.flush()
                next_version = 1
                parent = None
            else:
                last = (
                    s.query(orm.ResourceVersionRecord)
                    .filter(orm.ResourceVersionRecord.resource_id == resource_id)
                    .order_by(orm.ResourceVersionRecord.version.desc())
                    .first()
                )
                next_version = (last.version if last else 0) + 1
                parent = (
                    (last.version if last else None)
                    if parent_version is None
                    else parent_version
                )
            version = ResourceVersion.create(
                resource_id=resource_id,
                kind=kind,
                name=name,
                version=next_version,
                content=content,
                parent_version=parent,
                message=message,
            )
            s.add(
                orm.ResourceVersionRecord(
                    resource_id=resource_id,
                    kind=kind,
                    name=name,
                    version=version.version,
                    content=version.content,
                    content_hash=version.content_hash,
                    parent_version=version.parent_version,
                    message=version.message,
                    created_at=version.created_at,
                )
            )
            return version

    def head(self, kind: str, name: str) -> Optional[ResourceVersion]:
        resource_id = Resource.make_id(kind, name)
        with session_scope() as s:
            row = (
                s.query(orm.ResourceVersionRecord)
                .filter(orm.ResourceVersionRecord.resource_id == resource_id)
                .order_by(orm.ResourceVersionRecord.version.desc())
                .first()
            )
            return self._version_to_dataclass(row) if row else None

    def history(self, kind: str, name: str) -> List[ResourceVersion]:
        resource_id = Resource.make_id(kind, name)
        with session_scope() as s:
            rows = (
                s.query(orm.ResourceVersionRecord)
                .filter(orm.ResourceVersionRecord.resource_id == resource_id)
                .order_by(orm.ResourceVersionRecord.version)
                .all()
            )
            return [self._version_to_dataclass(r) for r in rows]


class PostgresMemoryProvider(MemoryProvider):
    """Durable, repo-scoped long-term memory backed by Postgres.

    This is the chosen memory backend for GNSIS. It honors the two invariants:
    writes are refused unless ``approved`` is true, and every read is filtered to
    a single repo, so one project's memory never leaks into another's context.
    """

    name = "postgres"

    def write(self, record: MemoryRecord):
        if not record.approved:
            return None  # approval-gated
        with session_scope() as s:
            s.add(
                orm.AgentMemory(
                    repo=record.repo,
                    kind=record.kind,
                    content=record.content,
                    meta=dict(record.metadata),
                    approved=True,
                )
            )
        return record

    def search(self, repo: str, query: str, limit: int = 5):
        # Lightweight relevance: rank repo-scoped rows by query-term overlap.
        # (Swap in pgvector/full-text later without changing the interface.)
        rows = self.recent(repo, limit=200)
        terms = [t for t in query.lower().split() if t]
        scored = []
        for rec in rows:
            score = sum(1 for t in terms if t in rec.content.lower())
            if score:
                scored.append((score, rec))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [rec for _, rec in scored[:limit]]

    def recent(self, repo: str, limit: int = 20):
        with session_scope() as s:
            rows = (
                s.query(orm.AgentMemory)
                .filter(orm.AgentMemory.repo == repo)
                .order_by(orm.AgentMemory.id.desc())
                .limit(limit)
                .all()
            )
            return [
                MemoryRecord(
                    repo=r.repo,
                    content=r.content,
                    kind=r.kind,
                    metadata=dict(r.meta or {}),
                    approved=r.approved,
                    created_at=r.created_at.isoformat() if r.created_at else "",
                )
                for r in rows
            ]
