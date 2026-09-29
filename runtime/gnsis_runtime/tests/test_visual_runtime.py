from __future__ import annotations

from PIL import Image

from gnsis_runtime.screen import LatestScreenFrameBuffer, ScreenFrame
from gnsis_runtime.visual.runtime import (
    PersistentVisualDecisionSession,
    recent_motion,
)
from gnsis_runtime.visual.schema import Decision, Target


class RecordingPolicy:
    name = "recording"

    def __init__(self):
        self.calls = []

    def decide(self, frame, goal, history, motion, viewport, cache):
        self.calls.append(
            {
                "frame_id": frame.frame_id,
                "goal": goal,
                "history": history,
                "motion": motion,
                "viewport": viewport,
                "cache": cache,
            }
        )
        return Decision(
            "click",
            0.9,
            Target(10, 10),
            frame_id=frame.frame_id,
        )


def _frame(frame_id: str, ts: int, value: int = 0) -> ScreenFrame:
    return ScreenFrame(
        frame_id=frame_id,
        image=Image.new("RGB", (64, 32), (value, value, value)),
        captured_at_ms=ts,
        metadata={"video_source": "screen"},
    )


def _consume(buf: LatestScreenFrameBuffer, frame: ScreenFrame) -> None:
    buf.publish(frame)
    buf.consume_for_unit()


def test_session_reads_existing_consumed_frame_without_owning_capture():
    buf = LatestScreenFrameBuffer(max_history_frames=8)
    _consume(buf, _frame("f1", 1000))
    policy = RecordingPolicy()
    cache = object()
    session = PersistentVisualDecisionSession(policy, buf, cache=cache)
    session.set_task("click the visible control")

    decision = session.decide()

    assert decision.frame_id == "f1"
    assert policy.calls[0]["frame_id"] == "f1"
    assert policy.calls[0]["viewport"] == (64, 32)
    assert policy.calls[0]["cache"] is cache
    assert session.is_current(decision)


def test_session_uses_bounded_existing_visual_history_for_motion():
    buf = LatestScreenFrameBuffer(max_history_frames=8)
    _consume(buf, _frame("f1", 1000, 0))
    _consume(buf, _frame("f2", 1200, 255))
    policy = RecordingPolicy()
    session = PersistentVisualDecisionSession(policy, buf)
    session.set_task("click it")

    session.decide()

    assert policy.calls[0]["motion"] > 0.0


def test_motion_ignores_frames_outside_window():
    old = _frame("old", 0, 0)
    first = _frame("first", 2000, 100)
    second = _frame("second", 2200, 100)
    assert recent_motion((second, first, old)) == 0.0


def test_attempt_history_preserves_prototype_semantics_and_is_bounded():
    buf = LatestScreenFrameBuffer(max_history_frames=8)
    _consume(buf, _frame("f1", 1000))
    session = PersistentVisualDecisionSession(RecordingPolicy(), buf)
    session.set_task("do something")
    for i in range(40):
        session.record_attempt(
            Decision("scroll", 0.9, direction="down", frame_id=f"f{i}")
        )
    assert len(session.history) == 24
    assert len(session.state()["history"]) == 6


def test_done_is_not_added_to_action_history():
    buf = LatestScreenFrameBuffer(max_history_frames=8)
    _consume(buf, _frame("f1", 1000))
    session = PersistentVisualDecisionSession(RecordingPolicy(), buf)
    session.set_task("finish")
    session.record_attempt(Decision("done", 1.0, frame_id="f1"))
    assert session.history == []


def test_newer_frame_marks_old_decision_stale():
    buf = LatestScreenFrameBuffer(max_history_frames=8)
    _consume(buf, _frame("f1", 1000))
    session = PersistentVisualDecisionSession(RecordingPolicy(), buf)
    session.set_task("click it")
    decision = session.decide()
    _consume(buf, _frame("f2", 1200))
    assert not session.is_current(decision)


def test_task_reset_clears_structured_action_history_not_visual_history():
    buf = LatestScreenFrameBuffer(max_history_frames=8)
    _consume(buf, _frame("f1", 1000))
    session = PersistentVisualDecisionSession(RecordingPolicy(), buf)
    session.set_task("first")
    session.record_attempt(Decision("scroll", 0.9, direction="down", frame_id="f1"))
    session.set_task("second")
    assert session.history == []
    assert buf.latest_frame().frame_id == "f1"
