"""Authenticated run callbacks: events, completion, failure.

Every callback is bound to the exact workflow run + attempt and is validated
server-side. Completion is where the control plane decides a run produced a
trustworthy change: the reported base SHA must equal the pinned base, the patch
must be a safe unified diff that applies cleanly to the untouched clean source,
the JSON outputs must match their schemas, and the server recomputes the patch
hash itself. Only then does the job move to ``awaiting_approval``. Completion is
idempotent and a second, *different* completion can never replace the first.
"""

from __future__ import annotations

from typing import Callable, Optional

from ...orchestration.models import Diff, LogEntry
from ...orchestration.status import JobStatus, is_terminal
from . import basecheckout
from .models import ExecutionStatus, FailureCategory
from .store import ExecutionStore
from .validation import (
    sha256_text,
    strip_control_sequences,
    validate_patch_structure,
    validate_receipt_json,
    validate_tests_json,
    patch_applies_to_base,
)


class CallbackError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status
        self.message = message


def _check_attempt_binding(run, body: dict) -> None:
    """Reject callbacks from an obsolete run/attempt (stale-attempt rejection)."""
    claimed_run = body.get("run_id")
    claimed_attempt = body.get("run_attempt")
    if run.workflow_run_id is not None and claimed_run is not None:
        if str(claimed_run) != str(run.workflow_run_id):
            raise CallbackError("callback run id does not match", status=409)
    if run.workflow_run_attempt is not None and claimed_attempt is not None:
        if str(claimed_attempt) != str(run.workflow_run_attempt):
            raise CallbackError("stale run attempt", status=409)


