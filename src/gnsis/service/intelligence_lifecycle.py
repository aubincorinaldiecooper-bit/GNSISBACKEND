"""Connect reviewed outcomes to CodeMemory with auditable provenance.

This service is intentionally small: execution runs already pin retrieved memory,
CodeMemory already writes/retrieves approved intelligence, and job approvals are
already the reviewed-outcome record. The missing join was the approval-gated,
idempotent handoff from a reviewed outcome into CodeMemory plus queryable lineage.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import List, Optional, Sequence

from . import orm
from .codememory import CodeMemory, MemoryItem, MemoryKind
from .db import session_scope
from .executor.store import ExecutionStore
from .repository import PostgresJobStore

#: ceiling on how many candidate lessons one run's evidence can propose
_MAX_PROPOSALS = 5
#: below this many characters a sentence is too thin to be a durable lesson
_MIN_CONTENT_CHARS = 20
_MAX_CONTENT_CHARS = 400
#: below this many words a sentence reads as generic filler, not a lesson
_MIN_CONTENT_WORDS = 4

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.;\n])\s+")

# Deterministic, keyword-based kind classification — no model call, so the
# same evidence always yields the same proposals. Order matters: first match
# wins, most specific categories first.
_KIND_KEYWORDS: Sequence[tuple] = (
    (MemoryKind.SECURITY_CONSTRAINT, (
        "security", "vulnerab", "auth", "credential", "secret", "sanitiz",
        "injection", "csrf", "xss", "permission", "unsafe", "exploit",
    )),
    (MemoryKind.TESTING_CONSTRAINT, (
        "test", "pytest", "assert", "coverage", "regression", "spec ",
    )),
    (MemoryKind.DEPENDENCY_PREFERENCE, (
        "dependency", "package", "library", "requirements.txt", "pip install",
        "npm install", "version bump", "upgrade",
    )),
    (MemoryKind.CONVENTION, (
        "convention", "style", "naming", "formatting", "lint", "pattern used",
    )),
    (MemoryKind.ARCHITECTURAL_DECISION, (
        "architecture", "module", "service layer", "integration", "interface",
        "design", "boundary",
    )),
)


def _classify_kind(content: str) -> str:
    lowered = content.lower()
    for kind, keywords in _KIND_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return kind
    return MemoryKind.ACCEPTED_CHANGE


@dataclass(frozen=True)
class ProposedIntelligenceItem:
    """One deterministic candidate lesson derived from a run's own evidence."""

    item_key: str
    content: str
    kind: str


def propose_intelligence_for_run(run) -> List[ProposedIntelligenceItem]:
    """Deterministically derive 0..N candidate lessons from a run's evidence.

    The sole input is ``run.outcome_summary`` — the agent's own account of what
    it did, captured from ``receipt.json`` at completion. The task instruction
    is never consulted, so proposals describe what happened, not what was
    asked for. No model call: the same evidence always yields the same
    proposals, so a reviewer's decision is reproducible and re-derivable for
    audit. A run with no summary, or only generic/short filler, proposes zero
    items — that is a correct outcome, not a bug.
    """
    summary = (getattr(run, "outcome_summary", None) or "").strip()
    if not summary:
        return []

    candidates: List[str] = []
    seen: set = set()
    for raw in _SENTENCE_SPLIT_RE.split(summary):
        text = raw.strip(" \t\n.;")
        if len(text) < _MIN_CONTENT_CHARS or len(text.split()) < _MIN_CONTENT_WORDS:
            continue
        normalized = " ".join(text.lower().split())
        if normalized in seen:
            continue
        seen.add(normalized)
        candidates.append(text[:_MAX_CONTENT_CHARS])
        if len(candidates) >= _MAX_PROPOSALS:
            break

    items = []
    for index, content in enumerate(candidates):
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:10]
        items.append(
            ProposedIntelligenceItem(
                item_key=f"prop-{index}-{digest}", content=content, kind=_classify_kind(content)
            )
        )
    return items


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
            if memory_kind not in MemoryKind.ALL:
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

    def capture_selected_intelligence(
        self, *, job_id: str, approval_id: int, selections: Sequence[dict]
    ) -> List[MemoryItem]:
        """Persist only the reviewer-selected intelligence for one approved run.

        Approval — not publication — is the trust boundary for reusable
        intelligence: an authorized human (or scoped key) accepting the run's
        outcome is what makes its lesson trustworthy, and an external API
        client must not have to wait on a pull-request publish that may be
        deferred, retried, or fail for unrelated infrastructure reasons.
        Publishing performs no intelligence capture of its own.

        ``selections`` is the reviewer's explicit decision at approval time:
        each entry names one of *this run's own* deterministic proposals (see
        :func:`propose_intelligence_for_run`, derived solely from the run's
        evidence, never the task instruction) by ``item_key``, and may supply
        edited ``content`` and/or a reclassified ``kind``. An ``item_key`` that
        does not match a proposal this run's own evidence actually produced is
        ignored — a reviewer selects and edits, but cannot inject fabricated
        intelligence unrelated to what happened. Approving with no selections
        (the default) creates zero intelligence; only explicitly selected
        items become active. A rejected or unknown outcome never reaches
        persistence.
        """
        if not selections:
            return []
        with session_scope() as s:
            approval = s.get(orm.JobApproval, approval_id)
            if approval is None or approval.job_id != job_id or approval.decision != "approved":
                return []
        run = self.runs.get_run_for_job(job_id)
        if run is None:
            return []
        proposals = {p.item_key: p for p in propose_intelligence_for_run(run)}
        items: List[ReviewedIntelligenceItem] = []
        for raw in selections:
            selection = raw if isinstance(raw, dict) else dict(raw)
            item_key = str(selection.get("item_key") or "").strip()
            proposal = proposals.get(item_key)
            if proposal is None:
                continue
            content = str(selection.get("content") or proposal.content).strip()[:_MAX_CONTENT_CHARS]
            if not content:
                continue
            kind = selection.get("kind") or proposal.kind
            if kind not in MemoryKind.ALL:
                kind = proposal.kind
            items.append(ReviewedIntelligenceItem(content=content, kind=kind, item_key=item_key))
        if not items:
            return []
        return self.process_reviewed_outcome_items(outcome_id=approval_id, intelligence_items=items)

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

    def full_provenance_for_memories(self, memory_ids: Sequence[str]) -> dict:
        """Batch-fetch the complete retained provenance for a set of active items.

        Every active item retains its source run, source approval, source
        model, policy version and the evidence item_key it was captured from —
        this is the one place that joins ``memory_provenance`` back to
        ``execution_runs`` to surface all of it together for the public
        intelligence view. Keyed by memory_id; a memory with no provenance row
        (should not happen for anything captured through this lifecycle) is
        simply absent from the result.
        """
        ids = [m for m in memory_ids if m]
        if not ids:
            return {}
        with session_scope() as s:
            rows = (
                s.query(orm.MemoryProvenance, orm.ExecutionRun)
                .join(orm.ExecutionRun, orm.ExecutionRun.id == orm.MemoryProvenance.source_run_id)
                .filter(orm.MemoryProvenance.memory_id.in_(ids))
                .all()
            )
            out = {}
            for prov, run in rows:
                out[prov.memory_id] = {
                    "source_run_id": prov.source_run_id,
                    "source_job_id": prov.source_job_id,
                    "source_approval_id": prov.outcome_id,
                    "item_key": prov.item_key,
                    "source_model": run.primary_model,
                    "policy_version": run.policy_version,
                }
            return out
