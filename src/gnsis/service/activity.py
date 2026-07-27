"""Assemble a job's Activity timeline from already-immutable records.

Mirrors :mod:`.receipts`'s "assembled on read" approach: there is no separate
Activity table. Activity merges two existing evidence sources, chronologically:

* ``job_logs`` — job-scoped narrative written by the control plane itself
  (dispatch acceptance, terminal outcomes);
* ``execution_events`` — run-scoped evidence recorded against the job's most
  recent execution run (preflight checks, the pinned policy/memory context, and
  whatever the authenticated executor reports as it works).

Both sources are already durable and already written at the moment the
underlying action happens, so merging them here is a read-time projection, not
a new source of truth. A job that never reached dispatch (blocked in preflight)
still has a run row — see ``gnsis.service.executor.preflight`` — so its
``preflight_blocked`` event is included the same way a dispatched run's events
are.
"""

from __future__ import annotations

from typing import List

#: Known event kinds this control plane itself emits, mapped to a short,
#: factual Activity line. Unrecognized kinds (including whatever vocabulary the
#: authenticated executor reports) still surface via the fallback below — Activity
#: never silently drops evidence it doesn't have a specific label for.
_BLOCKED_REASON_LABELS = {
    "blocked_repository_empty": 'repository has no initial commit (branch "{branch}" could not be resolved)',
    "blocked_branch_not_found": 'branch "{branch}" was not found',
    "blocked_installation_inaccessible": "GitHub installation is not accessible for this repository",
}


def _blocked_summary(payload: dict) -> str:
    reason = payload.get("reason_code") or ""
    branch = payload.get("base_branch") or ""
    template = _BLOCKED_REASON_LABELS.get(reason)
    if template:
        return template.format(branch=branch)
    return "a required prerequisite was missing"


def _event_to_view(event: dict) -> dict:
    kind = event.get("kind") or ""
    payload = event.get("payload") or {}
    if kind == "preflight_blocked":
        phase, level, message = "preflight", "warning", _blocked_summary(payload)
    elif kind == "policy_pinned":
        phase, level = "dispatch", "info"
        message = f"pinned policy {payload.get('name')} v{payload.get('version')}"
    elif kind == "memory_selected":
        phase, level = "dispatch", "info"
        message = f"selected {payload.get('count', 0)} memory item(s) for context"
    else:
        # Executor-reported or otherwise unrecognized: still surface it rather
        # than hide it. A "message" in the payload (if present) is preferred;
        # otherwise fall back to the event's own kind as the label.
        phase, level = "execute", ("error" if kind == "error" else "info")
        message = payload.get("message") or kind or "event"
    return {"phase": phase, "level": level, "message": message, "created_at": event.get("created_at") or ""}


#: Public lifecycle event vocabulary. Every value is derived from persisted
#: evidence — a job row, its execution run, or a recorded execution event —
#: never invented at read time.
class EventType:
    RUN_CREATED = "run.created"
    RUN_QUEUED = "run.queued"
    RUN_DISPATCH_STARTED = "run.dispatch_started"
    INSTALLATION_LOOKUP_STARTED = "executor.installation_lookup_started"
    WORKFLOW_DISPATCHED = "executor.workflow_dispatched"
    EXECUTION_STARTED = "run.execution_started"
    TOOL_CALLED = "tool.called"
    TESTS_COMPLETED = "tests.completed"
    AWAITING_APPROVAL = "run.awaiting_approval"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_BLOCKED = "run.blocked"
    RUN_APPROVED = "run.approved"
    RUN_REJECTED = "run.rejected"
    RECEIPT_READY = "receipt.ready"
    INTELLIGENCE_CONSUMED = "intelligence.consumed"
    INTELLIGENCE_CREATED = "intelligence.created"
    POLICY_PINNED = "policy.pinned"
    REPOSITORY_ACCESS_VERIFIED = "repository.access_verified"
    REPOSITORY_BASE_RESOLVED = "repository.base_resolved"
    AUTHENTICATION_STARTED = "executor.authentication_started"
    AUTHENTICATED = "executor.authenticated"
    SOURCE_DOWNLOAD_STARTED = "source.download_started"
    SOURCE_DOWNLOADED = "source.downloaded"
    OUTPUT_VALIDATION_STARTED = "output.validation_started"
    OUTPUT_VALIDATED = "output.validated"


#: Recorded execution-event kinds → public lifecycle type.
_KIND_TO_TYPE = {
    "preflight_blocked": EventType.RUN_BLOCKED,
    "policy_pinned": EventType.POLICY_PINNED,
    "memory_selected": EventType.INTELLIGENCE_CONSUMED,
    "tool_call": EventType.TOOL_CALLED,
    "tests_completed": EventType.TESTS_COMPLETED,
    "repository_access_verified": EventType.REPOSITORY_ACCESS_VERIFIED,
    "repository_base_resolved": EventType.REPOSITORY_BASE_RESOLVED,
    "dispatch_started": EventType.RUN_DISPATCH_STARTED,
    "workflow_dispatched": EventType.WORKFLOW_DISPATCHED,
    "executor_authentication_started": EventType.AUTHENTICATION_STARTED,
    "executor_authenticated": EventType.AUTHENTICATED,
    "run_spec_requested": "executor.run_spec_requested",
    "source_download_started": EventType.SOURCE_DOWNLOAD_STARTED,
    "source_downloaded": EventType.SOURCE_DOWNLOADED,
    "sandbox_prepare_started": "sandbox.prepare_started",
    "sandbox_ready": "sandbox.ready",
    "agent_started": "agent.started",
    "agent_progress": "agent.progress",
    "tool_file_read": "tool.file_read",
    "tool_file_changed": "tool.file_changed",
    "tool_command_started": "tool.command_started",
    "tool_command_completed": "tool.command_completed",
    "tests_started": "tests.started",
    "output_validation_started": EventType.OUTPUT_VALIDATION_STARTED,
    "output_validated": EventType.OUTPUT_VALIDATED,
    "awaiting_approval": EventType.AWAITING_APPROVAL,
    "executor_failure_received": EventType.RUN_FAILED,
    "run_failed": EventType.RUN_FAILED,
}

