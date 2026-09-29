"""Real-run learning records for GNSIS visual execution.

One canonical record per executed action. The recorder writes it and the
evaluator (:mod:`.evaluation`) reads it as-is: there is one contract, not a
recorder format and a benchmark format kept in step by hand.

The record deliberately does not create a second screen archive: pre/post
frame references point to the runtime's existing screen history or persisted
screen assets when available.

Two things are kept apart throughout:

- ``execution.actuator_success`` (the actuator says it acted) versus
  ``verified_success`` (the expected result was visibly there afterwards);
- the *observed* outcome of the variant that actually executed versus the
  *counterfactual* candidates (where the other variants would have pointed),
  which are only ever scored geometrically.

Local collection and shared-model export are separate controls. Shared export
is refused unless explicit consent is enabled.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping

from ..contracts import now_ms
from ..screen import LatestScreenFrameBuffer, ScreenFrame
from .runtime import recent_motion
from .verification import (
    DEFAULT_SETTLE_FRAMES,
    DEFAULT_UNSETTLED_MOTION,
    ExpectedState,
    VerificationRequest,
    VerificationResult,
    VisualVerifier,
    ambiguous,
)

SCHEMA_VERSION = 2
VariantName = Literal["raw", "raw+r24", "ocr", "ocr+r24"]
VARIANTS: tuple[str, ...] = ("raw", "raw+r24", "ocr", "ocr+r24")
CONTEXTS: tuple[str, ...] = ("browser", "desktop")
CANDIDATE_STATUSES: tuple[str, ...] = ("resolved", "abstained", "unavailable")


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


# --------------------------------------------------------------- the contract


@dataclass(frozen=True, slots=True)
class Point:
    x: float
    y: float

    @classmethod
    def from_json(cls, data: Any) -> "Point":
        if not isinstance(data, Mapping):
            raise ValueError("a point must be an object with x and y")
        return cls(float(data["x"]), float(data["y"]))

    def to_json(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y}


@dataclass(frozen=True, slots=True)
class Box:
    x: float
    y: float
    width: float
    height: float

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("target_box width/height must be positive")

    def contains(self, point: Point) -> bool:
        return self.x <= point.x <= self.x + self.width and self.y <= point.y <= self.y + self.height

    @classmethod
    def from_json(cls, data: Any) -> "Box":
        if not isinstance(data, Mapping):
            raise ValueError("target_box must be an object")
        return cls(float(data["x"]), float(data["y"]), float(data["width"]), float(data["height"]))

    def to_json(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


@dataclass(frozen=True, slots=True)
class Candidate:
    """Where one variant pointed, or why it did not.

    ``unavailable`` means the variant was not probed; ``abstained`` means it was
    probed and declined to pick a target.
    """

    status: str = "unavailable"
    point: Point | None = None
    method: str | None = None

    def __post_init__(self) -> None:
        if self.status not in CANDIDATE_STATUSES:
            raise ValueError(f"invalid candidate status {self.status!r}")
        if self.status == "resolved" and self.point is None:
            raise ValueError("a resolved candidate requires a point")

    @classmethod
    def from_json(cls, data: Any) -> "Candidate":
        if data is None:
            return cls()
        if not isinstance(data, Mapping):
            raise ValueError("a candidate must be an object")
        point = data.get("point")
        return cls(
            status=str(data.get("status", "resolved")),
            point=Point.from_json(point) if point is not None else None,
            method=_optional_text(data.get("method")),
        )

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"status": self.status}
        if self.point is not None:
            out["point"] = self.point.to_json()
        if self.method is not None:
            out["method"] = self.method
        return out


@dataclass(frozen=True, slots=True)
class Execution:
    """What the actuator reports about the one variant that actually ran."""

    executed_variant: str | None = None
    actuator_success: bool | None = None
    source_tab_id: int | None = None
    executed_tab_id: int | None = None
    started_at_ms: int | None = None
    completed_at_ms: int | None = None
    latency_ms: float | None = None
    call_id: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.executed_variant is not None and self.executed_variant not in VARIANTS:
            raise ValueError("invalid executed_variant")
        if (
            self.started_at_ms is not None
            and self.completed_at_ms is not None
            and self.completed_at_ms < self.started_at_ms
        ):
            raise ValueError("execution completed before it started")
        if self.latency_ms is not None and self.latency_ms < 0:
            raise ValueError("latency_ms must not be negative")

    @classmethod
    def from_json(cls, data: Any) -> "Execution":
        if data is None:
            return cls()
        if not isinstance(data, Mapping):
            raise ValueError("execution must be an object")
        success = data.get("actuator_success")
        if success is not None and not isinstance(success, bool):
            raise ValueError("actuator_success must be true, false or null")
        return cls(
            executed_variant=data.get("executed_variant"),
            actuator_success=success,
            source_tab_id=_optional_positive_int(data.get("source_tab_id")),
            executed_tab_id=_optional_positive_int(data.get("executed_tab_id")),
            started_at_ms=_optional_int(data.get("started_at_ms")),
            completed_at_ms=_optional_int(data.get("completed_at_ms")),
            latency_ms=float(data["latency_ms"]) if data.get("latency_ms") is not None else None,
            call_id=_optional_text(data.get("call_id")),
            error=_optional_text(data.get("error")),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "executed_variant": self.executed_variant,
            "actuator_success": self.actuator_success,
            "source_tab_id": self.source_tab_id,
            "executed_tab_id": self.executed_tab_id,
            "started_at_ms": self.started_at_ms,
            "completed_at_ms": self.completed_at_ms,
            "latency_ms": self.latency_ms,
            "call_id": self.call_id,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class RealRunRecord:
    """The one real-run record: written by the recorder, read by the evaluator."""

    run_id: str
    case_id: str
    captured_at_ms: int
    context: str
    frame_id: str
    goal: str
    action: str
    verification_status: str
    verification_reason: str
    schema_version: int = SCHEMA_VERSION
    source_ref: str | None = None
    post_frame_ids: tuple[str, ...] = ()
    expected_state: ExpectedState | None = None
    viewport: tuple[int, int] | None = None
    candidates: dict[str, Candidate] = field(default_factory=dict)
    execution: Execution = field(default_factory=Execution)
    target_box: Box | None = None
    verification_confidence: float | None = None
    verification_judge: str | None = None
    user_corrected: bool = False
    recovery_attempted: bool = False
    recovery_success: bool | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported real-run schema_version {self.schema_version!r}")
        if self.context not in CONTEXTS:
            raise ValueError("context must be browser|desktop")
        for name, value in (("run_id", self.run_id), ("case_id", self.case_id), ("frame_id", self.frame_id), ("action", self.action)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if self.captured_at_ms < 0:
            raise ValueError("captured_at_ms must not be negative")
        # Verification says what it saw; verified_success is derived from it, never set apart.
        VerificationResult(self.verification_status, self.verification_reason, self.verification_confidence)
        unknown = set(self.candidates) - set(VARIANTS)
        if unknown:
            raise ValueError(f"unknown candidate variants: {sorted(unknown)}")
        candidates = {name: self.candidates.get(name, Candidate()) for name in VARIANTS}
        object.__setattr__(self, "candidates", candidates)
        if self.viewport is not None:
            width, height = (int(v) for v in self.viewport)
            if width <= 0 or height <= 0:
                raise ValueError("viewport width/height must be positive")
            object.__setattr__(self, "viewport", (width, height))
            for name, candidate in candidates.items():
                point = candidate.point
                if point is not None and not (0 <= point.x < width and 0 <= point.y < height):
                    raise ValueError(f"{name} point is outside viewport")
        if self.recovery_success is not None and not self.recovery_attempted:
            raise ValueError("recovery_success needs recovery_attempted")
        object.__setattr__(self, "post_frame_ids", tuple(str(value) for value in self.post_frame_ids))

    @property
    def verified_success(self) -> bool | None:
        return {"success": True, "failure": False, "ambiguous": None}[self.verification_status]

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "case_id": self.case_id,
            "captured_at_ms": self.captured_at_ms,
            "context": self.context,
            "frame_id": self.frame_id,
            "source_ref": self.source_ref,
            "post_frame_ids": list(self.post_frame_ids),
            "goal": self.goal,
            "action": self.action,
            "expected_state": self.expected_state.to_json() if self.expected_state else None,
            "viewport": {"width": self.viewport[0], "height": self.viewport[1]} if self.viewport else None,
            "candidates": {name: candidate.to_json() for name, candidate in self.candidates.items()},
            "execution": self.execution.to_json(),
            "target_box": self.target_box.to_json() if self.target_box else None,
            "verified_success": self.verified_success,
            "verification_status": self.verification_status,
            "verification_reason": self.verification_reason,
            "verification_confidence": self.verification_confidence,
            "verification_judge": self.verification_judge,
            "user_corrected": self.user_corrected,
            "recovery_attempted": self.recovery_attempted,
            "recovery_success": self.recovery_success,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_json(cls, data: Any) -> "RealRunRecord":
        if not isinstance(data, Mapping):
            raise ValueError("a real-run record must be an object")
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported real-run schema_version {version!r}; "
                f"this reader takes version {SCHEMA_VERSION}, the format the runtime recorder writes"
            )
        viewport = data.get("viewport")
        raw_candidates = data.get("candidates") or {}
        if not isinstance(raw_candidates, Mapping):
            raise ValueError("candidates must be an object")
        record = cls(
            schema_version=version,
            run_id=str(data["run_id"]),
            case_id=str(data["case_id"]),
            captured_at_ms=int(data["captured_at_ms"]),
            context=str(data.get("context")),
            frame_id=str(data["frame_id"]),
            source_ref=_optional_text(data.get("source_ref")),
            post_frame_ids=tuple(data.get("post_frame_ids") or ()),
            goal=str(data.get("goal") or ""),
            action=str(data["action"]),
            expected_state=ExpectedState.from_value(data.get("expected_state")),
            viewport=(int(viewport["width"]), int(viewport["height"])) if viewport else None,
            candidates={name: Candidate.from_json(value) for name, value in raw_candidates.items()},
            execution=Execution.from_json(data.get("execution")),
            target_box=Box.from_json(data["target_box"]) if data.get("target_box") is not None else None,
            verification_status=str(data.get("verification_status")),
            verification_reason=str(data.get("verification_reason") or ""),
            verification_confidence=data.get("verification_confidence"),
            verification_judge=_optional_text(data.get("verification_judge")),
            user_corrected=bool(data.get("user_corrected", False)),
            recovery_attempted=bool(data.get("recovery_attempted", False)),
            recovery_success=data.get("recovery_success"),
            metadata=dict(data.get("metadata") or {}),
        )
        if "verified_success" in data and data["verified_success"] != record.verified_success:
            raise ValueError("verified_success disagrees with verification_status")
        return record


# ------------------------------------------------------------ browser evidence


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


# ------------------------------------------------------------------ recording


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
        if record.schema_version != SCHEMA_VERSION:
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


# ----------------------------------------------------- the post-action window


@dataclass(frozen=True, slots=True)
class PostActionWindow:
    """Frames from the shared history that can show an action's result."""

    frames: tuple[ScreenFrame, ...]
    settled: bool
    timed_out: bool
    motion: float

    @property
    def frame_ids(self) -> tuple[str, ...]:
        return tuple(frame.frame_id for frame in self.frames)


