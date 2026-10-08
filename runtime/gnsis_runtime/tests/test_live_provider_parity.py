"""One live runtime, two foreground models.

`realtime.provider` chooses who answers the model calls: the Thinker loaded in
process, or a native full-duplex provider behind `RealtimeSession`. Everything
the desktop Host talks to - sockets, lifecycle, coordinator, tool routing,
harness bridge, memory, timeline, delivery gate, playback acks - is the same
code. These tests drive both through the same harness and the same client
protocol, so a behaviour the Thinker path has and the native path lacks fails
here rather than in a browser.
"""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from gnsis_runtime.realtime_provider import ProviderEvent
from test_duplex_lifecycle import (
    _drain_until,
    _expect,
    _jpeg,
    _open_screen,
    _read_rejection,
    _screen_header,
    _settle,
    _stop,
    _wait_resumable,
    connect,
)

PROVIDERS = ("thinker", "venus")
ONE_UNIT = b"\x00\x00" * 16000
DESKTOP = "/ws/duplex?session_id=s1&host_tools=files,open&host_tools_version=desktop-v4"


def _audio_header(sequence: int, captured_at_ms: int) -> dict:
    return {
        "type": "audio.frame",
        "sequence": sequence,
        "start_sample": (sequence - 1) * 16000,
        "sample_count": 16000,
        "captured_at_ms": captured_at_ms,
    }


def _emit(client, session, event: ProviderEvent) -> None:
    """Hand the scripted provider an event on the server's loop."""

    client.portal.call(session.events.put, event)


def _timeline_kinds(h, session_id: str = "s1") -> list[str]:
    (log,) = h.media_dir.glob(f"{session_id}-*.timeline.jsonl")
    return [json.loads(line)["kind"] for line in log.read_text().splitlines()]


# --- the same app, the same lifecycle --------------------------------------


@pytest.mark.parametrize("foreground", PROVIDERS)
def test_ready_says_which_model_and_nothing_else_differs(harness, foreground):
    h = harness(foreground=foreground)
    with TestClient(h.app) as client:
        health = client.get("/health").json()
        with connect(client, "/ws/duplex?session_id=s1") as ws:
            ready = _settle(ws)
            _stop(ws)
    assert ready["foreground_provider"] == foreground
    assert health["foreground_provider"] == foreground
    assert ready["tool_call_protocol"] == "call_id_v1"
    assert ready["task_protocol"] == "task_tools_v1"
    assert ready["context_events"] == ["turn.final", "task_status", "screen"]
    assert ready["resume_token"]
    # Both builds hand the session to the same coordinator.
    assert len(h.coordinators) == 1
    assert h.coordinators[0].started == 1
    assert h.coordinators[0].closed == 1
    if foreground == "venus":
        assert h.thinkers == [], "a native provider must not open a Thinker"
        assert [s.closed for s in h.native.sessions] == [True]
        assert h.native.sessions[0].config.system_prompt == "native prompt"
    else:
        assert len(h.thinkers) == 1 and h.thinkers[0].closed


@pytest.mark.parametrize("foreground", PROVIDERS)
def test_the_slot_is_one_session_at_a_time_for_either_model(harness, foreground):
    h = harness(foreground=foreground)
    with TestClient(h.app) as client:
        with connect(client, "/ws/duplex?session_id=a") as first:
            _settle(first)
            busy = _read_rejection(client, "/ws/duplex?session_id=b")
            assert busy["type"] == "error" and busy["retry"] is True
            _stop(first)
        with connect(client, "/ws/duplex?session_id=b") as third:
            _settle(third)
            _stop(third)


@pytest.mark.parametrize("foreground", PROVIDERS)
def test_a_dropped_socket_resumes_the_same_model_session(harness, foreground):
    h = harness(foreground=foreground, reconnect_grace_sec=5.0)
    with TestClient(h.app) as client:
        with connect(client, "/ws/duplex?session_id=s1") as ws:
            token = _settle(ws)["resume_token"]
        _wait_resumable(client, "s1")
        with connect(client, f"/ws/duplex?session_id=s1&resume_token={token}") as ws:
            again = _drain_until(ws, "ready")
            assert again["resumed"] is True
            _stop(ws)
    opened = len(h.thinkers) if foreground == "thinker" else len(h.native.sessions)
    assert opened == 1, "a resume must not open a second model session"


