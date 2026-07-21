"""Connect reviewed outcomes to CodeMemory with auditable provenance.

This service is intentionally small: execution runs already pin retrieved memory,
CodeMemory already writes/retrieves approved intelligence, and job approvals are
already the reviewed-outcome record. The missing join was the approval-gated,
idempotent handoff from a reviewed outcome into CodeMemory plus queryable lineage.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from . import orm
from .codememory import CodeMemory, MemoryItem, MemoryKind
from .db import session_scope
from .executor.store import ExecutionStore
from .repository import PostgresJobStore


@dataclass(frozen=True)
class ReviewedIntelligence:
    content: str
    kind: Optional[str] = None
    item_key: Optional[str] = None
    metadata: Optional[Dict[str, object]] = None


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
    item_key: str = ""
    content_hash: Optional[str] = None


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
        item_key: Optional[str] = None,
    ) -> Optional[MemoryItem]:
        """Record one explicit reviewed intelligence item."""
        items = self.process_reviewed_outcome_items(
            outcome_id=outcome_id,
            items=[
                ReviewedIntelligence(
                    content=reusable_intelligence,
                    kind=kind,
                    item_key=item_key,
                )
            ],
        )
        return items[0] if items else None

    def process_reviewed_outcome_items(
        self,
        *,
        outcome_id: int,
        items: Sequence[ReviewedIntelligence],
    ) -> List[MemoryItem]:
        """Record multiple explicit intelligence items for one persisted outcome."""
        with session_scope() as s:
            approval = s.get(orm.JobApproval, outcome_id)
            if approval is None:
                return []
            approval_id = approval.id
            job_id = approval.job_id
            decision = approval.decision

        job = self.jobs.get_job(job_id)
        run = self.runs.get_run_for_job(job_id)
        if job is None or run is None:
            return []

        default_kind = (
            MemoryKind.ACCEPTED_CHANGE
            if decision == "approved"
            else MemoryKind.REJECTION_LESSON
            if decision == "rejected"
            else ""
        )
        if not default_kind:
            return []

        base_metadata = {
            "source_run_id": run.id,
            "source_job_id": job.id,
            "reviewed_outcome_id": approval_id,
            "reviewed_outcome_decision": decision,
        }
        payloads = []
        for item in items:
            text = (item.content or "").strip()
            if not text:
                continue
            memory_kind = item.kind or default_kind
            if memory_kind not in MemoryKind.ALL:
                continue
            payloads.append(
                {
                    "content": text,
                    "kind": memory_kind,
                    "item_key": item.item_key,
                    "metadata": dict(item.metadata or {}),
                }
            )
        if not payloads:
            return []
        return self.memory.record_reviewed_intelligence_batch(
            repo=job.repo,
            source_job_id=job.id,
            source_run_id=run.id,
            outcome_id=approval_id,
            outcome_decision=decision,
            items=payloads,
            workspace_id=job.workspace_id,
            repository_id=job.repository_id,
            metadata=base_metadata,
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
                .first()
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
                p.item_key or "",
                p.content_hash,
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
                    r.item_key or "",
                    r.content_hash,
                )
                for r in rows
            ]

    def later_runs_that_received(self, memory_id: str):
        return self.runs.runs_that_consumed_memory(memory_id)
