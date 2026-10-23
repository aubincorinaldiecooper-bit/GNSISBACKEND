"""EdgeGuard keeps a publicly addressable runtime (RunPod) closed to anyone
who did not come through the site, without breaking health checks or the
site's own forwarded traffic."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request, WebSocket
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from gnsis_runtime.edge_guard import EdgeGuard, guard_if_enabled

SECRET = "edge-secret-for-tests"


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.post("/api/asr/transcribe")
    async def transcribe(request: Request) -> dict:
        return {"text": "ran on the server"}

    @app.websocket("/ws/duplex")
    async def duplex(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_json({"type": "ready"})
        await websocket.close()

    return app


@pytest.fixture
def guarded(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("GNSIS_EDGE_SECRET", SECRET)
    return TestClient(EdgeGuard(_app()))


def test_health_stays_open_without_the_secret(guarded: TestClient) -> None:
    assert guarded.get("/health").status_code == 200


def test_other_http_routes_are_refused_without_the_secret(guarded: TestClient) -> None:
    response = guarded.post("/api/asr/transcribe", content=b"\x00\x00")
    assert response.status_code == 403
    assert "ran on the server" not in response.text


def test_wrong_secret_is_refused(guarded: TestClient) -> None:
    response = guarded.post(
        "/api/asr/transcribe", content=b"\x00\x00", headers={"X-GNSIS-Edge": "guess"}
    )
    assert response.status_code == 403


def test_site_forwarded_requests_are_served(guarded: TestClient) -> None:
    response = guarded.post(
        "/api/asr/transcribe", content=b"\x00\x00", headers={"X-GNSIS-Edge": SECRET}
    )
    assert response.status_code == 200
    with guarded.websocket_connect("/ws/duplex", headers={"X-GNSIS-Edge": SECRET}) as ws:
        assert ws.receive_json() == {"type": "ready"}


def test_websocket_without_the_secret_never_reaches_the_app(guarded: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect):
        with guarded.websocket_connect("/ws/duplex") as ws:
            ws.receive_json()


def test_turning_it_on_without_a_secret_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GNSIS_EDGE_ALL_ROUTES", "1")
    monkeypatch.delenv("GNSIS_EDGE_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="GNSIS_EDGE_SECRET is empty"):
        guard_if_enabled(_app())


def test_off_by_default_leaves_the_app_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GNSIS_EDGE_ALL_ROUTES", raising=False)
    app = _app()
    assert guard_if_enabled(app) is app
