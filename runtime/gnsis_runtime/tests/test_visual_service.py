from __future__ import annotations

import threading

from PIL import Image
import pytest

from gnsis_runtime.screen import ScreenFrame
from gnsis_runtime.visual.inspection import Region
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
        self.perception_focus = None
        self.perception_target = None
        self.perceptions = 0

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

    def perceive(self, frames, motion, viewport, focus=None, target=None):
        self.perceptions += 1
        self.perception_frames = tuple(frame.frame_id for frame in frames)
        self.perception_focus = focus
        self.perception_target = target
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


class BlockingPerceptionPolicy(FixedPolicy):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def perceive(self, frames, motion, viewport):
        self.started.set()
        if not self.release.wait(timeout=2):
            raise RuntimeError("test did not release perception")
        return super().perceive(frames, motion, viewport)


class BlockingDecisionPolicy(FixedPolicy):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

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
        self.started.set()
        if not self.release.wait(timeout=2):
            raise RuntimeError("test did not release decision")
        return super().decide(
            frame,
            goal,
            history,
            motion,
            viewport,
            cache,
            allowed_actions,
        )


class FailingPerceptionPolicy(FixedPolicy):
    def __init__(self) -> None:
        super().__init__()
        self.fail = True

    def perceive(self, frames, motion, viewport):
        if self.fail:
            self.fail = False
            raise RuntimeError("perception failed")
        return super().perceive(frames, motion, viewport)


def _service(policy=None, **kwargs):
    selected = policy if policy is not None else FixedPolicy()
    kwargs.setdefault("decision_provider", selected)
    return VisualService(selected, **kwargs)


