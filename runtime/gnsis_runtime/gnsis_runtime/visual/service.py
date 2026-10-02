from __future__ import annotations

import secrets
import threading
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Callable

from ..screen import LatestScreenFrameBuffer, ScreenFrame
from .runtime import PersistentVisualDecisionSession, VisualDecisionPolicy
from .schema import Decision

MAX_REPLAY_ENTRIES = 128
MAX_OUTSTANDING_DECISIONS = 32


class VisualServiceError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass
class VisualServiceSession:
    decision_session: PersistentVisualDecisionSession
    stream_token: str
    lock: threading.RLock = field(default_factory=threading.RLock)
    last_captured_at_ms: int = -1
    recent_frame_ids: deque[str] = field(default_factory=lambda: deque(maxlen=128))
    replay: OrderedDict[str, dict[str, Any]] = field(default_factory=OrderedDict)
    outstanding: OrderedDict[str, Decision] = field(default_factory=OrderedDict)
    usage: dict[str, int] = field(
        default_factory=lambda: {
            "frames_accepted": 0,
            "decisions": 0,
            "attempts_recorded": 0,
        }
    )


class VisualService:
    """Agent-independent task state around the Smaller GNSIS visual policy.

    The service owns bounded task and replay state. It does not own a browser,
    desktop capture source, actuator, permission decision, or credential store.
    """

    def __init__(
        self,
        policy: VisualDecisionPolicy,
        *,
        cache_factory: Callable[[], Any] | None = None,
        max_sessions: int = 32,
    ) -> None:
        if max_sessions < 1:
            raise ValueError("max_sessions must be positive")
        self.policy = policy
        self.cache_factory = cache_factory or (lambda: None)
        self.max_sessions = max_sessions
        self._lock = threading.RLock()
        self._sessions: dict[str, VisualServiceSession] = {}

    def create_session(self) -> tuple[str, str]:
        with self._lock:
            if len(self._sessions) >= self.max_sessions:
                raise VisualServiceError(
                    "capacity_exceeded",
                    "visual service session capacity is exhausted",
                    status_code=503,
                )
            session_id = secrets.token_urlsafe(24)
            stream_token = secrets.token_urlsafe(32)
            frames = LatestScreenFrameBuffer(
                max_pending_frames=8,
                max_history_frames=64,
                history_window_ms=10_000,
            )
            self._sessions[session_id] = VisualServiceSession(
                decision_session=PersistentVisualDecisionSession(
                    self.policy,
                    frames,
                    cache=self.cache_factory(),
                ),
                stream_token=stream_token,
            )
        return session_id, stream_token

    def close_session(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            raise self._unknown_session()
        with session.lock:
            session.decision_session.clear_task()
            session.decision_session.screen_frames.reset()
            session.replay.clear()
            session.outstanding.clear()

    def authenticate_stream(self, session_id: str, stream_token: str) -> bool:
        session = self._session(session_id)
        return secrets.compare_digest(session.stream_token, stream_token)

    def publish_frame(self, session_id: str, frame: ScreenFrame) -> dict[str, Any]:
        session = self._session(session_id)
        captured_at_ms = frame.captured_at_ms
        if captured_at_ms is None:
            raise VisualServiceError(
                "invalid_frame",
                "captured_at_ms is required for visual service frames",
            )
        with session.lock:
            if frame.frame_id in session.recent_frame_ids:
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
            session.decision_session.screen_frames.publish(frame)
            consumed = session.decision_session.screen_frames.consume_for_unit()
            if not consumed or consumed[0].frame_id != frame.frame_id:
                raise VisualServiceError(
                    "frame_not_consumed",
                    "frame was not accepted as the current visual state",
                    status_code=409,
                )
            session.last_captured_at_ms = captured_at_ms
            session.recent_frame_ids.append(frame.frame_id)
            session.usage["frames_accepted"] += 1
            return {
                "frame_id": frame.frame_id,
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
            try:
                session.decision_session.set_task(
                    goal,
                    allowed_actions=allowed_actions,
                )
            except ValueError as exc:
                raise VisualServiceError("invalid_task", str(exc)) from exc
            session.replay.clear()
            session.outstanding.clear()
            return self._state(session)

    def reset_task(self, session_id: str) -> dict[str, Any]:
        session = self._session(session_id)
        with session.lock:
            session.decision_session.clear_task()
            session.replay.clear()
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
        with session.lock:
            replayed = session.replay.get(request_id)
            if replayed is not None:
                session.replay.move_to_end(request_id)
                return dict(replayed)
            if not session.decision_session.goal:
                raise VisualServiceError(
                    "task_required",
                    "set a visual task before requesting a decision",
                    status_code=409,
                )
            if session.decision_session.screen_frames.latest_frame() is None:
                raise VisualServiceError(
                    "frame_required",
                    "the visual stream has not supplied a current frame",
                    status_code=409,
                )
            decision = session.decision_session.decide()
            if not session.decision_session.is_current(decision):
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
                "current": True,
            }
            session.replay[request_id] = response
            while len(session.replay) > MAX_REPLAY_ENTRIES:
                session.replay.popitem(last=False)
            session.outstanding[decision_id] = decision
            while len(session.outstanding) > MAX_OUTSTANDING_DECISIONS:
                session.outstanding.popitem(last=False)
            session.usage["decisions"] += 1
            return dict(response)

    def record_attempt(self, session_id: str, decision_id: str) -> dict[str, Any]:
        session = self._session(session_id)
        with session.lock:
            decision = session.outstanding.pop(decision_id, None)
            if decision is None:
                raise VisualServiceError(
                    "unknown_decision",
                    "decision is unknown, expired, or already recorded",
                    status_code=409,
                )
            session.decision_session.record_attempt(decision)
            session.usage["attempts_recorded"] += 1
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
            }

    def _state(self, session: VisualServiceSession) -> dict[str, Any]:
        state = session.decision_session.state()
        state["usage"] = dict(session.usage)
        return state

    def _session(self, session_id: str) -> VisualServiceSession:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise self._unknown_session()
        return session

    @staticmethod
    def _unknown_session() -> VisualServiceError:
        return VisualServiceError(
            "unknown_session",
            "visual service session does not exist",
            status_code=404,
        )
