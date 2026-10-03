"""Live sockets for a native full-duplex foreground provider.

``gnsis-serve`` runs this app when ``realtime.provider`` is not the Thinker.
It speaks the ``/ws/duplex`` and ``/ws/screen`` wire protocol the desktop
Host already speaks (``desktop/src/shared/protocol.ts``) and drives the model
only through the normalized ``RealtimeSession`` seam, so the Host never
learns which model is behind the socket. The Thinker keeps its own app
(``online_duplex``): its live loop is built around the Thinker/Talker
split and the task-tools coordinator, which a native model does not have.

What this app does not yet attach: the task-tools Gateway, the harness
bridge and durable session memory. A native session here is the foreground
model, the session timeline and the Host — the surface the matched
Thinker-versus-Venus evaluation needs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from .contracts import storage_key
from .media_timeline import AUDIO_INPUT_PROTOCOL, AudioFrameHeader
from .online_duplex import (
    _SESSION_ID,
    OnlineDuplexSettings,
    _came_through_the_front_door,
    _json,
)
from .realtime_provider import (
    ProviderEvent,
    ProviderSessionConfig,
    RealtimeProvider,
    RealtimeSession,
)
from .screen_transport import ScreenFrameHeader, decode_screen_frame
from .timeline import SessionTimeline

LOGGER = logging.getLogger("gnsis_runtime.native_duplex")

# How long one output poll waits before the loop checks for shutdown.
OUTPUT_POLL_SEC = 0.25
# Milliseconds of audio one `audio.chunk` header describes; informational for
# the Host's playback scheduler, the provider decides the real chunking.
DEFAULT_CHUNK_MS = 20


@dataclass
class _NativeSession:
    session: RealtimeSession
    timeline: SessionTimeline
    screen_token: str
    media_mode: str
    expires_at: float = 0.0
    ended: asyncio.Event = field(default_factory=asyncio.Event)
    frames_accepted: int = 0


class _SessionExpired(Exception):
    pass


def create_native_duplex_app(
    provider: RealtimeProvider,
    *,
    settings: OnlineDuplexSettings | None = None,
    media_dir: str | Path,
    system_prompt: str | None = None,
    chunk_ms: int = DEFAULT_CHUNK_MS,
) -> FastAPI:
    """Serve ``provider`` on the desktop Host's live sockets."""

    settings = settings or OnlineDuplexSettings()
    if settings.media_mode not in {"voice", "omni", "auto"}:
        raise ValueError(f"unsupported media_mode: {settings.media_mode!r}")
    if settings.max_screen_frame_bytes <= 0:
        raise ValueError("max_screen_frame_bytes must be positive")
    if settings.max_screen_pixels <= 0:
        raise ValueError("max_screen_pixels must be positive")
    if chunk_ms <= 0:
        raise ValueError("chunk_ms must be positive")
    media_path = Path(media_dir).expanduser().resolve()
    if not settings.edge_secret:
        LOGGER.warning(
            "GNSIS_EDGE_SECRET is not set: /ws/duplex and /ws/screen accept "
            "sockets from anywhere"
        )
    # One foreground session at a time, like the Thinker's model slot: the
    # provider behind this app is one model server on one GPU.
    slot = asyncio.Lock()
    sessions: dict[str, _NativeSession] = {}
    app = FastAPI(title="GNSIS Native Duplex", version="1.0.0")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        try:
            upstream: Any = await provider.health()
            status = "ok"
        except Exception as exc:  # the server is down, not this process
            upstream = {"error": type(exc).__name__}
            status = "degraded"
        return {
            "status": status,
            "foreground_provider": provider.provider_name,
            "provider": upstream,
            "sessions": len(sessions),
            "detached_talker": False,
            "audio_input_protocol": AUDIO_INPUT_PROTOCOL,
        }

    @app.websocket("/ws/duplex")
    async def duplex(websocket: WebSocket) -> None:
        if not _came_through_the_front_door(websocket, settings.edge_secret):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        session_id = websocket.query_params.get("session_id") or (
            f"duplex_{secrets.token_hex(8)}"
        )
        if not _SESSION_ID.fullmatch(session_id):
            await websocket.send_text(
                _json({"type": "error", "message": "invalid session_id", "fatal": True})
            )
            await websocket.close(code=1008)
            return
        if slot.locked():
            await websocket.send_text(
                _json(
                    {
                        "type": "error",
                        "message": "model is busy with another session",
                        "fatal": False,
                        "retry": True,
                    }
                )
            )
            await websocket.close(code=1013)
            return
        await slot.acquire()
        send_lock = asyncio.Lock()
        active: _NativeSession | None = None
        pump: asyncio.Task[None] | None = None
        pending_audio_header: AudioFrameHeader | None = None

        async def send_text(payload: dict[str, Any]) -> None:
            if payload.get("type") == "error" and "fatal" not in payload:
                payload = {**payload, "fatal": False}
            async with send_lock:
                await websocket.send_text(_json(payload))

        async def send_audio(payload: dict[str, Any], pcm16: bytes) -> None:
            payload.update(
                audio=bool(pcm16),
                audio_bytes=len(pcm16),
                audio_format="pcm16",
                audio_sample_rate=settings.output_sample_rate,
            )
            async with send_lock:
                await websocket.send_text(_json(payload))
                if pcm16:
                    await websocket.send_bytes(pcm16)

        def raise_if_expired() -> None:
            if (
                active is not None
                and active.expires_at
                and time.monotonic() >= active.expires_at
            ):
                raise _SessionExpired

        async def receive_message() -> dict[str, Any]:
            deadline = active.expires_at if active is not None else 0.0
            if not deadline:
                return await websocket.receive()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _SessionExpired
            try:
                return await asyncio.wait_for(websocket.receive(), remaining)
            except asyncio.TimeoutError:
                raise _SessionExpired from None

        async def forward_outputs(current: _NativeSession) -> None:
            """Provider events -> Host wire messages, one timeline event each."""

            sequence = 0
            generation: Any = 0
            while not current.ended.is_set():
                try:
                    event = await current.session.next_event(timeout_s=OUTPUT_POLL_SEC)
                except TimeoutError:
                    await asyncio.sleep(0)
                    continue
                if event.epoch is not None:
                    generation = event.epoch
                    current.timeline.note_output_epoch(int(event.epoch))
                current.timeline.emit(
                    f"foreground.{event.kind}",
                    component="foreground",
                    correlation_id=event.correlation_id,
                    output_epoch=event.epoch,
                    fields=_timeline_fields(event),
                )
                output_id = event.correlation_id or str(generation)
                if event.kind == "audio":
                    pcm16 = event.payload.get("pcm16") or b""
                    if isinstance(pcm16, str):
                        pcm16 = pcm16.encode("latin-1")
                    end_of_turn = bool(event.payload.get("turn_finished"))
                    sequence += 1
                    await send_audio(
                        {
                            "type": "audio.chunk",
                            "generation_id": generation,
                            "output_id": output_id,
                            "sequence": sequence,
                            "end_of_turn": end_of_turn,
                        },
                        bytes(pcm16),
                    )
                    if end_of_turn:
                        await send_text(
                            {
                                "type": "audio.done",
                                "generation_id": generation,
                                "output_id": output_id,
                                "end_of_turn": True,
                            }
                        )
                elif event.kind == "text":
                    await send_text(
                        {
                            "type": "text",
                            "generation_id": generation,
                            "output_id": output_id,
                            "text": event.payload.get("text")
                            or event.payload.get("value")
                            or "",
                        }
                    )
                elif event.kind == "interrupt":
                    await send_text(
                        {
                            "type": "playback.cancel",
                            "generation_id": generation,
                            "cancelled_generation_id": event.payload.get(
                                "cancelled_generation_id", generation
                            ),
                            "reason": event.payload.get("reason", "model_interrupt"),
                        }
                    )
                elif event.kind == "tool_call":
                    await send_text(
                        {
                            "type": "tool.call",
                            "generation_id": generation,
                            **event.payload,
                        }
                    )
                elif event.kind == "turn":
                    await send_text(
                        {
                            "type": "turn.done",
                            "generation_id": generation,
                            **event.payload,
                        }
                    )
                elif event.kind == "closed":
                    await send_text(
                        {
                            "type": "error",
                            "message": "foreground provider closed the session",
                            "fatal": True,
                        }
                    )
                    current.ended.set()
                    return
                # "control" events are bookkeeping; they are on the timeline only.

        disconnect_reason = "closed"
        try:
            opened = await provider.open_session(
                ProviderSessionConfig(
                    session_id=session_id,
                    input_sample_rate=settings.input_sample_rate,
                    output_sample_rate=settings.output_sample_rate,
                    system_prompt=system_prompt or settings.system_prompt,
                    ref_audio_path=settings.ref_audio_path,
                    extra={"media_mode": settings.media_mode},
                )
            )
            timeline = SessionTimeline(
                session_id,
                log_path=media_path / f"{storage_key(session_id)}.timeline.jsonl",
            )
            active = _NativeSession(
                session=opened,
                timeline=timeline,
                screen_token=secrets.token_urlsafe(32),
                media_mode=settings.media_mode,
                expires_at=(
                    time.monotonic() + settings.max_session_sec
                    if settings.max_session_sec > 0
                    else 0.0
                ),
            )
            sessions[session_id] = active
            timeline.emit(
                "session.opened",
                component="foreground",
                fields={"provider": provider.provider_name},
            )
            screen_enabled = settings.media_mode != "voice"
            pump = asyncio.create_task(
                forward_outputs(active), name=f"gnsis-native-output-{session_id}"
            )
            await send_text(
                {
                    "type": "ready",
                    "session_id": session_id,
                    "foreground_provider": provider.provider_name,
                    "resume_token": None,
                    "resumed": False,
                    "reconnect_grace_ms": 0,
                    "input_sample_rate": settings.input_sample_rate,
                    "output_sample_rate": settings.output_sample_rate,
                    "chunk_ms": chunk_ms,
                    "audio_input": {
                        "protocol": AUDIO_INPUT_PROTOCOL,
                        "encoding": "pcm_s16le",
                        "clock": "unix_ms",
                    },
                    "generate_audio": True,
                    "detached_talker": False,
                    "generation_id": 0,
                    "transport": "ws",
                    "media_mode": settings.media_mode,
                    "video_source": None,
                    "tool_call_protocol": "call_id_v1",
                    "tools": [],
                    "context_events": ["turn.final", "screen"],
                    "screen": {
                        "enabled": screen_enabled,
                        "path": "/ws/screen" if screen_enabled else None,
                        "token": active.screen_token if screen_enabled else None,
                        "protocol": "metadata-json+encoded-binary-v1",
                        "encodings": ["jpeg", "webp", "png"],
                        "max_frame_bytes": settings.max_screen_frame_bytes,
                        "max_pixels": settings.max_screen_pixels,
                    },
                }
            )
            LOGGER.info(
                "native duplex ready: provider=%s session_id=%s",
                provider.provider_name,
                session_id,
            )

            while True:
                if pump.done() and pump.exception() is not None:
                    raise pump.exception()  # type: ignore[misc]
                message = await receive_message()
                if message.get("type") == "websocket.disconnect":
                    disconnect_reason = "disconnect"
                    return
                if message.get("bytes") is not None:
                    raise_if_expired()
                    audio = message["bytes"]
                    header = pending_audio_header
                    pending_audio_header = None
                    timeline.emit(
                        "mic.frame",
                        component="host",
                        source_ts_ms=header.captured_at_ms if header else None,
                        fields={
                            "bytes": len(audio),
                            "sequence": header.sequence if header else None,
                        },
                    )
                    await active.session.push_audio(
                        audio,
                        capture_ts_ms=header.captured_at_ms if header else None,
                    )
                    continue
                raw = message.get("text")
                if raw is None:
                    continue
                try:
                    control = json.loads(raw)
                except json.JSONDecodeError:
                    await send_text(
                        {"type": "error", "message": "invalid json control event"}
                    )
                    continue
                if not isinstance(control, dict):
                    await send_text(
                        {"type": "error", "message": "control event must be an object"}
                    )
                    continue
                event_type = control.get("type")
                if event_type == "audio.frame":
                    if pending_audio_header is not None:
                        await send_text(
                            {
                                "type": "error",
                                "message": (
                                    "audio frame metadata requires a following "
                                    "binary payload"
                                ),
                            }
                        )
                        continue
                    try:
                        pending_audio_header = AudioFrameHeader.from_payload(control)
                    except ValueError as exc:
                        await send_text({"type": "error", "message": str(exc)})
                elif event_type == "ping":
                    await send_text({"type": "pong", "id": control.get("id")})
                elif event_type == "stop":
                    disconnect_reason = "stop"
                    await send_text({"type": "session.done"})
                    await websocket.close()
                    return
                elif event_type in {"break", "reset"}:
                    reason = str(control.get("reason") or event_type)
                    timeline.emit(
                        "user.interrupt", component="host", fields={"reason": reason}
                    )
                    await active.session.cancel_output(reason)
                    await send_text({"type": f"{event_type}.done"})
                elif event_type == "playback.ack":
                    playback_id = control.get("playback_id")
                    phase = control.get("phase")
                    if not isinstance(playback_id, str) or phase not in {
                        "started",
                        "finished",
                    }:
                        await send_text(
                            {"type": "error", "message": "invalid playback.ack"}
                        )
                        continue
                    source_ts = control.get("source_ts_ms")
                    timeline.emit(
                        f"playback.{phase}",
                        component="host",
                        correlation_id=playback_id,
                        output_epoch=(
                            control["epoch"]
                            if isinstance(control.get("epoch"), int)
                            else None
                        ),
                        source_ts_ms=source_ts if isinstance(source_ts, int) else None,
                    )
                    accepted = await active.session.acknowledge_playback(
                        playback_id,
                        chunks_played=int(control.get("chunks_played") or 0),
                    )
                    await send_text(
                        {"type": "playback.ack.done", "accepted": bool(accepted)}
                    )
                elif event_type == "host.event":
                    host_event = control.get("event")
                    if isinstance(host_event, dict):
                        call_id = host_event.get("call_id")
                        source_ts = host_event.get("ts_ms")
                        timeline.emit(
                            str(host_event.get("type", "host.event")),
                            component="host",
                            fields={k: v for k, v in host_event.items() if k != "type"},
                            correlation_id=call_id
                            if isinstance(call_id, str)
                            else None,
                            source_ts_ms=(
                                source_ts
                                if isinstance(source_ts, int)
                                and not isinstance(source_ts, bool)
                                else None
                            ),
                        )
                    await send_text({"type": "host.event.done"})
                elif event_type == "tool.response":
                    call_id = control.get("call_id")
                    if not isinstance(call_id, str) or not call_id:
                        await send_text(
                            {
                                "type": "error",
                                "message": "tool.response requires call_id",
                            }
                        )
                        continue
                    timeline.emit(
                        "tool.response", component="host", correlation_id=call_id
                    )
                    try:
                        await active.session.push_control(
                            {
                                "kind": "tool_response",
                                "call_id": call_id,
                                "response": control.get("content"),
                            }
                        )
                    except ValueError as exc:
                        await send_text({"type": "error", "message": str(exc)})
                        continue
                    await send_text(
                        {"type": "tool.response.queued", "call_id": call_id}
                    )
                elif event_type == "tool.progress":
                    await send_text({"type": "tool.progress.noted"})
                elif event_type == "turn.final":
                    turn_id = control.get("turn_id")
                    timeline.emit(
                        "turn.final",
                        component="host",
                        correlation_id=turn_id if isinstance(turn_id, str) else None,
                        fields={"chars": len(str(control.get("text") or ""))},
                    )
                    await send_text({"type": "turn.final.accepted", "turn_id": turn_id})
                else:
                    await send_text(
                        {
                            "type": "error",
                            "message": f"unsupported control event: {event_type}",
                        }
                    )
        except WebSocketDisconnect as exc:
            disconnect_reason = "disconnect"
            LOGGER.info(
                "native duplex disconnected: session_id=%s code=%s",
                session_id,
                getattr(exc, "code", None),
            )
        except _SessionExpired:
            disconnect_reason = "expired"
            try:
                await send_text(
                    {
                        "type": "error",
                        "message": (
                            "This session reached its time limit. "
                            "Start a new one to keep going."
                        ),
                        "fatal": True,
                    }
                )
            except (RuntimeError, WebSocketDisconnect):
                pass
        except Exception as exc:
            disconnect_reason = "error"
            LOGGER.exception("native duplex failed: %s", session_id)
            try:
                await send_text({"type": "error", "message": str(exc), "fatal": True})
            except (RuntimeError, WebSocketDisconnect):
                pass
        finally:
            if active is not None:
                active.ended.set()
                sessions.pop(session_id, None)
            if pump is not None:
                pump.cancel()
                await asyncio.gather(pump, return_exceptions=True)
            if active is not None:
                active.timeline.emit(
                    "session.closed",
                    component="foreground",
                    fields={"reason": disconnect_reason},
                )
                try:
                    await asyncio.shield(active.session.close())
                except Exception:
                    LOGGER.warning(
                        "closing native session %s failed", session_id, exc_info=True
                    )
            slot.release()
            LOGGER.info(
                "native duplex ended: session_id=%s reason=%s",
                session_id,
                disconnect_reason,
            )

    @app.websocket("/ws/screen")
    async def screen(websocket: WebSocket) -> None:
        if not _came_through_the_front_door(websocket, settings.edge_secret):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        session_id = websocket.query_params.get("session_id") or ""
        token = websocket.query_params.get("token") or ""
        active = sessions.get(session_id)
        if not _SESSION_ID.fullmatch(session_id) or active is None:
            await websocket.send_text(
                _json({"type": "error", "message": "duplex session is not active"})
            )
            await websocket.close(code=1008)
            return
        if active.media_mode == "voice":
            await websocket.send_text(
                _json(
                    {
                        "type": "error",
                        "message": "screen input is disabled in voice mode",
                    }
                )
            )
            await websocket.close(code=1008)
            return
        if not token or not secrets.compare_digest(token, active.screen_token):
            await websocket.send_text(
                _json({"type": "error", "message": "invalid screen session token"})
            )
            await websocket.close(code=1008)
            return
        await websocket.send_text(
            _json(
                {
                    "type": "screen.ready",
                    "session_id": session_id,
                    "media_mode": active.media_mode,
                    "video_source": None,
                    "max_frame_bytes": settings.max_screen_frame_bytes,
                    "max_pixels": settings.max_screen_pixels,
                }
            )
        )

        async def receive_while_active() -> dict[str, Any] | None:
            receive_task = asyncio.create_task(websocket.receive())
            ended_task = asyncio.create_task(active.ended.wait())
            done, pending = await asyncio.wait(
                (receive_task, ended_task), return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if ended_task in done:
                await asyncio.gather(receive_task, return_exceptions=True)
                try:
                    await websocket.close()
                except RuntimeError:
                    pass
                return None
            return receive_task.result()

        try:
            while sessions.get(session_id) is active:
                metadata_message = await receive_while_active()
                if (
                    metadata_message is None
                    or metadata_message.get("type") == "websocket.disconnect"
                ):
                    return
                frame_id: Any = None
                try:
                    raw_metadata = metadata_message.get("text")
                    if raw_metadata is None:
                        raise ValueError("screen frame metadata must be JSON text")
                    payload = json.loads(raw_metadata)
                    if not isinstance(payload, dict):
                        raise ValueError("screen frame metadata must be an object")
                    header = ScreenFrameHeader.from_payload(payload)
                    frame_id = header.frame_id
                    image_message = await receive_while_active()
                    if (
                        image_message is None
                        or image_message.get("type") == "websocket.disconnect"
                    ):
                        return
                    image_payload = image_message.get("bytes")
                    if image_payload is None:
                        raise ValueError("screen frame image must be binary")
                    # Decoded only to enforce the same byte/pixel bounds as the
                    # Thinker's channel; the provider receives the encoded image.
                    decoded = await asyncio.to_thread(
                        decode_screen_frame,
                        header,
                        image_payload,
                        max_bytes=settings.max_screen_frame_bytes,
                        max_pixels=settings.max_screen_pixels,
                    )
                    if sessions.get(session_id) is not active:
                        return
                    await active.session.push_video_frame(
                        image_payload,
                        mime_type=f"image/{header.encoding}",
                        ts_ms=header.captured_at_ms,
                    )
                    active.frames_accepted += 1
                    active.timeline.emit(
                        "video.frame",
                        component="host",
                        source_ts_ms=header.captured_at_ms,
                        fields={
                            "frame_id": header.frame_id,
                            "source": header.video_source,
                            "width": decoded.width,
                            "height": decoded.height,
                        },
                    )
                    await websocket.send_text(
                        _json(
                            {
                                "type": "screen.frame.accepted",
                                "frame_id": header.frame_id,
                                "captured_at_ms": header.captured_at_ms,
                                "width": decoded.width,
                                "height": decoded.height,
                                "asset_id": decoded.asset_id,
                                "context_sampled": True,
                            }
                        )
                    )
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    await websocket.send_text(
                        _json(
                            {
                                "type": "screen.frame.dropped",
                                "frame_id": frame_id,
                                "reason": str(exc),
                            }
                        )
                    )
                    continue
        except WebSocketDisconnect:
            return

    return app


def _timeline_fields(event: ProviderEvent) -> dict[str, Any]:
    """Timeline-safe view of a provider event: sizes and flags, never media."""

    fields: dict[str, Any] = {}
    pcm16 = event.payload.get("pcm16")
    if isinstance(pcm16, (bytes, bytearray)):
        fields["audio_bytes"] = len(pcm16)
    text = event.payload.get("text")
    if isinstance(text, str):
        fields["chars"] = len(text)
    for key in ("turn_finished", "finish_reason", "reason", "call_id", "name"):
        if key in event.payload:
            fields[key] = event.payload[key]
    if event.seq is not None:
        fields["provider_seq"] = event.seq
    return fields
