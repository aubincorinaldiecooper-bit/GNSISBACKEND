"""Bind System-1 visual decisions to GNSIS's existing persistent screen history.

This module deliberately owns no capture mechanism and no actuator. It consumes
`ScreenFrame` objects already accepted by the runtime and produces validated
structured decisions for an environment adapter to execute.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from PIL import Image, ImageChops, ImageStat

from ..screen import LatestScreenFrameBuffer, ScreenFrame
from .legal import IllegalDecision, LegalActionSet, legal_actions
from .perception import VisualPerception
from .schema import ACTIONS, Decision, DecisionError, bounded_actions

MAX_HISTORY = 6
MOTION_WINDOW_MS = 800
PERCEPTION_WINDOW_MS = 1_000
MAX_PERCEPTION_FRAMES = 4
_SIGNATURE_SIZE = (48, 30)
DEFAULT_MIN_CONFIDENCE = 0.5
DEFAULT_UNSETTLED_MOTION = 0.35
DEFAULT_UNCHANGED_SCREEN = 0.01
DEFAULT_REPEAT_RADIUS_PX = 24.0

GateStatus = Literal["act", "abstain", "rejected"]


class VisualDecisionProvider(Protocol):
    name: str

    def decide(
        self,
        frame: Any,
        goal: str,
        history: list[dict],
        motion: float,
        viewport: tuple[int, int],
        cache: Any,
        allowed_actions: tuple[str, ...] | None = None,
    ) -> Decision: ...


class PanopticPolicy(Protocol):
    name: str

    def perceive(
        self,
        frames: tuple[Any, ...],
        motion: float,
        viewport: tuple[int, int],
    ) -> VisualPerception: ...


@dataclass
class RuntimeFrameView:
    """Model-facing view over a frame GNSIS already captured.

    The image is reused directly. The tiny signature is derived from that image
    only for model-cache reuse; no screen reacquisition occurs.
    """

    frame_id: str
    _image: Image.Image
    _signature: Any | None = None

    @classmethod
    def from_screen_frame(cls, frame: ScreenFrame) -> "RuntimeFrameView":
        image = frame.image
        if not isinstance(image, Image.Image):
            raise TypeError("visual decision frame must contain a PIL image")
        return cls(frame_id=frame.frame_id, _image=image)

    def image(self) -> Image.Image:
        return self._image

    @property
    def signature(self) -> Any:
        if self._signature is None:
            import numpy as np

            self._signature = (
                np.asarray(
                    self._image.convert("L").resize(_SIGNATURE_SIZE, Image.BILINEAR),
                    dtype=np.float32,
                )
                / 255.0
            )
        return self._signature


def _thumbnail(frame: ScreenFrame) -> Image.Image:
    image = frame.image
    if not isinstance(image, Image.Image):
        raise TypeError("visual decision frame must contain a PIL image")
    return image.convert("L").resize(_SIGNATURE_SIZE, Image.BILINEAR)


def recent_motion(
    frames: tuple[ScreenFrame, ...],
    *,
    window_ms: int = MOTION_WINDOW_MS,
) -> float:
    """Match the browser prototype's bounded visual-change signal using retained frames."""

    if len(frames) < 2:
        return 0.0
    ordered = sorted(
        (frame for frame in frames if frame.captured_at_ms is not None),
        key=lambda frame: (
            int(frame.captured_at_ms) if frame.captured_at_ms is not None else -1
        ),
    )
    if len(ordered) < 2:
        return 0.0
    newest_value = ordered[-1].captured_at_ms
    assert newest_value is not None
    newest = int(newest_value)
    ordered = [
        frame
        for frame in ordered
        if frame.captured_at_ms is not None
        and newest - int(frame.captured_at_ms) <= window_ms
    ]
    if len(ordered) < 2:
        return 0.0
    thumbs = [_thumbnail(frame) for frame in ordered]
    diffs: list[float] = []
    for before, after in zip(thumbs, thumbs[1:]):
        stats = ImageStat.Stat(ImageChops.difference(before, after))
        diffs.append(float(stats.mean[0]) / 255.0)
    return min(1.0, sum(diffs) * 10.0)


