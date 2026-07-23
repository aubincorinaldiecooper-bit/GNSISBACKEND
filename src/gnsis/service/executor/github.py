"""The GitHub REST calls the executor control plane needs.

Kept separate from :mod:`gnsis.service.github_app` (which owns customer PR
publishing) but built on the same :class:`GitHubApp` for App-JWT signing and
installation-token minting. Everything here concerns the *platform* side:
resolving the executor installation, minting a **scope-narrowed** dispatch token
(Actions: write, Contents: read, one repository), dispatching the fixed
workflow, and polling run state — plus the read-only calls used to resolve a
customer base SHA, stream immutable source, and observe customer CI.

Uses stdlib ``urllib`` directly so it can see raw status codes (dispatch returns
204), stream large archive bodies, and cap byte counts.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from ..github_app import GitHubApp

_API = "https://api.github.com"
_UA = "gnsis-executor"
_APIV = "2022-11-28"


class GitHubHTTPError(RuntimeError):
    def __init__(self, method: str, url: str, status: int, body: str):
        super().__init__(f"GitHub {method} {url} -> {status}: {body[:500]}")
        self.status = status
        self.body = body


def _request(
    method: str,
    url: str,
    *,
    token: Optional[str] = None,
    bearer: Optional[str] = None,
    payload: Optional[dict] = None,
    accept: str = "application/vnd.github+json",
) -> Tuple[int, Dict[str, str], bytes]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", accept)
    req.add_header("User-Agent", _UA)
    req.add_header("X-GitHub-Api-Version", _APIV)
    if token:
        req.add_header("Authorization", f"token {token}")
    elif bearer:
        req.add_header("Authorization", f"Bearer {bearer}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise GitHubHTTPError(method, url, exc.code, body) from exc


def _json(status: int, body: bytes) -> Any:
    if not body:
        return None
    return json.loads(body.decode("utf-8"))


class ExecutorGitHub:
    """Executor-side GitHub operations built on a :class:`GitHubApp`."""

    def __init__(self, app: GitHubApp) -> None:
        self.app = app

    def _app_jwt(self) -> str:
        return self.app._app_jwt()  # noqa: SLF001 - intentional reuse

    # -- installation resolution -----------------------------------------
    def repo_installation(self, owner: str, repo: str) -> Dict[str, Any]:
        """The App installation covering ``owner/repo`` (repository-installation API)."""
        status, _, body = _request(
            "GET",
            f"{_API}/repos/{owner}/{repo}/installation",
            bearer=self._app_jwt(),
        )
        return _json(status, body)

    def scoped_installation_token(
        self,
        installation_id: Any,
        *,
        repositories: List[str],
        permissions: Dict[str, str],
    ) -> Dict[str, Any]:
        """Mint an installation token narrowed to specific repos + permissions."""
        status, _, body = _request(
            "POST",
            f"{_API}/app/installations/{installation_id}/access_tokens",
            bearer=self._app_jwt(),
            payload={"repositories": repositories, "permissions": permissions},
        )
        return _json(status, body)

    # -- repository metadata / refs --------------------------------------
    def get_repo(self, owner: str, repo: str, token: str) -> Dict[str, Any]:
        status, _, body = _request(
            "GET", f"{_API}/repos/{owner}/{repo}", token=token
        )
        return _json(status, body)

    def list_branches(
        self, owner: str, repo: str, token: str, *, per_page: int = 100, page: int = 1
    ) -> List[Dict[str, Any]]:
        """List branches for ``owner/repo`` (installation-token scoped).

        Returns GitHub's raw branch objects (``name`` + ``commit`` + ``protected``).
        The caller shapes the user-facing view and never leaks the token.
        """
        per_page = max(1, min(int(per_page), 100))
        page = max(1, int(page))
        status, _, body = _request(
            "GET",
            f"{_API}/repos/{owner}/{repo}/branches?per_page={per_page}&page={page}",
            token=token,
        )
        data = _json(status, body)
        return data if isinstance(data, list) else []

    def ref_sha(self, owner: str, repo: str, branch: str, token: str) -> str:
        """The exact commit SHA at the head of ``branch`` (immutable target)."""
        status, _, body = _request(
            "GET",
            f"{_API}/repos/{owner}/{repo}/git/ref/heads/{branch}",
            token=token,
        )
        data = _json(status, body)
        return data["object"]["sha"]

    # -- workflow dispatch / runs ----------------------------------------
    def dispatch_workflow(
        self,
        owner: str,
        repo: str,
        workflow_file: str,
        ref: str,
        inputs: Dict[str, str],
        token: str,
    ) -> int:
        """Dispatch a workflow. Returns the HTTP status (204 on success)."""
        status, _, _ = _request(
            "POST",
            f"{_API}/repos/{owner}/{repo}/actions/workflows/{workflow_file}/dispatches",
            token=token,
            payload={"ref": ref, "inputs": inputs},
        )
        return status

    def find_run_by_name(
        self,
        owner: str,
        repo: str,
        workflow_file: str,
        run_name: str,
        token: str,
    ) -> Optional[Dict[str, Any]]:
        """Locate a dispatched run by its ``run-name`` (which we set to the job)."""
        status, _, body = _request(
            "GET",
            f"{_API}/repos/{owner}/{repo}/actions/workflows/{workflow_file}/runs"
            "?event=workflow_dispatch&per_page=30",
            token=token,
        )
        data = _json(status, body) or {}
        for run in data.get("workflow_runs", []) or []:
            if (run.get("name") or "") == run_name:
                return run
        return None

    def get_workflow_run(
        self, owner: str, repo: str, run_id: int, token: str
    ) -> Dict[str, Any]:
        status, _, body = _request(
            "GET", f"{_API}/repos/{owner}/{repo}/actions/runs/{run_id}", token=token
        )
        return _json(status, body)

    def cancel_workflow_run(
        self, owner: str, repo: str, run_id: int, token: str
    ) -> int:
        try:
            status, _, _ = _request(
                "POST",
                f"{_API}/repos/{owner}/{repo}/actions/runs/{run_id}/cancel",
                token=token,
            )
            return status
        except GitHubHTTPError as exc:
            # 409 = already completed/cancelling; treat as best-effort success.
            if exc.status in (409, 404):
                return exc.status
            raise

    # -- immutable source archive ----------------------------------------
    def open_tarball(self, owner: str, repo: str, sha: str, token: str):
        """Open a streaming response for the tarball of an exact commit SHA.

        The caller streams and byte-counts the body; the token never leaves the
        control plane. Returns the ``urlopen`` context manager.
        """
        req = urllib.request.Request(
            f"{_API}/repos/{owner}/{repo}/tarball/{sha}", method="GET"
        )
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", _UA)
        req.add_header("X-GitHub-Api-Version", _APIV)
        req.add_header("Authorization", f"token {token}")
        return urllib.request.urlopen(req, timeout=120)  # noqa: S310

    # -- customer CI observation -----------------------------------------

    def commit_workflow_runs(self, owner: str, repo: str, sha: str, token: str) -> Dict[str, Any]:
        status, _, body = _request("GET", f"{_API}/repos/{owner}/{repo}/actions/runs?head_sha={sha}", token=token)
        return _json(status, body) or {}

    def commit_check_suites(self, owner: str, repo: str, sha: str, token: str) -> Dict[str, Any]:
        status, _, body = _request("GET", f"{_API}/repos/{owner}/{repo}/commits/{sha}/check-suites", token=token)
        return _json(status, body) or {}

    def pull_request(self, owner: str, repo: str, number: int, token: str) -> Dict[str, Any]:
        status, _, body = _request("GET", f"{_API}/repos/{owner}/{repo}/pulls/{number}", token=token)
        return _json(status, body) or {}

    def commit_check_runs(
        self, owner: str, repo: str, sha: str, token: str
    ) -> Dict[str, Any]:
        status, _, body = _request(
            "GET",
            f"{_API}/repos/{owner}/{repo}/commits/{sha}/check-runs",
            token=token,
        )
        return _json(status, body) or {}

    def commit_status(
        self, owner: str, repo: str, sha: str, token: str
    ) -> Dict[str, Any]:
        status, _, body = _request(
            "GET",
            f"{_API}/repos/{owner}/{repo}/commits/{sha}/status",
            token=token,
        )
        return _json(status, body) or {}
