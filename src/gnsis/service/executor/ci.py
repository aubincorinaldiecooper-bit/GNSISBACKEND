"""Read-only customer CI observation for published draft PRs.

Polling is idempotent and never reruns, cancels, or mutates customer CI. It only
reads GitHub workflow/check/status APIs and stores normalized observations on the
job context/run receipt so missed webhooks are repaired by the beat schedule.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ...orchestration.models import LogEntry
from ..github_app import GitHubApp
from .github import ExecutorGitHub
from .store import ExecutionStore

TERMINAL = {"passed", "failed", "cancelled", "timed_out", "not_configured"}
TIMEOUT_SECONDS = 60 * 60 * 6


def _norm(
    status: Optional[str] = None,
    conclusion: Optional[str] = None,
    state: Optional[str] = None,
) -> str:
    raw = (conclusion or state or status or "").lower()
    if raw in {"success", "passed"}:
        return "passed"
    if raw in {"failure", "failed", "error", "action_required"}:
        return "failed"
    if raw in {"cancelled", "canceled"}:
        return "cancelled"
    if raw in {"timed_out", "timeout"}:
        return "timed_out"
    if raw in {"in_progress", "waiting"}:
        return "running"
    if raw in {"queued", "requested", "pending", "expected"}:
        return "pending"
    return "pending"


def _overall(items: List[Dict[str, Any]]) -> str:
    if not items:
        return "not_configured"
    states = {i["state"] for i in items}
    if states & {"failed", "timed_out"}:
        return "failed"
    if "cancelled" in states:
        return "cancelled"
    if "running" in states:
        return "running"
    if "pending" in states:
        return "pending"
    return "passed"


def observe_all(settings, job_store, *, app: Optional[GitHubApp] = None, github: Optional[ExecutorGitHub] = None) -> int:
    app = app or GitHubApp(app_id=settings.github_app_id, private_key=settings.github_app_private_key, installation_id="0")
    github = github or ExecutorGitHub(app)
    count = 0
    for job in job_store.list_jobs(limit=200):
        pr = job_store.get_pr_metadata(job.id)
        if not pr:
            continue
        ctx = job.context or {}
        ci = ctx.get("customer_ci") or {}
        if ci.get("terminal"):
            continue
        try:
            observe_job(settings, job_store, ExecutionStore(), github, job, pr)
            count += 1
        except Exception as exc:  # noqa: BLE001
            job_store.append_log(LogEntry(job.id, "ci", "warning", f"CI observation failed: {str(exc)[:200]}"))
    return count


def observe_job(settings, job_store, exec_store, github: ExecutorGitHub, job, pr) -> Dict[str, Any]:
    owner, _, repo = job.repo.partition("/")
    token = _token_for_job(settings, github, job, repo)
    sha = pr.head_sha or _pr_head_sha(github, owner, repo, pr.number, token)
    items: List[Dict[str, Any]] = []
    for run in (github.commit_workflow_runs(owner, repo, sha, token).get("workflow_runs") or []) if hasattr(github, "commit_workflow_runs") else []:
        items.append({"name": run.get("name") or "workflow", "state": _norm(run.get("status"), run.get("conclusion")), "url": run.get("html_url")})
    for cr in (github.commit_check_runs(owner, repo, sha, token).get("check_runs") or []):
        items.append({"name": cr.get("name") or "check", "state": _norm(cr.get("status"), cr.get("conclusion")), "url": cr.get("html_url") or cr.get("details_url")})
    suites = github.commit_check_suites(owner, repo, sha, token) if hasattr(github, "commit_check_suites") else {}
    for cs in suites.get("check_suites", []) or []:
        items.append({"name": cs.get("app", {}).get("name") or "check suite", "state": _norm(cs.get("status"), cs.get("conclusion")), "url": cs.get("html_url")})
    status = github.commit_status(owner, repo, sha, token)
    for st in status.get("statuses", []) or []:
        items.append({"name": st.get("context") or "status", "state": _norm(state=st.get("state")), "url": st.get("target_url")})
    overall = _overall(items)
    terminal = overall in TERMINAL
    now = datetime.now(timezone.utc).isoformat()
    observed = {"head_sha": sha, "overall": overall, "terminal": terminal, "checks": items, "observed_at": now, "timeout_seconds": TIMEOUT_SECONDS}
    job_store.merge_context(job.id, {"customer_ci": observed})
    run = exec_store.get_run_for_job(job.id)
    if run:
        exec_store.merge_receipt_context(run.id, {"customer_ci": observed})
    job_store.append_log(LogEntry(job.id, "ci", "info", f"customer CI is {overall}", {"terminal": terminal, "checks": len(items)}))
    return observed


def _token_for_job(settings, github, job, repo_name: str) -> str:
    from .. import workspaces as ws
    if not (job.workspace_id and job.repository_id):
        raise RuntimeError("customer installation not resolvable")
    repo = ws.get_repository(job.workspace_id, job.repository_id)
    inst = ws.get_installation_by_record_id(repo.github_installation_record_id) if repo else None
    if inst is None:
        raise RuntimeError("customer installation not resolvable")
    return github.scoped_installation_token(inst.github_installation_id, repositories=[repo_name], permissions={"checks":"read", "statuses":"read", "pull_requests":"read", "actions":"read", "contents":"read"})["token"]


def _pr_head_sha(github, owner: str, repo: str, number: int, token: str) -> str:
    if hasattr(github, "pull_request"):
        return github.pull_request(owner, repo, number, token)["head"]["sha"]
    raise RuntimeError("PR head SHA unavailable")
