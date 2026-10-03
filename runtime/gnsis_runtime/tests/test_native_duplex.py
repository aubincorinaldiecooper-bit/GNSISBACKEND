"""The Host's live sockets served by a native provider through the seam."""

from __future__ import annotations

import asyncio
import io
import json
from typing import Any

import pytest
from starlette.testclient import TestClient

from gnsis_runtime.native_duplex import create_native_duplex_app
from gnsis_runtime.online_duplex import OnlineDuplexSettings
from gnsis_runtime.realtime_provider import ProviderEvent, ProviderSessionConfig


class ScriptedSession:
    """Records every seam call and replays scripted provider events."""

    def __init__(self, config: ProviderSessionConfig) -> None:
        self.config = config
        self.calls: list[tuple[str, Any]] = []
        self.events: asyncio.Queue[ProviderEvent] = asyncio.Queue()
        self.closed = False

    @property
    def session_id(self) -> str:
        return self.config.session_id

    async def push_audio(
        self, pcm16: bytes, *, capture_ts_ms: int | None = None
    ) -> None:
        self.calls.append(("audio", (pcm16, capture_ts_ms)))

    async def push_video_frame(
        self, data: bytes, *, mime_type: str = "image/jpeg", ts_ms: int | None = None
    ) -> None:
        self.calls.append(("video", (data, mime_type, ts_ms)))

    async def push_control(self, control: dict[str, Any]) -> None:
        self.calls.append(("control", control))

    async def next_event(self, timeout_s: float | None = None) -> ProviderEvent:
        try:
            return await asyncio.wait_for(self.events.get(), timeout_s)
        except asyncio.TimeoutError:
            raise TimeoutError from None

    async def acknowledge_playback(self, output_id: str, *, chunks_played: int) -> bool:
        self.calls.append(("ack", (output_id, chunks_played)))
        return True

    async def cancel_output(self, reason: str = "cancelled") -> None:
        self.calls.append(("cancel", reason))

    async def close(self) -> None:
        self.closed = True


class ScriptedProvider:
    provider_name = "venus"

    def __init__(self) -> None:
        self.sessions: list[ScriptedSession] = []

    async def open_session(self, config: ProviderSessionConfig) -> ScriptedSession:
        session = ScriptedSession(config)
        self.sessions.append(session)
        return session

    async def health(self) -> dict[str, Any]:
        return {"status": "ok"}

    async def close(self) -> None:
        return None


def _png() -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (8, 6), (10, 20, 30)).save(out, format="PNG")
    return out.getvalue()


def _app(provider: ScriptedProvider, tmp_path, **settings: Any):
    return create_native_duplex_app(
        provider,
        settings=OnlineDuplexSettings(media_mode="omni", **settings),
        media_dir=tmp_path / "media",
        system_prompt="native prompt",
    )


def test_the_host_protocol_reaches_the_provider_through_the_seam(tmp_path):
    provider = ScriptedProvider()
    with TestClient(_app(provider, tmp_path)) as client:
        with client.websocket_connect("/ws/duplex?session_id=s1") as ws:
            ready = ws.receive_json()
            assert ready["type"] == "ready"
            assert ready["foreground_provider"] == "venus"
            assert ready["detached_talker"] is False
            assert ready["screen"]["enabled"] and ready["screen"]["token"]
            session = provider.sessions[0]
            assert session.config.system_prompt == "native prompt"

            ws.send_json(
                {
                    "type": "audio.frame",
                    "sequence": 1,
                    "start_sample": 0,
                    "sample_count": 320,
                    "captured_at_ms": 1700000000000,
                }
            )
            ws.send_bytes(b"\x01\x00" * 320)
            ws.send_json({"type": "ping", "id": 7})
            assert ws.receive_json() == {"type": "pong", "id": 7}
            assert ("audio", (b"\x01\x00" * 320, 1700000000000)) in session.calls

            ws.send_json({"type": "break", "reason": "user_spoke"})
            assert ws.receive_json()["type"] == "break.done"
            assert ("cancel", "user_spoke") in session.calls

            ws.send_json(
                {
                    "type": "playback.ack",
                    "playback_id": "out-1",
                    "phase": "finished",
                    "epoch": 3,
                    "chunks_played": 4,
                }
            )
            assert ws.receive_json() == {"type": "playback.ack.done", "accepted": True}
            assert ("ack", ("out-1", 4)) in session.calls

            ws.send_json(
                {"type": "tool.response", "call_id": "c1", "content": {"ok": 1}}
            )
            assert ws.receive_json() == {
                "type": "tool.response.queued",
                "call_id": "c1",
            }
            assert (
                "control",
                {"kind": "tool_response", "call_id": "c1", "response": {"ok": 1}},
            ) in session.calls

            ws.send_json({"type": "stop"})
            assert ws.receive_json() == {"type": "session.done"}
        assert session.closed
    (log,) = (tmp_path / "media").glob("s1-*.timeline.jsonl")
    kinds = [json.loads(line)["kind"] for line in log.read_text().splitlines()]
    for expected in (
        "session.opened",
        "mic.frame",
        "user.interrupt",
        "playback.finished",
        "tool.response",
        "session.closed",
    ):
        assert expected in kinds


