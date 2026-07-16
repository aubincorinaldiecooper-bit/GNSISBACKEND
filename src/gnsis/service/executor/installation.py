"""Automatic resolution of the executor repository's GitHub App installation.

The operator never has to discover a global installation id by hand. The backend
authenticates as the App (App JWT) and asks GitHub which installation covers the
private executor repository, then verifies that installation is the App's own,
active, covers the executor repo, and carries exactly the permissions dispatch
needs (Actions: write, Contents: read). The numeric executor repository id and
its private visibility are confirmed with a freshly minted, repo-scoped token —
that id is what OIDC verification later pins against.

This platform installation is entirely separate from per-customer installations.
The result is cached briefly and re-resolved on any 404 / suspension /
permission / token error.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional

from ..github_app import GitHubApp
from .github import ExecutorGitHub, GitHubHTTPError

_CACHE_TTL_SECONDS = 300.0

# Permissions the dispatch token must have (and be narrowed to).
DISPATCH_PERMISSIONS: Dict[str, str] = {"actions": "write", "contents": "read"}


class ExecutorInstallationError(RuntimeError):
    """The executor installation could not be resolved or failed verification."""


@dataclass(frozen=True)
class ExecutorInstallation:
    installation_id: int
    repository_id: int
    owner: str
    repository: str
    permissions: Dict[str, str]


class _Cache:
    def __init__(self) -> None:
        self._value: Optional[ExecutorInstallation] = None
        self._at = 0.0
        self._lock = threading.Lock()

    def get(self) -> Optional[ExecutorInstallation]:
        if self._value and (time.monotonic() - self._at) < _CACHE_TTL_SECONDS:
            return self._value
        return None

    def set(self, value: ExecutorInstallation) -> None:
        self._value = value
        self._at = time.monotonic()

    def clear(self) -> None:
        self._value = None
        self._at = 0.0


_cache = _Cache()


def _verify(inst: dict, *, app_id: Optional[str], app_slug: Optional[str]) -> None:
    # The installation must belong to *our* App.
    if app_id and str(inst.get("app_id")) != str(app_id):
        raise ExecutorInstallationError("executor installation belongs to a different app")
    if not app_id and app_slug and inst.get("app_slug") != app_slug:
        raise ExecutorInstallationError("executor installation belongs to a different app")
    # Active (not suspended).
    if inst.get("suspended_at"):
        raise ExecutorInstallationError("executor installation is suspended")
    perms = inst.get("permissions") or {}
    if perms.get("actions") != "write":
        raise ExecutorInstallationError("executor installation lacks Actions: write")
    if perms.get("contents") not in ("read", "write"):
        raise ExecutorInstallationError("executor installation lacks Contents: read")


def resolve_executor_installation(
    settings,
    app: Optional[GitHubApp] = None,
    *,
    force: bool = False,
    github: Optional[ExecutorGitHub] = None,
) -> ExecutorInstallation:
    """Resolve (and cache) the executor repository's App installation."""
    if not force:
        cached = _cache.get()
        if cached is not None:
            return cached

    owner = settings.executor_owner
    repo = settings.executor_repo
    if not (owner and repo):
        raise ExecutorInstallationError("executor owner/repo not configured")
    if not (settings.github_app_id and settings.github_app_private_key):
        raise ExecutorInstallationError("GitHub App credentials not configured")

    if github is None:
        app = app or GitHubApp(
            app_id=settings.github_app_id,
            private_key=settings.github_app_private_key,
            installation_id="0",
        )
        github = ExecutorGitHub(app)

    try:
        inst = github.repo_installation(owner, repo)
        _verify(inst, app_id=settings.github_app_id, app_slug=settings.github_app_slug)
        installation_id = int(inst["id"])
        # A repo-scoped token both proves the repo is included in the installation
        # (GitHub 422s otherwise) and lets us read the numeric id + visibility.
        token_data = github.scoped_installation_token(
            installation_id,
            repositories=[repo],
            permissions=DISPATCH_PERMISSIONS,
        )
        token = token_data["token"]
        meta = github.get_repo(owner, repo, token)
    except GitHubHTTPError as exc:
        _cache.clear()
        raise ExecutorInstallationError(f"executor installation lookup failed: {exc}") from exc
    except (KeyError, TypeError, ValueError) as exc:
        _cache.clear()
        raise ExecutorInstallationError(f"malformed installation response: {exc}") from exc

    if not meta.get("private", False):
        raise ExecutorInstallationError("executor repository is not private")

    resolved = ExecutorInstallation(
        installation_id=installation_id,
        repository_id=int(meta["id"]),
        owner=owner,
        repository=repo,
        permissions=dict(inst.get("permissions") or {}),
    )
    _cache.set(resolved)
    return resolved


def invalidate_executor_installation_cache() -> None:
    _cache.clear()