def _compact_tests_summary(tests_raw: str) -> Optional[dict]:
    """Distil the (already validated) tests.json into an immutable receipt snapshot.

    Keeps only the small, non-sensitive outcome fields — never the raw output
    blob. Returns ``None`` if the payload can't be read as an object.
    """
    import json

    try:
        data = json.loads(tests_raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    def _int(key: str) -> int:
        value = data.get(key)
        return value if isinstance(value, bool) is False and isinstance(value, int) else 0

    return {
        "runner": str(data.get("runner") or "")[:64],
        "status": str(data.get("status") or "")[:32],
        "passed": _int("passed"),
        "failed": _int("failed"),
        "skipped": _int("skipped"),
    }


def record_run_event(settings, exec_store: ExecutionStore, run, body: dict) -> dict:
    """Persist a run event. Idempotent, sequence-checked, redacted."""
    if is_terminal(run.status) or run.status in ExecutionStatus.TERMINAL:
        raise CallbackError("run is not accepting events", status=409)
    if run.cancellation_requested:
        raise CallbackError("run is cancelling", status=409)
    _check_attempt_binding(run, body)

    kind = str(body.get("kind") or body.get("type") or "log")[:64]
    sequence = int(body.get("sequence") or 0)
    idem = str(body.get("idempotency_key") or f"{run.id}:{sequence}:{kind}")[:128]
    message = body.get("message")
    payload = body.get("data") if isinstance(body.get("data"), dict) else {}
    # No ANSI/control-sequence injection into trusted output.
    if isinstance(message, str):
        payload = {**payload, "message": strip_control_sequences(message)[:4000]}

    created = exec_store.record_event(
        run.id,
        job_id=run.job_id,
        workflow_run_attempt=run.workflow_run_attempt,
        sequence=sequence,
        idempotency_key=idem,
        kind=kind,
        payload=payload,
    )
    return {"accepted": True, "duplicate": not created}


def handle_failed(settings, job_store, exec_store: ExecutionStore, run, body: dict) -> dict:
    """Record an executor-reported failure and fail the job (idempotent)."""
    _check_attempt_binding(run, body)
    if run.status in ExecutionStatus.TERMINAL:
        return {"accepted": True, "status": run.status}
    reason = strip_control_sequences(str(body.get("reason") or "executor reported failure"))[:500]
    category = str(body.get("category") or FailureCategory.EXECUTOR_ERROR)[:64]
    exec_store.set_status(run.id, ExecutionStatus.FAILED, failure_category=category)
    exec_store.revoke_token(run.id)
    job = job_store.get_job(run.job_id)
    if job is not None and not is_terminal(job.status):
        job_store.set_status(run.job_id, JobStatus.FAILED, error=reason)
        job_store.append_log(LogEntry(run.job_id, "execute", "error", reason))
    return {"accepted": True, "status": ExecutionStatus.FAILED}


def handle_complete(
    settings,
    job_store,
    exec_store: ExecutionStore,
    run,
    body: dict,
    *,
    base_materializer: Optional[Callable[[], str]] = None,
) -> dict:
    """Validate outputs against the clean base and gate the job for approval."""
    _check_attempt_binding(run, body)

    outputs = body.get("outputs") or {}
    patch = outputs.get("patch.diff")
    if not isinstance(patch, str):
        raise CallbackError("outputs['patch.diff'] is required")

    # 1) Base SHA must match the pinned base exactly.
    reported_base = body.get("base_sha")
    if reported_base != run.base_sha:
        exec_store.set_status(run.id, ExecutionStatus.FAILED, failure_category=FailureCategory.VALIDATION)
        exec_store.revoke_token(run.id)
        _fail_job(job_store, run.job_id, "reported base sha does not match execution record")
        raise CallbackError("base sha mismatch", status=409)

    # 2) Structural + safety validation of the patch (no I/O).
    result = validate_patch_structure(patch, max_bytes=settings.executor_patch_max_bytes)
    if not result.ok:
        exec_store.set_status(run.id, ExecutionStatus.FAILED, failure_category=FailureCategory.SECURITY)
        exec_store.revoke_token(run.id)
        _fail_job(job_store, run.job_id, f"patch rejected: {result.reason}")
        raise CallbackError(f"patch rejected: {result.reason}", status=422)

    # 3) JSON outputs must be structurally valid.
    tests_raw = outputs.get("tests.json")
    tests_summary: Optional[dict] = None
    if isinstance(tests_raw, str):
        tv = validate_tests_json(tests_raw)
        if not tv.ok:
            raise CallbackError(tv.reason, status=422)
        tests_summary = _compact_tests_summary(tests_raw)
    receipt_raw = outputs.get("receipt.json")
    if isinstance(receipt_raw, str):
        rv = validate_receipt_json(receipt_raw)
        if not rv.ok:
            raise CallbackError(rv.reason, status=422)

    # 4) Server computes the patch hash; a supplied hash must agree.
    patch_sha256 = sha256_text(patch)
    reported_hashes = body.get("hashes") or {}
    if "patch.diff" in reported_hashes and reported_hashes["patch.diff"] != patch_sha256:
        raise CallbackError("patch hash mismatch", status=422)

    # 5) Idempotency: identical completion is a no-op; a different one is refused.
    fresh = exec_store.get_run(run.id)
    if fresh.patch_sha256:
        if fresh.patch_sha256 == patch_sha256:
            return {"accepted": True, "status": fresh.status, "patch_sha256": patch_sha256, "duplicate": True}
        raise CallbackError("a different completion already recorded", status=409)

    # 6) The patch must apply cleanly to the exact, untouched base commit.
    exec_store.set_status(run.id, ExecutionStatus.VALIDATING)
    base_dir = None
    try:
        base_dir = (base_materializer or (lambda: basecheckout.materialize_base(
            settings, run, run_repo(job_store, run)
        )))()
        applied = patch_applies_to_base(base_dir, patch)
    finally:
        basecheckout.cleanup(base_dir)
    if not applied.ok:
        exec_store.set_status(run.id, ExecutionStatus.FAILED, failure_category=FailureCategory.VALIDATION)
        exec_store.revoke_token(run.id)
        _fail_job(job_store, run.job_id, f"patch does not apply to base: {applied.reason}")
        raise CallbackError("patch does not apply to clean base", status=422)

    # 7) Record hashes, persist the diff, gate for approval, revoke the token.
    artifact_hashes = {
        name: sha256_text(outputs[name])
        for name in ("patch.diff", "tests.json", "receipt.json")
        if isinstance(outputs.get(name), str)
    }
    events_meta = outputs.get("events.jsonl")
    if isinstance(events_meta, dict) and events_meta.get("sha256"):
        artifact_hashes["events.jsonl"] = str(events_meta["sha256"])
    exec_store.set_patch_result(
        run.id,
        patch_sha256=patch_sha256,
        artifact_hashes=artifact_hashes,
        security_validation="passed",
        tests_summary=tests_summary,
    )
    exec_store.set_status(run.id, ExecutionStatus.COMPLETED)
    exec_store.revoke_token(run.id)

    job_store.save_diff(Diff(run.job_id, patch, files_changed=result.files))
    job = job_store.get_job(run.job_id)
    if job is not None and not is_terminal(job.status):
        job_store.set_status(run.job_id, JobStatus.AWAITING_APPROVAL)
        job_store.merge_context(run.job_id, {"base_sha": run.base_sha, "patch_sha256": patch_sha256})
        job_store.append_log(
            LogEntry(run.job_id, "summary", "info", "awaiting human approval before publishing")
        )
    return {"accepted": True, "status": ExecutionStatus.COMPLETED, "patch_sha256": patch_sha256}


def run_repo(job_store, run) -> str:
    job = job_store.get_job(run.job_id)
    return job.repo if job else ""


def _fail_job(job_store, job_id: str, error: str) -> None:
    job = job_store.get_job(job_id)
    if job is not None and not is_terminal(job.status):
        job_store.set_status(job_id, JobStatus.FAILED, error=error)
        job_store.append_log(LogEntry(job_id, "execute", "error", error))
