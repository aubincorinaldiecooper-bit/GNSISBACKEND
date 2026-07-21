"""Connect reviewed outcomes to CodeMemory with auditable provenance.

This service is intentionally small: execution runs already pin retrieved memory,
CodeMemory already writes/retrieves approved intelligence, and job approvals are
already the reviewed-outcome record. The missing join was the approval-gated,
idempotent handoff from a reviewed outcome into CodeMemory plus queryable lineage.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence

from . import orm
from .codememory import CodeMemory, MemoryItem, MemoryKind
from .db import session_scope
from .executor.store import ExecutionStore
from .repository import PostgresJobStore


@dataclass(frozen=True)
class IntelligenceProvenance:
    memory_id: str
    kind: str
    item_key: Optional[str]
    source_run_id: str
    source_job_id: str
    outcome_id: int
    outcome_decision: str
    workspace_id: Optional[str]
    repository_id: Optional[str]


@dataclass(frozen=True)
class ReviewedIntelligenceItem:
    content: str
    kind: Optional[str] = None
    item_key: Optional[str] = None


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
        items = self.process_reviewed_outcome_items(
            outcome_id=outcome_id,
            intelligence_items=[
                ReviewedIntelligenceItem(
                    content=reusable_intelligence,
                    kind=kind,
                    item_key=kind,
                )
            ],
        )
        return items[0] if items else None

    def process_reviewed_outcome_items(
        self,
        *,
        outcome_id: int,
        intelligence_items: Sequence[ReviewedIntelligenceItem | dict],
    ) -> List[MemoryItem]:
        """Record one or more explicit intelligence items for one reviewed outcome.

        Each item must have a stable ``item_key`` unique within the outcome. The
        key gives same-kind items independent identity and makes retries safe.
        Existing single-item callers use the kind as their compatibility key.
        """
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

        normalized = []
        seen_keys = set()
        for raw in intelligence_items:
            item = raw if isinstance(raw, ReviewedIntelligenceItem) else ReviewedIntelligenceItem(**raw)
            text = (item.content or "").strip()
            if not text:
                continue
            memory_kind = item.kind or (
                MemoryKind.ACCEPTED_CHANGE
                if decision == "approved"
                else MemoryKind.REJECTION_LESSON
                if decision == "rejected"
                else ""
            )
            if memory_kind not in (MemoryKind.ACCEPTED_CHANGE, MemoryKind.REJECTION_LESSON):
                continue
            item_key = (item.item_key or memory_kind).strip()
            if not item_key:
                continue
            if item_key in seen_keys:
                raise ValueError(f"duplicate reviewed intelligence item_key: {item_key}")
            seen_keys.add(item_key)
            metadata = {
                "source_run_id": run.id,
                "source_job_id": job.id,
                "reviewed_outcome_id": approval_id,
                "reviewed_outcome_decision": decision,
                "reviewed_intelligence_item_key": item_key,
            }
            normalized.append(
                {
                    "content": text,
                    "kind": memory_kind,
                    "item_key": item_key,
                    "metadata": metadata,
                }
            )
        if not normalized:
            return []

        return self.memory.record_reviewed_intelligence_batch(
            repo=job.repo,
            source_job_id=job.id,
            source_run_id=run.id,
            outcome_id=approval_id,
            outcome_decision=decision,
            workspace_id=job.workspace_id,
            repository_id=job.repository_id,
            items=normalized,
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
                p.item_key,
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
                    r.item_key,
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
