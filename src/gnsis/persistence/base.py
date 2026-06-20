"""Abstract storage backends (the persistence seam).

These interfaces are intentionally dependency-free: the file backends satisfy
them today, and a Postgres backend will satisfy them on Railway. Keeping the
abstractions here (rather than importing domain types) avoids import cycles.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class ResourceStoreBackend(ABC):
    """Versioned resource persistence (RSPL)."""

    @abstractmethod
    def commit(
        self,
        kind: str,
        name: str,
        content: Any,
        message: str = "",
        parent_version: Optional[int] = None,
    ): ...

    @abstractmethod
    def head(self, kind: str, name: str): ...

    @abstractmethod
    def get(self, kind: str, name: str, version: int): ...

    @abstractmethod
    def history(self, kind: str, name: str): ...

    @abstractmethod
    def rollback(self, kind: str, name: str, to_version: int, message: Optional[str] = None): ...

    @abstractmethod
    def delete(self, kind: str, name: str) -> bool: ...

    @abstractmethod
    def list_resources(self): ...


class MemoryProvider(ABC):
    """Durable event memory. File today; SimpleMem (repo-scoped) later."""

    @abstractmethod
    def remember(self, event: Dict[str, Any]) -> Dict[str, Any]: ...

    @abstractmethod
    def recall(self, limit: Optional[int] = None) -> List[Dict[str, Any]]: ...

    @abstractmethod
    def clear(self) -> None: ...
