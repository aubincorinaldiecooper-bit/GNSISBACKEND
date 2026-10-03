from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Callable

from ..screen import LatestScreenFrameBuffer, ScreenFrame
from .metering import UsageReport
from .perception import validate_target_point
from .runtime import (
    PanopticPolicy,
    PersistentPanopticSession,
    VisualDecisionProvider,
)
from .schema import Decision, bounded_actions


class VisualServiceError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class SessionTenant:
    workspace_id: str | None
    key_id: str | None
    grant_id: str | None
    project_id: str | None
    environment_id: str | None
    max_concurrent_sessions: int | None
    max_decisions_per_session: int | None
    max_frames_per_session: int | None


OPERATOR = SessionTenant(None, None, None, None, None, None, None, None)


@dataclass(frozen=True)
class SessionCredentials:
    session_id: str
    stream_token: str = field(repr=False)
    planner_token: str = field(repr=False)


@dataclass
class VisualServiceSession:
    """The 128-frame recent duplicate window is not the currentness authority;
    ``frame_seq`` is authoritative even after an ID leaves this window.
    """

    panoptic_session: PersistentPanopticSession
    session_id: str
    stream_token: str = field(repr=False)
    planner_token: str = field(repr=False)
    tenant: SessionTenant = OPERATOR
    lock: threading.RLock = field(default_factory=threading.RLock)
    inference_lock: threading.Lock = field(default_factory=threading.Lock)
    closed: bool = False
    reserved_inferences: int = 0
    last_captured_at_ms: int = -1
    frame_seq: int = 0
    recent_duplicate_frame_ids: deque[str] = field(
        default_factory=lambda: deque(maxlen=128)
    )
    replay: OrderedDict[str, dict[str, Any]] = field(default_factory=OrderedDict)
    expired_requests: OrderedDict[str, None] = field(default_factory=OrderedDict)
    perception_replay: OrderedDict[str, dict[str, Any]] = field(
        default_factory=OrderedDict
    )
    expired_perception_requests: OrderedDict[str, None] = field(
        default_factory=OrderedDict
    )
    outstanding: OrderedDict[str, tuple[Decision, int]] = field(
        default_factory=OrderedDict
    )
    created_monotonic: float = field(default_factory=time.monotonic)
    report_seq: int = 0
    reported_usage_by_day: dict[int, dict[str, int]] = field(default_factory=dict)
    usage_by_day: dict[int, dict[str, int]] = field(default_factory=dict)
    usage: dict[str, int] = field(
        default_factory=lambda: {
            "frames_accepted": 0,
            "frame_bytes": 0,
            "decisions": 0,
            "decisions_act": 0,
            "decisions_abstain": 0,
            "perceptions": 0,
            "attempts_recorded": 0,
            "inference_ms": 0,
        }
    )


