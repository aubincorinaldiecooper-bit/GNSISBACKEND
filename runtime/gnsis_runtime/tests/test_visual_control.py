from pathlib import Path

import pytest
from PIL import Image

from gnsis_runtime.screen import LatestScreenFrameBuffer, ScreenFrame
from gnsis_runtime.timeline import SessionTimeline
from gnsis_runtime.visual.control import ExecutionReport, VerifiedStepController, VisualStep
from gnsis_runtime.visual.evaluation import evaluate
from gnsis_runtime.visual.real_runs import Execution, Point, RealRunCoordinator, RealRunRecorder
from gnsis_runtime.visual.verification import ExpectedState, SemanticVisualVerifier


class Screen:
    """A stand-in display: the executor changes what the next frames show."""

    def __init__(self, buffer: LatestScreenFrameBuffer):
        self.buffer = buffer
        self.clock = 1000
        self.label = "USD"
        self.seq = 0

    def frame(self) -> str:
        self.seq += 1
        self.clock += 100
        frame_id = f"f-{self.seq}"
        image = Image.new("RGB", (200, 120), "white")
        image.info["label"] = self.label
        self.buffer.publish(ScreenFrame(frame_id, image, captured_at_ms=self.clock))
        self.buffer.consume_for_unit()
        return frame_id


class LabelJudge:
    """Reads the 'label' the stand-in screen drew; decides like the OCR judge."""

    name = "label"

    def judge(self, request):
        shown = request.after[-1].image.info.get("label")
        if shown == "CAD":
            return {"status": "success", "reason": "CAD shown", "confidence": 0.95}
        if shown == "USD":
            return {"status": "failure", "reason": "USD still shown", "confidence": 0.95}
        return {"status": "ambiguous", "reason": "nothing readable"}


class Executor:
    def __init__(self, screen: Screen, effects):
        self.screen = screen
        self.effects = list(effects)
        self.steps = []

    def execute(self, step):
        self.steps.append(step)
        effect = self.effects.pop(0)
        acted = self.screen.clock + 1
        ok = effect != "not-run"
        if ok and effect is not None:
            self.screen.label = effect
        for _ in range(2):
            self.screen.frame()
        return ExecutionReport(
            execution=Execution(
                executed_variant="raw",
                actuator_success=ok,
                call_id=f"call-{len(self.steps)}",
                latency_ms=12,
                error=None if ok else "stale frame",
            ),
            acted_at_ms=acted,
            viewport=(200, 120),
        )


def controller(tmp_path: Path, effects, *, max_reverify=1):
    buffer = LatestScreenFrameBuffer(max_history_frames=32)
    screen = Screen(buffer)
    executor = Executor(screen, effects)
    timeline = SessionTimeline("s-1")
    recorder = RealRunRecorder(tmp_path)
    ctl = VerifiedStepController(
        executor=executor,
        coordinator=RealRunCoordinator(recorder, verifier=SemanticVisualVerifier([LabelJudge()])),
        screen_frames=buffer,
        timeline=timeline,
        max_reverify=max_reverify,
        window={"timeout_ms": 0},
    )
    return ctl, screen, executor, timeline, recorder


def step(frame_id: str, **overrides) -> VisualStep:
    values = dict(
        run_id="run-1",
        case_id="case-1",
        goal="Change the currency to CAD",
        action="click",
        frame_id=frame_id,
        target=Point(40, 30),
        expected_state=ExpectedState("Prices are shown in CAD", visible_text=("CAD",), absent_text=("USD",)),
    )
    values.update(overrides)
    return VisualStep(**values)


def kinds(timeline):
    return [event.kind for event in timeline.snapshot()]


def test_success_continues_after_one_attempt(tmp_path):
    ctl, screen, executor, timeline, recorder = controller(tmp_path, ["CAD"])
    outcome = ctl.run_step(step(screen.frame()))
    assert outcome.status == "succeeded" and outcome.should_continue
    assert (outcome.first_attempt_success, outcome.recovery_attempted, outcome.escalated) == (True, False, False)
    assert len(executor.steps) == 1
    assert kinds(timeline) == ["visual.step.requested", "visual.action.executed", "visual.verification", "visual.step.completed"]
    assert len(recorder.jsonl_path.read_text().splitlines()) == 1