#: Terminal job status → public lifecycle type.
_STATUS_TO_TYPE = {
    "completed": EventType.RUN_COMPLETED,
    "failed": EventType.RUN_FAILED,
    "blocked": EventType.RUN_BLOCKED,
    "rejected": EventType.RUN_REJECTED,
    "awaiting_approval": EventType.AWAITING_APPROVAL,
    "approved": EventType.RUN_APPROVED,
}

# These lifecycle milestones have one public occurrence. New runs persist the
# exact boundary as an execution event; projections synthesize the milestone
# from older run/job rows only when that canonical event is absent.
_SINGULAR_TYPES = frozenset({
    EventType.RUN_DISPATCH_STARTED,
    EventType.WORKFLOW_DISPATCHED,
    EventType.RUN_FAILED,
    EventType.RUN_BLOCKED,
    EventType.AWAITING_APPROVAL,
})


def _append_persisted_events(events: List[dict], raw_events: List[dict]) -> set[str]:
    """Append canonical ledger events, collapsing singular public milestones."""
    persisted_types: set[str] = set()
    for raw in raw_events:
        kind = raw.get("kind") or ""
        event_type = _KIND_TO_TYPE.get(kind, f"executor.{kind}" if kind else "executor.event")
        if event_type in _SINGULAR_TYPES and event_type in persisted_types:
            continue
        events.append({
            "type": event_type,
            "at": raw.get("created_at") or "",
            "payload": dict(raw.get("payload") or {}),
        })
        persisted_types.add(event_type)
    return persisted_types


def build_lifecycle_events(job_id: str) -> List[dict]:
    """The run's ordered public lifecycle events, projected from stored evidence.

    Ordering is by the underlying record's timestamp; ``sequence`` is a stable
    0-based cursor over the returned list. Payloads carry only safe structured
    fields — never tokens, provider credentials, or raw environment data.
    """
    from .executor.store import ExecutionStore
    from . import orm
    from .db import session_scope

    events: List[dict] = []

    with session_scope() as s:
        job = s.get(orm.Job, job_id)
        if job is None:
            return []
        created_at = job.created_at.isoformat() if job.created_at else ""
        updated_at = job.updated_at.isoformat() if job.updated_at else ""
        status = job.status
        instruction_present = bool(job.instruction)

    events.append({
        "type": EventType.RUN_CREATED,
        "at": created_at,
        "payload": {"has_instruction": instruction_present},
    })
    events.append({"type": EventType.RUN_QUEUED, "at": created_at, "payload": {}})

    run = ExecutionStore().get_run_for_job(job_id)
    persisted_types: set[str] = set()
    if run is not None:
        events.append({
            "type": EventType.INSTALLATION_LOOKUP_STARTED,
            "at": run.created_at,
            "payload": {"repository_id": run.repository_id},
        })
        persisted_types = _append_persisted_events(
            events, ExecutionStore().events_for(run.id)
        )
        if EventType.RUN_DISPATCH_STARTED not in persisted_types:
            events.append({
                "type": EventType.RUN_DISPATCH_STARTED,
                "at": run.created_at,
                "payload": {"execution_run_id": run.id},
            })
        if run.workflow_run_id and EventType.WORKFLOW_DISPATCHED not in persisted_types:
            events.append({
                "type": EventType.WORKFLOW_DISPATCHED,
                "at": run.created_at,
                "payload": {"workflow_run_id": run.workflow_run_id},
            })
        if run.status in ("running", "validating", "completed"):
            events.append({
                "type": EventType.EXECUTION_STARTED,
                "at": run.created_at,
                "payload": {"execution_run_id": run.id},
            })

    terminal_type = _STATUS_TO_TYPE.get(status)
    if terminal_type and terminal_type not in persisted_types:
        events.append({"type": terminal_type, "at": updated_at, "payload": {"status": status}})
    # Canonical receipts are assembled from the persisted job/run evidence.
    # Readiness is independent of whether the terminal lifecycle milestone came
    # from the ledger or the legacy status fallback.
    if status in ("completed", "failed", "blocked", "rejected") and run is not None:
        from .receipts import build_receipt
        if build_receipt(run.workspace_id, job_id) is not None:
            events.append({"type": EventType.RECEIPT_READY, "at": updated_at, "payload": {}})

    events.sort(key=lambda e: e["at"] or "")
    for i, event in enumerate(events):
        event["id"] = f"evt_{job_id}_{i}"
        event["run_id"] = job_id
        event["sequence"] = i
    return events


def build_activity(job_id: str) -> List[dict]:
    """The job's Activity timeline: job-level + run-level evidence, merged and
    ordered chronologically (ISO-8601 timestamps sort correctly as strings).

    A job with no execution run yet (queued, or blocked before one could be
    created — though preflight blocking now always creates one) simply has
    whatever job-level narrative exists; this never raises.
    """
    from .executor.store import ExecutionStore
    from .repository import PostgresJobStore

    job_logs = PostgresJobStore().get_logs(job_id)
    views = [
        {"phase": e.phase, "level": e.level, "message": e.message, "created_at": e.created_at}
        for e in job_logs
    ]

    run = ExecutionStore().get_run_for_job(job_id)
    if run is not None:
        views.extend(_event_to_view(e) for e in ExecutionStore().events_for(run.id))

    views.sort(key=lambda v: v["created_at"] or "")
    return views