@pytest.mark.parametrize("foreground", PROVIDERS)
def test_a_session_expires_the_same_way_for_either_model(harness, foreground):
    h = harness(foreground=foreground, max_session_sec=1.0)
    with TestClient(h.app) as client:
        with connect(client, "/ws/duplex?session_id=s1") as ws:
            _settle(ws)
            ended = _expect(ws, "error")
    assert ended["fatal"] is True and "time limit" in ended["message"]
    if foreground == "venus":
        assert h.native.sessions[0].closed


# --- what reaches the native model -----------------------------------------


def test_audio_and_breaks_reach_the_native_model_with_their_timing(harness):
    h = harness(foreground="venus", provider_name=None, real_coordinator=True)
    with TestClient(h.app) as client:
        with connect(client, "/ws/duplex?session_id=s1") as ws:
            _settle(ws)
            session = h.native.sessions[0]
            ws.send_text(json.dumps(_audio_header(1, 1_700_000_000_000)))
            ws.send_bytes(ONE_UNIT)
            ws.send_text(json.dumps({"type": "ping", "id": 7}))
            assert _drain_until(ws, "pong")["id"] == 7
            ws.send_text(json.dumps({"type": "break", "reason": "user_spoke"}))
            _drain_until(ws, "break.done")
            _stop(ws)
    audio = session.calls_of("audio")
    assert audio == [(ONE_UNIT, 1_700_000_000_000)]
    assert session.calls_of("cancel") == ["client_break"]
    assert "user.interruption" in _timeline_kinds(h)


def test_screen_frames_reach_the_native_model_through_the_bounded_history(harness):
    h = harness(foreground="venus", media_mode="omni", provider_name=None)
    with TestClient(h.app) as client:
        with connect(client, "/ws/duplex?session_id=s1") as ws:
            with _open_screen(client, ws) as screen:
                _drain_until(screen, "screen.ready")
                screen.send_text(json.dumps(_screen_header("f1")))
                screen.send_bytes(_jpeg())
                accepted = _drain_until(screen, "screen.frame.accepted")
                assert accepted["frame_id"] == "f1"
                screen.send_text(json.dumps(_screen_header("f2")))
                screen.send_bytes(b"not an image")
                assert _drain_until(screen, "screen.frame.dropped")["frame_id"] == "f2"
            _stop(ws)
    session = h.native.sessions[0]
    (frame,) = session.calls_of("video")
    data, mime, ts_ms = frame
    assert mime == "image/jpeg" and data[:2] == b"\xff\xd8"
    assert ts_ms == 1_000


def test_native_speech_arrives_as_desktop_audio_and_is_cancelled_by_interrupts(harness):
    h = harness(foreground="venus", provider_name=None)
    with TestClient(h.app) as client:
        with connect(client, "/ws/duplex?session_id=s1") as ws:
            _settle(ws)
            session = h.native.sessions[0]
            _emit(
                client,
                session,
                ProviderEvent(
                    kind="audio",
                    payload={"pcm16": b"\x02\x00" * 10, "turn_finished": False},
                    epoch=5,
                    correlation_id="out-5",
                ),
            )
            chunk = _expect(ws, "audio.chunk")
            assert chunk["audio_sample_rate"] == 24000 and chunk["audio_bytes"] == 20
            assert ws.receive_bytes() == b"\x02\x00" * 10
            generation = chunk["generation_id"]
            _emit(
                client,
                session,
                ProviderEvent(
                    kind="audio",
                    payload={"pcm16": b"", "turn_finished": True},
                    epoch=5,
                    correlation_id="out-5",
                ),
            )
            done = _expect(ws, "audio.done")
            assert done["generation_id"] == generation and done["end_of_turn"] is True

            ws.send_text(
                json.dumps(
                    {
                        "type": "playback.ack",
                        "playback_id": "out-5",
                        "phase": "finished",
                        "chunks_played": 1,
                    }
                )
            )
            _expect(ws, "playback.ack.done")

            _emit(
                client,
                session,
                ProviderEvent(kind="interrupt", payload={"reason": "user"}, epoch=6),
            )
            cancel = _expect(ws, "playback.cancel")
            assert cancel["cancelled_generation_id"] == generation
            assert cancel["generation_id"] > generation
            _stop(ws)
    assert ("ack", ("out-5", 1)) in session.calls