def screen_change(before: Image.Image, frame: ScreenFrame) -> float:
    """Mean visible change between a retained thumbnail and a consumed frame."""

    stats = ImageStat.Stat(ImageChops.difference(before, _thumbnail(frame)))
    return float(stats.mean[0]) / 255.0


@dataclass(frozen=True, slots=True)
class DecisionGate:
    """When a legal System-1 proposal is executed rather than turned into WAIT.

    * below ``min_confidence`` the policy is not sure enough to act;
    * at or above ``unsettled_motion`` the screen is still changing, so a target
      read from it may not be where it will be when the actuator gets there;
    * repeating the last attempted action on a screen that has not visibly
      changed since (below ``unchanged_screen``, same arguments, target within
      ``repeat_radius_px``) would loop on an action that did nothing.
    """

    min_confidence: float = DEFAULT_MIN_CONFIDENCE
    unsettled_motion: float = DEFAULT_UNSETTLED_MOTION
    unchanged_screen: float = DEFAULT_UNCHANGED_SCREEN
    repeat_radius_px: float = DEFAULT_REPEAT_RADIUS_PX

    def __post_init__(self) -> None:
        for name in ("min_confidence", "unsettled_motion", "unchanged_screen"):
            value = float(getattr(self, name))
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be within [0,1]")
        if self.repeat_radius_px < 0:
            raise ValueError("repeat_radius_px must not be negative")

    def to_json(self) -> dict[str, float]:
        return {
            "min_confidence": self.min_confidence,
            "unsettled_motion": self.unsettled_motion,
            "unchanged_screen": self.unchanged_screen,
            "repeat_radius_px": self.repeat_radius_px,
        }


@dataclass(frozen=True, slots=True)
class GatedDecision:
    """A policy proposal after the legal-set check and the abstention gate.

    ``decision`` is the only thing an actuator may execute: the proposal when
    ``status`` is ``act``, otherwise a WAIT bound to the observed frame.
    """

    status: GateStatus
    decision: Decision
    proposed: Decision | None
    frame_id: str
    reason: str | None = None
    choice_key: str | None = None
    motion: float = 0.0
    change_since_last_action: float | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "decision": self.decision.to_json(),
            "proposed": self.proposed.to_json() if self.proposed is not None else None,
            "frame_id": self.frame_id,
            "reason": self.reason,
            "choice_key": self.choice_key,
            "motion": self.motion,
            "change_since_last_action": self.change_since_last_action,
        }


@dataclass(frozen=True, slots=True)
class DecisionSnapshot:
    view: RuntimeFrameView
    goal: str
    history: tuple[dict, ...]
    motion: float
    viewport: tuple[int, int]
    allowed_actions: tuple[str, ...]
    legal: LegalActionSet
    last_attempt: Decision | None
    change_since_last_action: float | None


def _repeats(proposed: Decision, previous: Decision, radius_px: float) -> bool:
    if proposed.action != previous.action:
        return False
    if (proposed.text, proposed.url, proposed.direction) != (
        previous.text,
        previous.url,
        previous.direction,
    ):
        return False
    if proposed.target is None or previous.target is None:
        return proposed.target is None and previous.target is None
    dx = proposed.target.x - previous.target.x
    dy = proposed.target.y - previous.target.y
    return (dx * dx + dy * dy) ** 0.5 <= radius_px


