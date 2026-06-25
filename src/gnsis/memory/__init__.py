"""Memory.

Two distinct things live here, deliberately kept apart:

* :class:`Memory` — the durable cross-run *event log* ("Remember"): a simple,
  append-only record of what the evolution loop did. Always on.
* :class:`MemoryProvider` and friends — the *long-term agent memory* interface
  (repo-scoped, approval-gated) that will let GNSIS specialize to a codebase.
  Only the interface and a no-op default ship today; the real ``SimpleMem``
  provider is a stub.
"""

from .base import (
    InMemoryMemoryProvider,
    MemoryProvider,
    MemoryRecord,
    NullMemoryProvider,
    SimpleMemProvider,
)
from .memory import Memory

__all__ = [
    "Memory",
    "MemoryProvider",
    "MemoryRecord",
    "NullMemoryProvider",
    "InMemoryMemoryProvider",
    "SimpleMemProvider",
]
