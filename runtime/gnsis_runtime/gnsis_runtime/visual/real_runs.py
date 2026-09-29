"""Real-run learning records for GNSIS visual execution.

This module stores bounded metadata from actual GNSIS actions. It deliberately
does not create a second screen archive: pre/post frame references point to the
runtime's existing persisted screen assets when available.

Local collection and shared-model export are separate controls. Shared export is
refused unless explicit consent is enabled.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..contracts import now_ms


@dataclass(frozen=True, slots=True)
class RealRunConsent:
    local_collection: bool = True
    shared_training: bool = False

    @classmethod
    def from_env(cls) -> "RealRunConsent":
        return cls(
            local_collection=_env_bool("GNSIS_REAL_RUN_COLLECTION", True),
            shared_training=_env_bool("GNSIS_SHARED_TRAINING_CONSENT", False),
        )


@dataclass(frozen=True, slots=True)
class BrowserExecutionEvidence:
    action: str
    source_tab_id: int | None
    executed_tab_id: int | None
    started_at_ms: int
    completed_at_ms: int
    latency_ms: int
    source_viewport: dict[str, int] | None = None
    raw_target: dict[str, float] | None = None
    resolve_target: bool = False
    resolution_method: str | None = None
    resolved_target: dict[str, float] | None = None
    target_box: dict[str, float] | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "BrowserExecutionEvidence":
        if payload.get("context") not in {None, "browser"}:
            raise ValueError("browser execution evidence context must be browser")
        started = int(payload["started_at_ms"])
        completed = int(payload["completed_at_ms"])
        latency = int(payload.get("latency_ms", completed - started))
        if started < 0 or completed < started or latency < 0:
            raise ValueError("invalid browser execution timing")
        return cls(
            action=str(payload["action"]),
            source_tab_id=_optional_positive_int(payload.get("source_tab_id")),
            executed_tab_id=_optional_positive_int(payload.get("executed_tab_id")),
            started_at_ms=started,
            completed_at_ms=completed,
            latency_ms=latency,
            source_viewport=_optional_mapping(payload.get("source_viewport")),
            raw_target=_optional_mapping(payload.get("raw_target")),
            resolve_target=bool(payload.get("resolve_target", False)),
            resolution_method=_optional_text(payload.get("resolution_method")),
            resolved_target=_optional_mapping(payload.get("resolved_target")),
            target_box=_optional_mapping(payload.get("target_box")),
        )


@dataclass(frozen=True, slots=True)
class RealRunRecord:
    schema_version: int
    run_id: str
    case_id: str
    captured_at_ms: int
    context: str
    frame_id: str
    goal: str
    action: str
    source_ref: str | None
    post_frame_ids: tuple[str, ...]
    execution: dict[str, Any]
    verified_success: bool | None
    verification_reason: str | None
    user_corrected: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        value = asdict(self)
        value["post_frame_ids"] = list(self.post_frame_ids)
        return value


class VisualOutcomeVerifier(Protocol):
    """Semantic verifier over the actual pre/post visual sequence."""

    def verify(
        self,
        *,
        goal: str,
        action: str,
        before_frame_id: str,
        post_frame_ids: tuple[str, ...],
        execution: dict[str, Any],
    ) -> tuple[bool | None, str | None]: ...


class RealRunRecorder:
    """Append-only local metadata recorder with explicit shared-export consent."""

    def __init__(
        self,
        root: str | Path,
        *,
        consent: RealRunConsent | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.consent = consent or RealRunConsent.from_env()
        self._lock = threading.Lock()

    @property
    def jsonl_path(self) -> Path:
        return self.root / "real-runs.jsonl"

    def record(self, record: RealRunRecord) -> bool:
        if not self.consent.local_collection:
            return False
        if record.schema_version != 1:
            raise ValueError("unsupported real-run schema_version")
        self.root.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record.to_json(), ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            with self.jsonl_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        return True

    def export_for_shared_training(self, destination: str | Path) -> Path:
        if not self.consent.shared_training:
            raise PermissionError(
                "shared-model training export requires explicit GNSIS_SHARED_TRAINING_CONSENT"
            )
        source = self.jsonl_path
        if not source.exists():
            raise FileNotFoundError(source)
        destination_path = Path(destination).expanduser().resolve()
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        destination_path.write_bytes(source.read_bytes())
        return destination_path


class RealRunCoordinator:
    """Turn one executed visual action plus later frames into one learning case."""

    def __init__(
        self,
        recorder: RealRunRecorder,
        *,
        verifier: VisualOutcomeVerifier | None = None,
    ) -> None:
        self.recorder = recorder
        self.verifier = verifier

    def finalize(
        self,
        *,
        run_id: str,
        case_id: str,
        frame_id: str,
        goal: str,
        action: str,
        execution: dict[str, Any],
        post_frame_ids: tuple[str, ...],
        source_ref: str | None = None,
        user_corrected: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> RealRunRecord:
        if not post_frame_ids:
            verified: bool | None = None
            reason = "no post-action visual frame available"
        elif self.verifier is None:
            verified = None
            reason = "semantic verifier not configured"
        else:
            verified, reason = self.verifier.verify(
                goal=goal,
                action=action,
                before_frame_id=frame_id,
                post_frame_ids=post_frame_ids,
                execution=execution,
            )

        record = RealRunRecord(
            schema_version=1,
            run_id=str(run_id),
            case_id=str(case_id),
            captured_at_ms=now_ms(),
            context="browser",
            frame_id=str(frame_id),
            goal=str(goal),
            action=str(action),
            source_ref=source_ref,
            post_frame_ids=tuple(str(value) for value in post_frame_ids),
            execution=dict(execution),
            verified_success=verified,
            verification_reason=reason,
            user_corrected=bool(user_corrected),
            metadata=dict(metadata or {}),
        )
        self.recorder.record(record)
        return record


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value")


def _optional_positive_int(value: Any) -> int | None:
    if value is None:
        return None
    result = int(value)
    if result <= 0:
        raise ValueError("tab id must be positive")
    return result


def _optional_mapping(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("execution geometry must be an object")
    return dict(value)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
