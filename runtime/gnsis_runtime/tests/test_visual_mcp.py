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
        "visual_record_attempt",
        "visual_state",
        "visual_reset",
    }
    assert not {tool.name for tool in tools} & {
        "click",
        "type",
        "navigate",
        "execute",
    }


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
