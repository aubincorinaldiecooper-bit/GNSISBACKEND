from __future__ import annotations

from PIL import Image
import pytest

from gnsis_runtime.screen import ScreenFrame
from gnsis_runtime.visual.perception import PerceivedElement, VisualPerception
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
        self.perception_frames = ()

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

    def perceive(self, frames, motion, viewport):
        self.perception_frames = tuple(frame.frame_id for frame in frames)
        return VisualPerception(
            summary="A settings window is visible.",
            visible_text=("Settings", "Save"),
            elements=(
                PerceivedElement(
                    "Save",
                    "button",
                    "Save",
                    (8, 8, 20, 12),
                    "enabled",
                    0.95,
                ),
            ),
            changes=("The Save button appeared.",) if len(frames) > 1 else (),
            confidence=0.9,
            frame_id=str(frames[-1].frame_id),
            observed_frame_ids=tuple(str(frame.frame_id) for frame in frames),
            motion=motion,
            viewport=viewport,
        )


class DecisionOnlyProvider:
    name = "decision-only"

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
        return Decision(
            "click",
            0.9,
            Target(10, 10),
            frame_id=frame.frame_id,
        )


def _service(policy=None, **kwargs):
    selected = policy if policy is not None else FixedPolicy()
    kwargs.setdefault("decision_provider", selected)
    return VisualService(selected, **kwargs)


def _frame(frame_id: str, captured_at_ms: int) -> ScreenFrame:
    return ScreenFrame(
        frame_id=frame_id,
        image=Image.new("RGB", (64, 32), "white"),
        captured_at_ms=captured_at_ms,
        metadata={"video_source": "screen", "width": 64, "height": 32},
    )


def test_session_credentials_keep_tokens_out_of_repr() -> None:
    credentials = _service(FixedPolicy()).create_session()
    rendered = repr(credentials)

    assert credentials.session_id in rendered
    assert credentials.stream_token not in rendered
    assert credentials.planner_token not in rendered


def test_service_requires_task_and_frame_before_deciding() -> None:
    service = _service(FixedPolicy())
    session_id = service.create_session().session_id

    with pytest.raises(VisualServiceError, match="set a visual task"):
        service.decide(session_id, "request-1")

    service.set_task(session_id, "click the control")
    with pytest.raises(VisualServiceError, match="current frame"):
        service.decide(session_id, "request-1")


def test_service_replays_decision_requests_without_rerunning_policy() -> None:
    policy = FixedPolicy()
    service = _service(policy)
    session_id = service.create_session().session_id
    service.set_task(session_id, "click the control", allowed_actions=("click",))
    service.publish_frame(session_id, _frame("f1", 1000))

    first = service.decide(session_id, "request-1")
    second = service.decide(session_id, "request-1")

    assert second == first
    assert policy.calls == 1
    assert policy.allowed_actions == ("click",)
    assert first["decision"]["frame_id"] == "f1"


def test_service_perception_is_task_independent_temporal_and_idempotent() -> None:
    policy = FixedPolicy()
    service = _service(policy, decision_provider=policy)
    session_id = service.create_session().session_id
    service.publish_frame(session_id, _frame("f0", 750))
    service.publish_frame(session_id, _frame("f1", 1000))
    service.publish_frame(session_id, _frame("f2", 1250))
    service.publish_frame(session_id, _frame("f3", 1500))
    service.publish_frame(session_id, _frame("f4", 1750))

    first = service.perceive(session_id, "perception-1")
    replayed = service.perceive(session_id, "perception-1")

    assert replayed == first
    assert first["perception"]["summary"] == "A settings window is visible."
    assert first["perception"]["frame_id"] == "f4"
    assert first["perception"]["observed_frame_ids"] == ["f1", "f2", "f3", "f4"]
    assert first["perception"]["elements"][0]["role"] == "button"
    assert policy.perception_frames == ("f1", "f2", "f3", "f4")
    assert service.state(session_id)["goal"] is None
    assert service.state(session_id)["usage"]["perceptions"] == 1


def test_service_perception_requires_a_frame_but_not_a_task() -> None:
    policy = FixedPolicy()
    service = _service(policy, decision_provider=policy)
    session_id = service.create_session().session_id

    with pytest.raises(VisualServiceError) as missing:
        service.perceive(session_id, "perception-1")

    assert missing.value.code == "frame_required"


def test_service_accepts_a_separate_decision_only_provider() -> None:
    service = _service(FixedPolicy(), decision_provider=DecisionOnlyProvider())
    session_id = service.create_session().session_id
    service.set_task(session_id, "click the control")
    service.publish_frame(session_id, _frame("f1", 1000))

    assert (
        service.perceive(session_id, "perception-1")["perception"]["frame_id"] == "f1"
    )
    assert service.decide(session_id, "decision-1")["decision"]["frame_id"] == "f1"