class VisualService:
    """Agent-independent task state around the Smaller GNSIS visual policy.

    The service owns bounded task and replay state. It does not own a browser,
    desktop capture source, actuator, permission decision, or credential store.
    """

    def __init__(
        self,
        policy: PanopticPolicy,
        *,
        decision_provider: VisualDecisionProvider | None = None,
        cache_factory: Callable[[], Any] | None = None,
        max_sessions: int = 32,
        max_replay_entries: int = 128,
        max_outstanding_decisions: int = 32,
        max_expired_requests: int = 4096,
        max_pending_reports: int = 10_000,
    ) -> None:
        if max_sessions < 1:
            raise ValueError("max_sessions must be positive")
        if max_replay_entries < 1:
            raise ValueError("max_replay_entries must be positive")
        if max_outstanding_decisions < 1:
            raise ValueError("max_outstanding_decisions must be positive")
        if max_expired_requests < 1:
            raise ValueError("max_expired_requests must be positive")
        if max_pending_reports < 1:
            raise ValueError("max_pending_reports must be positive")
        self.policy = policy
        self.decision_provider = decision_provider
        self.cache_factory = cache_factory or (lambda: None)
        self.max_sessions = max_sessions
        self.max_replay_entries = max_replay_entries
        self.max_outstanding_decisions = max_outstanding_decisions
        self.max_expired_requests = max_expired_requests
        self.max_pending_reports = max_pending_reports
        self._lock = threading.RLock()
        self._sessions: dict[str, VisualServiceSession] = {}
        self._pending_reports: deque[UsageReport] = deque()
        self._pending_report_drops = 0

    def create_session(self, tenant: SessionTenant = OPERATOR) -> SessionCredentials:
        with self._lock:
            if len(self._sessions) >= self.max_sessions:
                raise VisualServiceError(
                    "capacity_exceeded",
                    "visual service session capacity is exhausted",
                    status_code=503,
                )
            if tenant.workspace_id is not None:
                live = sum(
                    1
                    for session in self._sessions.values()
                    if session.tenant.workspace_id == tenant.workspace_id
                )
                if (
                    tenant.max_concurrent_sessions is not None
                    and live >= tenant.max_concurrent_sessions
                ):
                    raise VisualServiceError(
                        "quota_exceeded",
                        "visual workspace session quota is exhausted",
                        status_code=429,
                    )
            session_id = secrets.token_urlsafe(24)
            stream_token = secrets.token_urlsafe(32)
            planner_token = secrets.token_urlsafe(32)
            frames = LatestScreenFrameBuffer(
                max_pending_frames=8,
                max_history_frames=64,
                history_window_ms=10_000,
            )
            self._sessions[session_id] = VisualServiceSession(
                panoptic_session=PersistentPanopticSession(
                    self.policy,
                    frames,
                    decision_provider=self.decision_provider,
                    cache=self.cache_factory(),
                ),
                session_id=session_id,
                tenant=tenant,
                stream_token=stream_token,
                planner_token=planner_token,
            )
        return SessionCredentials(session_id, stream_token, planner_token)

    def close_session(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            raise self._unknown_session()
        with session.inference_lock:
            with session.lock:
                session.closed = True
                self._enqueue_report(session, closed=True)
                session.panoptic_session.clear_task()
                session.panoptic_session.screen_frames.reset()
                session.replay.clear()
                session.expired_requests.clear()
                session.perception_replay.clear()
                session.expired_perception_requests.clear()
                session.outstanding.clear()

    def authorize_tenant(self, session_id: str, tenant: SessionTenant) -> None:
        session = self._session(session_id)
        if tenant.workspace_id is not None and (
            tenant.workspace_id != session.tenant.workspace_id
            or tenant.key_id != session.tenant.key_id
            or tenant.project_id != session.tenant.project_id
            or tenant.environment_id != session.tenant.environment_id
        ):
            raise self._unknown_session()

    def authenticate_stream(self, session_id: str, stream_token: str) -> bool:
        session = self._session(session_id)
        return secrets.compare_digest(session.stream_token, stream_token)

    def authenticate_planner(self, session_id: str, token: str) -> bool:
        with self._lock:
            session = self._sessions.get(session_id)
        return session is not None and secrets.compare_digest(
            session.planner_token, token
        )

    def is_planner_token(self, token: str) -> bool:
        with self._lock:
            planner_tokens = [
                session.planner_token for session in self._sessions.values()
            ]
        matched = False
        for planner_token in planner_tokens:
            matched = secrets.compare_digest(planner_token, token) or matched
        return matched

    def has_session(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._sessions

    def publish_frame(
        self,
        session_id: str,
        frame: ScreenFrame,
        *,
        frame_bytes: int = 0,
    ) -> dict[str, Any]:
        if type(frame_bytes) is not int or frame_bytes < 0:
            raise VisualServiceError(
                "invalid_frame",
                "frame_bytes must be a non-negative integer",
            )
        session = self._session(session_id)
        captured_at_ms = frame.captured_at_ms
        if captured_at_ms is None:
            raise VisualServiceError(
                "invalid_frame",
                "captured_at_ms is required for visual service frames",
            )
        with session.lock:
            self._require_open(session)
            max_frames = session.tenant.max_frames_per_session
            if (
                max_frames is not None
                and session.usage["frames_accepted"] >= max_frames
            ):
                raise VisualServiceError(
                    "quota_exceeded",
                    "visual session frame quota is exhausted",
                    status_code=429,
                )
            if frame.frame_id in session.recent_duplicate_frame_ids:
                raise VisualServiceError(
                    "replayed_frame",
                    "frame_id has already been accepted",
                    status_code=409,
                )
            if captured_at_ms <= session.last_captured_at_ms:
                raise VisualServiceError(
                    "stale_frame",
                    "frame capture time must increase monotonically",
                    status_code=409,
                )
            session.panoptic_session.screen_frames.publish(frame)
            consumed = session.panoptic_session.screen_frames.consume_for_unit()
            if not consumed or consumed[0].frame_id != frame.frame_id:
                raise VisualServiceError(
                    "frame_not_consumed",
                    "frame was not accepted as the current visual state",
                    status_code=409,
                )
            session.last_captured_at_ms = captured_at_ms
            session.recent_duplicate_frame_ids.append(frame.frame_id)
            session.frame_seq += 1
            self._record_usage(
                session,
                frames_accepted=1,
                frame_bytes=frame_bytes,
            )
            return {
                "frame_id": frame.frame_id,
                "frame_seq": session.frame_seq,
                "captured_at_ms": captured_at_ms,
                "width": frame.metadata.get("width"),
                "height": frame.metadata.get("height"),
            }

    def set_task(
        self,
        session_id: str,
        goal: str,
        *,
        allowed_actions: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        session = self._session(session_id)
        with session.lock:
            normalized_goal = str(goal).strip()
            try:
                normalized_actions = bounded_actions(allowed_actions)
            except ValueError as exc:
                raise VisualServiceError("invalid_task", str(exc)) from exc
            panoptic_session = session.panoptic_session
            if (
                panoptic_session.goal == normalized_goal
                and panoptic_session.allowed_actions == normalized_actions
            ):
                return self._state(session)
            try:
                panoptic_session.set_task(
                    goal,
                    allowed_actions=allowed_actions,
                )
            except ValueError as exc:
                raise VisualServiceError("invalid_task", str(exc)) from exc
            session.replay.clear()
            session.expired_requests.clear()
            session.outstanding.clear()
            return self._state(session)

    def reset_task(self, session_id: str) -> dict[str, Any]:
        session = self._session(session_id)
        with session.lock:
            session.panoptic_session.clear_task()
            session.replay.clear()
            session.expired_requests.clear()
            session.outstanding.clear()
            return self._state(session)

    def decide(self, session_id: str, request_id: str) -> dict[str, Any]:
        request_id = str(request_id).strip()
        if not request_id or len(request_id) > 256:
            raise VisualServiceError(
                "invalid_request_id",
                "request_id must contain 1 to 256 characters",
            )
        session = self._session(session_id)
        with session.inference_lock:
            with session.lock:
                self._require_open(session)
                replayed = session.replay.get(request_id)
                if replayed is not None:
                    session.replay.move_to_end(request_id)
                    return dict(replayed)
                if request_id in session.expired_requests:
                    raise VisualServiceError(
                        "request_expired",
                        "request_id is outside the idempotency window; use a new request_id",
                        status_code=409,
                    )
                if not session.panoptic_session.goal:
                    raise VisualServiceError(
                        "task_required",
                        "set a visual task before requesting a decision",
                        status_code=409,
                    )
                if session.panoptic_session.screen_frames.latest_frame() is None:
                    raise VisualServiceError(
                        "frame_required",
                        "the visual stream has not supplied a current frame",
                        status_code=409,
                    )
                seq = session.frame_seq
                snapshot = session.panoptic_session.decision_snapshot()
                self._reserve_inference(session)
            started = time.monotonic()
            try:
                gated = session.panoptic_session.decide_gated(snapshot)
            except Exception:
                with session.lock:
                    session.reserved_inferences -= 1
                raise
            inference_ms = max(0, int((time.monotonic() - started) * 1000))
            with session.lock:
                session.reserved_inferences -= 1
                self._record_usage(
                    session,
                    inference_ms=inference_ms,
                )
                if gated.status == "rejected":
                    raise VisualServiceError(
                        "illegal_decision",
                        gated.reason or "the policy returned an illegal decision",
                        status_code=422,
                    )
                decision = gated.decision
                if session.frame_seq != seq or not session.panoptic_session.is_current(
                    decision
                ):
                    raise VisualServiceError(
                        "stale_decision",
                        "the visual state changed while the decision was generated",
                        status_code=409,
                    )
                decision_id = secrets.token_urlsafe(18)
                response = {
                    "request_id": request_id,
                    "decision_id": decision_id,
                    "decision": decision.to_json(),
                    "gate": gated.to_json(),
                    "frame_seq": seq,
                    "current": True,
                }
                session.replay[request_id] = response
                while len(session.replay) > self.max_replay_entries:
                    expired_request_id, _ = session.replay.popitem(last=False)
                    session.expired_requests[expired_request_id] = None
                    while len(session.expired_requests) > self.max_expired_requests:
                        session.expired_requests.popitem(last=False)
                session.outstanding[decision_id] = (decision, seq)
                while len(session.outstanding) > self.max_outstanding_decisions:
                    session.outstanding.popitem(last=False)
                decision_usage = {"decisions": 1}
                if gated.status == "act":
                    decision_usage["decisions_act"] = 1
                elif gated.status == "abstain":
                    decision_usage["decisions_abstain"] = 1
                self._record_usage(session, **decision_usage)
                return dict(response)

    def perceive(
        self,
        session_id: str,
        request_id: str,
        focus: str | None = None,
        target: tuple[int, int] | None = None,
    ) -> dict[str, Any]:
        request_id = str(request_id).strip()
        if not request_id or len(request_id) > 256:
            raise VisualServiceError(
                "invalid_request_id",
                "request_id must contain 1 to 256 characters",
            )
        session = self._session(session_id)
        with session.inference_lock:
            with session.lock:
                self._require_open(session)
                replayed = session.perception_replay.get(request_id)
                if replayed is not None:
                    session.perception_replay.move_to_end(request_id)
                    return dict(replayed)
                if request_id in session.expired_perception_requests:
                    raise VisualServiceError(
                        "request_expired",
                        "request_id is outside the idempotency window; use a new request_id",
                        status_code=409,
                    )
                if session.panoptic_session.screen_frames.latest_frame() is None:
                    raise VisualServiceError(
                        "frame_required",
                        "the visual stream has not supplied a current frame",
                        status_code=409,
                    )
                seq = session.frame_seq
                snapshot = session.panoptic_session.perception_snapshot()
                if target is not None:
                    try:
                        target = validate_target_point(target, snapshot[2])
                    except ValueError as exc:
                        raise VisualServiceError("invalid_target", str(exc)) from exc
                self._reserve_inference(session)
            started = time.monotonic()
            try:
                perception = session.panoptic_session.perceive(
                    snapshot,
                    focus=focus,
                    target=target,
                )
            except ValueError as exc:
                with session.lock:
                    session.reserved_inferences -= 1
                raise VisualServiceError(
                    "invalid_perception",
                    str(exc),
                    status_code=502,
                ) from exc
            except Exception:
                with session.lock:
                    session.reserved_inferences -= 1
                raise
            inference_ms = max(0, int((time.monotonic() - started) * 1000))
            with session.lock:
                session.reserved_inferences -= 1
                latest = session.panoptic_session.latest_frame()
                current = (
                    session.frame_seq == seq and perception.frame_id == latest.frame_id
                )
                response = {
                    "request_id": request_id,
                    "perception": perception.to_json(),
                    "frame_seq": seq,
                    "current": current,
                }
                session.perception_replay[request_id] = response
                while len(session.perception_replay) > self.max_replay_entries:
                    expired_request_id, _ = session.perception_replay.popitem(last=False)
                    session.expired_perception_requests[expired_request_id] = None
                    while (
                        len(session.expired_perception_requests)
                        > self.max_expired_requests
                    ):
                        session.expired_perception_requests.popitem(last=False)
                self._record_usage(
                    session,
                    perceptions=1,
                    inference_ms=inference_ms,
                )
                return dict(response)

    def record_attempt(self, session_id: str, decision_id: str) -> dict[str, Any]:
        session = self._session(session_id)
        with session.lock:
            outstanding = session.outstanding.pop(decision_id, None)
            if outstanding is None:
                raise VisualServiceError(
                    "unknown_decision",
                    "decision is unknown, expired, or already recorded",
                    status_code=409,
                )
            decision, _seq = outstanding
            session.panoptic_session.record_attempt(decision)
            self._record_usage(session, attempts_recorded=1)
            return {
                "decision_id": decision_id,
                "recorded": True,
                "state": self._state(session),
            }

    def state(self, session_id: str) -> dict[str, Any]:
        session = self._session(session_id)
        with session.lock:
            return self._state(session)

    def health(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": "ok",
                "policy": self.policy.name,
                "active_sessions": len(self._sessions),
                "session_capacity": self.max_sessions,
                "pending_usage_reports": len(self._pending_reports),
                "usage_report_drops": self._pending_report_drops,
            }

    def collect_usage(self, *, include_closed: bool = True) -> list[UsageReport]:
        with self._lock:
            reports = list(self._pending_reports) if include_closed else []
            if include_closed:
                self._pending_reports.clear()
            sessions = list(self._sessions.values())
        for session in sessions:
            if session.tenant.workspace_id is None:
                continue
            with session.lock:
                for day_ms, usage in sorted(session.usage_by_day.items()):
                    reported = session.reported_usage_by_day.get(day_ms, {})
                    delta = {
                        name: usage[name] - reported.get(name, 0) for name in usage
                    }
                    if not any(delta.values()):
                        continue
                    reports.append(
                        self._report_for(
                            session,
                            delta,
                            closed=False,
                            generated_at_ms=day_ms,
                        )
                    )
                    session.reported_usage_by_day[day_ms] = dict(usage)
        return reports

    def _state(self, session: VisualServiceSession) -> dict[str, Any]:
        state = session.panoptic_session.state()
        state["frame_seq"] = session.frame_seq
        state["usage"] = dict(session.usage)
        return state

    def _enqueue_report(self, session: VisualServiceSession, *, closed: bool) -> None:
        if session.tenant.workspace_id is None:
            return
        pending: list[tuple[int, dict[str, int]]] = []
        for day_ms, usage in sorted(session.usage_by_day.items()):
            reported = session.reported_usage_by_day.get(day_ms, {})
            delta = {name: usage[name] - reported.get(name, 0) for name in usage}
            if any(delta.values()):
                pending.append((day_ms, delta))
        if not pending:
            pending.append((self._usage_day_ms(), self._empty_usage()))
        reports = [
            self._report_for(
                session,
                delta,
                closed=closed and index == len(pending) - 1,
                session_ms=int((time.monotonic() - session.created_monotonic) * 1000),
                generated_at_ms=day_ms,
            )
            for index, (day_ms, delta) in enumerate(pending)
        ]
        with self._lock:
            self._pending_reports.extend(reports)
            while len(self._pending_reports) > self.max_pending_reports:
                self._pending_reports.popleft()
                self._pending_report_drops += 1

    def _report_for(
        self,
        session: VisualServiceSession,
        delta: dict[str, int],
        *,
        closed: bool,
        session_ms: int = 0,
        generated_at_ms: int | None = None,
    ) -> UsageReport:
        session.report_seq += 1
        return UsageReport(
            workspace_id=session.tenant.workspace_id or "",
            virtual_key_id=session.tenant.key_id or "",
            project_id=session.tenant.project_id,
            environment_id=session.tenant.environment_id,
            grant_id=session.tenant.grant_id or "",
            session_id=session.session_id,
            report_seq=session.report_seq,
            frames_accepted=delta.get("frames_accepted", 0),
            frame_bytes=delta.get("frame_bytes", 0),
            decisions=delta.get("decisions", 0),
            decisions_act=delta.get("decisions_act", 0),
            decisions_abstain=delta.get("decisions_abstain", 0),
            perceptions=delta.get("perceptions", 0),
            attempts_recorded=delta.get("attempts_recorded", 0),
            inference_ms=delta.get("inference_ms", 0),
            session_ms=session_ms,
            closed=closed,
            generated_at_ms=generated_at_ms or int(time.time() * 1000),
        )

    @staticmethod
    def _empty_usage() -> dict[str, int]:
        return {
            "frames_accepted": 0,
            "frame_bytes": 0,
            "decisions": 0,
            "decisions_act": 0,
            "decisions_abstain": 0,
            "perceptions": 0,
            "attempts_recorded": 0,
            "inference_ms": 0,
        }

    @staticmethod
    def _usage_day_ms(now_ms: int | None = None) -> int:
        timestamp_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        return timestamp_ms - (timestamp_ms % 86_400_000)

    def _record_usage(
        self,
        session: VisualServiceSession,
        *,
        generated_at_ms: int | None = None,
        **deltas: int,
    ) -> None:
        day_ms = self._usage_day_ms(generated_at_ms)
        bucket = session.usage_by_day.setdefault(day_ms, self._empty_usage())
        for name, value in deltas.items():
            session.usage[name] += value
            bucket[name] += value

    def _session(self, session_id: str) -> VisualServiceSession:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise self._unknown_session()
        return session

    def _reserve_inference(self, session: VisualServiceSession) -> None:
        max_inferences = session.tenant.max_decisions_per_session
        inference_count = (
            session.usage["decisions"]
            + session.usage["perceptions"]
            + session.reserved_inferences
        )
        if max_inferences is not None and inference_count >= max_inferences:
            raise VisualServiceError(
                "quota_exceeded",
                "visual session inference quota is exhausted",
                status_code=429,
            )
        session.reserved_inferences += 1

    @classmethod
    def _require_open(cls, session: VisualServiceSession) -> None:
        if session.closed:
            raise cls._unknown_session()

    @staticmethod
    def _unknown_session() -> VisualServiceError:
        return VisualServiceError(
            "unknown_session",
            "visual service session does not exist",
            status_code=404,
        )
