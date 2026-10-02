from __future__ import annotations

from PIL import Image
import pytest

from gnsis_runtime.screen import ScreenFrame
from gnsis_runtime.visual.schema import Decision, Target
from gnsis_runtime.visual.service import VisualService, VisualServiceError


class FixedPolicy:
    name = "fixed"

    def __init__(self) -> None:
        self.calls = 0

    def decide(
        self,
        frame,
        goal,
        history,
        motion,
        viewport,
        cache,
        allowed_actions=None,
    ):
        self.calls += 1
        return Decision(
            "click",
            0.9,
            Target(10, 10),
            frame_id=frame.frame_id,
        )


def _frame(frame_id: str, captured_at_ms: int) -> ScreenFrame:
    return ScreenFrame(
        frame_id=frame_id,
        image=Image.new("RGB", (64, 32), "white"),
        captured_at_ms=captured_at_ms,
        metadata={"video_source": "screen", "width": 64, "height": 32},
    )


def test_service_requires_task_and_frame_before_deciding() -> None:
    service = VisualService(FixedPolicy())
    session_id, _ = service.create_session()

    with pytest.raises(VisualServiceError, match="set a visual task"):
        service.decide(session_id, "request-1")

    service.set_task(session_id, "click the control")
    with pytest.raises(VisualServiceError, match="current frame"):
        service.decide(session_id, "request-1")


def test_service_replays_decision_requests_without_rerunning_policy() -> None:
    policy = FixedPolicy()
    service = VisualService(policy)
    session_id, _ = service.create_session()
    service.set_task(session_id, "click the control", allowed_actions=("click",))
    service.publish_frame(session_id, _frame("f1", 1000))

    first = service.decide(session_id, "request-1")
    second = service.decide(session_id, "request-1")

    assert second == first
    assert policy.calls == 1
    assert first["decision"]["frame_id"] == "f1"


def test_service_rejects_stale_and_replayed_frames() -> None:
    service = VisualService(FixedPolicy())
    session_id, _ = service.create_session()
    service.publish_frame(session_id, _frame("f1", 1000))

    with pytest.raises(VisualServiceError, match="already been accepted"):
        service.publish_frame(session_id, _frame("f1", 1100))
    with pytest.raises(VisualServiceError, match="increase monotonically"):
        service.publish_frame(session_id, _frame("f2", 900))


def test_attempt_ids_are_single_use_and_history_is_recorded() -> None:
    service = VisualService(FixedPolicy())
    session_id, _ = service.create_session()
    service.set_task(session_id, "click the control")
    service.publish_frame(session_id, _frame("f1", 1000))
    result = service.decide(session_id, "request-1")

    recorded = service.record_attempt(session_id, result["decision_id"])

    assert recorded["recorded"] is True
    assert recorded["state"]["history"] == [{"action": "click"}]
    with pytest.raises(VisualServiceError, match="already recorded"):
        service.record_attempt(session_id, result["decision_id"])


def test_unknown_and_closed_sessions_are_not_reusable() -> None:
    service = VisualService(FixedPolicy())
    session_id, _ = service.create_session()
    service.close_session(session_id)

    with pytest.raises(VisualServiceError, match="does not exist"):
        service.state(session_id)


def test_invalid_task_action_set_is_rejected() -> None:
    service = VisualService(FixedPolicy())
    session_id, _ = service.create_session()

    with pytest.raises(VisualServiceError, match="unknown actions"):
        service.set_task(session_id, "do it", allowed_actions=("shell",))
