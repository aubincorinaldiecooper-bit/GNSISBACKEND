from __future__ import annotations

import asyncio
import io
import json
import urllib.error

import pytest

from gnsis_runtime.visual_mcp import (
    VisualAPIClient,
    VisualMCPConfig,
    build_server,
)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


def _config() -> VisualMCPConfig:
    return VisualMCPConfig(
        api_base="https://visual.example",
        api_token="secret-token",
        session_id="session-1",
    )


def test_official_mcp_server_exposes_only_non_actuating_visual_tools() -> None:
    server = build_server(_config())
    tools = asyncio.run(server.list_tools())

    assert {tool.name for tool in tools} == {
        "visual_set_task",
        "visual_decide",
        "visual_perceive",
        "visual_state",
        "visual_reset",
    }
    assert not {tool.name for tool in tools} & {
        "click",
        "type",
        "navigate",
        "execute",
    }
    assert not hasattr(VisualAPIClient(_config()), "record_attempt")


@pytest.mark.parametrize(
    "base_url",
    ["http://visual.example", "http://192.168.1.2:8790", "ftp://visual.example"],
)
def test_mcp_config_rejects_insecure_transports(monkeypatch, base_url) -> None:
    monkeypatch.setenv("GNSIS_VISUAL_API_BASE", base_url)
    monkeypatch.setenv("GNSIS_VISUAL_API_TOKEN", "planner-token")
    monkeypatch.setenv("GNSIS_VISUAL_SESSION_ID", "session-1")

    with pytest.raises(RuntimeError, match="requires HTTPS"):
        VisualMCPConfig.from_env()


@pytest.mark.parametrize(
    "base_url",
    ["http://localhost:8790", "http://127.0.0.1:8790", "http://[::1]:8790"],
)
def test_mcp_config_allows_loopback_http(monkeypatch, base_url) -> None:
    monkeypatch.setenv("GNSIS_VISUAL_API_BASE", base_url)
    monkeypatch.setenv("GNSIS_VISUAL_API_TOKEN", "planner-token")
    monkeypatch.setenv("GNSIS_VISUAL_SESSION_ID", "session-1")

    assert VisualMCPConfig.from_env().api_base == base_url


def test_mcp_config_allows_remote_https(monkeypatch) -> None:
    monkeypatch.setenv("GNSIS_VISUAL_API_BASE", "https://visual.example")
    monkeypatch.setenv("GNSIS_VISUAL_API_TOKEN", "planner-token")
    monkeypatch.setenv("GNSIS_VISUAL_SESSION_ID", "session-1")

    assert VisualMCPConfig.from_env().api_base == "https://visual.example"


def test_mcp_client_forwards_session_and_auth_without_returning_credentials(
    monkeypatch,
) -> None:
    captured = {}

    def urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["authorization"] = request.headers["Authorization"]
        captured["timeout"] = timeout
        return FakeResponse(
            json.dumps(
                {
                    "request_id": "request-1",
                    "decision_id": "decision-1",
                    "decision": {"action": "wait", "confidence": 0.8},
                }
            ).encode()
        )

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    result = VisualAPIClient(_config()).decide("request-1")

    assert captured["url"].endswith("/v1/visual/sessions/session-1/decisions")
    assert captured["authorization"] == "Bearer secret-token"
    assert "secret-token" not in json.dumps(result)


def test_mcp_client_requests_task_independent_perception(monkeypatch) -> None:
    captured = {}

    def urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data)
        return FakeResponse(
            json.dumps(
                {
                    "request_id": "perception-1",
                    "perception": {
                        "summary": "A browser page is visible.",
                        "frame_id": "frame-1",
                    },
                }
            ).encode()
        )

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    result = VisualAPIClient(_config()).perceive(
        "perception-1",
        "What is inside the marker?",
        (175, 387),
    )

    assert captured["url"].endswith("/v1/visual/sessions/session-1/perceptions")
    assert captured["body"] == {
        "request_id": "perception-1",
        "focus": "What is inside the marker?",
        "target": {"x": 175, "y": 387},
    }
    assert result["perception"]["frame_id"] == "frame-1"


def test_mcp_client_surfaces_structured_api_errors(monkeypatch) -> None:
    body = io.BytesIO(
        json.dumps(
            {"error": {"code": "task_required", "message": "set a task"}}
        ).encode()
    )
    error = urllib.error.HTTPError(
        "https://visual.example",
        409,
        "Conflict",
        {},
        body,
    )

    def urlopen(request, timeout):
        raise error

    monkeypatch.setattr("urllib.request.urlopen", urlopen)

    with pytest.raises(RuntimeError, match="task_required"):
        VisualAPIClient(_config()).decide("request-1")
