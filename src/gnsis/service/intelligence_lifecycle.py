"""Connect reviewed outcomes to CodeMemory with auditable provenance.

This service is intentionally small: execution runs already pin retrieved memory,
CodeMemory already writes/retrieves approved intelligence, and job approvals are
already the reviewed-outcome record. The missing join was the approval-gated,
idempotent handoff from a reviewed outcome into CodeMemory plus queryable lineage.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from . import orm
from .codememory import CodeMemory, MemoryItem, MemoryKind
from .db import session_scope
from .executor.store import ExecutionStore
from .repository import PostgresJobStore


@dataclass(frozen=True)
class IntelligenceProvenance:
    memory_id: str
    kind: str
    source_run_id: str
    source_job_id: str
    outcome_id: int
    outcome_decision: str
    workspace_id: Optional[str]
    repository_id: Optional[str]


class IntelligenceLifecycle:
    """Approval-gated lifecycle coordinator for durable intelligence."""

    def __init__(
        self,
        *,
        jobs: Optional[PostgresJobStore] = None,
        runs: Optional[ExecutionStore] = None,
        memory: Optional[CodeMemory] = None,
    ) -> None:
        self.jobs = jobs or PostgresJobStore()
        self.runs = runs or ExecutionStore()
        self.memory = memory or CodeMemory()

    def process_reviewed_outcome(
        self,
        *,
        outcome_id: int,
        reusable_intelligence: str,
        kind: Optional[str] = None,
    ) -> Optional[MemoryItem]:
        """Record explicit reviewed feedback for one persisted outcome exactly once."""
        with session_scope() as s:
            approval = s.get(orm.JobApproval, outcome_id)
            if approval is None:
                return None
            approval_id = approval.id
            job_id = approval.job_id
            decision = approval.decision

        job = self.jobs.get_job(job_id)
        run = self.runs.get_run_for_job(job_id)
        text = (reusable_intelligence or "").strip()
        if job is None or run is None or not text:
            return None

        memory_kind = kind or (
            MemoryKind.ACCEPTED_CHANGE
            if decision == "approved"
            else MemoryKind.REJECTION_LESSON
            if decision == "rejected"
            else ""
        )
        if memory_kind not in (MemoryKind.ACCEPTED_CHANGE, MemoryKind.REJECTION_LESSON):
            return None

        existing = self._memory_for_outcome(
            approval_id, memory_kind, job.workspace_id, job.repository_id, job.repo
        )
        if existing is not None:
            return existing

        metadata = {
            "source_run_id": run.id,
            "source_job_id": job.id,
            "reviewed_outcome_id": approval_id,
            "reviewed_outcome_decision": decision,
        }
        return self.memory.record_reviewed_intelligence(
            repo=job.repo,
            source_job_id=job.id,
            source_run_id=run.id,
            outcome_id=approval_id,
            outcome_decision=decision,
            content=text,
            kind=memory_kind,
            workspace_id=job.workspace_id,
            repository_id=job.repository_id,
            metadata=metadata,
        )

    def process_latest_reviewed_outcome(
        self,
        *,
        job_id: str,
        reusable_intelligence: str,
        kind: Optional[str] = None,
    ) -> Optional[MemoryItem]:
        """Compatibility wrapper; production paths should pass outcome_id."""
        approval = self.jobs.get_latest_approval(job_id)
        if approval is None or approval.id is None:
            return None
        return self.process_reviewed_outcome(
            outcome_id=approval.id,
            reusable_intelligence=reusable_intelligence,
            kind=kind,
        )

    def _memory_for_outcome(
        self,
        outcome_id: int,
        kind: str,
        workspace_id: Optional[str],
        repository_id: Optional[str],
        repo: str,
    ) -> Optional[MemoryItem]:
        with session_scope() as s:
            prov = (
                s.query(orm.MemoryProvenance)
                .filter(
                    orm.MemoryProvenance.outcome_id == outcome_id,
                    orm.MemoryProvenance.kind == kind,
                )
                .one_or_none()
            )
            memory_id = prov.memory_id if prov else None
        if not memory_id:
            return None
        found = self.memory.get_records_by_ids(
            memory_ids=[memory_id],
            workspace_id=workspace_id,
            repository_id=repository_id,
            repo=repo,
        )
        return found[0] if found else None

    def provenance_for_memory(self, memory_id: str) -> Optional[IntelligenceProvenance]:
        with session_scope() as s:
            p = (
                s.query(orm.MemoryProvenance)
                .filter(orm.MemoryProvenance.memory_id == memory_id)
                .one_or_none()
            )
            if p is None:
                return None
            return IntelligenceProvenance(
                p.memory_id,
                p.kind,
                p.source_run_id,
                p.source_job_id,
                p.outcome_id,
                p.outcome_decision,
                p.workspace_id,
                p.repository_id,
            )

    def intelligence_from_run(self, run_id: str) -> List[IntelligenceProvenance]:
        with session_scope() as s:
            rows = (
                s.query(orm.MemoryProvenance)
                .filter(orm.MemoryProvenance.source_run_id == run_id)
                .order_by(orm.MemoryProvenance.id)
                .all()
            )
            return [
                IntelligenceProvenance(
                    r.memory_id,
                    r.kind,
                    r.source_run_id,
                    r.source_job_id,
                    r.outcome_id,
                    r.outcome_decision,
                    r.workspace_id,
                    r.repository_id,
                )
                for r in rows
            ]

    def later_runs_that_received(self, memory_id: str):
        return self.runs.runs_that_consumed_memory(memory_id)
