from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ScreenFrame:
    """A decoded screen image ready for visual prefill."""

    frame_id: str
    image: Any = field(repr=False, compare=False)
    captured_at_ms: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.frame_id, str):
            raise TypeError("frame_id must be a string")
        frame_id = self.frame_id.strip()
        if not frame_id or len(frame_id) > 256:
            raise ValueError("frame_id must contain 1 to 256 characters")
        object.__setattr__(self, "frame_id", frame_id)
        if self.image is None:
            raise ValueError("screen frame image must not be None")
        if self.captured_at_ms is not None and self.captured_at_ms < 0:
            raise ValueError("captured_at_ms must not be negative")


class LatestScreenFrameBuffer:
    """Capture-ordered screen buffer consumed at the MiniCPM unit clock.

    Producers publish independently of inference. Timestamped consumers select the newest
    frame available at audio-unit start; continuous visual mode may reuse the last frame.
    """

    def __init__(
        self,
        *,
        max_pending_frames: int = 128,
        max_history_frames: int = 64,
        history_window_ms: float | None = None,
    ) -> None:
        if max_pending_frames < 1:
            raise ValueError("max_pending_frames must be positive")
        if max_history_frames < 0:
            raise ValueError("max_history_frames must not be negative")
        if history_window_ms is not None and history_window_ms <= 0:
            raise ValueError("history_window_ms must be positive")
        self._lock = threading.Lock()
        self.max_pending_frames = int(max_pending_frames)
        self.max_history_frames = int(max_history_frames)
        self.history_window_ms = history_window_ms
        self._pending: list[ScreenFrame] = []
        self._last: ScreenFrame | None = None
        # Timestamp-indexed retention of frames the model actually consumed:
        # capture-ordered, bounded by count and (optionally) capture-time
        # window. This is the short-horizon "what was on screen N ms ago"
        # surface — every entry came from the live stream, never a screenshot.
        self._history: deque[ScreenFrame] = deque()

    @staticmethod
    def _capture_order(frame: ScreenFrame) -> int:
        return -1 if frame.captured_at_ms is None else frame.captured_at_ms

    def publish(self, frame: ScreenFrame) -> None:
        if not isinstance(frame, ScreenFrame):
            raise TypeError("frame must be a ScreenFrame")
        with self._lock:
            self._pending.append(frame)
            overflow = len(self._pending) - self.max_pending_frames
            if overflow > 0:
                del self._pending[:overflow]

    def consume_for_unit(
        self,
        *,
        reuse_base: bool = False,
        captured_not_after_ms: float | None = None,
    ) -> tuple[ScreenFrame, ...]:
        """Atomically select frames for one model unit.

        ``reuse_base`` implements continuous visual mode: after the newest frame is
        consumed, static-screen units reuse it.
        """

        with self._lock:
            frame: ScreenFrame | None = None
            if captured_not_after_ms is None:
                if self._pending:
                    frame = self._pending[-1]
                    self._pending.clear()
            else:
                eligible = [
                    (index, pending)
                    for index, pending in enumerate(self._pending)
                    if self._capture_order(pending) <= captured_not_after_ms
                ]
                if eligible:
                    _, frame = max(
                        eligible,
                        key=lambda item: (self._capture_order(item[1]), item[0]),
                    )
                    self._pending = [
                        pending
                        for pending in self._pending
                        if self._capture_order(pending) > captured_not_after_ms
                    ]

            fresh = False
            if frame is not None:
                if (
                    self._last is None
                    or self._capture_order(frame) >= self._capture_order(self._last)
                ):
                    self._last = frame
                    fresh = True
                else:
                    frame = None
            if frame is None and reuse_base:
                if (
                    self._last is not None
                    and (
                        captured_not_after_ms is None
                        or self._capture_order(self._last) <= captured_not_after_ms
                    )
                ):
                    frame = self._last
            if fresh and frame is not None and self.max_history_frames > 0:
                self._history.append(frame)
                while len(self._history) > self.max_history_frames:
                    self._history.popleft()
                if self.history_window_ms is not None:
                    cutoff = self._capture_order(frame) - self.history_window_ms
                    while (
                        self._history
                        and self._capture_order(self._history[0]) < cutoff
                    ):
                        self._history.popleft()
        return (frame,) if frame is not None else ()

    def latest_frame(self) -> ScreenFrame | None:
        """The newest frame the model has consumed, or None."""

        with self._lock:
            return self._history[-1] if self._history else None

    def frame_at_or_before(self, captured_at_ms: int) -> ScreenFrame | None:
        """Newest consumed frame captured no later than the given timestamp."""

        with self._lock:
            for frame in reversed(self._history):
                if self._capture_order(frame) <= captured_at_ms:
                    return frame
            return None

    def recent_frames(
        self,
        *,
        limit: int | None = None,
        within_ms: float | None = None,
    ) -> tuple[ScreenFrame, ...]:
        """Consumed frames, newest first — bounded temporal lookup for
        "what changed" / "what was there N ms ago" style reasoning."""

        with self._lock:
            frames = list(reversed(self._history))
        if within_ms is not None:
            newest = frames[0] if frames else None
            if newest is not None:
                cutoff = self._capture_order(newest) - within_ms
                frames = [
                    frame
                    for frame in frames
                    if self._capture_order(frame) >= cutoff
                ]
        if limit is not None:
            frames = frames[:limit]
        return tuple(frames)

    def reset(self) -> None:
        """Clear pending, reusable, and retained frames — a media-mode or
        session boundary must not leak stale visual history into the next
        context."""

        with self._lock:
            self._pending.clear()
            self._last = None
            self._history.clear()


class ScreenFrameRateGate:
    """Keep at most one frame per capture-time interval."""

    def __init__(self, min_interval_ms: float) -> None:
        if min_interval_ms <= 0:
            raise ValueError("min_interval_ms must be positive")
        self.min_interval_ms = float(min_interval_ms)
        self._last_accepted_ms: int | None = None

    def accept(self, captured_at_ms: int) -> bool:
        if captured_at_ms < 0:
            raise ValueError("captured_at_ms must not be negative")
        previous = self._last_accepted_ms
        if previous is not None:
            if captured_at_ms <= previous:
                return False
            if captured_at_ms - previous < self.min_interval_ms:
                return False
        self._last_accepted_ms = captured_at_ms
        return True
