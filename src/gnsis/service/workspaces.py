"""Tenancy data access: workspaces, installations, repositories.

Everything here is scoped to a workspace, which is owned by exactly one Better
Auth subject. IDs are *deterministic* (a hash of their natural key) so that
"create if absent" is idempotent and concurrency-safe: two racing requests
compute the same primary key, one insert wins, the other converges by re-reading
rather than creating a duplicate.

Returns small frozen dataclasses rather than ORM rows so callers never touch a
detached SQLAlchemy instance outside its session.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.exc import IntegrityError

from . import orm
from .auth_client import VerifiedInstallation
from .db import session_scope


def _det_id(prefix: str, *parts: Any) -> str:
    raw = "|".join(str(p) for p in parts)
    return f"{prefix}_{hashlib.sha256(raw.encode()).hexdigest()[:24]}"


@dataclass(frozen=True)
class WorkspaceRecord:
    id: str
    owner_auth_subject: str
    name: str


@dataclass(frozen=True)
class InstallationRecord:
    id: str
    workspace_id: str
    github_installation_id: int
    github_account_id: Optional[int]
    github_account_login: Optional[str]
    github_account_type: Optional[str]
    status: str


@dataclass(frozen=True)
class RepositoryRecord:
    id: str
    workspace_id: str
    github_installation_record_id: str
    github_repository_id: int
    owner: str
    name: str
    full_name: str
    default_branch: str
    private: bool
    enabled: bool
    archived: bool


class WorkspaceConflictError(Exception):
    """A resource is already owned by a different workspace."""


def _ws_record(row: orm.Workspace) -> WorkspaceRecord:
    return WorkspaceRecord(row.id, row.owner_auth_subject, row.name)


def _inst_record(row: orm.GitHubInstallation) -> InstallationRecord:
    return InstallationRecord(
        id=row.id,
        workspace_id=row.workspace_id,
        github_installation_id=row.github_installation_id,
        github_account_id=row.github_account_id,
        github_account_login=row.github_account_login,
        github_account_type=row.github_account_type,
        status=row.status,
    )


def _repo_record(row: orm.Repository) -> RepositoryRecord:
    return RepositoryRecord(
        id=row.id,
        workspace_id=row.workspace_id,
        github_installation_record_id=row.github_installation_record_id,
        github_repository_id=row.github_repository_id,
        owner=row.owner,
        name=row.name,
        full_name=row.full_name,
        default_branch=row.default_branch,
        private=row.private,
        enabled=row.enabled,
        archived=row.archived,
    )


# -- workspaces ----------------------------------------------------------------


def get_or_create_workspace(subject: str, name: str = "Personal") -> WorkspaceRecord:
    """Idempotently return the personal workspace for a Better Auth subject."""
    ws_id = _det_id("ws", subject)
    with session_scope() as s:
        row = s.get(orm.Workspace, ws_id)
        if row is not None:
            return _ws_record(row)
        row = orm.Workspace(id=ws_id, owner_auth_subject=subject, name=name)
        s.add(row)
        try:
            s.flush()
        except IntegrityError:
            # Lost a concurrent race — the other request created it. Re-read.
            s.rollback()
            existing = s.get(orm.Workspace, ws_id)
            if existing is None:
                existing = (
                    s.query(orm.Workspace)
                    .filter(orm.Workspace.owner_auth_subject == subject)
                    .one()
                )
            return _ws_record(existing)
        return _ws_record(row)


def get_workspace_by_subject(subject: str) -> Optional[WorkspaceRecord]:
    with session_scope() as s:
        row = s.get(orm.Workspace, _det_id("ws", subject))
        return _ws_record(row) if row else None


# -- installations -------------------------------------------------------------


def upsert_installation(
    workspace_id: str, verified: VerifiedInstallation
) -> InstallationRecord:
    """Store/refresh a verified installation under a workspace. Idempotent.

    If the same GitHub installation is already claimed by a *different*
    workspace, this raises rather than silently reassigning it.
    """
    with session_scope() as s:
        row = (
            s.query(orm.GitHubInstallation)
            .filter(
                orm.GitHubInstallation.github_installation_id
                == verified.installation_id
            )
            .one_or_none()
        )
        if row is not None and row.workspace_id != workspace_id:
            raise WorkspaceConflictError(
                "installation already claimed by another workspace"
            )
        if row is None:
            row = orm.GitHubInstallation(
                id=_det_id("inst", verified.installation_id),
                workspace_id=workspace_id,
                github_installation_id=verified.installation_id,
            )
            s.add(row)
        row.github_account_id = verified.account_id
        row.github_account_login = verified.account_login
        row.github_account_type = verified.account_type
        row.status = "active"
        row.suspended_at = None
        s.flush()
        return _inst_record(row)


def list_installations(workspace_id: str) -> List[InstallationRecord]:
    with session_scope() as s:
        rows = (
            s.query(orm.GitHubInstallation)
            .filter(orm.GitHubInstallation.workspace_id == workspace_id)
            .order_by(orm.GitHubInstallation.created_at)
            .all()
        )
        return [_inst_record(r) for r in rows]


def get_installation_for_workspace(
    workspace_id: str, github_installation_id: int
) -> Optional[InstallationRecord]:
    with session_scope() as s:
        row = (
            s.query(orm.GitHubInstallation)
            .filter(
                orm.GitHubInstallation.workspace_id == workspace_id,
                orm.GitHubInstallation.github_installation_id
                == github_installation_id,
            )
            .one_or_none()
        )
        return _inst_record(row) if row else None


def get_installation_by_record_id(
    installation_record_id: str,
) -> Optional[InstallationRecord]:
    with session_scope() as s:
        row = s.get(orm.GitHubInstallation, installation_record_id)
        return _inst_record(row) if row else None


def set_installation_status(
    github_installation_id: int, status: str, suspended: bool = False
) -> Optional[InstallationRecord]:
    with session_scope() as s:
        row = (
            s.query(orm.GitHubInstallation)
            .filter(
                orm.GitHubInstallation.github_installation_id
                == github_installation_id
            )
            .one_or_none()
        )
        if row is None:
            return None
        row.status = status
        row.suspended_at = datetime.now(timezone.utc) if suspended else None
        if status == "deleted":
            for repo in row.repositories:
                repo.enabled = False
        s.flush()
        return _inst_record(row)


# -- repositories --------------------------------------------------------------


def sync_repositories(
    workspace_id: str,
    installation_record_id: str,
    github_repos: List[Dict[str, Any]],
) -> List[RepositoryRecord]:
    """Reconcile stored repositories with the installation's current set.

    Repos present in ``github_repos`` are upserted and (re)enabled; stored repos
    for this installation that are absent are marked disabled (never deleted, so
    historical runs survive).
    """
    seen_github_ids = set()
    with session_scope() as s:
        for gh in github_repos:
            gh_id = int(gh["id"])
            seen_github_ids.add(gh_id)
            full_name = gh.get("full_name") or ""
            owner = (gh.get("owner") or {}).get("login") or full_name.split("/")[0]
            name = gh.get("name") or (full_name.split("/")[-1] if full_name else "")
            row = (
                s.query(orm.Repository)
                .filter(
                    orm.Repository.workspace_id == workspace_id,
                    orm.Repository.github_repository_id == gh_id,
                )
                .one_or_none()
            )
            if row is None:
                row = orm.Repository(
                    id=_det_id("repo", workspace_id, gh_id),
                    workspace_id=workspace_id,
                    github_installation_record_id=installation_record_id,
                    github_repository_id=gh_id,
                )
                s.add(row)
            row.github_installation_record_id = installation_record_id
            row.owner = owner
            row.name = name
            row.full_name = full_name
            row.default_branch = gh.get("default_branch") or "main"
            row.private = bool(gh.get("private", False))
            row.archived = bool(gh.get("archived", False))
            row.enabled = True

        # Disable repos previously synced for this installation but now absent.
        stored = (
            s.query(orm.Repository)
            .filter(
                orm.Repository.workspace_id == workspace_id,
                orm.Repository.github_installation_record_id == installation_record_id,
            )
            .all()
        )
        for row in stored:
            if row.github_repository_id not in seen_github_ids:
                row.enabled = False

        s.flush()
        return [
            _repo_record(r)
            for r in sorted(stored, key=lambda r: r.full_name)
        ]


def list_repositories(
    workspace_id: str, include_disabled: bool = False
) -> List[RepositoryRecord]:
    with session_scope() as s:
        q = s.query(orm.Repository).filter(
            orm.Repository.workspace_id == workspace_id
        )
        if not include_disabled:
            q = q.filter(orm.Repository.enabled.is_(True))
        rows = q.order_by(orm.Repository.full_name).all()
        return [_repo_record(r) for r in rows]


def get_repository(workspace_id: str, repository_id: str) -> Optional[RepositoryRecord]:
    with session_scope() as s:
        row = s.get(orm.Repository, repository_id)
        if row is None or row.workspace_id != workspace_id:
            return None
        return _repo_record(row)


def remove_repositories_by_github_id(
    github_installation_id: int, github_repo_ids: List[int]
) -> int:
    """Disable specific repos on an installation (webhook: repositories removed)."""
    disabled = 0
    with session_scope() as s:
        inst = (
            s.query(orm.GitHubInstallation)
            .filter(
                orm.GitHubInstallation.github_installation_id
                == github_installation_id
            )
            .one_or_none()
        )
        if inst is None:
            return 0
        for gh_id in github_repo_ids:
            row = (
                s.query(orm.Repository)
                .filter(
                    orm.Repository.workspace_id == inst.workspace_id,
                    orm.Repository.github_repository_id == int(gh_id),
                )
                .one_or_none()
            )
            if row is not None and row.enabled:
                row.enabled = False
                disabled += 1
        s.flush()
    return disabled


# -- webhook idempotency -------------------------------------------------------


def delivery_already_processed(delivery_id: str, event: str) -> bool:
    """Record a webhook delivery id; return True if it was already processed."""
    with session_scope() as s:
        existing = s.get(orm.WebhookDelivery, delivery_id)
        if existing is not None:
            return True
        s.add(orm.WebhookDelivery(delivery_id=delivery_id, event=event))
        try:
            s.flush()
        except IntegrityError:
            s.rollback()
            return True
    return False
