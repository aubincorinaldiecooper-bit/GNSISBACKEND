"""GitHub App auth and PR publishing — the only place GNSIS writes to GitHub.

Two responsibilities, both gated behind human approval and run by the worker:

1. **Mint a scoped installation token.** A short-lived RS256 JWT (signed with the
   App's private key) is exchanged for an installation access token. Tokens are
   never persisted; they live only for the duration of a publish.
2. **Open the PR.** Clone the base branch fresh, create the head branch, apply the
   *stored* unified diff (the source of truth in Postgres — independent of the now
   gone generation workspace), commit, push, and open the pull request.

The HTTP calls use stdlib ``urllib`` to keep the dependency surface small; only
JWT signing needs PyJWT + cryptography (the ``service`` extra).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from ..orchestration.models import Diff, JobRecord, PRMetadata

_API = "https://api.github.com"


class GitHubApp:
    """Mints scoped installation tokens for a GitHub App."""

    def __init__(
        self,
        app_id: str,
        private_key: str,
        installation_id: str,
    ) -> None:
        if not (app_id and private_key and installation_id):
            raise ValueError("GitHub App credentials are incomplete")
        self.app_id = app_id
        self.private_key = private_key
        self.installation_id = installation_id

    def _app_jwt(self) -> str:
        import jwt  # PyJWT

        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + 540, "iss": self.app_id}
        return jwt.encode(payload, self.private_key, algorithm="RS256")

    def installation_token(self) -> str:
        token_jwt = self._app_jwt()
        data = _request(
            "POST",
            f"{_API}/app/installations/{self.installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {token_jwt}"},
        )
        return data["token"]


class GitHubPublisher:
    """A :class:`gnsis.orchestration.pipeline.Publisher` backed by a GitHub App."""

    def __init__(self, app: GitHubApp, workspace_root: str) -> None:
        self.app = app
        self.workspace_root = workspace_root

    def publish(self, job: JobRecord, diff: Diff) -> PRMetadata:
        token = self.app.installation_token()
        branch = job.branch or f"gnsis/{job.id}"
        path = os.path.join(self.workspace_root, f"publish-{job.id}")
        if os.path.exists(path):
            shutil.rmtree(path)

        try:
            self._clone(job.repo, job.base_branch, token, path)
            self._run(["git", "checkout", "-b", branch], path)
            self._apply_patch(path, diff.patch)
            self._run(["git", "add", "-A"], path)
            self._run(
                ["git", "commit", "-m", _commit_message(job)],
                path,
            )
            head_sha = self._run(["git", "rev-parse", "HEAD"], path).strip()
            self._run(["git", "push", "-u", "origin", branch], path)
            pr = self._open_pr(job, branch, token)
        finally:
            shutil.rmtree(path, ignore_errors=True)

        return PRMetadata(
            job_id=job.id,
            number=pr["number"],
            url=pr["html_url"],
            branch=branch,
            head_sha=head_sha,
        )

    # -- git --------------------------------------------------------------
    def _clone(self, repo: str, base_branch: str, token: str, path: str) -> None:
        url = f"https://x-access-token:{token}@github.com/{repo}.git"
        self._run(["git", "clone", "--branch", base_branch, url, path], cwd=None)
        self._run(["git", "config", "user.email", "gnsis@users.noreply.github.com"], path)
        self._run(["git", "config", "user.name", "GNSIS"], path)

    def _apply_patch(self, path: str, patch: str) -> None:
        patch_file = os.path.join(path, ".gnsis.patch")
        with open(patch_file, "w", encoding="utf-8") as handle:
            handle.write(patch if patch.endswith("\n") else patch + "\n")
        try:
            # Plain apply works for a clean clone (incl. new files); fall back to
            # a three-way merge only if context has drifted.
            proc = subprocess.run(
                ["git", "apply", ".gnsis.patch"],
                cwd=path,
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0:
                fallback = subprocess.run(
                    ["git", "apply", "--3way", ".gnsis.patch"],
                    cwd=path,
                    capture_output=True,
                    text=True,
                )
                if fallback.returncode != 0:
                    raise RuntimeError(
                        f"git apply failed: {proc.stderr.strip()} / "
                        f"{fallback.stderr.strip()}"
                    )
        finally:
            os.remove(patch_file)

    def _run(self, args, cwd: Optional[str]) -> str:
        proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"{' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}"
            )
        return proc.stdout

    # -- API --------------------------------------------------------------
    def _open_pr(self, job: JobRecord, branch: str, token: str) -> Dict[str, Any]:
        body = _pr_body(job)
        return _request(
            "POST",
            f"{_API}/repos/{job.repo}/pulls",
            headers={"Authorization": f"token {token}"},
            payload={
                "title": _pr_title(job),
                "head": branch,
                "base": job.base_branch,
                "body": body,
            },
        )


def _commit_message(job: JobRecord) -> str:
    first_line = job.instruction.strip().splitlines()[0][:72]
    return f"{first_line}\n\nGenerated by GNSIS (job {job.id})."


def _pr_title(job: JobRecord) -> str:
    return job.instruction.strip().splitlines()[0][:72]


def _pr_body(job: JobRecord) -> str:
    return (
        f"{job.instruction}\n\n---\n"
        f"Opened by GNSIS after human approval (job `{job.id}`)."
    )


def _request(
    method: str,
    url: str,
    headers: Optional[Dict[str, str]] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "gnsis")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # surface GitHub's error body
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"GitHub API {method} {url} -> {exc.code}: {detail}") from exc


def publisher_from_env(settings: Any) -> GitHubPublisher:
    """Build a publisher from :class:`gnsis.service.settings.Settings`."""
    app = GitHubApp(
        app_id=settings.github_app_id,
        private_key=settings.github_app_private_key,
        installation_id=settings.github_app_installation_id,
    )
    return GitHubPublisher(app, settings.workspace_root)
