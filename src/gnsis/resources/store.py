"""A small, file-backed store for versioned resources.

Each resource lives at ``<workdir>/resources/<kind>/<name>.json`` so the entire
substrate is human-inspectable — you can ``cat`` a prompt's full evolutionary
history. The store is the concrete home of the RSPL: commit, history, checkout,
and rollback.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, List, Optional, Tuple

from ..persistence.base import ResourceStoreBackend
from .resource import Resource, ResourceVersion

_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def _slug(value: str) -> str:
    return _SAFE.sub("_", value)


class ResourceStore(ResourceStoreBackend):
    """Versioned persistence for :class:`Resource` objects (file-backed)."""

    def __init__(self, workdir: str) -> None:
        self.root = os.path.join(workdir, "resources")

    # -- paths ------------------------------------------------------------
    def _path(self, kind: str, name: str) -> str:
        return os.path.join(self.root, _slug(kind), f"{_slug(name)}.json")

    def exists(self, kind: str, name: str) -> bool:
        return os.path.exists(self._path(kind, name))

    # -- load / save ------------------------------------------------------
    def load(self, kind: str, name: str) -> Optional[Resource]:
        path = self._path(kind, name)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as handle:
            return Resource.from_dict(json.load(handle))

    def _save(self, resource: Resource) -> None:
        path = self._path(resource.kind, resource.name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(resource.to_dict(), handle, indent=2, ensure_ascii=False)
        os.replace(tmp, path)  # atomic on POSIX

    # -- commands ---------------------------------------------------------
    def commit(
        self,
        kind: str,
        name: str,
        content: Any,
        message: str = "",
        parent_version: Optional[int] = None,
    ) -> ResourceVersion:
        """Append a new version of a resource and return it."""
        resource = self.load(kind, name)
        if resource is None:
            resource = Resource(Resource.make_id(kind, name), kind, name, [])
            next_version = 1
            parent = None
        else:
            next_version = resource.head.version + 1
            parent = resource.head.version if parent_version is None else parent_version
        version = ResourceVersion.create(
            resource_id=resource.resource_id,
            kind=kind,
            name=name,
            version=next_version,
            content=content,
            parent_version=parent,
            message=message,
        )
        resource.versions.append(version)
        self._save(resource)
        return version

    def head(self, kind: str, name: str) -> Optional[ResourceVersion]:
        resource = self.load(kind, name)
        return resource.head if resource and resource.versions else None

    def get(self, kind: str, name: str, version: int) -> ResourceVersion:
        resource = self.load(kind, name)
        if resource is None:
            raise KeyError(f"no resource {kind}:{name}")
        return resource.get(version)

    def history(self, kind: str, name: str) -> List[ResourceVersion]:
        resource = self.load(kind, name)
        return list(resource.versions) if resource else []

    def rollback(
        self, kind: str, name: str, to_version: int, message: Optional[str] = None
    ) -> ResourceVersion:
        """Roll a resource back to an earlier version.

        Rollback is *append-only*: it creates a new head whose content equals
        the target version, preserving the full audit trail (git-revert style)
        rather than erasing history.
        """
        resource = self.load(kind, name)
        if resource is None:
            raise KeyError(f"no resource {kind}:{name}")
        target = resource.get(to_version)
        return self.commit(
            kind,
            name,
            content=target.content,
            message=message or f"rollback to v{to_version}",
        )

    def delete(self, kind: str, name: str) -> bool:
        """Remove a resource and its entire history. Returns True if it existed."""
        path = self._path(kind, name)
        if os.path.exists(path):
            os.remove(path)
            return True
        return False

    def list_resources(self) -> List[Tuple[str, str]]:
        found: List[Tuple[str, str]] = []
        if not os.path.isdir(self.root):
            return found
        for kind in sorted(os.listdir(self.root)):
            kind_dir = os.path.join(self.root, kind)
            if not os.path.isdir(kind_dir):
                continue
            for fname in sorted(os.listdir(kind_dir)):
                if fname.endswith(".json"):
                    resource = self.load(kind, fname[:-5])
                    if resource is not None:
                        found.append((resource.kind, resource.name))
        return found
