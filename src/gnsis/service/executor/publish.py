"""Publishing — the only place an approved change reaches the customer repo.

Runs in the worker, behind approval. It mints a *fresh* customer installation
token, reconstructs the exact approved base commit, re-verifies the approval
binding and re-derives the patch hash, applies the exact approved patch onto a
new GNSIS branch, pushes only that change, and opens a **draft** PR. It never
pushes to the default branch and never auto-merges. If the target branch has
moved off the approved base SHA it fails clearly and asks for a new run.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from typing import Any, Dict, Optional

from ...memory.base import MemoryProvider, MemoryRecord
from ...orchestration.models import LogEntry, PRMetadata
from ...orchestration.status import JobStatus
from ..github_app import GitHubApp, _request
from .approval import verify_binding
from .github import ExecutorGitHub
from .store import ExecutionStore
from .validation import sha256_text

_API = "https://api.github.com"


class PublishError(RuntimeError):
    pass


def _sanitize(text: object) -> str:
    raw = str(text)
    raw = re.sub(r"x-access-token:[^@\s]+@", "x-access-token:***@", raw)
    raw = re.sub(r"(token |Bearer )[A-Za-z0-9_\-]+", r"\1***", raw, flags=re.I)
    return raw[:500]


def _git(args, cwd: str) -> str:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        safe_args = [re.sub(r"x-access-token:[^@\s]+@", "x-access-token:***@", str(a)) for a in args]
        raise PublishError(f"{' '.join(safe_args)} failed: {_sanitize(proc.stderr)}")
    return proc.stdout


def _customer_installation(job_store, job):
    from .. import workspaces as ws

    if not (job.workspace_id and job.repository_id):
        return None, None
    repo = ws.get_repository(job.workspace_id, job.repository_id)
    if repo is None:
        return None, None
    inst = ws.get_installation_by_record_id(repo.github_installation_record_id)
    return repo, inst


def publish_approved(
    job_store,
    settings,
    job_id: str,
    *,
    memory: Optional[MemoryProvider] = None,
    app: Optional[GitHubApp] = None,
) -> PRMetadata:
    job = job_store.get_job(job_id)
    if job is None:
        raise KeyError(job_id)

    approval = job_store.get_latest_approval(job_id)
    if approval is None or approval.decision != "approved":
        raise PermissionError(f"job {job_id} is not approved for publishing")

    exec_store = ExecutionStore()
    run = exec_store.get_run_for_job(job_id)
    if run is None or not run.patch_sha256:
        raise PublishError("no validated execution run to publish")

    diff = job_store.get_diff(job_id)
    if diff is None:
        raise PublishError("no diff to publish")
    diff_sha = sha256_text(diff.patch)

    binding = (job.context or {}).get("approval_binding") or {}
    verify_binding(binding, run=run, diff_patch_sha256=diff_sha)

    repo, inst = _customer_installation(job_store, job)
    if inst is None:
        raise PublishError("customer installation not resolvable")

    app = app or GitHubApp(
        app_id=settings.github_app_id,
        private_key=settings.github_app_private_key,
        installation_id="0",
    )
    github = ExecutorGitHub(app)
    owner, _, name = job.repo.partition("/")

    # Fresh, minimal token for exactly this publish.
    token = github.scoped_installation_token(
        inst.github_installation_id,
        repositories=[name],
        permissions={"contents": "write", "pull_requests": "write"},
    )["token"]

    # Moving base: the branch must still be at the approved base SHA.
    current_head = github.ref_sha(owner, name, run.base_branch, token)
    if current_head != run.base_sha:
        job_store.set_status(job_id, JobStatus.FAILED, error="base branch moved since approval; a new run is required")
        raise PublishError(
            f"target branch {run.base_branch} moved ({current_head} != approved "
            f"{run.base_sha}); a new run is required"
        )

    job_store.set_status(job_id, JobStatus.PUBLISHING)
    branch = job.branch or f"gnsis/{job_id}"
    workdir = tempfile.mkdtemp(prefix=f"gnsis-publish-{job_id}-")
    url = f"https://x-access-token:{token}@github.com/{job.repo}.git"
    pushed = False
    try:
        # Reconstruct the exact approved base commit and branch from it.
        _git(["git", "init", "-q"], workdir)
        _git(["git", "remote", "add", "origin", url], workdir)
        _git(["git", "fetch", "-q", "--depth", "1", "origin", run.base_sha], workdir)
        _git(["git", "checkout", "-q", "-b", branch, "FETCH_HEAD"], workdir)
        _git(["git", "config", "user.email", "gnsis@users.noreply.github.com"], workdir)
        _git(["git", "config", "user.name", "GNSIS"], workdir)
        _apply_exact_patch(workdir, diff.patch)
        _git(["git", "add", "-A"], workdir)
        _git(["git", "commit", "-m", _commit_message(job)], workdir)
        head_sha = _git(["git", "rev-parse", "HEAD"], workdir).strip()
        _git(["git", "push", "-u", "origin", branch], workdir)
        pushed = True
        existing = job_store.get_pr_metadata(job_id)
        if existing:
            pr = {"number": existing.number, "html_url": existing.url, "draft": True}
        else:
            pr = _find_existing_open_pr(job, branch, run.base_branch, token)
            if pr is None:
                pr = _open_draft_pr(job, branch, run.base_branch, token)
        meta = PRMetadata(
            job_id=job_id,
            number=pr["number"],
            url=pr["html_url"],
            branch=branch,
            head_sha=head_sha,
        )
        job_store.save_pr_metadata(meta)
        job_store.merge_context(
            job_id,
            {
                "pr": {
                    "number": pr["number"],
                    "url": pr["html_url"],
                    "draft": pr.get("draft", True),
                },
                "published_base_sha": run.base_sha,
                "published_patch_sha256": run.patch_sha256,
            },
        )
    except Exception as exc:  # noqa: BLE001
        if pushed:
            try:
                _git(["git", "push", "origin", f":refs/heads/{branch}"], workdir)
            except Exception:
                job_store.merge_context(job_id, {"published_branch_needs_reuse": branch})
        message = "publishing failed: " + _sanitize(exc)
        job_store.set_status(job_id, JobStatus.FAILED, error=message)
        job_store.append_log(LogEntry(job_id, "publish", "error", message, {"branch": branch, "pushed": pushed}))
        raise PublishError(message) from exc
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    job_store.set_status(job_id, JobStatus.COMPLETED)

    if memory is not None:
        # Only a human-approved, successfully-published change reaches memory, and
        # it is written tenant-scoped + provenance-tagged so CodeMemory can safely
        # retrieve it for future runs on this repository (and never another's).
        memory.write(
            MemoryRecord(
                repo=job.repo,
                content=(job.instruction.strip().splitlines() or [job.instruction])[0],
                kind="accepted_change",
                metadata={"job_id": job_id, "pr": pr["number"], "files": diff.files_changed},
                approved=True,
                workspace_id=job.workspace_id,
                repository_id=job.repository_id,
                source_job_id=job_id,
            )
        )
    return meta


def _apply_exact_patch(path: str, patch: str) -> None:
    patch_file = os.path.join(path, ".gnsis.patch")
    with open(patch_file, "w", encoding="utf-8") as handle:
        handle.write(patch if patch.endswith("\n") else patch + "\n")
    try:
        proc = subprocess.run(
            ["git", "apply", "--index", ".gnsis.patch"], cwd=path, capture_output=True, text=True
        )
        if proc.returncode != 0:
            raise PublishError(f"exact patch did not apply: {_sanitize(proc.stderr)}")
    finally:
        try:
            os.remove(patch_file)
        except OSError:
            pass


def _find_existing_open_pr(
    job, branch: str, base_branch: str, token: str
) -> Optional[Dict[str, Any]]:
    owner, _, _ = job.repo.partition("/")
    pulls = _request(
        "GET",
        f"{_API}/repos/{job.repo}/pulls?state=open&head={owner}:{branch}&base={base_branch}",
        headers={"Authorization": f"token {token}"},
    )
    if isinstance(pulls, list) and pulls:
        return pulls[0]
    return None


def _open_draft_pr(job, branch: str, base_branch: str, token: str) -> Dict[str, Any]:
    title = job.instruction.strip().splitlines()[0][:72]
    body = (
        f"{job.instruction}\n\n---\n"
        f"Opened by GNSIS after human approval (job `{job.id}`). "
        "This is a draft PR; the repository's own CI verifies it independently."
    )
    return _request(
        "POST",
        f"{_API}/repos/{job.repo}/pulls",
        headers={"Authorization": f"token {token}"},
        payload={"title": title, "head": branch, "base": base_branch, "body": body, "draft": True},
    )


def _commit_message(job) -> str:
    first = job.instruction.strip().splitlines()[0][:72]
    return f"{first}\n\nGenerated by GNSIS (job {job.id}); published after approval."
