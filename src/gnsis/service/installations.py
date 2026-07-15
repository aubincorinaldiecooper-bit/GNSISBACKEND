"""Installation claiming and repository synchronization.

Ties together the three trust boundaries:

1. The authenticated Better Auth subject (from the verified JWT — never the body).
2. The auth service's confirmation that the installation is accessible to that
   user's GitHub account (:mod:`gnsis.service.auth_client`).
3. The platform GitHub App credentials, used to mint a short-lived installation
   token and list the repositories that installation can actually reach
   (:mod:`gnsis.service.github_app`).

Only after (1) and (2) agree is the installation stored under the user's
workspace and its repositories synced. The installation id appearing in a setup
URL is never, on its own, treated as proof of ownership.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from . import workspaces
from .auth_client import AuthServiceClient
from .github_app import GitHubApp, list_installation_repositories
from .workspaces import InstallationRecord, RepositoryRecord


@dataclass(frozen=True)
class ClaimResult:
    installation: InstallationRecord
    repositories: List[RepositoryRecord]


def claim_installation(
    *,
    auth_subject: str,
    installation_id: int,
    auth_client: AuthServiceClient,
    github_app: GitHubApp,
) -> ClaimResult:
    """Verify ownership, store the installation, and sync its repos. Idempotent."""
    workspace = workspaces.get_or_create_workspace(auth_subject)

    # (2) The auth service must confirm this user can access this installation.
    verified = auth_client.verify_installation(auth_subject, installation_id)

    installation = workspaces.upsert_installation(workspace.id, verified)
    repos = _sync(workspace.id, installation, github_app)
    return ClaimResult(installation=installation, repositories=repos)


def sync_installation(
    *,
    workspace_id: str,
    installation: InstallationRecord,
    github_app: GitHubApp,
) -> List[RepositoryRecord]:
    """Re-sync repositories for an already-claimed installation."""
    return _sync(workspace_id, installation, github_app)


def _sync(
    workspace_id: str,
    installation: InstallationRecord,
    github_app: GitHubApp,
) -> List[RepositoryRecord]:
    # A short-lived token, used only to list repos, never persisted.
    token = github_app.token_for_installation(installation.github_installation_id)
    github_repos = list_installation_repositories(token)
    return workspaces.sync_repositories(workspace_id, installation.id, github_repos)