def test_a_native_tool_call_is_correlated_answered_once_and_replay_safe(harness):
    """The Host contract the Thinker path already honours, on the native path."""

    from gnsis_runtime import host_tools as host_tools_module

    h = harness(
        foreground="venus",
        provider_name=None,
        real_coordinator=True,
        host_tool_catalog=host_tools_module.load_host_tool_catalog(
            "runtime/configs/gnsis-host-tools.json"
        ),
    )
    with TestClient(h.app) as client:
        with connect(client, DESKTOP) as ws:
            ready = _settle(ws)
            assert ready["host_tools"]["accepted"] == ["open", "files"]
            session = h.native.sessions[0]
            offered = [tool["name"] for tool in session.config.extra["tools"]]
            assert {"open", "files"} <= set(offered)

            _emit(
                client,
                session,
                ProviderEvent(
                    kind="tool_call",
                    payload={
                        "name": "files",
                        "arguments": {
                            "action": "move",
                            "path": "report.pdf",
                            "to": "Projects",
                        },
                    },
                    epoch=1,
                    correlation_id="native-1",
                ),
            )
            call = _expect(ws, "tool.call")
            call_id = call["call_id"]
            assert call_id.startswith("call_") and call["dispatch"] == "client"
            assert call["tool_calls"] == [
                {
                    "name": "files",
                    "arguments": {
                        "action": "move",
                        "path": "report.pdf",
                        "to": "Projects",
                    },
                }
            ]

            ws.send_text(
                json.dumps(
                    {"type": "tool.response", "call_id": "call_other", "content": {}}
                )
            )
            assert _expect(ws, "tool.response.stale")["reason"] == "not_pending"
            assert session.calls_of("control") == []

            result = {"status": "done", "moved": "report.pdf"}
            ws.send_text(
                json.dumps(
                    {"type": "tool.response", "call_id": call_id, "content": result}
                )
            )
            assert _expect(ws, "tool.response.queued")["call_id"] == call_id
            assert session.calls_of("control") == [
                {"kind": "tool_response", "response": result}
            ]

            ws.send_text(
                json.dumps(
                    {"type": "tool.response", "call_id": call_id, "content": result}
                )
            )
            assert _expect(ws, "tool.response.stale")["reason"] == "already_finished"
            assert len(session.calls_of("control")) == 1
            _stop(ws)
    tools = [
        (event.kind, event.correlation_id)
        for event in h.coordinators[0].timeline.snapshot()
        if event.component == "tools"
    ]
    assert tools == [
        ("tool.requested", call_id),
        ("tool.response.received", call_id),
        ("tool.response.injected", call_id),
    ]


def test_native_text_is_a_chunk_the_coordinator_saw(harness):
    h = harness(foreground="venus", provider_name=None, real_coordinator=True)
    with TestClient(h.app) as client:
        with connect(client, "/ws/duplex?session_id=s1") as ws:
            _settle(ws)
            session = h.native.sessions[0]
            _emit(
                client,
                session,
                ProviderEvent(
                    kind="text",
                    payload={"text": "Done, it is in Projects.", "turn_finished": True},
                    epoch=1,
                ),
            )
            chunk = _expect(ws, "chunk")
            assert chunk["text"] == "Done, it is in Projects."
            assert chunk["end_of_turn"] is True
            assert chunk["metrics"]["provider"] == "venus"
            _stop(ws)
    coordinator = h.coordinators[0]
    replies = [
        entry
        for entry in coordinator.gateway.ledger.list_realtime_context(
            coordinator.owner_id
        )
        if entry.kind == "frontbrain_reply"
    ]
    assert [entry.text for entry in replies] == ["Done, it is in Projects."]