def collect_post_action_frames(
    screen_frames: LatestScreenFrameBuffer,
    *,
    before_frame_id: str,
    acted_at_ms: int | None = None,
    min_frames: int = 2,
    max_frames: int = 6,
    settle_frames: int = DEFAULT_SETTLE_FRAMES,
    unsettled_motion: float = DEFAULT_UNSETTLED_MOTION,
    timeout_ms: int = 3000,
    poll_ms: int = 40,
) -> PostActionWindow:
    """Wait, within a bound, for frames that can show the action's result.

    Only the shared runtime history is observed: no capture path, no screenshot.
    Frames the action was decided on or captured before it started are
    discarded (a frame newer than the decision frame can still predate the
    action). Collection ends as soon as there are ``min_frames`` and the last
    ``settle_frames`` of them stopped moving, or at the timeout with whatever
    arrived — the caller then treats an unsettled window as not yet evidence.
    """

    if timeout_ms < 0 or min_frames < 1 or max_frames < min_frames or settle_frames < 2 or poll_ms < 1:
        raise ValueError("invalid post-action window configuration")

    deadline = time.monotonic() + timeout_ms / 1000.0
    while True:
        ordered = list(reversed(screen_frames.recent_frames()))  # oldest first
        before_index = next(
            (index for index, frame in enumerate(ordered) if frame.frame_id == before_frame_id),
            None,
        )
        newer = ordered[before_index + 1 :] if before_index is not None else []
        if acted_at_ms is not None:
            newer = [
                frame
                for frame in newer
                if frame.captured_at_ms is None or frame.captured_at_ms >= acted_at_ms
            ]
        frames = tuple(newer[-max_frames:])
        tail = frames[-settle_frames:]
        measure = unsettled_motion != float("inf") and len(tail) >= 2
        motion = round(recent_motion(tail, window_ms=10**9), 4) if measure else 0.0
        enough = len(frames) >= min_frames
        settled = enough and motion < unsettled_motion
        if settled:
            return PostActionWindow(frames, True, False, motion)
        if time.monotonic() >= deadline:
            return PostActionWindow(frames, False, True, motion)
        time.sleep(poll_ms / 1000.0)


