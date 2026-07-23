"""Tenant-scoped branch listing for a selected repository.

The browser never talks to GitHub with privileged credentials. This runs
server-side: it verifies the repo belongs to the caller's workspace and its
installation is still active, mints a **least-privilege, short-lived**
installation token scoped to that one repo, lists branches, and returns only
branch names — never the token. Failures for suspended, deleted, inaccessible,
or rate-limited installations surface as safe user-facing messages.

The token requests ``contents:read`` (the same read permission the source /
archive path uses): GitHub's List-branches endpoint returns 403 for a token
narrowed to only ``metadata:read`` on **private** repositories, so branch
selection for the private repos this workflow supports needs ``contents:read``.
"""

from __future__ import annotations

from typing import Optional

from . import workspaces as ws
from .executor.github import ExecutorGitHub, GitHubHTTPError


class BranchListError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.message = message
        self.status = status


def _safe_github_message(status: int) -> str:
    if status in (401, 403):
        return "GitHub denied access or the rate limit was reached; try again shortly"
    if status == 404:
        return "repository is no longer accessible to the GNSIS app"
    return "could not load branches from GitHub; try again shortly"


def list_repository_branches(
    settings,
    app,
    *,
    workspace_id: str,
    repository_id: str,
    search: Optional[str] = None,
    limit: int = 100,
) -> Optional[dict]:
    """Return ``{default_branch, branches:[{name,is_default}]}`` or None if unknown.

    ``None`` means the repository does not exist or is not owned by this
    workspace (the route maps that to 404). ``BranchListError`` carries a safe
    message + status for installation/GitHub failures.
    """
    repo = ws.get_repository(workspace_id, repository_id)
    if repo is None:
        return None

    inst = ws.get_installation_by_record_id(repo.github_installation_record_id)
    if inst is None or inst.status == "deleted":
        raise BranchListError("repository installation is unavailable", status=409)
    if inst.status == "suspended":
        raise BranchListError("repository installation is suspended", status=409)

    gh = ExecutorGitHub(app)
    try:
        token_data = gh.scoped_installation_token(
            inst.github_installation_id,
            repositories=[repo.name],
            # ``contents:read`` (not just ``metadata:read``) — List branches 403s
            # for a metadata-only token on private repos. Same read scope as the
            # source/archive path; still narrowed to this one repo, read-only.
            permissions={"contents": "read"},
        )
        token = token_data["token"]
        raw = gh.list_branches(repo.owner, repo.name, token)
    except GitHubHTTPError as exc:
        raise BranchListError(_safe_github_message(exc.status), status=502) from exc
    except (KeyError, TypeError) as exc:
        raise BranchListError("could not authorize branch lookup", status=502) from exc

    names = [b.get("name") for b in raw if isinstance(b, dict) and b.get("name")]
    default = repo.default_branch

    term = (search or "").strip().lower()
    if term:
        names = [n for n in names if term in n.lower()]

    # Default branch first (when present in the result set), then the rest sorted.
    rest = sorted(n for n in names if n != default)
    ordered = ([default] if default in names else []) + rest
    ordered = ordered[: max(1, min(int(limit), 200))]

    return {
        "default_branch": default,
        "branches": [{"name": n, "is_default": n == default} for n in ordered],
    }