def gate_decision(
    proposed: Any,
    legal: LegalActionSet,
    *,
    motion: float,
    gate: DecisionGate,
    last_attempt: Decision | None = None,
    change_since_last_action: float | None = None,
) -> GatedDecision:
    """Apply the legal-set check and abstention rules to one policy proposal."""

    def hold(
        status: GateStatus, reason: str, confidence: float, key: str | None = None
    ) -> GatedDecision:
        return GatedDecision(
            status=status,
            decision=Decision("wait", confidence, frame_id=legal.frame_id),
            proposed=proposed if isinstance(proposed, Decision) else None,
            frame_id=legal.frame_id,
            reason=reason,
            choice_key=key,
            motion=motion,
            change_since_last_action=change_since_last_action,
        )

    if isinstance(proposed, DecisionError):
        return hold("rejected", str(proposed), 0.0)
    if not isinstance(proposed, Decision):
        return hold(
            "rejected",
            f"policy returned {type(proposed).__name__}, not a Decision",
            0.0,
        )
    try:
        choice = legal.match(proposed)
    except DecisionError as exc:
        return hold("rejected", str(exc), 0.0)
    confidence = float(proposed.confidence)
    if proposed.action != "wait":
        if confidence < gate.min_confidence:
            return hold(
                "abstain",
                f"confidence {confidence:.2f} is below {gate.min_confidence:.2f}",
                confidence,
                choice.key,
            )
        if proposed.target is not None and motion >= gate.unsettled_motion:
            return hold(
                "abstain",
                f"screen is still changing (motion {motion:.2f})",
                confidence,
                choice.key,
            )
        if (
            last_attempt is not None
            and change_since_last_action is not None
            and change_since_last_action < gate.unchanged_screen
            and _repeats(proposed, last_attempt, gate.repeat_radius_px)
        ):
            return hold(
                "abstain",
                "repeats the last action, which produced no visible change",
                confidence,
                choice.key,
            )
    return GatedDecision(
        status="act",
        decision=proposed,
        proposed=proposed,
        frame_id=legal.frame_id,
        choice_key=choice.key,
        motion=motion,
        change_since_last_action=change_since_last_action,
    )