def wait_for_post_action_frames(
    screen_frames: LatestScreenFrameBuffer,
    *,
    before_frame_id: str,
    timeout_ms: int = 1500,
    min_frames: int = 1,
    max_frames: int = 3,
    poll_ms: int = 25,
) -> tuple[str, ...]:
    """Ids of frames newer than the action's frame (no settle requirement).

    Kept for callers that only need frame references. Evidence for
    verification should come from :func:`collect_post_action_frames`.
    """

    if timeout_ms < 0 or min_frames < 0 or max_frames < 1:
        raise ValueError("invalid post-action frame wait configuration")
    if min_frames == 0:
        min_frames = 1
    window = collect_post_action_frames(
        screen_frames,
        before_frame_id=before_frame_id,
        min_frames=min(min_frames, max_frames),
        max_frames=max_frames,
        unsettled_motion=float("inf"),
        timeout_ms=timeout_ms,
        poll_ms=max(1, poll_ms),
    )
    return window.frame_ids if len(window.frames) >= min_frames else ()


# ---------------------------------------------------------------- coordinating


def find_frame(screen_frames: LatestScreenFrameBuffer, frame_id: str) -> ScreenFrame | None:
    return next((frame for frame in screen_frames.recent_frames() if frame.frame_id == frame_id), None)