def test_provider_audio_arrives_as_desktop_audio_chunks_and_done(tmp_path):
    provider = ScriptedProvider()
    with TestClient(_app(provider, tmp_path)) as client:
        with client.websocket_connect("/ws/duplex?session_id=s2") as ws:
            assert ws.receive_json()["type"] == "ready"
            session = provider.sessions[0]
            loop = client.portal
            loop.call(
                session.events.put,
                ProviderEvent(
                    kind="audio",
                    payload={"pcm16": b"\x02\x00" * 10, "turn_finished": False},
                    epoch=5,
                    correlation_id="out-5",
                ),
            )
            header = ws.receive_json()
            assert header["type"] == "audio.chunk"
            assert header["generation_id"] == 5
            assert header["output_id"] == "out-5"
            assert header["audio_sample_rate"] == 24000
            assert ws.receive_bytes() == b"\x02\x00" * 10
            loop.call(
                session.events.put,
                ProviderEvent(
                    kind="audio",
                    payload={"pcm16": b"", "turn_finished": True},
                    epoch=5,
                    correlation_id="out-5",
                ),
            )
            assert ws.receive_json()["type"] == "audio.chunk"
            done = ws.receive_json()
            assert done == {
                "type": "audio.done",
                "generation_id": 5,
                "output_id": "out-5",
                "end_of_turn": True,
            }
            loop.call(
                session.events.put,
                ProviderEvent(kind="interrupt", payload={"reason": "user"}, epoch=6),
            )
            cancel = ws.receive_json()
            assert cancel["type"] == "playback.cancel"
            assert cancel["generation_id"] == 6
            loop.call(
                session.events.put,
                ProviderEvent(
                    kind="tool_call",
                    payload={"call_id": "c9", "name": "task_start", "arguments": {}},
                    epoch=6,
                    correlation_id="c9",
                ),
            )
            call = ws.receive_json()
            assert call["type"] == "tool.call" and call["call_id"] == "c9"


def test_screen_frames_go_to_the_provider_encoded_with_their_capture_time(tmp_path):
    provider = ScriptedProvider()
    png = _png()
    with TestClient(_app(provider, tmp_path)) as client:
        with client.websocket_connect("/ws/duplex?session_id=s3") as ws:
            ready = ws.receive_json()
            token = ready["screen"]["token"]
            with client.websocket_connect("/ws/screen?session_id=s3&token=nope") as bad:
                assert bad.receive_json()["type"] == "error"
            with client.websocket_connect(
                f"/ws/screen?session_id=s3&token={token}"
            ) as screen:
                assert screen.receive_json()["type"] == "screen.ready"
                screen.send_json(
                    {
                        "type": "screen.frame",
                        "frame_id": "f1",
                        "captured_at_ms": 1700000000500,
                        "encoding": "png",
                        "video_source": "screen",
                    }
                )
                screen.send_bytes(png)
                accepted = screen.receive_json()
                assert accepted["type"] == "screen.frame.accepted"
                assert accepted["frame_id"] == "f1"
                assert (accepted["width"], accepted["height"]) == (8, 6)
                screen.send_json(
                    {
                        "type": "screen.frame",
                        "frame_id": "f2",
                        "captured_at_ms": 1,
                        "encoding": "png",
                    }
                )
                screen.send_bytes(b"not an image")
                assert screen.receive_json()["type"] == "screen.frame.dropped"
            session = provider.sessions[0]
            assert ("video", (png, "image/png", 1700000000500)) in session.calls
            ws.send_json({"type": "stop"})
            ws.receive_json()


def test_the_native_slot_serves_one_session_at_a_time(tmp_path):
    provider = ScriptedProvider()
    with TestClient(_app(provider, tmp_path)) as client:
        with client.websocket_connect("/ws/duplex?session_id=a") as first:
            assert first.receive_json()["type"] == "ready"
            with client.websocket_connect("/ws/duplex?session_id=b") as second:
                busy = second.receive_json()
                assert busy["type"] == "error" and busy["retry"] is True
            first.send_json({"type": "stop"})
            first.receive_json()
        with client.websocket_connect("/ws/duplex?session_id=b") as third:
            assert third.receive_json()["type"] == "ready"
            third.send_json({"type": "stop"})
            third.receive_json()
    assert [s.session_id for s in provider.sessions] == ["a", "b"]
    assert all(s.closed for s in provider.sessions)


def test_health_reports_the_provider_not_a_thinker(tmp_path):
    with TestClient(_app(ScriptedProvider(), tmp_path)) as client:
        payload = client.get("/health").json()
    assert payload["foreground_provider"] == "venus"
    assert payload["detached_talker"] is False
    assert payload["provider"] == {"status": "ok"}


def test_the_edge_secret_guards_both_sockets(tmp_path):
    from starlette.websockets import WebSocketDisconnect

    with TestClient(_app(ScriptedProvider(), tmp_path, edge_secret="shh")) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/duplex?session_id=x"):
                pass
        with client.websocket_connect(
            "/ws/duplex?session_id=x", headers={"x-gnsis-edge": "shh"}
        ) as ws:
            assert ws.receive_json()["type"] == "ready"
            ws.send_json({"type": "stop"})
            ws.receive_json()
