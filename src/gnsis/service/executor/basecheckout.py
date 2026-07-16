"""Materialize the exact, untouched base commit for validation and publishing.

Fetches *only* the pinned commit (``git fetch --depth 1 <sha>``) using a
short-lived, read-scoped customer installation token, into a throwaway
directory. Because the SHA is immutable, this is deterministic regardless of
where the branch has since moved. The result is the "clean source" the trusted
host validates the patch against and reconstructs before publishing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import Optional

from ..github_app import GitHubApp
from .github import ExecutorGitHub
from .models import ExecutionRunRecord


def _run(args, cwd: str) -> None:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed: {proc.stderr.strip()[:300]}")


def _customer_installation_id(run: ExecutionRunRecord) -> Optional[int]:
    from .. import workspaces as ws

    if not run.repository_id or not run.workspace_id:
        return None
    repo = ws.get_repository(run.workspace_id, run.repository_id)
    if repo is None:
        return None
    inst = ws.get_installation_by_record_id(repo.github_installation_record_id)
    return inst.github_installation_id if inst else None


def materialize_base(
    settings,
    run: ExecutionRunRecord,
    repo_full_name: str,
    *,
    app: Optional[GitHubApp] = None,
) -> str:
    """Clone exactly ``run.base_sha`` into a temp dir and return its path."""
    installation_id = _customer_installation_id(run)
    if installation_id is None:
        raise RuntimeError("customer installation not resolvable")
    owner, _, name = repo_full_name.partition("/")
    app = app or GitHubApp(
        app_id=settings.github_app_id,
        private_key=settings.github_app_private_key,
        installation_id="0",
    )
    github = ExecutorGitHub(app)
    token = github.scoped_installation_token(
        installation_id, repositories=[name], permissions={"contents": "read"}
    )["token"]
    url = f"https://x-access-token:{token}@github.com/{repo_full_name}.git"

    path = tempfile.mkdtemp(prefix=f"gnsis-base-{run.id}-")
    try:
        _run(["git", "init", "-q"], path)
        _run(["git", "remote", "add", "origin", url], path)
        _run(["git", "fetch", "-q", "--depth", "1", "origin", run.base_sha], path)
        _run(["git", "checkout", "-q", "FETCH_HEAD"], path)
        _run(["git", "config", "user.email", "gnsis@users.noreply.github.com"], path)
        _run(["git", "config", "user.name", "GNSIS"], path)
    except Exception:
        shutil.rmtree(path, ignore_errors=True)
        raise
    return path


def cleanup(path: Optional[str]) -> None:
    if path:
        shutil.rmtree(path, ignore_errors=True)
