"""Postgres-backed implementations of the core persistence contracts.

:class:`PostgresJobStore` satisfies :class:`gnsis.orchestration.store.JobStore`,
so the same pipeline that runs against the in-memory store in tests runs against
Postgres on Railway with no code change. :class:`PostgresResourceStore` mirrors
the file-backed :class:`gnsis.resources.store.ResourceStore` API so prompts and
their lineage become durable too.
"""

from __future__ import annotations

from typing import List, Optional

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

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
        workspace_id=row.workspace_id,
        repository_id=row.repository_id,
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
                workspace_id=spec.workspace_id,
                repository_id=spec.repository_id,
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

    def merge_context(self, job_id: str, updates: dict) -> JobRecord:
        with session_scope() as s:
            row = s.get(orm.Job, job_id)
            if row is None:
                raise KeyError(job_id)
            # Reassign (rather than mutate in place) so SQLAlchemy's change
            # tracking on the JSON column actually notices the update.
            row.context = {**(row.context or {}), **updates}
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
            row = orm.JobApproval(
                job_id=approval.job_id,
                decision=approval.decision,
                actor=approval.actor,
                note=approval.note,
            )
            s.add(row)
            s.flush()
            approval.id = row.id
            approval.created_at = (
                row.created_at.isoformat() if row.created_at else approval.created_at
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
                id=row.id,
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


def _mem_to_record(row: orm.AgentMemory) -> MemoryRecord:
    return MemoryRecord(
        repo=row.repo,
        content=row.content,
        kind=row.kind,
        metadata=dict(row.meta or {}),
        approved=row.approved,
        created_at=row.created_at.isoformat() if row.created_at else "",
        workspace_id=row.workspace_id,
        repository_id=row.repository_id,
        memory_id=row.memory_id,
        source_job_id=row.source_job_id,
    )


class PostgresMemoryProvider(MemoryProvider):
    """Durable, repo-scoped long-term memory backed by Postgres.

    This is the chosen memory backend for GNSIS. It honors the two invariants:
    writes are refused unless ``approved`` is true, and every read is filtered to
    a single repo, so one project's memory never leaks into another's context.
    The scoped read helpers (:meth:`recent_scoped`, :meth:`by_memory_ids`) add
    tenant-strict ``workspace_id`` filtering for the CodeMemory application layer;
    the base :class:`MemoryProvider` contract (repo/query/limit) is unchanged.
    """

    name = "postgres"

    def write(self, record: MemoryRecord):
        if not record.approved:
            return None  # approval-gated
        memory_id = record.memory_id or new_id("mem")
        with session_scope() as s:
            s.add(
                orm.AgentMemory(
                    repo=record.repo,
                    kind=record.kind,
                    content=record.content,
                    meta=dict(record.metadata),
                    approved=True,
                    workspace_id=record.workspace_id,
                    repository_id=record.repository_id,
                    memory_id=memory_id,
                    source_job_id=record.source_job_id,
                )
            )
        # Reflect the assigned handle back to the caller without another read.
        record.memory_id = memory_id
        return record


    def write_with_provenance(self, record: MemoryRecord, provenance: dict):
        """Persist approved memory and its reviewed-outcome provenance atomically.

        This is the CodeMemory lifecycle write path: either both the memory row
        and provenance row commit, or neither does. If a concurrent retry already
        created the same outcome/kind provenance, return that existing memory.
        """
        if not record.approved:
            return None
        memory_id = record.memory_id or new_id("mem")
        try:
            with session_scope() as s:
                existing = (
                    s.query(orm.MemoryProvenance)
                    .filter(
                        orm.MemoryProvenance.outcome_id == provenance["outcome_id"],
                        or_(
                            orm.MemoryProvenance.item_key
                            == provenance.get("item_key", record.kind),
                            (
                                orm.MemoryProvenance.item_key.is_(None)
                                & (orm.MemoryProvenance.kind == record.kind)
                            ),
                        ),
                    )
                    .one_or_none()
                )
                if existing is not None:
                    row = (
                        s.query(orm.AgentMemory)
                        .filter(orm.AgentMemory.memory_id == existing.memory_id)
                        .one_or_none()
                    )
                    if row is None:
                        return None
                    if row.kind != record.kind or row.content != record.content:
                        raise ValueError(
                            f"conflicting reviewed intelligence identity: "
                            f"{provenance.get('item_key', record.kind)}"
                        )
                    return _mem_to_record(row)
                s.add(
                    orm.AgentMemory(
                        repo=record.repo,
                        kind=record.kind,
                        content=record.content,
                        meta=dict(record.metadata),
                        approved=True,
                        workspace_id=record.workspace_id,
                        repository_id=record.repository_id,
                        memory_id=memory_id,
                        source_job_id=record.source_job_id,
                    )
                )
                s.add(
                    orm.MemoryProvenance(
                        memory_id=memory_id,
                        item_key=provenance.get("item_key", record.kind),
                        kind=record.kind,
                        source_run_id=provenance["source_run_id"],
                        source_job_id=provenance["source_job_id"],
                        outcome_id=provenance["outcome_id"],
                        outcome_decision=provenance["outcome_decision"],
                        workspace_id=provenance.get("workspace_id"),
                        repository_id=provenance.get("repository_id"),
                    )
                )
                record.memory_id = memory_id
                s.flush()
                return record
        except IntegrityError:
            # Concurrent duplicate: the unique outcome/kind or memory_id won in
            # another transaction. Resolve the already-created provenance.
            with session_scope() as s:
                existing = (
                    s.query(orm.MemoryProvenance)
                    .filter(
                        orm.MemoryProvenance.outcome_id == provenance["outcome_id"],
                        or_(
                            orm.MemoryProvenance.item_key
                            == provenance.get("item_key", record.kind),
                            (
                                orm.MemoryProvenance.item_key.is_(None)
                                & (orm.MemoryProvenance.kind == record.kind)
                            ),
                        ),
                    )
                    .one_or_none()
                )
                if existing is None:
                    raise
                row = (
                    s.query(orm.AgentMemory)
                    .filter(orm.AgentMemory.memory_id == existing.memory_id)
                    .one_or_none()
                )
                return _mem_to_record(row) if row else None

    def write_many_with_provenance(self, records: List[MemoryRecord], provenances: List[dict]):
        if len(records) != len(provenances):
            raise ValueError("records and provenances must have the same length")
        if any(not record.approved for record in records):
            return []
        keys = [p.get("item_key", r.kind) for r, p in zip(records, provenances)]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate provenance item_key in batch")
        with session_scope() as s:
            existing_rows = (
                s.query(orm.MemoryProvenance)
                .filter(
                    orm.MemoryProvenance.outcome_id == provenances[0]["outcome_id"],
                    or_(
                        orm.MemoryProvenance.item_key.in_(keys),
                        (
                            orm.MemoryProvenance.item_key.is_(None)
                            & orm.MemoryProvenance.kind.in_(
                                [r.kind for r, key in zip(records, keys) if key == r.kind]
                            )
                        ),
                    ),
                )
                .all()
            )
            by_key = {row.item_key or row.kind: row for row in existing_rows}
            output = []
            for record, provenance, item_key in zip(records, provenances, keys):
                existing = by_key.get(item_key)
                if existing is not None:
                    row = (
                        s.query(orm.AgentMemory)
                        .filter(orm.AgentMemory.memory_id == existing.memory_id)
                        .one_or_none()
                    )
                    if row is None:
                        raise ValueError(f"provenance points to missing memory: {existing.memory_id}")
                    if row.kind != record.kind or row.content != record.content:
                        raise ValueError(f"conflicting reviewed intelligence identity: {item_key}")
                    output.append(_mem_to_record(row))
                    continue
                memory_id = record.memory_id or new_id("mem")
                s.add(
                    orm.AgentMemory(
                        repo=record.repo,
                        kind=record.kind,
                        content=record.content,
                        meta=dict(record.metadata),
                        approved=True,
                        workspace_id=record.workspace_id,
                        repository_id=record.repository_id,
                        memory_id=memory_id,
                        source_job_id=record.source_job_id,
                    )
                )
                s.add(
                    orm.MemoryProvenance(
                        memory_id=memory_id,
                        item_key=item_key,
                        kind=record.kind,
                        source_run_id=provenance["source_run_id"],
                        source_job_id=provenance["source_job_id"],
                        outcome_id=provenance["outcome_id"],
                        outcome_decision=provenance["outcome_decision"],
                        workspace_id=provenance.get("workspace_id"),
                        repository_id=provenance.get("repository_id"),
                    )
                )
                record.memory_id = memory_id
                output.append(record)
            s.flush()
            return output

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
            return [_mem_to_record(r) for r in rows]

    # -- scoped reads for the CodeMemory application layer ------------------
    def recent_scoped(
        self,
        *,
        repo: str,
        workspace_id: Optional[str],
        repository_id: Optional[str],
        limit: int = 200,
    ) -> List[MemoryRecord]:
        """Most-recent approved memories for a repo, tenant-strict.

        Scopes to ``repo`` (globally-unique ``owner/name``) and, when a
        ``workspace_id`` is given, to rows owned by that workspace *or* legacy
        rows with no workspace set — never another workspace's rows. Ordered by
        id descending so the result is deterministic for a given DB state.
        """
        with session_scope() as s:
            q = s.query(orm.AgentMemory).filter(
                orm.AgentMemory.repo == repo,
                orm.AgentMemory.approved.is_(True),
            )
            if workspace_id:
                q = q.filter(
                    or_(
                        orm.AgentMemory.workspace_id == workspace_id,
                        orm.AgentMemory.workspace_id.is_(None),
                    )
                )
            if repository_id:
                q = q.filter(
                    or_(
                        orm.AgentMemory.repository_id == repository_id,
                        orm.AgentMemory.repository_id.is_(None),
                    )
                )
            rows = q.order_by(orm.AgentMemory.id.desc()).limit(limit).all()
            return [_mem_to_record(r) for r in rows]

    def by_memory_ids(
        self,
        *,
        memory_ids: List[str],
        workspace_id: Optional[str],
        repository_id: Optional[str],
        repo: Optional[str] = None,
    ) -> List[MemoryRecord]:
        """Look up specific memories by their stable handles, tenant-strict.

        Used to reconstruct the exact memory a run was pinned to. Applies the
        same workspace scoping as :meth:`recent_scoped` so a pinned id from
        another workspace can never be resolved.
        """
        if not memory_ids:
            return []
        with session_scope() as s:
            q = s.query(orm.AgentMemory).filter(
                orm.AgentMemory.memory_id.in_(list(memory_ids)),
                orm.AgentMemory.approved.is_(True),
            )
            if repo:
                q = q.filter(orm.AgentMemory.repo == repo)
            if workspace_id:
                q = q.filter(
                    or_(
                        orm.AgentMemory.workspace_id == workspace_id,
                        orm.AgentMemory.workspace_id.is_(None),
                    )
                )
            if repository_id:
                q = q.filter(
                    or_(
                        orm.AgentMemory.repository_id == repository_id,
                        orm.AgentMemory.repository_id.is_(None),
                    )
                )
            rows = q.all()
            by_id = {r.memory_id: _mem_to_record(r) for r in rows}
            # Preserve the caller's order and drop ids that didn't resolve.
            return [by_id[m] for m in memory_ids if m in by_id]
