"""Assemble a run receipt from already-immutable records (G6, minimal).

The receipt is *assembled on read* — there is no separate receipt table. Every
field is pulled from a record that is never rewritten after the fact:

* the pinned run context + accrued usage on ``execution_runs`` (policy version /
  hash, memory ids, base sha, patch hash, token/call/cost counters, lifecycle
  timestamps, the immutable ``tests_summary`` snapshot);
* the exact money that was charged, from ``usage_charges`` (retail, service fee)
  and ``usage_records`` (provider cost, the historical ``pricing_version_id``,
  reconciliation state) — **never recomputed with the current markup or price**;
* the human decision from ``job_approvals``; the PR link from ``pull_requests``;
* the memory the run consumed (``memory_consumptions``) and the reviewed
  intelligence it produced (``memory_provenance``).

Because those source rows are immutable, a receipt is historically accurate: a
later pricing or policy change cannot alter what a past run reports. Reads are
tenant-scoped — a receipt is only ever built for a job owned by the calling
workspace.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import List, Optional

from . import orm
from .db import session_scope


def _iso(dt) -> Optional[str]:
    return dt.isoformat() if dt else None


def _sum_decimals(values: List[Optional[str]]) -> str:
    total = Decimal("0")
    for value in values:
        if value in (None, ""):
            continue
        try:
            total += Decimal(str(value))
        except (InvalidOperation, ValueError):
            continue
    return str(total)


def _duration_seconds(start, end) -> Optional[float]:
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds())


def build_receipt(workspace_id: str, job_id: str) -> Optional[dict]:
    """Return the run receipt for ``job_id`` within ``workspace_id``, or None.

    ``None`` means the job does not exist *or* is not owned by this workspace —
    callers should surface both as a 404 so ownership is not leaked. A job that
    exists but never produced an execution run yields a receipt whose run-scoped
    fields are null (a truthful "nothing ran yet" state).
    """
    with session_scope() as s:
        job = s.get(orm.Job, job_id)
        if job is None or job.workspace_id != workspace_id:
            return None

        run = (
            s.query(orm.ExecutionRun)
            .filter(orm.ExecutionRun.job_id == job_id)
            .order_by(orm.ExecutionRun.created_at.desc())
            .first()
        )

        approval = (
            s.query(orm.JobApproval)
            .filter(orm.JobApproval.job_id == job_id)
            .order_by(orm.JobApproval.id.desc())
            .first()
        )
        pr = s.get(orm.PullRequest, job_id)
        diff = s.get(orm.JobDiff, job_id)

        receipt: dict = {
            "job_id": job_id,
            "run_id": run.id if run else None,
            "task": job.instruction,
            "repository": job.repo,
            "workspace_id": job.workspace_id,
            "repository_id": job.repository_id,
            "agent": job.engine,
            "status": run.status if run else job.status,
            "approval": (
                {"decision": approval.decision, "approver": approval.actor,
                 "at": _iso(approval.created_at)}
                if approval else None
            ),
            "pull_request": (
                {"number": pr.number, "url": pr.url, "branch": pr.branch}
                if pr else None
            ),
            "files_changed": list(diff.files_changed or []) if diff else [],
        }

        if run is None:
            # No execution run yet — return the job-scoped shell with null run data.
            receipt.update(
                {
                    "model": None, "base_sha": None, "patch_hash": None,
                    "policy": None, "memory_ids_consumed": [],
                    "reviewed_intelligence_created": [], "tokens": None,
                    "model_calls": 0, "tool_calls": 0, "tests": None,
                    "cost": None, "timing": None,
                    "failure_category": None, "failure_message": None,
                }
            )
            return receipt

        run_id = run.id

        model_calls = (
            s.query(orm.ExecutionModelCall)
            .filter(orm.ExecutionModelCall.run_id == run_id)
            .all()
        )
        models_used = sorted({c.model for c in model_calls if c.model})

        events = (
            s.query(orm.ExecutionEvent)
            .filter(orm.ExecutionEvent.run_id == run_id)
            .all()
        )
        tool_call_events = [e for e in events if e.kind == "tool_call"]
        files_read = sum(
            1 for e in tool_call_events
            if isinstance(e.payload, dict) and e.payload.get("name") == "read_file"
        )

        usage_rows = (
            s.query(orm.UsageRecord)
            .filter(orm.UsageRecord.run_id == run_id)
            .all()
        )
        charge_rows = (
            s.query(orm.UsageCharge)
            .filter(orm.UsageCharge.run_id == run_id)
            .all()
        )
        consumptions = (
            s.query(orm.MemoryConsumption)
            .filter(orm.MemoryConsumption.run_id == run_id)
            .order_by(orm.MemoryConsumption.id)
            .all()
        )
        provenance = (
            s.query(orm.MemoryProvenance)
            .filter(orm.MemoryProvenance.source_run_id == run_id)
            .order_by(orm.MemoryProvenance.id)
            .all()
        )

        # Reconciliation: any row needing reconciliation dominates.
        recon = "resolved"
        for row in usage_rows:
            if row.reconciliation_state and row.reconciliation_state != "resolved":
                recon = row.reconciliation_state
                break
        pricing_versions = sorted(
            {r.pricing_version_id for r in usage_rows if r.pricing_version_id}
        )
        rate_card_versions = sorted(
            {c.rate_card_version for c in charge_rows if c.rate_card_version}
        )

        receipt.update(
            {
                "model": models_used[0] if models_used else None,
                "models_used": models_used,
                "base_sha": run.base_sha or None,
                "patch_hash": run.patch_sha256,
                "policy": (
                    {"name": run.policy_name, "version": run.policy_version,
                     "hash": run.policy_hash}
                    if run.policy_version is not None else None
                ),
                "memory_ids_consumed": [c.memory_id for c in consumptions],
                "reviewed_intelligence_created": [
                    {"memory_id": p.memory_id, "item_key": p.item_key, "kind": p.kind}
                    for p in provenance
                ],
                "tokens": {
                    "input": run.input_tokens,
                    "output": run.output_tokens,
                    "cached": sum(r.cached_tokens or 0 for r in usage_rows),
                    "reasoning": sum(r.reasoning_tokens or 0 for r in usage_rows),
                },
                "model_calls": run.model_calls,
                "tool_calls": len(tool_call_events),
                "files_read": files_read,
                "tests": dict(run.tests_summary) if run.tests_summary else None,
                "cost": {
                    "provider_cost": _sum_decimals([r.upstream_cost for r in usage_rows]),
                    "gnsis_service_fee": _sum_decimals([c.service_fee for c in charge_rows]),
                    "total_billed": _sum_decimals([c.retail_cost for c in charge_rows]),
                    "currency": (charge_rows[0].currency if charge_rows else "USD"),
                    "pricing_version": pricing_versions[0] if pricing_versions else None,
                    "rate_card_version": rate_card_versions[0] if rate_card_versions else None,
                    "reconciliation_state": recon,
                },
                "timing": {
                    "dispatched_at": _iso(run.dispatched_at),
                    "started_at": _iso(run.started_at),
                    "completed_at": _iso(run.completed_at),
                    "cancelled_at": _iso(run.cancelled_at),
                    "duration_seconds": _duration_seconds(
                        run.started_at or run.dispatched_at, run.completed_at
                    ),
                },
                "failure_category": run.failure_category,
                # job.error is already control-sequence-stripped + truncated in
                # the failure paths, so it is safe to surface to the user.
                "failure_message": job.error if run.status == "failed" else None,
            }
        )
        return receipt
