"""SimpleMem adapter (placeholder).

Long-term agent memory is intentionally *not* implemented yet. When it is, it
will plug in here behind :class:`~gnsis.persistence.base.MemoryProvider`, using
SimpleMem as the provider with **repo-scoped, approval-gated** writes — never a
generic vector/RAG store. The seam exists now so adding it later touches nothing
above this file.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..persistence.base import MemoryProvider

_NOT_WIRED = (
    "SimpleMem provider is not wired yet. Long-term agent memory is on the "
    "roadmap (repo-scoped, approval-gated writes); use the file-backed Memory "
    "or a Postgres provider until then."
)


class SimpleMemAdapter(MemoryProvider):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._args = args
        self._kwargs = kwargs

    def remember(self, event: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError(_NOT_WIRED)

    def recall(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        raise NotImplementedError(_NOT_WIRED)

    def clear(self) -> None:
        raise NotImplementedError(_NOT_WIRED)
