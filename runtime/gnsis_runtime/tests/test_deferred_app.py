from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi import FastAPI, WebSocket
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

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
        # The class name, not the exception text: /health is public on this
        # runtime's own address, and an exception string carries file paths.
        assert failed["load_error"] == "RuntimeError"
        assert "weights are corrupt" not in json.dumps(failed)

        with client.websocket_connect("/ws/duplex") as websocket:
            message = websocket.receive_json()
            # Depending on scheduling the connection can observe failed
            # immediately, without a preceding loading heartbeat.
            if message.get("type") == "runtime.status":
                message = websocket.receive_json()
            assert message["type"] == "error"
            assert message["fatal"] is True
            assert message["message"] == "runtime failed to load (RuntimeError)"
            assert "weights are corrupt" not in message["message"]


def test_a_cold_start_socket_without_the_edge_secret_is_refused(monkeypatch):
    """The front-door check has to hold during the load, not just after it.

    The real app makes this check too, but it does not exist yet while the
    model is loading — so without it here, a caller who found this runtime's
    public address held an accepted socket for the whole cold start and was
    only turned away at handoff.
    """

    monkeypatch.setenv("GNSIS_EDGE_SECRET", "front-door-secret")
    release = threading.Event()

    def loader():
        release.wait(2.0)
        return _inner_app()

    app = create_deferred_app(loader, heartbeat_interval_s=0.05)
    try:
        with TestClient(app) as client:
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect("/ws/duplex"):
                    pass
            # The same socket, with the header, is served as before.
            with client.websocket_connect(
                "/ws/duplex", headers={"X-GNSIS-Edge": "front-door-secret"}
            ) as ws:
                assert ws.receive_json() == {
                    "type": "runtime.status",
                    "status": "loading",
                }
    finally:
        release.set()


def test_a_non_ascii_edge_secret_does_not_crash_the_check(monkeypatch):
    """compare_digest refuses str operands holding non-ASCII — even equal ones.

    Compared as text this raised whenever EITHER side was non-ASCII, so an
    operator who put an accent in the secret broke every request, including
    ordinary ones from the real site. Compared as bytes both a wrong header
    and the right one are ordinary outcomes.
    """

    monkeypatch.setenv("GNSIS_EDGE_SECRET", "clé-de-la-porte")
    release = threading.Event()

    def loader():
        release.wait(2.0)
        return _inner_app()

    app = create_deferred_app(loader, heartbeat_interval_s=0.05)
    try:
        with TestClient(app) as client:
            # A plain ASCII header against the non-ASCII secret. This is the
            # case any real client produces, and it used to raise.
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(
                    "/ws/duplex", headers={"X-GNSIS-Edge": "wrong"}
                ):
                    pass
            # The matching secret, as the bytes a client actually puts on the
            # wire, is still served.
            with client.websocket_connect(
                "/ws/duplex",
                headers={"X-GNSIS-Edge": "clé-de-la-porte".encode("utf-8")},
            ) as ws:
                assert ws.receive_json()["status"] == "loading"
    finally:
        release.set()


def test_registered_app_hooks_run_around_the_inner_app():
    """Starlette 1.6 has no `router.startup()`; handlers live on `on_startup`.

    Reading the old attribute meant the real app's startup hooks — the
    static-prefix preparation among them — were never run: production sat at
    `prefix_cache_status: pending` and every session prefilled the system
    prompt live.
    """

    calls: list[str] = []

    def loader():
        app = _inner_app()

        async def startup():
            calls.append("startup")

        def shutdown():
            calls.append("shutdown")

        app.router.add_event_handler("startup", startup)
        app.router.add_event_handler("shutdown", shutdown)
        return app

    app = create_deferred_app(loader, heartbeat_interval_s=0.05)
    with TestClient(app) as client:
        _wait_for_state(client, "ready")
        assert calls == ["startup"]
    assert calls == ["startup", "shutdown"]