class PersistentPanopticSession:
    """Rolling visual understanding over the shared GNSIS timeline.

    The session owns bounded temporal perception state. An optional decision
    provider may consume that same state without defining the Panoptic policy.
    """

    def __init__(
        self,
        policy: PanopticPolicy,
        screen_frames: LatestScreenFrameBuffer,
        *,
        decision_provider: VisualDecisionProvider | None = None,
        cache: Any = None,
        gate: DecisionGate | None = None,
    ) -> None:
        self.policy = policy
        self.decision_provider = decision_provider
        self.screen_frames = screen_frames
        self.cache = cache
        self.gate = gate if gate is not None else DecisionGate()
        self.goal: str | None = None
        self.history: list[dict] = []
        self.allowed_actions = bounded_actions()
        self._last_attempt: tuple[Decision, Image.Image] | None = None

    def set_task(
        self,
        goal: str,
        *,
        allowed_actions: tuple[str, ...] | None = None,
    ) -> None:
        normalized_goal = str(goal).strip()
        if not normalized_goal:
            raise ValueError("visual task goal must not be empty")
        normalized_actions = bounded_actions(allowed_actions)
        self.goal = normalized_goal
        self.history = []
        self.allowed_actions = normalized_actions
        self._last_attempt = None

    def clear_task(self) -> None:
        self.goal = None
        self.history = []
        self.allowed_actions = bounded_actions(ACTIONS)
        self._last_attempt = None

    def latest_frame(self) -> ScreenFrame:
        frame = self.screen_frames.latest_frame()
        if frame is None:
            raise RuntimeError("no consumed visual frame is available")
        return frame

    def change_since_last_action(
        self, frame: ScreenFrame | None = None
    ) -> float | None:
        frame = frame if frame is not None else self.screen_frames.latest_frame()
        if self._last_attempt is None or frame is None:
            return None
        return screen_change(self._last_attempt[1], frame)

    def decision_snapshot(self) -> DecisionSnapshot:
        """One System-1 decision over the current consumed frame, gated.

        The legal set is generated from the goal and that frame before the
        policy runs, and the same frame's retained neighbours supply motion and
        change since the last attempted action.
        """

        if self.decision_provider is None:
            raise RuntimeError("visual decisions are not configured")
        if not self.goal:
            raise ValueError("no visual task is set")
        source = self.latest_frame()
        view = RuntimeFrameView.from_screen_frame(source)
        viewport = view.image().size
        legal = legal_actions(
            self.goal,
            source.frame_id,
            viewport,
            self.allowed_actions,
        )
        recent = self.screen_frames.recent_frames(within_ms=MOTION_WINDOW_MS)
        motion = recent_motion(recent)
        return DecisionSnapshot(
            view=view,
            goal=self.goal,
            history=tuple(dict(item) for item in self.history),
            motion=motion,
            viewport=viewport,
            allowed_actions=self.allowed_actions,
            legal=legal,
            last_attempt=self._last_attempt[0]
            if self._last_attempt is not None
            else None,
            change_since_last_action=self.change_since_last_action(source),
        )

    def decide_gated(
        self,
        snapshot: DecisionSnapshot | None = None,
    ) -> GatedDecision:
        selected = snapshot or self.decision_snapshot()
        if self.decision_provider is None:
            raise RuntimeError("visual decisions are not configured")
        try:
            proposed: Any = self.decision_provider.decide(
                selected.view,
                selected.goal,
                list(selected.history),
                selected.motion,
                selected.viewport,
                self.cache,
                selected.allowed_actions,
            )
        except DecisionError as exc:
            proposed = exc
        return gate_decision(
            proposed,
            selected.legal,
            motion=selected.motion,
            gate=self.gate,
            last_attempt=selected.last_attempt,
            change_since_last_action=selected.change_since_last_action,
        )

    def decide(self) -> Decision:
        """The executable decision: the proposal, or WAIT when the gate abstains.

        A proposal outside the legal set raises instead of becoming WAIT, so an
        invented target, value, URL or command is never silently absorbed.
        """

        gated = self.decide_gated()
        if gated.status == "rejected":
            raise IllegalDecision(gated.reason or "illegal decision")
        return gated.decision

    def perception_snapshot(
        self,
    ) -> tuple[tuple[RuntimeFrameView, ...], float, tuple[int, int]]:
        source = self.latest_frame()
        recent = self.screen_frames.recent_frames(
            limit=MAX_PERCEPTION_FRAMES,
            within_ms=PERCEPTION_WINDOW_MS,
        )
        selected = tuple(reversed(recent)) if recent else (source,)
        views = tuple(RuntimeFrameView.from_screen_frame(frame) for frame in selected)
        return views, recent_motion(recent), views[-1].image().size

    def perceive(
        self,
        snapshot: tuple[tuple[RuntimeFrameView, ...], float, tuple[int, int]]
        | None = None,
    ) -> VisualPerception:
        views, motion, viewport = snapshot or self.perception_snapshot()
        return self.policy.perceive(
            views,
            motion,
            viewport,
        )

    def _attempt_frame(self, decision: Decision) -> ScreenFrame | None:
        if decision.frame_id is not None:
            for frame in self.screen_frames.recent_frames():
                if frame.frame_id == str(decision.frame_id):
                    return frame
        return self.screen_frames.latest_frame()

    def record_attempt(self, decision: Decision) -> None:
        """Record one attempted action exactly as the prototype's session did."""

        if decision.action == "done":
            return
        frame = self._attempt_frame(decision)
        if frame is not None:
            self._last_attempt = (decision, _thumbnail(frame))
        self.history.append(
            {
                key: value
                for key, value in decision.to_json().items()
                if key in ("action", "text", "url", "direction")
            }
        )
        self.history = self.history[-MAX_HISTORY * 4 :]

    def is_current(self, decision: Decision) -> bool:
        """Whether a target was produced from the runtime's current consumed frame."""

        frame = self.screen_frames.latest_frame()
        return frame is not None and decision.frame_id == frame.frame_id

    def state(self) -> dict[str, Any]:
        frame = self.screen_frames.latest_frame()
        return {
            "policy": self.policy.name,
            "goal": self.goal,
            "history": self.history[-MAX_HISTORY:],
            "allowed_actions": list(self.allowed_actions),
            "gate": self.gate.to_json(),
            "change_since_last_action": self.change_since_last_action(frame),
            "frame_id": frame.frame_id if frame else None,
            "video_source": (
                frame.metadata.get("video_source") if frame is not None else None
            ),
        }
