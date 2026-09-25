"""Memory.

Two distinct things live here, deliberately kept apart:

* :class:`Memory` — the durable cross-run *event log* ("Remember"): a simple,
  append-only record of what the evolution loop did. Always on.
* :class:`MemoryProvider` and friends — the *long-term agent memory* interface
  (repo-scoped, approval-gated) that will let GNSIS specialize to a codebase.
  ``SimpleMemProvider`` is the general durable recall surface (Omni-SimpleMem);
  Postgres remains the audit store for approved coding intelligence.
"""

from .base import (
    InMemoryMemoryProvider,
    MemoryProvider,
    MemoryRecord,
    NullMemoryProvider,
)
from .memory import Memory
from .simplemem import MEMORY_TYPES, SimpleMemProvider, SimpleMemUnavailable

__all__ = [
    "Memory",
    "MemoryProvider",
    "MemoryRecord",
    "NullMemoryProvider",
    "InMemoryMemoryProvider",
    "SimpleMemProvider",
    "SimpleMemUnavailable",
    "MEMORY_TYPES",
]
