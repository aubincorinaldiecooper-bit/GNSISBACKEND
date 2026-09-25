"""Thinker baseline provider — thin adapter over GNSISDuplexSession.

Keeps the current Thinker/Talker stack (raw audio+vision in, detached speech
synthesis) behind the same RealtimeSession contract Venus implements, so the
comparison harness and any future provider swap drive both models through
identical calls.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from ..duplex_bridge import GNSISDuplexSession
from ..realtime_provider import (
    ProviderEvent,
    ProviderSessionConfig,
    RealtimeSession,
)

LOGGER = logging.getLogger("gnsis_runtime.providers.thinker")


class ThinkerRealtimeProvider:
    """Opens Thinker sessions via the runtime's existing session builder."""

    def __init__(
        self,
        session_factory: Callable[[str, ProviderSessionConfig], GNSISDuplexSession],
    ) -> None:
        # The factory is the runtime's `_build_session`-equivalent: it owns
        # model weights, CUDA devices, and the detached Talker plumbing.
        self._session_factory = session_factory

    @property
    def provider_name(self) -> str:
        return "thinker"

    async def open_session(
        self, config: ProviderSessionConfig
    ) -> "ThinkerRealtimeSession":
        session = await asyncio.to_thread(
            self._session_factory, config.session_id, config
        )
        return ThinkerRealtimeSession(session)

    async def health(self) -> dict[str, Any]:
        return {"ready": True, "provider": self.provider_name}

    async def close(self) -> None:
        return None


class ThinkerRealtimeSession:
    """One live Thinker session behind the normalized contract."""

    def __init__(self, session: GNSISDuplexSession) -> None:
        self._session = session

    @property
    def session_id(self) -> str:
        live = getattr(self._session.live, "session_id", None)
        return str(live or "thinker")

    async def push_audio(
        self, pcm16: bytes, *, capture_ts_ms: int | None = None
    ) -> None:
        events = await asyncio.to_thread(
            self._session.feed_pcm16,
            pcm16,
            unit_capture_start_ms=(
                (float(capture_ts_ms),) if capture_ts_ms is not None else None
            ),
        )
        self._pending = list(getattr(self, "_pending", ())) + list(events)

    async def push_video_frame(
        self, data: bytes, *, mime_type: str = "image/jpeg", ts_ms: int | None = None
    ) -> None:
        import secrets

        from ..screen import ScreenFrame

        frame = ScreenFrame(
            frame_id=secrets.token_hex(8),
            image=data,
            captured_at_ms=ts_ms,
            metadata={"mime_type": mime_type},
        )
        self._session.enqueue_screen_frame(frame)

    async def push_control(self, control: dict[str, Any]) -> None:
        kind = control.get("kind")
        if kind == "interrupt":
            await asyncio.to_thread(self._session.interrupt_output)
            return
        if kind == "tool_response":
            await asyncio.to_thread(
                self._session.feed_tool_response, control.get("response")
            )
            return
        raise ValueError(f"unsupported thinker control kind: {kind!r}")

    async def next_event(self, timeout_s: float | None = None) -> ProviderEvent:
        deadline = None if timeout_s is None else asyncio.get_running_loop().time() + timeout_s
        while True:
            pending = getattr(self, "_pending", [])
            if pending:
                self._pending = pending[1:]
                return _normalize_output(pending[0])
            drained = await asyncio.to_thread(self._session.drain_outputs)
            if drained:
                self._pending = drained[1:]
                return _normalize_output(drained[0])
            if self._session.closed:
                return ProviderEvent(kind="closed", payload={})
            if deadline is not None and asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("no thinker output within timeout")
            await asyncio.sleep(0.01)

    async def acknowledge_playback(self, output_id: str, *, chunks_played: int) -> bool:
        # Thinker's send-receipt path finalizes delivery server-side; the
        # playback-ACK truth lives in the delivery gate, not the model.
        return True

    async def cancel_output(self, reason: str = "cancelled") -> None:
        await asyncio.to_thread(self._session.interrupt_output)

    async def close(self) -> None:
        await asyncio.to_thread(self._session.close)


def _normalize_output(output: Any) -> ProviderEvent:
    """Map one mcpmft duplex output event to a ProviderEvent."""

    kind = getattr(output, "kind", None) or getattr(output, "type", None) or "control"
    payload: dict[str, Any]
    if isinstance(output, dict):
        payload = dict(output)
        kind = payload.pop("kind", kind)
    else:
        payload = {"value": getattr(output, "value", None)}
    normalized = {
        "audio": "audio",
        "speech": "audio",
        "text": "text",
        "turn": "turn",
        "interrupt": "interrupt",
        "tool_call": "tool_call",
    }.get(str(kind), "control")
    return ProviderEvent(
        kind=normalized,
        payload=payload,
        epoch=getattr(output, "epoch", None),
        correlation_id=getattr(output, "output_id", None),
    )
