"""Safe, best-effort writes to the existing execution event ledger."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Optional

from .store import ExecutionStore

logger = logging.getLogger("gnsis.executor.events")

KNOWN_KINDS = frozenset({
    "repository_access_verified", "repository_base_resolved", "dispatch_started",
    "workflow_dispatched", "executor_authentication_started", "executor_authenticated",
    "run_spec_requested", "source_download_started", "source_downloaded",
    "sandbox_prepare_started", "sandbox_ready", "agent_started", "agent_progress",
    "tool_file_read", "tool_file_changed", "tool_command_started", "tool_command_completed",
    "tests_started", "tests_completed", "output_validation_started", "output_validated",
    "executor_completion_received", "executor_failure_received", "awaiting_approval",
    "run_failed", "receipt_ready", "preflight_blocked", "policy_pinned", "memory_selected",
    "tool_call",
})

_UNSAFE_KEY = re.compile(
    r"(?:authorization|cookie|credential|password|secret|api[_-]?key|jwt|token|nonce|environment|env)",
    re.I,
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_API_KEY = re.compile(r"\b(?:sk|gh[opusr]|github_pat)_[A-Za-z0-9_-]{12,}\b", re.I)


def _redact_text(value: object, limit: int = 4000) -> str:
    text = str(value).replace("\x00", "")
    for pattern in (_BEARER, _JWT, _API_KEY):
        text = pattern.sub("[REDACTED]", text)
    return text[:limit]


def safe_payload(value: Any, *, depth: int = 0) -> Any:
    """Return bounded JSON-safe data with credential-shaped fields removed."""
    if depth >= 4:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:32]:
            name = _redact_text(key, 64)
            if _UNSAFE_KEY.search(name):
                continue
            result[name] = safe_payload(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [safe_payload(item, depth=depth + 1) for item in list(value)[:32]]
    if isinstance(value, str):
        return _redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_text(value, 256)


def record_lifecycle_event(
    store: ExecutionStore,
    run,
    kind: str,
    payload: Optional[dict] = None,
    *,
    sequence: int = 0,
    identity: str = "",
) -> bool:
    """Write a known event idempotently; observability can never fail a run."""
    if kind not in KNOWN_KINDS:
        logger.warning("refusing unknown lifecycle event kind %s", kind)
        return False
    cleaned = safe_payload(payload or {})
    # Impose a final serialized ceiling even for unusually large scalar JSON.
    encoded = json.dumps(cleaned, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 16_384:
        cleaned = {"message": "Event details were truncated."}
        encoded = json.dumps(cleaned)
    digest = hashlib.sha256(f"{run.id}:{kind}:{identity}:{encoded}".encode()).hexdigest()[:24]
    try:
        return store.record_event(
            run.id, job_id=run.job_id,
            workflow_run_attempt=run.workflow_run_attempt, sequence=sequence,
            idempotency_key=f"lifecycle:{kind}:{digest}", kind=kind, payload=cleaned,
        )
    except Exception:  # noqa: BLE001 - telemetry must not affect execution
        logger.exception("failed to record lifecycle event %s for run %s", kind, run.id)
        return False
