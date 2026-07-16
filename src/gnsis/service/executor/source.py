"""Immutable source delivery — the control plane hands the VM exact source.

The GitHub token used to read the customer repository never leaves the backend:
the backend mints a short-lived, repo-scoped installation token, opens the
tarball for the *exact base commit SHA*, and streams it to the executor while
counting bytes against a ceiling. Because the SHA is immutable, a moving branch
is irrelevant. The download is single-use — claimed atomically in the store — and
bound to the authenticated run.
"""

from __future__ import annotations

from typing import Callable, Iterator, Optional

from ..github_app import GitHubApp
from .github import ExecutorGitHub
from .models import ExecutionRunRecord

_CHUNK = 1024 * 256


class SourceError(RuntimeError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _customer_installation_id(run: ExecutionRunRecord) -> Optional[int]:
    from .. import workspaces as ws

    if not run.repository_id or not run.workspace_id:
        return None
    repo = ws.get_repository(run.workspace_id, run.repository_id)
    if repo is None:
        return None
    inst = ws.get_installation_by_record_id(repo.github_installation_record_id)
    return inst.github_installation_id if inst else None


def stream_source(
    settings,
    run: ExecutionRunRecord,
    repo_full_name: str,
    *,
    app: Optional[GitHubApp] = None,
    open_archive: Optional[Callable] = None,
) -> Iterator[bytes]:
    """Yield the exact-SHA tarball bytes, enforcing the max-size ceiling.

    ``open_archive`` is injectable for tests: ``open_archive(owner, name, sha,
    token)`` returns a readable, context-managed response.
    """
    max_bytes = settings.executor_source_max_bytes
    installation_id = _customer_installation_id(run)
    if installation_id is None:
        raise SourceError("customer installation not resolvable", status=409)

    owner, _, name = repo_full_name.partition("/")
    app = app or GitHubApp(
        app_id=settings.github_app_id,
        private_key=settings.github_app_private_key,
        installation_id="0",
    )
    # A short-lived token scoped to reading this one repository. Never streamed.
    github = ExecutorGitHub(app)
    token_data = github.scoped_installation_token(
        installation_id,
        repositories=[name],
        permissions={"contents": "read"},
    )
    token = token_data["token"]

    opener = open_archive or (lambda o, n, s, t: github.open_tarball(o, n, s, t))
    total = 0
    resp = opener(owner, name, run.base_sha, token)
    try:
        while True:
            chunk = resp.read(_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise SourceError(f"source exceeds {max_bytes} bytes", status=413)
            yield chunk
    finally:
        close = getattr(resp, "close", None)
        if close:
            close()
