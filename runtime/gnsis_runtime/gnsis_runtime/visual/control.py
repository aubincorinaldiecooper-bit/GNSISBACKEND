"""Verification-driven control for one visual step (V4).

Verification is not only training metadata; it decides what happens next::

    execute ──> verify
                 ├─ success   ──> continue
                 ├─ failure   ──> reobserve ──> reground ──> ONE alternate attempt ──> verify
                 │                                             ├─ success ──> continue
                 │                                             └─ otherwise ──> escalate
                 └─ ambiguous ──> gather more frames ──> verify again (bounded) ──> escalate if still unsure

Everything is bounded: at most ``max_reverify`` extra looks for an ambiguous
result and exactly one alternate attempt after a failure. There is no loop
back into planning here — regrounding is a single call to whoever planned the
step (System 1's decision session or System 2's grounding), and escalation
hands the step back to them.

A step that states no expected visible result cannot be verified; it is
recorded as ambiguous and control continues without waiting for evidence
that could never decide it.

Every attempt becomes one real-run record, and every transition is a
``visual.*`` event on the session timeline, correlated by case and call id.
The waits block, so async callers run steps in a worker thread.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Literal, Mapping, Protocol

from ..screen import LatestScreenFrameBuffer
from .real_runs import Box, Candidate, Execution, Point, RealRunCoordinator, RealRunRecord
from .runtime import GatedDecision
from .verification import (
    ExpectedState,
    VerificationResult,
    actuator_reported_failure,
    ambiguous,
    derive_expected_state,
)

StepStatus = Literal["succeeded", "unverified", "escalated"]
ActionProvenance = Literal["direct_user", "mixed", "observed_untrusted", "delegated_result", "unknown"]
PolicyDecision = Literal["allow", "confirm", "deny"]
ConfirmationState = Literal["not_required", "approved", "denied", "missing"]
DEFAULT_MAX_REVERIFY = 2
ABSTAIN_WAIT_MS = 500


@dataclass(frozen=True, slots=True)
class ActionAuthority:
    """Trusted execution authority attached to a model-produced visual step.

    The visual model may propose an action, but it cannot grant itself authority
    to execute it. The caller binds the step to a trusted user turn and records
    the policy/confirmation result plus the capability manifest that was in
    force. Browser execution fails closed unless this object explicitly permits
    the action.
    """

    turn_id: str
    provenance: ActionProvenance
    policy_decision: PolicyDecision
    policy_reason: str
    capability_manifest_id: str
    allowed_actions: tuple[str, ...]
    confirmation: ConfirmationState = "not_required"

    def __post_init__(self) -> None:
        for name in ("turn_id", "policy_reason", "capability_manifest_id"):
            value = str(getattr(self, name)).strip()
            if not value:
                raise ValueError(f"{name} must be a non-empty string")
            object.__setattr__(self, name, value)
        if self.provenance not in {"direct_user", "mixed", "observed_untrusted", "delegated_result", "unknown"}:
            raise ValueError("invalid action provenance")
        if self.policy_decision not in {"allow", "confirm", "deny"}:
            raise ValueError("invalid policy decision")
        if self.confirmation not in {"not_required", "approved", "denied", "missing"}:
            raise ValueError("invalid confirmation state")
        allowed = tuple(dict.fromkeys(str(action).strip() for action in self.allowed_actions if str(action).strip()))
        if not allowed:
            raise ValueError("allowed_actions must name at least one capability")
        object.__setattr__(self, "allowed_actions", allowed)

    @property
    def execution_allowed(self) -> bool:
        if self.policy_decision == "deny":
            return False
        # A generic allow is not enough when the intent cannot be traced to the
        # person: unknown provenance fails closed, and an action induced by
        # observed content needs their explicit confirmation
        # (docs/computer-use/AGENTS.md, "Required provenance classes").
        if self.policy_decision == "confirm" or self.provenance in {"unknown", "observed_untrusted"}:
            return self.confirmation == "approved"
        return self.confirmation in {"not_required", "approved"}

    def to_json(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "provenance": self.provenance,
            "policy_decision": self.policy_decision,
            "policy_reason": self.policy_reason,
            "capability_manifest_id": self.capability_manifest_id,
            "allowed_actions": list(self.allowed_actions),
            "confirmation": self.confirmation,
        }


@dataclass(frozen=True, slots=True)
class VisualStep:
    """One planned action, stated with the result it should visibly produce."""

    run_id: str
    case_id: str
    goal: str
    action: str
    frame_id: str
    expected_state: ExpectedState | None = None
    target: Point | None = None
    text: str | None = None
    option: str | None = None
    url: str | None = None
    direction: str | None = None
    tab_id: int | None = None
    wait_ms: int | None = None
    context: str = "browser"
    planner: str = "system1"
    confidence: float | None = None
    # r24 is execution cleanup, never perception: off unless a step opts in.
    resolve_target: bool = False
    max_radius_px: int | None = None
    authority: ActionAuthority | None = None

    def __post_init__(self) -> None:
        if self.max_radius_px is not None and not 0 <= self.max_radius_px <= 24:
            raise ValueError("max_radius_px must be within 0..24")

    def detail(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.target is not None:
            out["target"] = self.target.to_json()
        for name in ("text", "option", "url", "direction", "tab_id", "wait_ms"):
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        return out


def step_from_decision(
    gated: GatedDecision,
    *,
    run_id: str,
    case_id: str,
    goal: str,
    authority: ActionAuthority | None = None,
    expected_state: ExpectedState | None = None,
    planner: str = "system1",
) -> VisualStep:
    """The one deterministic mapping from a gated System-1 decision to a step.

    Only ``gated.decision`` is read, so an abstained or rejected proposal can
    only ever reach the actuator as a bounded WAIT on the observed frame.
    """

    decision = gated.decision
    if gated.status != "act" or decision.action == "wait":
        return VisualStep(
            run_id=run_id,
            case_id=case_id,
            goal=goal,
            action="wait",
            frame_id=gated.frame_id,
            wait_ms=ABSTAIN_WAIT_MS,
            planner=planner,
            confidence=decision.confidence,
            authority=authority,
        )
    return VisualStep(
        run_id=run_id,
        case_id=case_id,
        goal=goal,
        action=decision.action,
        frame_id=gated.frame_id,
        expected_state=(
            expected_state
            if expected_state is not None
            else derive_expected_state(goal, decision.action, text=decision.text)
        ),
        target=Point(decision.target.x, decision.target.y) if decision.target is not None else None,
        text=decision.text,
        url=decision.url,
        direction=decision.direction,
        planner=planner,
        confidence=decision.confidence,
        authority=authority,
    )


@dataclass(frozen=True, slots=True)
class ExecutionReport:
    """What an environment actuator reports back for one step.

    ``acted_at_ms`` must be on the same clock as the frames' ``captured_at_ms``
    (the capture side's clock), so frames from before the action can be told
    apart from frames that could show its result.
    """

    execution: Execution
    acted_at_ms: int | None = None
    viewport: tuple[int, int] | None = None
    candidates: Mapping[str, Candidate] = field(default_factory=dict)
    target_box: Box | None = None
    source_ref: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


class StepExecutor(Protocol):
    """The execution router's view of an environment actuator."""

    def execute(self, step: VisualStep) -> ExecutionReport: ...


class Timeline(Protocol):
    def emit(self, kind: str, *, component: str, fields: dict[str, Any] | None = None, **kwargs: Any) -> Any: ...


Reground = Callable[[VisualStep, VerificationResult], "VisualStep | None"]


@dataclass(frozen=True, slots=True)
class StepOutcome:
    status: StepStatus
    reason: str
    verification: VerificationResult
    first_attempt_success: bool | None
    recovery_attempted: bool
    recovery_success: bool | None
    escalated: bool
    records: tuple[RealRunRecord, ...]

    @property
    def should_continue(self) -> bool:
        return self.status != "escalated"


class VerifiedStepController:
    def __init__(
        self,
        *,
        executor: StepExecutor,
        coordinator: RealRunCoordinator,
        screen_frames: LatestScreenFrameBuffer,
        timeline: Timeline | None = None,
        max_reverify: int = DEFAULT_MAX_REVERIFY,
        window: Mapping[str, Any] | None = None,
    ) -> None:
        if max_reverify < 0:
            raise ValueError("max_reverify must not be negative")
        self.executor = executor
        self.coordinator = coordinator
        self.screen_frames = screen_frames
        self.timeline = timeline
        self.max_reverify = int(max_reverify)
        self.window = dict(window or {})

    # ------------------------------------------------------------------ steps

    def run_step(self, step: VisualStep, *, reground: Reground | None = None) -> StepOutcome:
        self._emit(
            "visual.step.requested",
            step,
            action=step.action,
            frame_id=step.frame_id,
            planner=step.planner,
            expected_state=step.expected_state.description if step.expected_state else None,
            authority=step.authority.to_json() if step.authority is not None else None,
        )
        first, record = self._attempt(step, attempt=1)
        records = [record]
        if first.status == "success":
            return self._finish(step, "succeeded", "The expected result is visible.", first, True, False, None, records)
        if self._policy_blocked(record):
            return self._finish(
                step,
                "escalated",
                f"Execution was blocked by policy: {record.execution.error or first.reason}",
                first,
                None,
                False,
                None,
                records,
            )
        if self._outcome_unknown(record):
            return self._finish(
                step,
                "escalated",
                f"The browser action may have happened, so it will not be retried automatically: {first.reason}",
                first,
                None,
                False,
                None,
                records,
            )
        if first.status == "ambiguous" and not self._failed_to_act(record):
            if step.expected_state is None:
                return self._finish(step, "unverified", first.reason, first, None, False, None, records)
            return self._finish(
                step,
                "escalated",
                f"Still could not tell after looking again: {first.reason}",
                first,
                None,
                False,
                None,
                records,
            )

        # Failure, or the actuator did not carry the action out: one alternate attempt.
        alternate = reground(step, first) if reground is not None else None
        self._emit("visual.recovery", step, attempt=2, cause=first.status, alternate=alternate is not None)
        if alternate is None:
            return self._finish(
                step, "escalated", f"No alternate attempt was available after: {first.reason}", first, False, False, None, records
            )
        alternate = replace(alternate, run_id=step.run_id, case_id=f"{step.case_id}#2")
        second, retry_record = self._attempt(alternate, attempt=2, recovery=True)
        records.append(retry_record)
        if second.status == "success":
            return self._finish(step, "succeeded", "The alternate attempt worked.", second, False, True, True, records)
        return self._finish(
            step,
            "escalated",
            f"The alternate attempt did not visibly work: {second.reason}",
            second,
            False,
            True,
            False,
            records,
        )

    # --------------------------------------------------------------- internals

    def _attempt(self, step: VisualStep, *, attempt: int, recovery: bool = False) -> tuple[VerificationResult, RealRunRecord]:
        report = self.executor.execute(step)
        execution = report.execution
        self._emit(
            "visual.action.executed",
            step,
            attempt=attempt,
            actuator_success=execution.actuator_success,
            executed_variant=execution.executed_variant,
            latency_ms=execution.latency_ms,
            error=execution.error,
            correlation_id=execution.call_id,
        )
        verification, window = self._verify(step, report)
        looks = 0
        while (
            verification.status == "ambiguous"
            and step.expected_state is not None
            and looks < self.max_reverify
            and not actuator_reported_failure(execution.to_json())
        ):
            # A re-look must wait for evidence newer than what was already judged.
            # Re-reading the same settled window can exhaust every retry in
            # milliseconds while the page is still loading.
            newer_than = window.frames[-1].frame_id if window is not None and window.frames else None
            looks += 1
            verification, next_window = self._verify(step, report, newer_than_frame_id=newer_than)
            window = next_window
            if newer_than is not None and (window is None or not window.frames):
                break
        self._emit(
            "visual.verification",
            step,
            attempt=attempt,
            status=verification.status,
            reason=verification.reason,
            confidence=verification.confidence,
            judge=verification.judge,
            looks=looks + 1,
            post_frame_ids=list(window.frame_ids) if window is not None else [],
            correlation_id=execution.call_id,
        )
        record = self.coordinator.finalize(
            run_id=step.run_id,
            case_id=step.case_id,
            frame_id=step.frame_id,
            goal=step.goal,
            action=step.action,
            verification=verification,
            post_frame_ids=window.frame_ids if window is not None else (),
            context=step.context,
            expected_state=step.expected_state,
            viewport=report.viewport,
            candidates=report.candidates,
            execution=execution,
            target_box=report.target_box,
            source_ref=report.source_ref,
            recovery_attempted=recovery,
            recovery_success=verification.verified_success if recovery else None,
            metadata={**dict(report.metadata), "planner": step.planner, "attempt": attempt, "verification_looks": looks + 1},
        )
        return verification, record

    def _verify(
        self,
        step: VisualStep,
        report: ExecutionReport,
        *,
        newer_than_frame_id: str | None = None,
    ):
        if actuator_reported_failure(report.execution.to_json()):
            return ambiguous(f"The action was not carried out: {report.execution.error or 'the actuator reported failure'}."), None
        return self.coordinator.verify(
            self.screen_frames,
            frame_id=step.frame_id,
            goal=step.goal,
            action=step.action,
            expected_state=step.expected_state,
            execution=report.execution.to_json(),
            action_detail=step.detail(),
            acted_at_ms=report.acted_at_ms,
            window=self.window,
            newer_than_frame_id=newer_than_frame_id,
        )

    @staticmethod
    def _failed_to_act(record: RealRunRecord) -> bool:
        return actuator_reported_failure(record.execution.to_json())

    @staticmethod
    def _outcome_unknown(record: RealRunRecord) -> bool:
        return bool(record.metadata.get("outcome_unknown"))

    @staticmethod
    def _policy_blocked(record: RealRunRecord) -> bool:
        return bool(record.metadata.get("policy_blocked"))

    def _finish(
        self,
        step: VisualStep,
        status: StepStatus,
        reason: str,
        verification: VerificationResult,
        first_attempt_success: bool | None,
        recovery_attempted: bool,
        recovery_success: bool | None,
        records: list[RealRunRecord],
    ) -> StepOutcome:
        outcome = StepOutcome(
            status=status,
            reason=reason,
            verification=verification,
            first_attempt_success=first_attempt_success,
            recovery_attempted=recovery_attempted,
            recovery_success=recovery_success,
            escalated=status == "escalated",
            records=tuple(records),
        )
        self._emit(
            "visual.step.completed",
            step,
            outcome=status,
            reason=reason,
            first_attempt_success=first_attempt_success,
            recovery_attempted=recovery_attempted,
            recovery_success=recovery_success,
            escalated=outcome.escalated,
        )
        return outcome

    def _emit(self, kind: str, step: VisualStep, *, correlation_id: str | None = None, **fields: Any) -> None:
        if self.timeline is None:
            return
        self.timeline.emit(
            kind,
            component="visual",
            fields={"run_id": step.run_id, "case_id": step.case_id, **fields},
            correlation_id=correlation_id or step.case_id,
        )
