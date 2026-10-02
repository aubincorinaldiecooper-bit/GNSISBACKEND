from __future__ import annotations

import asyncio
import io
import json
import os
import selectors
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from PIL import Image

from gnsis_visual_sdk import (
    FrameStream,
    VisualClient,
    VisualServiceError,
    VisualSession,
)


@pytest.fixture(scope="session")
def fixture_base_url() -> str:
    repo_root = Path(__file__).resolve().parents[3]
    fixture_path = repo_root / "sdks" / "testing" / "fixture_server.py"
    env = os.environ.copy()
    runtime_path = str(repo_root / "runtime" / "gnsis_runtime")
    env["PYTHONPATH"] = os.pathsep.join(
        [runtime_path, env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    process = subprocess.Popen(
        [sys.executable, str(fixture_path)],
        cwd=repo_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("visual API fixture exited before becoming ready")
            if selector.select(timeout=0.1):
                line = process.stdout.readline().strip()
                if line.startswith("READY "):
                    port = int(line.removeprefix("READY "))
                    yield f"http://127.0.0.1:{port}"
                    return
                raise RuntimeError(f"unexpected fixture server output: {line!r}")
        raise RuntimeError("visual API fixture did not become ready")
    finally:
        selector.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        process.stdout.close()


def _jpeg() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (16, 12), "white").save(output, format="JPEG")
    return output.getvalue()


def test_decide_retries_with_the_same_request_id(monkeypatch) -> None:
    requests: list[httpx.Request] = []
    delays: list[float] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                503,
                json={"error": {"code": "temporarily_unavailable", "message": "retry"}},
            )
        return httpx.Response(
            200,
            json={
                "request_id": "stable-request",
                "decision_id": "decision-1",
                "decision": {"action": "wait"},
                "gate": {"status": "act"},
                "current": True,
            },
        )

    monkeypatch.setattr("gnsis_visual_sdk.client.time.sleep", delays.append)
    client = VisualClient(
        "https://visual.example",
        "api-secret",
        max_retries=2,
        transport=httpx.MockTransport(respond),
    )
    try:
        result = client.decide("session-1", "stable-request")
    finally:
        client.close()

    assert result["request_id"] == "stable-request"
    assert len(requests) == 2
    assert [json.loads(request.content)["request_id"] for request in requests] == [
        "stable-request",
        "stable-request",
    ]
    assert delays == [0.2]


def test_record_attempt_does_not_retry_after_503(monkeypatch) -> None:
    requests: list[httpx.Request] = []
    delays: list[float] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            503,
            json={"error": {"code": "temporarily_unavailable", "message": "retry"}},
        )

    monkeypatch.setattr("gnsis_visual_sdk.client.time.sleep", delays.append)
    client = VisualClient(
        "https://visual.example",
        "api-secret",
        max_retries=3,
        transport=httpx.MockTransport(respond),
    )
    try:
        try:
            client.record_attempt("session-1", "decision-1")
        except VisualServiceError as exc:
            assert exc.code == "temporarily_unavailable"
            assert exc.status_code == 503
            assert exc.retryable is True
            assert "api-secret" not in str(exc)
        else:
            raise AssertionError("expected a VisualServiceError")
    finally:
        client.close()

    assert len(requests) == 1
    assert delays == []


@pytest.mark.parametrize("base_url", ["http://visual.example", "http://10.0.0.2"])
def test_visual_client_rejects_insecure_remote_http(base_url: str) -> None:
    with pytest.raises(VisualServiceError) as insecure:
        VisualClient(base_url, "api-secret")
    assert insecure.value.code == "insecure_transport"


@pytest.mark.parametrize(
    "base_url",
    ["http://localhost:8790", "http://127.0.0.1:8790", "http://[::1]:8790"],
)
def test_visual_client_allows_loopback_http(base_url: str) -> None:
    client = VisualClient(base_url, "api-secret")
    client.close()


def test_frame_stream_rejects_insecure_remote_http() -> None:
    session = VisualSession(
        "session-1",
        "/stream",
        "stream-secret",
        "planner-secret",
        "screen-frame-v1",
    )

    with pytest.raises(VisualServiceError) as insecure:
        asyncio.run(FrameStream.connect("http://visual.example", session))
    assert insecure.value.code == "insecure_transport"


def test_frame_stream_allows_loopback_http(monkeypatch) -> None:
    connected_urls: list[str] = []

    class FakeWebSocket:
        async def close(self) -> None:
            return None

    async def connect(url: str) -> FakeWebSocket:
        connected_urls.append(url)
        return FakeWebSocket()

    monkeypatch.setattr("gnsis_visual_sdk.stream.websockets.connect", connect)
    session = VisualSession(
        "session-1",
        "/stream",
        "stream-secret",
        "planner-secret",
        "screen-frame-v1",
    )

    async def connect_loopback() -> None:
        stream = await FrameStream.connect("http://[::1]:8790", session)
        await stream.close()

    asyncio.run(connect_loopback())
    assert connected_urls[0].startswith("ws://[::1]:8790/stream?")


def test_real_api_lifecycle_and_credential_redaction(
    fixture_base_url: str,
) -> None:
    client = VisualClient(fixture_base_url, "sdk-test-host-token")
    bad_client = VisualClient(fixture_base_url, "bad-api-token")
    planner_client: VisualClient | None = None
    try:
        assert client.health()["status"] == "ok"
        with pytest.raises(VisualServiceError) as unauthorized:
            bad_client.state("missing")
        assert unauthorized.value.code == "unauthorized"
        assert "bad-api-token" not in str(unauthorized.value)

        session = client.create_session()
        assert session.stream_token not in repr(session)
        assert session.planner_token not in repr(session)
        planner_client = VisualClient(fixture_base_url, session.planner_token)

        async def stream_frames() -> None:
            stream = await FrameStream.connect(fixture_base_url, session)
            try:
                accepted = await stream.send_frame(
                    "sdk-frame-1",
                    1_000,
                    _jpeg(),
                )
                assert accepted["type"] == "screen.frame.accepted"
                assert accepted["frame_seq"] == 1
                with pytest.raises(VisualServiceError) as duplicate:
                    await stream.send_frame(
                        "sdk-frame-1",
                        1_001,
                        _jpeg(),
                    )
                assert duplicate.value.code == "replayed_frame"
            finally:
                await stream.close()

        asyncio.run(stream_frames())

        planner_client.set_task(session.session_id, "click the control", ["click"])
        first = planner_client.decide(session.session_id, "sdk-request-1")
        repeated = planner_client.decide(session.session_id, "sdk-request-1")
        assert first["decision_id"] == repeated["decision_id"]
        assert first["decision"]["action"] == "click"
        assert first["decision"]["frame_id"] == "sdk-frame-1"

        recorded = client.record_attempt(
            session.session_id,
            first["decision_id"],
        )
        assert recorded["recorded"] is True
        with pytest.raises(VisualServiceError) as duplicate_attempt:
            client.record_attempt(session.session_id, first["decision_id"])
        assert duplicate_attempt.value.code == "unknown_decision"

        state = planner_client.state(session.session_id)
        assert any(item["action"] == "click" for item in state["history"])
        assert planner_client.reset_task(session.session_id)["goal"] is None
        assert client.close_session(session.session_id)["closed"] is True
        with pytest.raises(VisualServiceError) as closed:
            planner_client.state(session.session_id)
        assert closed.value.code == "unauthorized"
        with pytest.raises(VisualServiceError) as host_closed:
            client.state(session.session_id)
        assert host_closed.value.code == "unknown_session"
    finally:
        client.close()
        bad_client.close()
        if planner_client is not None:
            planner_client.close()
