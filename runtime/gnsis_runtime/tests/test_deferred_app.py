from __future__ import annotations

import threading
import time

from fastapi import FastAPI, WebSocket
from starlette.testclient import TestClient

from gnsis_runtime.deferred_app import create_deferred_app


def _inner_app() -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"status": "ok", "inner": True}

    @app.websocket("/ws/duplex")
    async def duplex(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_json(
            {"type": "ready", "session_id": "cold-start-handoff"}
        )
        try:
            await websocket.receive()
        except Exception:
            pass

    return app


def _wait_for_state(client: TestClient, wanted: str, timeout: float = 2.0) -> dict:
    deadline = time.monotonic() + timeout
    payload = {}
    while time.monotonic() < deadline:
        payload = client.get("/health").json()
        if payload["runtime_state"] == wanted:
            return payload
        time.sleep(0.01)
    raise AssertionError(f"runtime never reached {wanted!r}: {payload!r}")


def test_http_server_is_available_while_model_load_is_blocked():
    release = threading.Event()

    def loader():
        release.wait(2.0)
        return _inner_app()

    app = create_deferred_app(loader, heartbeat_interval_s=0.05)
    try:
        with TestClient(app) as client:
            health = client.get("/health")
            assert health.status_code == 200
            payload = health.json()
            assert payload["status"] == "loading"
            assert payload["runtime_state"] == "loading"
            assert payload["startup"]["server_started_unix_ms"] is not None
            assert payload["startup"]["model_load_started_unix_ms"] is not None
            # The public surface exists before the model does.
            assert client.get("/live").status_code == 200

            release.set()
            ready = _wait_for_state(client, "ready")
            assert ready["status"] == "ok"
            assert ready["inner"] is True
            assert ready["startup"]["model_load_ms"] is not None
            assert ready["startup"]["server_to_runtime_ready_ms"] is not None
    finally:
        release.set()


def test_duplex_socket_connects_before_model_is_ready_and_is_handed_off():
    release = threading.Event()

    def loader():
        release.wait(2.0)
        return _inner_app()

    app = create_deferred_app(loader, heartbeat_interval_s=0.05)
    try:
        with TestClient(app) as client:
            with client.websocket_connect("/ws/duplex") as websocket:
                loading = websocket.receive_json()
                assert loading == {"type": "runtime.status", "status": "loading"}

                release.set()

                message = websocket.receive_json()
                while message.get("type") == "runtime.status":
                    message = websocket.receive_json()
                assert message["type"] == "ready"
                assert message["session_id"] == "cold-start-handoff"

                health = _wait_for_state(client, "ready")
                assert (
                    health["startup"]["last_websocket_connected_unix_ms"] is not None
                )
                assert (
                    health["startup"]["last_session_inference_started_unix_ms"]
                    is not None
                )
    finally:
        release.set()


def test_model_load_failure_stays_observable_instead_of_killing_server():
    def loader():
        raise RuntimeError("weights are corrupt")

    app = create_deferred_app(loader, heartbeat_interval_s=0.05)
    with TestClient(app) as client:
        failed = _wait_for_state(client, "failed")
        assert failed["status"] == "failed"
        assert failed["runtime_state"] == "failed"
        assert "weights are corrupt" in failed["load_error"]

        with client.websocket_connect("/ws/duplex") as websocket:
            message = websocket.receive_json()
            # Depending on scheduling the connection can observe failed
            # immediately, without a preceding loading heartbeat.
            if message.get("type") == "runtime.status":
                message = websocket.receive_json()
            assert message["type"] == "error"
            assert message["fatal"] is True
            assert "weights are corrupt" in message["message"]
