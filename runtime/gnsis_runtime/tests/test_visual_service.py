from __future__ import annotations

from PIL import Image
import pytest

from gnsis_runtime.screen import ScreenFrame
from gnsis_runtime.visual.schema import Decision, Target
from gnsis_runtime.visual.service import (
    OPERATOR,
    SessionTenant,
    VisualService,
    VisualServiceError,
)


class FixedPolicy:
    name = "fixed"

    def __init__(self, confidence: float = 0.9) -> None:
        self.calls = 0
        self.allowed_actions = None
        self.confidence = confidence

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
        self.allowed_actions = allowed_actions
        return Decision(
            "click",
            self.confidence,
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


def test_session_credentials_keep_tokens_out_of_repr() -> None:
    credentials = VisualService(FixedPolicy()).create_session()
    rendered = repr(credentials)

    assert credentials.session_id in rendered
    assert credentials.stream_token not in rendered
    assert credentials.planner_token not in rendered


def test_service_requires_task_and_frame_before_deciding() -> None:
    service = VisualService(FixedPolicy())
    session_id = service.create_session().session_id

    with pytest.raises(VisualServiceError, match="set a visual task"):
        service.decide(session_id, "request-1")

    service.set_task(session_id, "click the control")
    with pytest.raises(VisualServiceError, match="current frame"):
        service.decide(session_id, "request-1")


def test_service_replays_decision_requests_without_rerunning_policy() -> None:
    policy = FixedPolicy()
    service = VisualService(policy)
    session_id = service.create_session().session_id
    service.set_task(session_id, "click the control", allowed_actions=("click",))
    service.publish_frame(session_id, _frame("f1", 1000))

    first = service.decide(session_id, "request-1")
    second = service.decide(session_id, "request-1")

    assert second == first
    assert policy.calls == 1
    assert policy.allowed_actions == ("click",)
    assert first["decision"]["frame_id"] == "f1"


def test_service_rejects_stale_and_replayed_frames() -> None:
    service = VisualService(FixedPolicy())
    session_id = service.create_session().session_id
    service.publish_frame(session_id, _frame("f1", 1000))

    with pytest.raises(VisualServiceError, match="already been accepted"):
        service.publish_frame(session_id, _frame("f1", 1100))
    with pytest.raises(VisualServiceError, match="increase monotonically"):
        service.publish_frame(session_id, _frame("f2", 900))


def test_service_exposes_abstention_without_executing_the_proposal() -> None:
    service = VisualService(FixedPolicy(confidence=0.2))
    session_id = service.create_session().session_id
    service.set_task(session_id, "click the control")
    service.publish_frame(session_id, _frame("f1", 1000))

    result = service.decide(session_id, "request-1")

    assert result["decision"]["action"] == "wait"
    assert result["gate"]["status"] == "abstain"
    assert result["gate"]["proposed"]["action"] == "click"


def test_attempt_ids_are_single_use_and_history_is_recorded() -> None:
    service = VisualService(FixedPolicy())
    session_id = service.create_session().session_id
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
    session_id = service.create_session().session_id
    service.close_session(session_id)

    with pytest.raises(VisualServiceError, match="does not exist"):
        service.state(session_id)


def test_invalid_task_does_not_clear_the_existing_task_or_decision() -> None:
    service = VisualService(FixedPolicy())
    session_id = service.create_session().session_id
    service.set_task(session_id, "existing task", allowed_actions=("click",))
    service.publish_frame(session_id, _frame("f1", 1000))
    result = service.decide(session_id, "request-1")
    before = service.state(session_id)

    with pytest.raises(VisualServiceError, match="unknown actions"):
        service.set_task(session_id, "goal2", allowed_actions=("bogus",))

    after = service.state(session_id)
    assert after["goal"] == before["goal"] == "existing task"
    assert after["allowed_actions"] == before["allowed_actions"] == ["click"]
    assert after["history"] == before["history"] == []
    recorded = service.record_attempt(session_id, result["decision_id"])
    assert recorded["recorded"] is True
    assert recorded["state"]["history"] == [{"action": "click"}]


def test_recent_duplicate_window_allows_old_ids_with_new_frame_sequence() -> None:
    service = VisualService(FixedPolicy())
    session_id = service.create_session().session_id
    service.set_task(session_id, "click the control")

    first_frame = service.publish_frame(session_id, _frame("f1", 1000))
    old_decision = service.decide(session_id, "old-request")
    assert first_frame["frame_seq"] == old_decision["frame_seq"] == 1

    for sequence in range(2, 130):
        service.publish_frame(session_id, _frame(f"f{sequence}", 1000 + sequence))

    repeated = service.publish_frame(session_id, _frame("f1", 2000))
    state = service.state(session_id)

    assert repeated["frame_seq"] == 130
    assert state["frame_seq"] == 130
    assert state["frame_seq"] != old_decision["frame_seq"]


def test_replay_window_rejects_expired_request_ids() -> None:
    service = VisualService(FixedPolicy(), max_replay_entries=2)
    session_id = service.create_session().session_id
    service.set_task(session_id, "click the control")
    service.publish_frame(session_id, _frame("f1", 1000))

    first = service.decide(session_id, "request-1")
    service.decide(session_id, "request-2")
    service.decide(session_id, "request-3")

    with pytest.raises(VisualServiceError) as expired:
        service.decide(session_id, "request-1")
    assert expired.value.code == "request_expired"
    assert expired.value.status_code == 409
    assert str(expired.value) == (
        "request_id is outside the idempotency window; use a new request_id"
    )
    assert service.decide(session_id, "request-2")["decision_id"]
    assert first["decision_id"]


def _tenant(
    workspace_id: str = "workspace-1",
    *,
    max_concurrent_sessions: int = 4,
    max_decisions_per_session: int = 2000,
    max_frames_per_session: int = 100000,
) -> SessionTenant:
    return SessionTenant(
        workspace_id=workspace_id,
        key_id="key-1",
        grant_id="grant-1",
        project_id="project-1",
        environment_id="environment-1",
        max_concurrent_sessions=max_concurrent_sessions,
        max_decisions_per_session=max_decisions_per_session,
        max_frames_per_session=max_frames_per_session,
    )


def test_tenant_concurrency_and_per_session_quotas_are_enforced() -> None:
    service = VisualService(FixedPolicy())
    tenant = _tenant(
        max_concurrent_sessions=1,
        max_decisions_per_session=1,
        max_frames_per_session=1,
    )
    first = service.create_session(tenant).session_id

    with pytest.raises(VisualServiceError) as concurrent:
        service.create_session(tenant)
    assert concurrent.value.code == "quota_exceeded"
    assert concurrent.value.status_code == 429

    service.set_task(first, "click the control")
    service.publish_frame(first, _frame("f1", 1000))
    with pytest.raises(VisualServiceError) as frames:
        service.publish_frame(first, _frame("f2", 1100))
    assert frames.value.code == "quota_exceeded"
    response = service.decide(first, "request-1")
    assert service.decide(first, "request-1") == response
    with pytest.raises(VisualServiceError) as decisions:
        service.decide(first, "request-2")
    assert decisions.value.code == "quota_exceeded"

    other = service.create_session(_tenant("workspace-2")).session_id
    assert service.state(other)["frame_seq"] == 0


def test_usage_reports_are_deltas_and_operator_sessions_are_not_reported() -> None:
    service = VisualService(FixedPolicy())
    tenant = _tenant()
    session_id = service.create_session(tenant).session_id
    service.set_task(session_id, "click the control")
    service.publish_frame(session_id, _frame("f1", 1000))
    decision = service.decide(session_id, "request-1")
    service.record_attempt(session_id, decision["decision_id"])

    first = service.collect_usage()
    assert len(first) == 1
    assert first[0].event_id == f"{session_id}:1"
    assert first[0].frames_accepted == 1
    assert first[0].decisions == 1
    assert first[0].attempts_recorded == 1
    assert service.collect_usage() == []

    service.publish_frame(session_id, _frame("f2", 1100))
    second = service.collect_usage()
    assert len(second) == 1
    assert second[0].report_seq == 2
    assert second[0].event_id != first[0].event_id
    service.close_session(session_id)
    closed = service.collect_usage()
    assert len(closed) == 1
    assert closed[0].closed is True
    assert closed[0].session_ms >= 0

    operator_id = service.create_session(OPERATOR).session_id
    service.publish_frame(operator_id, _frame("operator", 2000))
    assert service.collect_usage() == []
