"""Output epochs, result lifecycle, and the delivery gate (AGENTS.md 7/Phase 2).

"Generation complete" is never "play now". A background result moves through

    running -> completed -> result_ready -> awaiting_delivery -> delivering
    -> delivered

with cancellation/supersession off any state. ``delivering`` -> ``delivered``
happens only on the *device* playback ACK — the authoritative signal that
audio actually started/finished — or on the deterministic ACK-timeout
recovery path.

Gates checked before a queued result may announce:

- the user is not currently speaking;
- the foreground model is not currently speaking (no open output epoch);
- a prior announcement's playback is not still draining;
- the result belongs to the current output epoch (not superseded/stale);
- the result has not been cancelled;
- there is no queued interrupt-priority result ahead of it.

Every transition is emitted onto the session timeline so the run can be
reconstructed: ``result.state``, ``output.epoch``, ``playback.ack``,
``delivery.gate``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

from .timeline import SessionTimeline

ResultState = Literal[
    "running",
    "completed",
    "result_ready",
    "awaiting_delivery",
    "delivering",
    "delivered",
    "cancelled",
    "superseded",
    "failed",
]

_TERMINAL = frozenset({"delivered", "cancelled", "superseded", "failed"})


@dataclass
class TrackedResult:
    """The delivery-visible lifecycle of one background result."""

    result_id: str
    delivery_id: str | None = None
    epoch: int = 0
    state: ResultState = "result_ready"
    blocked_by: str | None = None
    reason: str | None = None
    history: list[tuple[str, ResultState, int]] = field(default_factory=list)

    def record(self, at_ms: int, state: ResultState, reason: str | None = None) -> None:
        self.state = state
        self.reason = reason
        self.history.append((self.reason or "", state, at_ms))


class DeliveryGate:
    """Decides when queued results may speak and owns output epochs.

    The gate is the single answer to "is the foreground free to announce":
    it tracks user/model speech activity, outstanding device playback ACKs,
    and the current output epoch that stale chunks must never outlive.
    """

    def __init__(
        self,
        timeline: SessionTimeline,
        *,
        playback_ack_timeout_sec: float = 15.0,
        user_speech_window_sec: float = 0.4,
    ) -> None:
        if playback_ack_timeout_sec <= 0:
            raise ValueError("playback_ack_timeout_sec must be positive")
        if user_speech_window_sec < 0:
            raise ValueError("user_speech_window_sec cannot be negative")
        self.timeline = timeline
        self.playback_ack_timeout_sec = playback_ack_timeout_sec
        self.user_speech_window_sec = user_speech_window_sec
        # ``_output_epoch`` increments on every meaningful interruption; any
        # output or playback tagged with an older epoch is stale.
        self._output_epoch = 0
        self._user_last_spoke_sec: float | None = None
        self._model_output_open = False
        # playback_id -> (epoch, started_monotonic) for ACKs in flight.
        self._pending_playbacks: dict[str, tuple[int, float]] = {}
        # ACKs that timed out since the last ``take_expired`` — the coordinator
        # turns them into deterministic 'undelivered' finishes.
        self._expired: list[str] = []
        self._results: dict[str, TrackedResult] = {}

    # -- epochs -----------------------------------------------------------

    @property
    def output_epoch(self) -> int:
        return self._output_epoch

    def interrupt(self, *, reason: str) -> int:
        """Cancel the active output epoch on a meaningful interruption.

        Bumps the epoch, marks model output closed, and drops in-flight
        playback expectations for the stale epoch: a late ACK for it is
        ignored rather than confusing the new epoch.
        """
        self._output_epoch += 1
        self._model_output_open = False
        stale = [pid for pid, (epoch, _) in self._pending_playbacks.items() if epoch < self._output_epoch]
        for pid in stale:
            del self._pending_playbacks[pid]
        self.timeline.emit(
            "output.epoch",
            component="delivery_gate",
            fields={"reason": reason, "stale_playbacks_dropped": len(stale)},
            output_epoch=self._output_epoch,
        )
        for result in self._results.values():
            if result.state == "delivering" and result.epoch < self._output_epoch:
                self._transition(result, "cancelled", reason="interrupted")
        return self._output_epoch

    # -- speech activity ---------------------------------------------------

    def note_user_speech(self, *, at_ms: int | None = None) -> None:
        """User audio activity: a bound turn lands while the user speaks."""
        self._user_last_spoke_sec = time.monotonic()
        self.timeline.emit(
            "user.speech",
            component="delivery_gate",
            source_ts_ms=at_ms,
        )

    def note_model_output_started(self, *, epoch: int | None = None) -> None:
        self._model_output_open = True
        self.timeline.emit(
            "model.output_started",
            component="delivery_gate",
            output_epoch=epoch,
        )

    def note_model_output_finished(self, *, epoch: int | None = None) -> None:
        self._model_output_open = False
        self.timeline.emit(
            "model.output_finished",
            component="delivery_gate",
            output_epoch=epoch,
        )

    # -- playback ACK truth --------------------------------------------------

    def note_output_sent(self, playback_id: str, *, epoch: int | None = None) -> None:
        """Audio for ``playback_id`` left the server; awaiting device ACK."""
        epoch = self._output_epoch if epoch is None else int(epoch)
        self._pending_playbacks[playback_id] = (epoch, time.monotonic())
        self.timeline.emit(
            "playback.awaiting_ack",
            component="delivery_gate",
            correlation_id=playback_id,
            output_epoch=epoch,
        )

    def note_playback_ack(
        self,
        playback_id: str,
        phase: str,
        *,
        epoch: int | None = None,
        source_ts_ms: int | None = None,
    ) -> bool:
        """Apply a device playback ACK. Returns False when it was stale."""
        if phase not in {"started", "finished", "cancelled"}:
            raise ValueError(f"unknown playback ack phase {phase!r}")
        pending = self._pending_playbacks.get(playback_id)
        ack_epoch = epoch if epoch is not None else (pending[0] if pending else self._output_epoch)
        stale = ack_epoch < self._output_epoch
        self.timeline.emit(
            "playback.ack",
            component="delivery_gate",
            correlation_id=playback_id,
            fields={"phase": phase, "stale": stale},
            source_ts_ms=source_ts_ms,
            output_epoch=ack_epoch,
        )
        if stale:
            return False
        if phase in {"finished", "cancelled"}:
            self._pending_playbacks.pop(playback_id, None)
        elif pending is not None:
            # Refresh the timeout clock on a started ACK: playback began.
            self._pending_playbacks[playback_id] = (ack_epoch, time.monotonic())
        return True

    def playback_draining(self, *, now: float | None = None) -> bool:
        """True while any sent output still awaits a device finished ACK."""
        now = time.monotonic() if now is None else now
        expired = [
            pid
            for pid, (_, started) in self._pending_playbacks.items()
            if now - started > self.playback_ack_timeout_sec
        ]
        for pid in expired:
            # ACK lost/timeout: deterministic recovery — the playback is
            # declared undelivered (never silently 'played'), the pending slot
            # frees, and the timeline records why.
            del self._pending_playbacks[pid]
            self._expired.append(pid)
            self.timeline.emit(
                "playback.ack_timeout",
                component="delivery_gate",
                correlation_id=pid,
            )
        return bool(self._pending_playbacks)

    def take_expired(self) -> tuple[str, ...]:
        """Playback ids whose ACK timed out since the last call."""
        expired = tuple(self._expired)
        self._expired.clear()
        return expired

    # -- result lifecycle ----------------------------------------------------

    def register(self, result_id: str, *, delivery_id: str | None = None) -> TrackedResult:
        existing = self._results.get(result_id)
        if existing is not None:
            return existing
        result = TrackedResult(result_id=result_id, delivery_id=delivery_id)
        self._results[result_id] = result
        self._transition(result, "awaiting_delivery")
        return result

    def cancel(self, result_id: str, *, reason: str = "cancelled") -> None:
        result = self._results.get(result_id)
        if result is None or result.state in _TERMINAL:
            return
        self._transition(result, "cancelled", reason=reason)

    def supersede(self, result_id: str) -> None:
        result = self._results.get(result_id)
        if result is None or result.state in _TERMINAL:
            return
        self._transition(result, "superseded", reason="superseded")

    def note_delivering(self, result_id: str) -> None:
        result = self._results.get(result_id)
        if result is None or result.state in _TERMINAL:
            return
        result.epoch = self._output_epoch
        self._transition(result, "delivering")

    def note_delivered(self, result_id: str) -> None:
        result = self._results.get(result_id)
        if result is None or result.state in _TERMINAL:
            return
        self._transition(result, "delivered")

    def note_failed(self, result_id: str, *, reason: str) -> None:
        result = self._results.get(result_id)
        if result is None or result.state in _TERMINAL:
            return
        self._transition(result, "failed", reason=reason)

    def _transition(
        self, result: TrackedResult, state: ResultState, *, reason: str | None = None
    ) -> None:
        previous = result.state
        result.record(int(time.time() * 1000), state, reason)
        self.timeline.emit(
            "result.state",
            component="delivery_gate",
            correlation_id=result.result_id,
            fields={
                "from": previous,
                "to": state,
                "reason": reason,
                "delivery_id": result.delivery_id,
            },
        )

    def result(self, result_id: str) -> TrackedResult | None:
        return self._results.get(result_id)

    # -- the gate --------------------------------------------------------------

    def blocked_reason(self, *, timing: str = "safe_pause") -> str | None:
        """Why delivery is blocked right now, or None when the gate is open.

        ``interrupt``-priority results preempt model output and in-flight
        playback; everything else waits for a quiet foreground.
        """
        if self.user_active():
            return "user_speaking"
        if timing != "interrupt":
            if self._model_output_open:
                return "model_speaking"
            if self.playback_draining():
                return "playback_draining"
        return None

    def may_announce(self, result_id: str, *, timing: str = "safe_pause") -> tuple[bool, str | None]:
        """Gate check for one queued result: (allowed, blocked_by)."""
        result = self._results.get(result_id)
        if result is not None and result.state in _TERMINAL:
            return False, result.state
        reason = self.blocked_reason(timing=timing)
        if result is not None:
            result.blocked_by = reason
        return reason is None, reason

    def user_active(self) -> bool:
        """The user spoke within the recent window (bound turns land live)."""
        if self._user_last_spoke_sec is None:
            return False
        return (time.monotonic() - self._user_last_spoke_sec) <= self.user_speech_window_sec