def test_service_rejects_stale_and_replayed_frames() -> None:
    service = _service(FixedPolicy())
    session_id = service.create_session().session_id
    service.publish_frame(session_id, _frame("f1", 1000))

    with pytest.raises(VisualServiceError, match="already been accepted"):
        service.publish_frame(session_id, _frame("f1", 1100))
    with pytest.raises(VisualServiceError, match="increase monotonically"):
        service.publish_frame(session_id, _frame("f2", 900))


def test_frame_byte_usage_is_separate_from_frame_metadata() -> None:
    service = _service(FixedPolicy())
    session_id = service.create_session().session_id
    frame = _frame("f1", 1000)

    service.publish_frame(session_id, frame, frame_bytes=23)

    assert service.state(session_id)["usage"]["frame_bytes"] == 23
    assert "frame_bytes" not in frame.metadata
    with pytest.raises(VisualServiceError) as invalid:
        service.publish_frame(session_id, _frame("f2", 1100), frame_bytes=-1)
    assert invalid.value.code == "invalid_frame"
    assert service.state(session_id)["usage"]["frames_accepted"] == 1


def test_service_exposes_abstention_without_executing_the_proposal() -> None:
    service = _service(FixedPolicy(confidence=0.2))
    session_id = service.create_session().session_id
    service.set_task(session_id, "click the control")
    service.publish_frame(session_id, _frame("f1", 1000))

    result = service.decide(session_id, "request-1")

    assert result["decision"]["action"] == "wait"
    assert result["gate"]["status"] == "abstain"
    assert result["gate"]["proposed"]["action"] == "click"


def test_attempt_ids_are_single_use_and_history_is_recorded() -> None:
    service = _service(FixedPolicy())
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
    service = _service(FixedPolicy())
    session_id = service.create_session().session_id
    service.close_session(session_id)

    with pytest.raises(VisualServiceError, match="does not exist"):
        service.state(session_id)


def test_invalid_task_does_not_clear_the_existing_task_or_decision() -> None:
    service = _service(FixedPolicy())
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
    service = _service(FixedPolicy())
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
    service = _service(FixedPolicy(), max_replay_entries=2)
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
    service = _service(FixedPolicy())
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
    policy = FixedPolicy()
    service = _service(policy, decision_provider=policy)
    tenant = _tenant()
    session_id = service.create_session(tenant).session_id
    service.publish_frame(session_id, _frame("f1", 1000))
    service.perceive(session_id, "perception-1")
    service.set_task(session_id, "click the control")
    decision = service.decide(session_id, "request-1")
    service.record_attempt(session_id, decision["decision_id"])

    first = service.collect_usage()
    assert len(first) == 1
    assert first[0].event_id == f"{session_id}:1"
    assert first[0].frames_accepted == 1
    assert first[0].decisions == 1
    assert first[0].perceptions == 1
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


def test_usage_reports_split_decisions_at_utc_day_boundaries(monkeypatch) -> None:
    service = _service(FixedPolicy())
    session_id = service.create_session(_tenant()).session_id
    service.set_task(session_id, "click the control")
    service.publish_frame(session_id, _frame("f1", 1000))

    before_midnight = 1_799_971_199.0
    after_midnight = before_midnight + 2
    monkeypatch.setattr(
        "gnsis_runtime.visual.service.time.time",
        lambda: before_midnight,
    )
    service.decide(session_id, "request-1")
    monkeypatch.setattr(
        "gnsis_runtime.visual.service.time.time",
        lambda: after_midnight,
    )
    service.publish_frame(session_id, _frame("f2", 1100))
    service.decide(session_id, "request-2")

    reports = service.collect_usage()
    decision_reports = [report for report in reports if report.decisions]

    assert len(decision_reports) == 2
    assert [report.decisions for report in decision_reports] == [1, 1]
    assert (
        len({report.generated_at_ms // 86_400_000 for report in decision_reports}) == 2
    )


def test_set_task_is_idempotent_and_preserves_replay() -> None:
    service = _service(FixedPolicy())
    session_id = service.create_session().session_id
    service.set_task(session_id, "click the control", allowed_actions=("click",))
    service.publish_frame(session_id, _frame("f1", 1000))
    first = service.decide(session_id, "request-1")

    service.set_task(session_id, "click the control", allowed_actions=("click",))
    replayed = service.decide(session_id, "request-1")
    assert replayed["decision_id"] == first["decision_id"]

    service.set_task(session_id, "different goal")
    replacement = service.decide(session_id, "request-1")
    assert replacement["decision_id"] != first["decision_id"]
