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
    """One durable, repo-scoped memory."""

    repo: str
    content: str
    kind: str = "note"  # e.g. "convention" | "decision" | "accepted_change"
    metadata: Dict[str, Any] = field(default_factory=dict)
    approved: bool = False
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


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


class SimpleMemProvider(MemoryProvider):
    """Placeholder for the intended ``SimpleMem``-backed provider.

    When implemented, this is the long-term memory provider for GNSIS, with
    repo-scoped namespaces and approval-gated writes. It is intentionally not
    implemented yet — constructing or calling it raises, so it can never be
    enabled by accident before it is real.
    """

    name = "simplemem"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(
            "SimpleMem-backed memory is not implemented yet. Use NullMemoryProvider "
            "until the SimpleMem adapter (repo-scoped, approval-gated) is built."
        )

    def write(self, record: MemoryRecord) -> Optional[MemoryRecord]:  # pragma: no cover
        raise NotImplementedError

    def search(self, repo: str, query: str, limit: int = 5) -> List[MemoryRecord]:  # pragma: no cover
        raise NotImplementedError

    def recent(self, repo: str, limit: int = 20) -> List[MemoryRecord]:  # pragma: no cover
        raise NotImplementedError
