"""Bind System-1 visual decisions to GNSIS's existing persistent screen history.

This module deliberately owns no capture mechanism and no actuator. It consumes
`ScreenFrame` objects already accepted by the runtime and produces validated
structured decisions for an environment adapter to execute.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from PIL import Image, ImageChops, ImageStat

from ..screen import LatestScreenFrameBuffer, ScreenFrame
from .schema import Decision, validate_decision

MAX_HISTORY = 6
MOTION_WINDOW_MS = 800
_SIGNATURE_SIZE = (48, 30)


class VisualDecisionPolicy(Protocol):
    name: str

    def decide(
        self,
        frame: Any,
        goal: str,
        history: list[dict],
        motion: float,
        viewport: tuple[int, int],
        cache: Any,
    ) -> Decision: ...


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
        key=lambda frame: int(frame.captured_at_ms) if frame.captured_at_ms is not None else -1,
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


class PersistentVisualDecisionSession:
    """Task state for System-1 decisions over the shared GNSIS visual timeline.

    This is intentionally not another browser session. It does not own a tab,
    capture source, websocket, or actuator.
    """

    def __init__(
        self,
        policy: VisualDecisionPolicy,
        screen_frames: LatestScreenFrameBuffer,
        *,
        cache: Any = None,
        required_surface: str | None = "browser_tab",
        surface_id: str | None = None,
    ) -> None:
        self.policy = policy
        self.screen_frames = screen_frames
        self.cache = cache
        self.required_surface = required_surface
        self.surface_id = surface_id
        self.goal: str | None = None
        self.history: list[dict] = []

    def set_task(self, goal: str) -> None:
        goal = str(goal).strip()
        if not goal:
            raise ValueError("visual task goal must not be empty")
        self.goal = goal
        self.history = []

    def clear_task(self) -> None:
        self.goal = None
        self.history = []

    def _matches_surface(self, frame: ScreenFrame) -> bool:
        if self.required_surface is not None and frame.metadata.get("visual_surface") != self.required_surface:
            return False
        if self.surface_id is not None and frame.metadata.get("surface_id") != self.surface_id:
            return False
        return True

    def _eligible_frames(
        self,
        *,
        within_ms: float | None = None,
    ) -> tuple[ScreenFrame, ...]:
        return tuple(
            frame
            for frame in self.screen_frames.recent_frames(within_ms=within_ms)
            if self._matches_surface(frame)
        )

    def latest_frame(self) -> ScreenFrame:
        frames = self._eligible_frames()
        if not frames:
            surface = self.required_surface or "any"
            suffix = f" ({self.surface_id})" if self.surface_id is not None else ""
            raise RuntimeError(
                f"no consumed visual frame is available for surface {surface}{suffix}"
            )
        return frames[0]

    def decide(self) -> Decision:
        if not self.goal:
            raise ValueError("no visual task is set")
        source = self.latest_frame()
        view = RuntimeFrameView.from_screen_frame(source)
        viewport = view.image().size
        recent = self._eligible_frames(within_ms=MOTION_WINDOW_MS)
        motion = recent_motion(recent)
        decision = self.policy.decide(
            view,
            self.goal,
            list(self.history),
            motion,
            viewport,
            self.cache,
        )
        return validate_decision(decision, viewport)

    def record_attempt(self, decision: Decision) -> None:
        """Record one attempted action exactly as the prototype's session did."""

        if decision.action == "done":
            return
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

        try:
            frame = self.latest_frame()
        except RuntimeError:
            return False
        return decision.frame_id == frame.frame_id

    def state(self) -> dict[str, Any]:
        try:
            frame = self.latest_frame()
        except RuntimeError:
            frame = None
        return {
            "policy": self.policy.name,
            "goal": self.goal,
            "history": self.history[-MAX_HISTORY:],
            "frame_id": frame.frame_id if frame else None,
            "video_source": (
                frame.metadata.get("video_source")
                if frame is not None
                else None
            ),
            "visual_surface": (
                frame.metadata.get("visual_surface")
                if frame is not None
                else self.required_surface
            ),
            "surface_id": (
                frame.metadata.get("surface_id")
                if frame is not None
                else self.surface_id
            ),
        }