def _frame(
    frame_id: str,
    captured_at_ms: int,
    color: str = "white",
    size: tuple[int, int] = (64, 32),
) -> ScreenFrame:
    return ScreenFrame(
        frame_id=frame_id,
        image=Image.new("RGB", size, color),
        captured_at_ms=captured_at_ms,
        metadata={"video_source": "screen", "width": size[0], "height": size[1]},
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


def test_service_passes_optional_perception_focus_without_setting_a_task() -> None:
    policy = FixedPolicy()
    service = _service(policy, decision_provider=policy)
    session_id = service.create_session().session_id
    service.publish_frame(session_id, _frame("f1", 1000))

    result = service.perceive(
        session_id,
        "perception-1",
        "What is inside the magenta marker?",
    )

    assert result["perception"]["frame_id"] == "f1"
    assert policy.perception_focus == "What is inside the magenta marker?"
    assert service.state(session_id)["goal"] is None


def test_service_grounds_a_target_point_only_inside_the_current_viewport() -> None:
    policy = FixedPolicy()
    service = _service(policy, decision_provider=policy)
    session_id = service.create_session().session_id
    service.publish_frame(session_id, _frame("f1", 1000))

    result = service.perceive(session_id, "perception-1", None, (10, 20))

    assert result["perception"]["frame_id"] == "f1"
    assert policy.perception_target == (10, 20)
    assert policy.perception_focus is None

    with pytest.raises(VisualServiceError) as excinfo:
        service.perceive(session_id, "perception-2", None, (64, 0))
    assert excinfo.value.code == "invalid_target"
    assert excinfo.value.status_code == 400
    assert service.state(session_id)["usage"]["perceptions"] == 1


def test_service_perception_requires_a_frame_but_not_a_task() -> None:
    policy = FixedPolicy()
    service = _service(policy, decision_provider=policy)
    session_id = service.create_session().session_id

    with pytest.raises(VisualServiceError) as missing:
        service.perceive(session_id, "perception-1")

    assert missing.value.code == "frame_required"


def test_service_accepts_frames_while_perception_is_running() -> None:
    policy = BlockingPerceptionPolicy()
    service = _service(policy)
    session_id = service.create_session().session_id
    service.publish_frame(session_id, _frame("f1", 1000))
    result = {}

    perception_thread = threading.Thread(
        target=lambda: result.update(service.perceive(session_id, "perception-1"))
    )
    perception_thread.start()
    assert policy.started.wait(timeout=1)

    published = threading.Event()

    def publish_next_frame() -> None:
        service.publish_frame(session_id, _frame("f2", 1250))
        published.set()

    publish_thread = threading.Thread(target=publish_next_frame)
    publish_thread.start()
    try:
        assert published.wait(timeout=1)
    finally:
        policy.release.set()
        publish_thread.join(timeout=1)
        perception_thread.join(timeout=1)

    assert not publish_thread.is_alive()
    assert not perception_thread.is_alive()
    assert result["perception"]["frame_id"] == "f1"
    assert result["frame_seq"] == 1
    assert result["current"] is False


def test_service_accepts_frames_while_decision_is_running() -> None:
    policy = BlockingDecisionPolicy()
    service = _service(policy)
    session_id = service.create_session().session_id
    service.set_task(session_id, "click the control")
    service.publish_frame(session_id, _frame("f1", 1000))
    errors = []

    def request_decision() -> None:
        try:
            service.decide(session_id, "decision-1")
        except VisualServiceError as exc:
            errors.append(exc)

    decision_thread = threading.Thread(target=request_decision)
    decision_thread.start()
    assert policy.started.wait(timeout=1)

    published = service.publish_frame(session_id, _frame("f2", 1250))
    policy.release.set()
    decision_thread.join(timeout=1)

    assert published["frame_id"] == "f2"
    assert not decision_thread.is_alive()
    assert len(errors) == 1
    assert errors[0].code == "stale_decision"


def test_close_waits_for_perception_and_reports_its_usage() -> None:
    policy = BlockingPerceptionPolicy()
    service = _service(policy)
    session_id = service.create_session(_tenant()).session_id
    service.publish_frame(session_id, _frame("f1", 1000))
    perception = {}

    perception_thread = threading.Thread(
        target=lambda: perception.update(service.perceive(session_id, "perception-1"))
    )
    perception_thread.start()
    assert policy.started.wait(timeout=1)

    closed = threading.Event()

    def close_session() -> None:
        service.close_session(session_id)
        closed.set()

    close_thread = threading.Thread(target=close_session)
    close_thread.start()
    assert not closed.wait(timeout=0.05)
    policy.release.set()
    perception_thread.join(timeout=1)
    close_thread.join(timeout=1)

    assert perception["perception"]["frame_id"] == "f1"
    assert closed.is_set()
    reports = service.collect_usage()
    assert len(reports) == 1
    assert reports[0].closed is True
    assert reports[0].perceptions == 1


def test_concurrent_perception_and_decision_share_inference_quota() -> None:
    policy = BlockingPerceptionPolicy()
    service = _service(policy)
    session_id = service.create_session(_tenant(max_decisions_per_session=1)).session_id
    service.set_task(session_id, "click the control")
    service.publish_frame(session_id, _frame("f1", 1000))
    perception = {}
    decision_errors = []

    perception_thread = threading.Thread(
        target=lambda: perception.update(service.perceive(session_id, "perception-1"))
    )
    perception_thread.start()
    assert policy.started.wait(timeout=1)

    def request_decision() -> None:
        try:
            service.decide(session_id, "decision-1")
        except VisualServiceError as exc:
            decision_errors.append(exc)

    decision_thread = threading.Thread(target=request_decision)
    decision_thread.start()
    policy.release.set()
    perception_thread.join(timeout=1)
    decision_thread.join(timeout=1)

    assert perception["perception"]["frame_id"] == "f1"
    assert len(decision_errors) == 1
    assert decision_errors[0].code == "quota_exceeded"
    usage = service.state(session_id)["usage"]
    assert usage["perceptions"] == 1
    assert usage["decisions"] == 0


def test_failed_inference_releases_its_quota_reservation() -> None:
    policy = FailingPerceptionPolicy()
    service = _service(policy)
    session_id = service.create_session(_tenant(max_decisions_per_session=1)).session_id
    service.publish_frame(session_id, _frame("f1", 1000))

    with pytest.raises(RuntimeError, match="perception failed"):
        service.perceive(session_id, "perception-1")

    response = service.perceive(session_id, "perception-2")
    assert response["perception"]["frame_id"] == "f1"
    assert service.state(session_id)["usage"]["perceptions"] == 1


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


def _freeze_monotonic(monkeypatch, start: float = 1000.0) -> list[float]:
    clock = [start]
    monkeypatch.setattr("gnsis_runtime.visual.service.time.monotonic", lambda: clock[0])
    return clock


def test_usage_reports_are_deltas_and_operator_sessions_are_not_reported(
    monkeypatch,
) -> None:
    _freeze_monotonic(monkeypatch)
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


def test_service_reuses_a_still_screen_perception_without_re_inferring() -> None:
    policy = FixedPolicy()
    service = _service(policy, decision_provider=policy)
    session_id = service.create_session().session_id
    service.publish_frame(session_id, _frame("f1", 1000))
    service.publish_frame(session_id, _frame("f2", 1250))

    first = service.perceive(session_id, "p1")
    inference_ms = service.state(session_id)["usage"]["inference_ms"]
    service.publish_frame(session_id, _frame("f3", 1500))
    reused = service.perceive(session_id, "p2")

    assert policy.perceptions == 1
    assert first["reused_from"] is None
    assert reused["reused_from"] == "p1"
    assert reused["current"] is True
    assert reused["frame_seq"] == 3
    assert reused["perception"]["frame_id"] == "f2"
    assert reused["perception"]["observed_frame_ids"] == ["f1", "f2"]
    assert service.perceive(session_id, "p2") == reused
    usage = service.state(session_id)["usage"]
    assert usage["perceptions"] == 2
    assert usage["inference_ms"] == inference_ms


def test_service_re_infers_when_the_screen_focus_target_or_viewport_changes() -> None:
    policy = FixedPolicy()
    service = _service(policy, decision_provider=policy)
    session_id = service.create_session().session_id
    service.publish_frame(session_id, _frame("f1", 1000))
    service.perceive(session_id, "p1")

    service.perceive(session_id, "focus", "What is highlighted?")
    service.perceive(session_id, "target", None, (10, 20))
    service.perceive(session_id, "both", "What is highlighted?", (10, 20))
    assert policy.perceptions == 4

    assert service.perceive(session_id, "p1-again")["reused_from"] == "p1"
    focused_again = service.perceive(session_id, "focus-again", "What is highlighted?")
    assert focused_again["reused_from"] == "focus"
    assert policy.perceptions == 4

    service.publish_frame(session_id, _frame("f2", 1250, "black"))
    changed = service.perceive(session_id, "p2")
    assert changed["reused_from"] is None
    assert changed["perception"]["frame_id"] == "f2"
    assert policy.perceptions == 5

    service.publish_frame(session_id, _frame("f3", 3000, "black", (128, 64)))
    assert service.perceive(session_id, "p3")["reused_from"] is None
    assert policy.perceptions == 6


def test_service_never_reuses_a_perception_of_a_changing_window() -> None:
    policy = FixedPolicy()
    service = _service(policy, decision_provider=policy)
    session_id = service.create_session().session_id
    service.publish_frame(session_id, _frame("f1", 1000, "white"))
    service.publish_frame(session_id, _frame("f2", 1250, "black"))
    service.perceive(session_id, "p1")

    service.publish_frame(session_id, _frame("f3", 2500, "black"))
    service.publish_frame(session_id, _frame("f4", 2750, "black"))
    service.perceive(session_id, "p2")
    assert policy.perceptions == 2
    assert policy.perception_frames == ("f3", "f4")

    service.publish_frame(session_id, _frame("f5", 3000, "black"))
    assert service.perceive(session_id, "p3")["reused_from"] == "p2"
    assert policy.perceptions == 2


def test_service_reuse_cache_is_bounded_and_cleared_on_close() -> None:
    policy = FixedPolicy()
    service = _service(policy, decision_provider=policy)
    session_id = service.create_session().session_id
    service.publish_frame(session_id, _frame("f1", 1000))
    for index in range(12):
        service.perceive(session_id, f"p{index}", f"question {index}")

    session = service._session(session_id)
    assert len(session.perception_reuse) == 8
    assert service.perceive(session_id, "old", "question 0")["reused_from"] is None
    assert service.perceive(session_id, "new", "question 11")["reused_from"] == "p11"

    service.close_session(session_id)
    assert session.perception_reuse == {}


def test_service_inspects_and_reads_pixels_of_retained_frames_only() -> None:
    policy = FixedPolicy()
    service = _service(policy, decision_provider=policy)
    session_id = service.create_session().session_id
    service.publish_frame(session_id, _frame("f1", 1000, "red"))
    service.publish_frame(session_id, _frame("f2", 1250, "blue"))
    service.perceive(session_id, "perception-1")

    history = service.history(session_id)
    assert [frame["frame_id"] for frame in history["frames"]] == ["f2", "f1"]
    assert history["frames"][0]["width"] == 64
    assert history["perceptions"][0]["request_id"] == "perception-1"
    assert history["perceptions"][0]["frame_id"] == "f2"

    view = service.inspect(session_id, "f1", Region(0, 0, 16, 8), 64)
    assert view["frame_id"] == "f1"
    assert view["region"] == [0, 0, 16, 8]
    assert (view["image"]["width"], view["image"]["height"]) == (64, 32)
    assert view["image"]["mime_type"] == "image/png"
    assert service.inspect(session_id)["frame_id"] == "f2"

    pixels = service.read_pixels(session_id, Region(0, 0, 4, 2), "f1", step=2)
    assert pixels["columns"] == [0, 2]
    assert pixels["rows"] == [{"y": 0, "colors": ["#ff0000", "#ff0000"]}]
    assert service.read_pixels(session_id, Region(0, 0, 1, 1))["rows"][0]["colors"] == [
        "#0000ff"
    ]
    assert service.state(session_id)["usage"]["perceptions"] == 1

    with pytest.raises(VisualServiceError) as expired:
        service.inspect(session_id, "f0")
    assert expired.value.code == "frame_expired"
    with pytest.raises(VisualServiceError) as outside:
        service.read_pixels(session_id, Region(60, 0, 8, 1))
    assert outside.value.code == "invalid_region"


def test_service_inspection_requires_a_streamed_frame() -> None:
    service = _service(FixedPolicy())
    session_id = service.create_session().session_id

    with pytest.raises(VisualServiceError) as missing:
        service.inspect(session_id)
    assert missing.value.code == "frame_required"


def test_open_sessions_report_session_time_every_cycle(monkeypatch) -> None:
    clock = _freeze_monotonic(monkeypatch)
    service = _service(FixedPolicy())
    session_id = service.create_session(_tenant()).session_id
    operator_id = service.create_session(OPERATOR).session_id

    clock[0] += 10.0
    first = service.collect_usage()
    assert [(r.session_id, r.session_ms, r.closed) for r in first] == [
        (session_id, 10_000, False)
    ]
    assert service.collect_usage() == []

    clock[0] += 4.5
    service.close_session(session_id)
    service.close_session(operator_id)
    closed = service.collect_usage()
    assert [(r.session_ms, r.closed) for r in closed] == [(4_500, True)]
    total = sum(r.session_ms for r in first + closed)
    assert total == 14_500


def test_light_tools_and_clients_are_metered(monkeypatch) -> None:
    _freeze_monotonic(monkeypatch)
    service = _service(FixedPolicy())
    credentials = service.create_session(_tenant(), host_client="panoptic-host/1.0")
    session_id = credentials.session_id
    assert service.authenticate_planner(
        session_id, credentials.planner_token, client="claude-code/2.1\n"
    )
    assert not service.authenticate_planner(session_id, "wrong", client="intruder")
    service.publish_frame(session_id, _frame("f1", 1000))
    service.history(session_id)
    service.inspect(session_id, region=Region(0, 0, 10, 10))
    service.read_pixels(session_id, Region(0, 0, 4, 4))

    (report,) = service.collect_usage()
    assert report.history_reads == 1
    assert report.inspections == 1
    assert report.pixel_reads == 1
    assert report.host_client == "panoptic-host/1.0"
    assert report.planner_client == "claude-code/2.1"


def test_session_time_reports_split_at_midnight(monkeypatch) -> None:
    clock = _freeze_monotonic(monkeypatch)
    end_ms = 1_799_971_201_000
    monkeypatch.setattr("gnsis_runtime.visual.service.time.time", lambda: end_ms / 1000)
    service = _service(FixedPolicy())
    session_id = service.create_session(_tenant()).session_id
    clock[0] += 3
    reports = service.collect_usage()
    assert [r.session_ms for r in reports] == [2000, 1000]
    assert sum(r.session_ms for r in reports) == 3000
    assert reports[1].generated_at_ms - reports[0].generated_at_ms == 86_400_000
    service.close_session(session_id)
    assert service.collect_usage()[0].session_ms == 0


def test_idle_sessions_close_and_send_their_final_usage(monkeypatch) -> None:
    clock = _freeze_monotonic(monkeypatch)
    service = _service(FixedPolicy())
    idle_id = service.create_session(_tenant()).session_id
    active_id = service.create_session(OPERATOR).session_id

    clock[0] += 100.0
    service.publish_frame(active_id, _frame("f1", 1000))
    clock[0] += 20.0
    assert service.close_idle_sessions() == [idle_id]

    reports = service.collect_usage()
    assert [(r.session_id, r.session_ms, r.closed) for r in reports] == [
        (idle_id, 120_000, True)
    ]
    with pytest.raises(VisualServiceError, match="does not exist"):
        service.state(idle_id)

    clock[0] += 119.0
    service.state(active_id)
    clock[0] += 119.0
    assert service.close_idle_sessions() == []
    clock[0] += 1.0
    assert service.close_idle_sessions() == [active_id]


def test_idle_close_skips_sessions_with_inference_in_flight(monkeypatch) -> None:
    clock = _freeze_monotonic(monkeypatch)
    service = _service(FixedPolicy())
    session_id = service.create_session(_tenant()).session_id
    service._sessions[session_id].reserved_inferences = 1

    clock[0] += 500.0
    assert service.close_idle_sessions() == []


def test_idle_timeout_can_be_disabled(monkeypatch) -> None:
    clock = _freeze_monotonic(monkeypatch)
    service = _service(FixedPolicy(), idle_timeout_s=None)
    service.create_session(_tenant())

    clock[0] += 10_000.0
    assert service.close_idle_sessions() == []
    with pytest.raises(ValueError, match="idle_timeout_s"):
        _service(FixedPolicy(), idle_timeout_s=0)
