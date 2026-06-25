"""Workspace preparation — a fresh, authenticated checkout per job.

The Celery worker clones the target repo into an ephemeral directory using a
short-lived GitHub App installation token, hands the resulting
:class:`~gnsis.orchestration.engine.Workspace` to the engine, and tears it down
afterwards. The clone URL embeds the token only in-process; it is never logged or
persisted.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Optional

from ..orchestration.engine import Workspace


def _clone_url(repo: str, token: Optional[str]) -> str:
    if token:
        return f"https://x-access-token:{token}@github.com/{repo}.git"
    return f"https://github.com/{repo}.git"


def prepare_workspace(
    repo: str,
    base_branch: str,
    token: Optional[str],
    root: str,
    job_id: str,
) -> Workspace:
    """Clone ``repo`` at ``base_branch`` into ``root/<job_id>`` and return it."""
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, job_id)
    if os.path.exists(path):
        shutil.rmtree(path)

    subprocess.run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            base_branch,
            _clone_url(repo, token),
            path,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    # Identify commits so the worker can commit the engine's changes.
    subprocess.run(["git", "config", "user.email", "gnsis@users.noreply.github.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "GNSIS"], cwd=path, check=True)
    return Workspace(path=path, repo=repo, base_branch=base_branch)


def cleanup_workspace(workspace: Workspace) -> None:
    shutil.rmtree(workspace.path, ignore_errors=True)
