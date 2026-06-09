"""RSPL — the Resource Substrate Protocol Layer.

In Autogenesis, prompts, agents, tools, environments, and memory are treated as
*versioned, lifecycle-aware resources*. This module provides the minimal,
faithful core of that idea: an append-only version history with content
hashing and explicit lineage (each version records its parent). That is what
makes evolution auditable and rollback possible.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def canonical_hash(content: Any) -> str:
    """A stable SHA-256 over JSON-canonicalized content.

    Hashing canonical JSON (sorted keys, no insignificant whitespace) means two
    semantically identical resources hash identically regardless of how they
    were constructed — the basis for detecting "did this actually change?".
    """
    payload = json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ResourceVersion:
    """One immutable point in a resource's history."""

    resource_id: str
    kind: str
    name: str
    version: int
    content: Any
    content_hash: str
    parent_version: Optional[int]
    message: str
    created_at: str

    @classmethod
    def create(
        cls,
        resource_id: str,
        kind: str,
        name: str,
        version: int,
        content: Any,
        parent_version: Optional[int],
        message: str,
    ) -> "ResourceVersion":
        return cls(
            resource_id=resource_id,
            kind=kind,
            name=name,
            version=version,
            content=content,
            content_hash=canonical_hash(content),
            parent_version=parent_version,
            message=message,
            created_at=_now(),
        )

    @property
    def short_hash(self) -> str:
        return self.content_hash[:12]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ResourceVersion":
        return cls(**data)


@dataclass
class Resource:
    """A logical resource with an append-only list of versions."""

    resource_id: str
    kind: str
    name: str
    versions: List[ResourceVersion] = field(default_factory=list)

    @staticmethod
    def make_id(kind: str, name: str) -> str:
        return f"{kind}:{name}"

    @property
    def head(self) -> ResourceVersion:
        if not self.versions:
            raise ValueError(f"resource {self.resource_id} has no versions")
        return self.versions[-1]

    def get(self, version: int) -> ResourceVersion:
        for candidate in self.versions:
            if candidate.version == version:
                return candidate
        raise KeyError(f"{self.resource_id} has no version {version}")

    def lineage(self, version: Optional[int] = None) -> List[ResourceVersion]:
        """Walk parent links from ``version`` (default: head) back to the root."""
        if not self.versions:
            return []
        cursor: Optional[int] = self.head.version if version is None else version
        chain: List[ResourceVersion] = []
        by_version = {v.version: v for v in self.versions}
        while cursor is not None and cursor in by_version:
            node = by_version[cursor]
            chain.append(node)
            cursor = node.parent_version
        return list(reversed(chain))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "kind": self.kind,
            "name": self.name,
            "versions": [v.to_dict() for v in self.versions],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Resource":
        return cls(
            resource_id=data["resource_id"],
            kind=data["kind"],
            name=data["name"],
            versions=[ResourceVersion.from_dict(v) for v in data.get("versions", [])],
        )
