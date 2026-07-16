"""Approval binding — freeze exactly what a human approved.

An approval is not "approve this job"; it is "approve this *exact* patch hash on
this *exact* base commit of this *exact* repository, verified this way, by this
user, until this time." Publishing later re-checks every one of those bindings,
so nothing about the change can drift between the click and the push.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional


def build_binding(
    *,
    job,
    run,
    repo,
    installation_record_id: Optional[str],
    actor: str,
    verification: str,
    ttl_seconds: int,
    patch_sha256: str,
) -> dict:
    now = int(time.time())
    return {
        "workspace_id": job.workspace_id,
        "repository_id": job.repository_id,
        "repository_full_name": job.repo,
        "github_installation_record_id": installation_record_id,
        "base_branch": run.base_branch,
        "base_sha": run.base_sha,
        "patch_sha256": patch_sha256,
        "execution_run_id": run.id,
        "workflow_run_id": run.workflow_run_id,
        "workflow_run_attempt": run.workflow_run_attempt,
        "verification": verification,
        "approved_by": actor,
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "expires_at_epoch": now + ttl_seconds,
    }


class ApprovalBindingError(RuntimeError):
    pass


def verify_binding(binding: dict, *, run, diff_patch_sha256: str) -> None:
    """Re-check an approval binding at publish time. Raises on any drift."""
    if not binding:
        raise ApprovalBindingError("job has no approval binding")
    if binding.get("expires_at_epoch") and time.time() > float(binding["expires_at_epoch"]):
        raise ApprovalBindingError("approval has expired; re-approve required")
    if binding.get("execution_run_id") != run.id:
        raise ApprovalBindingError("approval bound to a different execution run")
    if binding.get("base_sha") != run.base_sha:
        raise ApprovalBindingError("approved base sha does not match execution record")
    if binding.get("patch_sha256") != run.patch_sha256:
        raise ApprovalBindingError("approved patch hash does not match execution record")
    # The stored diff must be byte-identical to what was approved.
    if diff_patch_sha256 != binding.get("patch_sha256"):
        raise ApprovalBindingError("stored patch hash does not match approved hash")