class RealRunCoordinator:
    """Turn one executed visual action plus later frames into one learning case."""

    def __init__(
        self,
        recorder: RealRunRecorder,
        *,
        verifier: VisualVerifier | None = None,
    ) -> None:
        self.recorder = recorder
        self.verifier = verifier

    def verify(
        self,
        screen_frames: LatestScreenFrameBuffer,
        *,
        frame_id: str,
        goal: str,
        action: str,
        expected_state: ExpectedState | None,
        execution: Mapping[str, Any],
        action_detail: Mapping[str, Any] | None = None,
        acted_at_ms: int | None = None,
        window: Mapping[str, Any] | None = None,
    ) -> tuple[VerificationResult, PostActionWindow | None]:
        """Collect the post-action window from shared history and judge it."""

        before = find_frame(screen_frames, frame_id)
        if before is None:
            return ambiguous("The frame the action was decided on is no longer in the screen history."), None
        collected = collect_post_action_frames(
            screen_frames,
            before_frame_id=frame_id,
            acted_at_ms=acted_at_ms,
            **dict(window or {}),
        )
        evidence = {"post_frame_ids": list(collected.frame_ids), "motion": collected.motion, "settled": collected.settled}
        if not collected.frames:
            return ambiguous("No frame from after the action arrived in time.", **evidence), collected
        if not collected.settled:
            return ambiguous("The screen had not settled when the wait for the result ended.", **evidence), collected
        if self.verifier is None:
            return ambiguous("No semantic verifier is configured.", **evidence), collected
        result = self.verifier.verify(
            VerificationRequest(
                goal=goal,
                action=action,
                expected_state=expected_state,
                before=before,
                after=collected.frames,
                execution=execution,
                action_detail=dict(action_detail or {}),
                acted_at_ms=acted_at_ms,
            )
        )
        merged = {**evidence, **result.evidence}
        return VerificationResult(result.status, result.reason, result.confidence, result.judge, merged), collected

    def finalize(
        self,
        *,
        run_id: str,
        case_id: str,
        frame_id: str,
        goal: str,
        action: str,
        verification: VerificationResult | None = None,
        post_frame_ids: tuple[str, ...] = (),
        context: str = "browser",
        expected_state: ExpectedState | str | Mapping[str, Any] | None = None,
        viewport: tuple[int, int] | None = None,
        candidates: Mapping[str, Candidate] | None = None,
        execution: Execution | Mapping[str, Any] | None = None,
        target_box: Box | None = None,
        source_ref: str | None = None,
        user_corrected: bool = False,
        recovery_attempted: bool = False,
        recovery_success: bool | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RealRunRecord:
        if verification is None:
            verification = (
                ambiguous("No frame from after the action was available.")
                if not post_frame_ids
                else ambiguous("No semantic verifier is configured.")
            )
        if not isinstance(execution, Execution):
            execution = Execution.from_json(execution)
        record = RealRunRecord(
            run_id=str(run_id),
            case_id=str(case_id),
            captured_at_ms=now_ms(),
            context=context,
            frame_id=str(frame_id),
            goal=str(goal),
            action=str(action),
            source_ref=source_ref,
            post_frame_ids=tuple(post_frame_ids),
            expected_state=ExpectedState.from_value(expected_state),
            viewport=viewport,
            candidates=dict(candidates or {}),
            execution=execution,
            target_box=target_box,
            verification_status=verification.status,
            verification_reason=verification.reason,
            verification_confidence=verification.confidence,
            verification_judge=verification.judge,
            user_corrected=bool(user_corrected),
            recovery_attempted=bool(recovery_attempted),
            recovery_success=recovery_success,
            metadata={**dict(metadata or {}), **({"verification_evidence": verification.evidence} if verification.evidence else {})},
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


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


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
