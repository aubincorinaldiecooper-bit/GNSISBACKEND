"""Long-term agent memory — the interface, not (yet) the implementation.

This is the seam for the thing that lets GNSIS *specialize to how you work*:
durable, cross-session knowledge of a repo's conventions, decisions, and which
changes were accepted or rejected and why. The underlying coding model is rented
and can't be out-coded; this layer is where a real, compounding edge lives.

Per the project's deliberate scope, **no generic vector/RAG memory is implemented
yet.** We ship:

* :class:`MemoryProvider` — the contract every provider satisfies.
* :class:`NullMemoryProvider` — the safe default: writes are accepted and dropped,
  reads return nothing, so the rest of the system runs unchanged.
* :class:`SimpleMemProvider` — a named placeholder for the intended provider
  (`SimpleMem`). It is intentionally inert until built.

Two invariants the design bakes in for when memory *is* built:

* **Repo-scoped** — every record is namespaced to a repo, so one project's memory
  never leaks into another's context.
* **Approval-gated writes** — only *validated* outcomes (an approved change, a
  confirmed decision) should be committed to memory. Low-signal chatter must not
  pollute it. The ``approved`` flag on :meth:`MemoryProvider.write` carries that
  intent end-to-end.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


@dataclass
class MemoryRecord:
    """One durable, repo-scoped memory.

    ``repo`` has always been the primary namespace. The optional
    ``workspace_id``/``repository_id`` add tenant-strict scoping, ``memory_id`` is
    a stable public handle assigned on write, and ``source_job_id`` records the
    approved job that produced it. All four default to ``None`` so existing
    callers and providers are unaffected.
    """

    repo: str
    content: str
    kind: str = "note"  # e.g. "convention" | "decision" | "accepted_change"
    metadata: Dict[str, Any] = field(default_factory=dict)
    approved: bool = False
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    workspace_id: Optional[str] = None
    repository_id: Optional[str] = None
    memory_id: Optional[str] = None
    source_job_id: Optional[str] = None


class MemoryProvider(ABC):
    """Pluggable long-term memory. Implementations must honor the two invariants
    documented in this module: repo scoping and approval-gated writes."""

    name: str = "base"

    @abstractmethod
    def write(self, record: MemoryRecord) -> Optional[MemoryRecord]:
        """Persist a memory. Implementations should refuse (return ``None``) any
        record whose ``approved`` flag is ``False`` once gating is enforced."""

    @abstractmethod
    def search(self, repo: str, query: str, limit: int = 5) -> List[MemoryRecord]:
        """Return memories relevant to ``query`` *within ``repo`` only*."""

    @abstractmethod
    def recent(self, repo: str, limit: int = 20) -> List[MemoryRecord]:
        """Return the most recent memories for ``repo``."""


class NullMemoryProvider(MemoryProvider):
    """The default: behaves like a system with no long-term memory.

    Writes are accepted and discarded; reads are empty. This keeps long-term
    memory *off* until a real provider is wired in, without sprinkling
    ``if memory is not None`` across the codebase.
    """

    name = "null"

    def write(self, record: MemoryRecord) -> Optional[MemoryRecord]:
        return None

    def search(self, repo: str, query: str, limit: int = 5) -> List[MemoryRecord]:
        return []

    def recent(self, repo: str, limit: int = 20) -> List[MemoryRecord]:
        return []


class InMemoryMemoryProvider(MemoryProvider):
    """A process-local provider for tests and single-process local runs.

    Enforces the two invariants so the contract is exercised offline: writes are
    refused unless ``approved`` is true, and every read is filtered to one repo.
    Not durable — the Postgres provider is the real backend.
    """

    name = "memory"

    def __init__(self) -> None:
        self._by_repo: Dict[str, List[MemoryRecord]] = {}

    def write(self, record: MemoryRecord) -> Optional[MemoryRecord]:
        if not record.approved:
            return None  # approval-gated: only validated outcomes persist
        self._by_repo.setdefault(record.repo, []).append(record)
        return record

    def search(self, repo: str, query: str, limit: int = 5) -> List[MemoryRecord]:
        terms = [t for t in query.lower().split() if t]
        scored = []
        for rec in self._by_repo.get(repo, []):
            haystack = rec.content.lower()
            score = sum(1 for t in terms if t in haystack)
            if score:
                scored.append((score, rec))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [rec for _, rec in scored[:limit]]

    def recent(self, repo: str, limit: int = 20) -> List[MemoryRecord]:
        return list(reversed(self._by_repo.get(repo, [])))[:limit]


class SimpleMemProvider(MemoryProvider):
    """Optional future ``SimpleMem``-backed provider (not the chosen default).

    Postgres is the selected memory backend for GNSIS (see
    :class:`gnsis.service.repository.PostgresMemoryProvider`); SimpleMem remains
    a possible alternative adapter and is intentionally left unimplemented so it
    can never be enabled by accident before it is real.
    """

    name = "simplemem"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(
            "SimpleMem-backed memory is not implemented. The chosen backend is "
            "Postgres (PostgresMemoryProvider); use that or NullMemoryProvider."
        )

    def write(self, record: MemoryRecord) -> Optional[MemoryRecord]:  # pragma: no cover
        raise NotImplementedError

    def search(self, repo: str, query: str, limit: int = 5) -> List[MemoryRecord]:  # pragma: no cover
        raise NotImplementedError

    def recent(self, repo: str, limit: int = 20) -> List[MemoryRecord]:  # pragma: no cover
        raise NotImplementedError