def test_failure_gets_exactly_one_regrounded_attempt(tmp_path):
    ctl, screen, executor, timeline, recorder = controller(tmp_path, [None, "CAD"])
    regrounded = []

    def reground(failed_step, verification):
        assert verification.status == "failure"
        fresh = screen.buffer.latest_frame().frame_id
        regrounded.append(fresh)
        return step(fresh, target=Point(60, 30))

    outcome = ctl.run_step(step(screen.frame()), reground=reground)
    assert outcome.status == "succeeded"
    assert (outcome.first_attempt_success, outcome.recovery_attempted, outcome.recovery_success) == (False, True, True)
    assert len(executor.steps) == 2
    assert executor.steps[1].frame_id == regrounded[0]
    assert executor.steps[1].case_id == "case-1#2"
    report = evaluate([record for record in outcome.records])
    assert report["recovery"] == {"attempted": 1, "succeeded": 1, "failed": 0}
    assert report["first_attempt_verified_success"] == {"ok": 0, "total": 1, "rate": 0.0}


def test_a_second_failure_escalates_instead_of_looping(tmp_path):
    ctl, screen, executor, timeline, _ = controller(tmp_path, [None, None, "CAD"])
    outcome = ctl.run_step(step(screen.frame()), reground=lambda s, v: step(screen.buffer.latest_frame().frame_id))
    assert outcome.status == "escalated" and not outcome.should_continue
    assert (outcome.recovery_attempted, outcome.recovery_success) == (True, False)
    assert len(executor.steps) == 2
    assert kinds(timeline)[-1] == "visual.step.completed"
    assert "visual.recovery" in kinds(timeline)


def test_failure_without_an_alternate_escalates(tmp_path):
    ctl, screen, executor, _, _ = controller(tmp_path, [None])
    outcome = ctl.run_step(step(screen.frame()))
    assert outcome.status == "escalated"
    assert outcome.recovery_attempted is False
    assert len(executor.steps) == 1


def test_ambiguous_looks_again_a_bounded_number_of_times_then_escalates(tmp_path):
    ctl, screen, executor, timeline, _ = controller(tmp_path, ["???"], max_reverify=2)
    outcome = ctl.run_step(step(screen.frame()))
    assert outcome.status == "escalated"
    assert outcome.verification.status == "ambiguous"
    assert outcome.first_attempt_success is None
    verification = next(timeline.kinds("visual.verification"))
    assert verification.fields["looks"] == 3
    assert len(executor.steps) == 1


def test_a_step_with_no_expected_result_continues_unverified(tmp_path):
    ctl, screen, executor, _, _ = controller(tmp_path, ["CAD"])
    outcome = ctl.run_step(step(screen.frame(), expected_state=None))
    assert outcome.status == "unverified" and outcome.should_continue
    assert outcome.verification.status == "ambiguous"
    assert outcome.records[0].verified_success is None


def test_an_action_that_did_not_run_is_retried_once_not_judged(tmp_path):
    ctl, screen, executor, _, _ = controller(tmp_path, ["not-run", "CAD"])
    outcome = ctl.run_step(step(screen.frame()), reground=lambda s, v: step(screen.buffer.latest_frame().frame_id))
    assert outcome.status == "succeeded"
    first = outcome.records[0]
    assert first.execution.actuator_success is False
    assert first.verification_status == "ambiguous"
    assert "not carried out" in first.verification_reason


@pytest.mark.parametrize("value", [-1])
def test_limits_are_validated(tmp_path, value):
    with pytest.raises(ValueError):
        VerifiedStepController(
            executor=None,
            coordinator=None,
            screen_frames=LatestScreenFrameBuffer(),
            max_reverify=value,
        )
