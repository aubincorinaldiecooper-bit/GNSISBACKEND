from __future__ import annotations

import asyncio
import base64
import json
from collections import deque
from dataclasses import dataclass
from typing import Any

import pytest

from gnsis_visual_sdk.browser_host import (
    BrowserHostConfig,
    BrowserHubConnector,
    BrowserHubError,
)
from gnsis_visual_sdk.client import VisualSession


class FakeSocket:
    def __init__(
        self,
        messages: list[dict[str, Any]],
        *,
        action_frames: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.messages = asyncio.Queue[str]()
        self.action_results: dict[str, dict[str, Any]] = {}
        self.action_frames = action_frames or {}
        for message in messages:
            call_id = message.get("call_id")
            if message.get("type") == "browser.action.result" and isinstance(
                call_id,
                str,
            ):
                self.action_results[call_id] = message
            else:
                self.messages.put_nowait(json.dumps(message))
        self.sent: list[dict[str, Any]] = []

    async def recv(self) -> str:
        return await self.messages.get()

    async def send(self, message: str) -> None:
        decoded = json.loads(message)
        self.sent.append(decoded)
        call_id = decoded.get("call_id")
        if decoded.get("type") == "browser.action" and isinstance(call_id, str):
            for frame_message in self.action_frames.pop(call_id, []):
                self.messages.put_nowait(json.dumps(frame_message))
            result = self.action_results.pop(call_id, None)
            if result is not None:
                self.messages.put_nowait(json.dumps(result))


@dataclass
class FakeClient:
    token: str
    decisions: deque[dict[str, Any]]
    closed_session: str | None = None
    recorded: list[tuple[str, str]] | None = None
    task: tuple[str, str, tuple[str, ...]] | None = None

    def create_session(self) -> VisualSession:
        return VisualSession(
            "session-1", "/stream", "stream-token", "planner-token", "v1"
        )

    def set_task(
        self, session_id: str, goal: str, allowed_actions: tuple[str, ...]
    ) -> dict:
        self.task = (session_id, goal, allowed_actions)
        return {}

    def decide(self, session_id: str) -> dict[str, Any]:
        assert session_id == "session-1"
        return self.decisions.popleft()

    def record_attempt(self, session_id: str, decision_id: str) -> dict:
        if self.recorded is None:
            self.recorded = []
        self.recorded.append((session_id, decision_id))
        return {}

    def close_session(self, session_id: str) -> dict:
        self.closed_session = session_id
        return {}

    def close(self) -> None:
        return


class FakeStream:
    def __init__(self) -> None:
        self.frames: list[tuple[str, int, bytes, str, str, dict[str, Any]]] = []
        self.closed = False

    async def send_frame(
        self,
        frame_id: str,
        captured_at_ms: int,
        image: bytes,
        *,
        encoding: str,
        video_source: str,
        metadata: dict[str, Any],
    ) -> dict:
        self.frames.append(
            (frame_id, captured_at_ms, image, encoding, video_source, metadata)
        )
        return {"type": "screen.frame.accepted"}

    async def close(self) -> None:
        self.closed = True


def frame(
    frame_id: str = "frame-1",
    captured_at_ms: int = 1000,
) -> dict[str, Any]:
    return {
        "type": "capture.frame",
        "frame_id": frame_id,
        "captured_at_ms": captured_at_ms,
        "encoding": "jpeg",
        "image_base64": base64.b64encode(b"jpeg").decode(),
        "source": {
            "kind": "browser_tab",
            "tab_id": 17,
            "capture_session_id": "capture-1",
            "width": 1280,
            "height": 720,
        },
    }


def test_browser_connector_streams_decides_executes_and_records() -> None:
    socket = FakeSocket(
        [
            {"type": "ready", "session_id": "hub-1"},
            {
                "type": "capture.started",
                "capture_session_id": "capture-1",
                "tab_id": 17,
            },
            frame(),
            {
                "type": "browser.action.result",
                "call_id": "decision-1",
                "frame_id": "frame-1",
                "success": True,
                "done": True,
                "message": "Task is complete.",
                "evidence": {},
            },
        ],
        action_frames={"decision-1": [frame("frame-2", 1500)]},
    )
    decisions = deque(
        [
            {
                "decision_id": "decision-1",
                "decision": {
                    "action": "done",
                    "confidence": 0.99,
                    "frame_id": "frame-1",
                },
            }
        ]
    )
    clients: dict[str, FakeClient] = {}

    def client_factory(token: str) -> FakeClient:
        client = FakeClient(token, decisions)
        clients[token] = client
        return client

    stream = FakeStream()
    connector = BrowserHubConnector(
        BrowserHostConfig(
            base_url="http://127.0.0.1:8765",
            host_token="host-token",
            task="Finish the visible task",
        ),
        client_factory=client_factory,
        stream_factory=lambda _session: asyncio.sleep(0, result=stream),
    )

    result = asyncio.run(connector.run(socket))

    assert result.success is True
    assert result.steps == 1
    assert stream.frames == [
        (
            "frame-1",
            1000,
            b"jpeg",
            "jpeg",
            "screen",
            {
                "source_kind": "browser_tab",
                "source_tab_id": 17,
                "capture_session_id": "capture-1",
                "source_width": 1280,
                "source_height": 720,
            },
        ),
        (
            "frame-2",
            1500,
            b"jpeg",
            "jpeg",
            "screen",
            {
                "source_kind": "browser_tab",
                "source_tab_id": 17,
                "capture_session_id": "capture-1",
                "source_width": 1280,
                "source_height": 720,
            },
        ),
    ]
    action = next(
        message for message in socket.sent if message["type"] == "browser.action"
    )
    assert action["frame_id"] == "frame-1"
    assert action["source_tab_id"] == 17
    assert action["decision"]["viewport"] == {"width": 1280, "height": 720}
    assert action["authority"]["provenance"] == "mixed"
    assert clients["host-token"].recorded == [("session-1", "decision-1")]
    assert clients["host-token"].closed_session == "session-1"
    assert stream.closed is True
    assert [message["type"] for message in socket.sent].count("capture.start") == 1
    assert (
        next(message for message in socket.sent if message["type"] == "capture.start")[
            "fps"
        ]
        == 4
    )
    assert [message["type"] for message in socket.sent].count("capture.stop") == 1


def test_browser_connector_rejects_disallowed_decision() -> None:
    socket = FakeSocket(
        [
            {"type": "ready", "session_id": "hub-1"},
            frame(),
        ]
    )
    decisions = deque(
        [
            {
                "decision_id": "decision-1",
                "decision": {
                    "action": "recover",
                    "confidence": 1,
                    "frame_id": "frame-1",
                },
            }
        ]
    )
    stream = FakeStream()
    connector = BrowserHubConnector(
        BrowserHostConfig(
            base_url="http://127.0.0.1:8765",
            host_token="host-token",
            task="Recover",
        ),
        client_factory=lambda token: FakeClient(token, decisions),
        stream_factory=lambda _session: asyncio.sleep(0, result=stream),
    )

    with pytest.raises(BrowserHubError, match="disallowed action"):
        asyncio.run(connector.run(socket))

    assert all(message["type"] != "browser.action" for message in socket.sent)


def test_browser_connector_closes_persistent_capture_after_failure() -> None:
    socket = FakeSocket(
        [
            {"type": "ready", "session_id": "hub-1"},
            {
                "type": "capture.stopped",
                "reason": "failed",
                "message": "selected tab is no longer available",
            },
        ]
    )
    decisions: deque[dict[str, Any]] = deque()
    clients: dict[str, FakeClient] = {}

    def client_factory(token: str) -> FakeClient:
        client = FakeClient(token, decisions)
        clients[token] = client
        return client

    stream = FakeStream()
    connector = BrowserHubConnector(
        BrowserHostConfig(
            base_url="http://127.0.0.1:8765",
            host_token="host-token",
            task="Continue browsing",
        ),
        client_factory=client_factory,
        stream_factory=lambda _session: asyncio.sleep(0, result=stream),
    )

    with pytest.raises(BrowserHubError, match="selected tab"):
        asyncio.run(connector.run(socket))

    assert [message["type"] for message in socket.sent].count("capture.start") == 1
    assert [message["type"] for message in socket.sent].count("capture.stop") == 1
    assert clients["host-token"].closed_session == "session-1"
    assert stream.closed is True


def test_browser_host_config_rejects_unbounded_actions() -> None:
    with pytest.raises(ValueError, match="unsupported browser action"):
        BrowserHostConfig(
            base_url="http://127.0.0.1:8765",
            host_token="host-token",
            task="Do something",
            allowed_actions=("click", "open_url"),
        )
