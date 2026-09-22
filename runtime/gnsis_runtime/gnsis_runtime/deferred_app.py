from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import secrets
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

LOGGER = logging.getLogger(__name__)
_PROCESS_STARTED_AT = time.time()


def _came_through_the_front_door(scope: dict[str, Any]) -> bool:
    """Whether this socket carries the site's shared edge secret.

    The same check the runtime's own handlers make, read straight off the ASGI
    scope because the real app does not exist yet while the model is loading.
    The value is read from the environment for the same reason: this wrapper is
    built before any config is parsed.

    An empty secret means the check is off — the behaviour before the header
    existed — so the two ends can be configured in either order.

    Compared as bytes, never as text. compare_digest refuses two str operands
    if either holds a non-ASCII character — even when they are equal — so a
    secret with an accent in it would raise on every request and take the live
    page down, and one non-ASCII byte in the header would be an uncaught error
    anyone could trigger.
    """

    expected = os.environ.get("GNSIS_EDGE_SECRET", "")
    if not expected:
        return True
    presented = b""
    for name, value in scope.get("headers") or ():
        if name == b"x-gnsis-edge":
            presented = value
            break
    return secrets.compare_digest(presented, expected.encode("utf-8"))


class DeferredRuntimeApp:
    """Serve immediately while the expensive realtime runtime loads in background.

    The existing GNSIS app remains the source of truth once it is ready. Before
    that happens this wrapper keeps the HTTP surface alive, serves the static
    live page, accepts the duplex websocket, and reports explicit runtime state.

    A websocket accepted while the model is loading is handed into the finished
    FastAPI app without reconnecting. The wrapper replays the ASGI connect event
    to the inner app and suppresses its second accept frame.
    """

    def __init__(
        self,
        loader: Callable[[], Any],
        *,
        heartbeat_interval_s: float = 15.0,
    ) -> None:
        if heartbeat_interval_s <= 0:
            raise ValueError("heartbeat_interval_s must be positive")
        self._loader = loader
        self._heartbeat_interval_s = heartbeat_interval_s
        self._inner: Any | None = None
        self._load_task: asyncio.Task[None] | None = None
        self._ready_event: asyncio.Event | None = None
        self._state = "loading"
        # Two forms on purpose. The full text goes to the container log,
        # where the operator reads it; only the exception's class name leaves
        # the process, because /health and this websocket are both reachable
        # from the public internet and an exception string carries file paths.
        self._error: str | None = None
        self._error_public: str | None = None

        self._server_started_at: float | None = None
        self._model_load_started_at: float | None = None
        self._model_load_completed_at: float | None = None
        self._runtime_ready_at: float | None = None
        self._last_websocket_connected_at: float | None = None
        self._last_session_ready_at: float | None = None

        self._bootstrap = self._build_bootstrap_app()

    @property
    def runtime_state(self) -> str:
        return self._state

    def _event(self) -> asyncio.Event:
        if self._ready_event is None:
            self._ready_event = asyncio.Event()
        return self._ready_event

    def _build_bootstrap_app(self) -> FastAPI:
        from mcpmft.infer.web import STATIC_DIR

        static_dir = Path(STATIC_DIR).resolve()
        app = FastAPI(title="GNSIS Runtime Bootstrap", version="1.0.0")

        @app.get("/health")
        async def health() -> dict[str, Any]:
            return await self.health_payload()

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(
                static_dir / "index.html",
                media_type="text/html",
                headers={"Cache-Control": "no-store"},
            )

        @app.get("/live")
        async def live() -> FileResponse:
            return FileResponse(
                static_dir / "live.html",
                media_type="text/html",
                headers={"Cache-Control": "no-store"},
            )

        @app.get("/assets/{asset_path:path}")
        async def asset(asset_path: str) -> FileResponse:
            path = (static_dir / asset_path).resolve()
            if path != static_dir and static_dir not in path.parents:
                raise HTTPException(status_code=404, detail="not found")
            if not path.is_file():
                raise HTTPException(status_code=404, detail="not found")
            return FileResponse(path, headers={"Cache-Control": "no-store"})

        @app.api_route(
            "/{path:path}",
            methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        )
        async def loading(path: str) -> JSONResponse:
            return JSONResponse(
                {
                    "status": self._state,
                    "runtime_state": self._state,
                    "message": (
                        "GNSIS runtime is loading"
                        if self._state == "loading"
                        else "GNSIS runtime failed to load"
                    ),
                },
                status_code=503,
                headers={"Retry-After": "2"},
            )

        return app

    async def _inner_health(self) -> dict[str, Any]:
        inner = self._inner
        if inner is None:
            return {}
        for route in getattr(inner, "routes", ()):
            if getattr(route, "path", None) != "/health":
                continue
            methods = getattr(route, "methods", None) or set()
            if "GET" not in methods:
                continue
            endpoint = getattr(route, "endpoint", None)
            if endpoint is None:
                continue
            value = endpoint()
            if inspect.isawaitable(value):
                value = await value
            if isinstance(value, dict):
                return dict(value)
        return {}

    def _startup_timings(self) -> dict[str, Any]:
        def unix_ms(value: float | None) -> int | None:
            return round(value * 1000) if value is not None else None

        model_load_ms = None
        if (
            self._model_load_started_at is not None
            and self._model_load_completed_at is not None
        ):
            model_load_ms = round(
                (self._model_load_completed_at - self._model_load_started_at) * 1000
            )

        runtime_ready_ms = None
        if (
            self._server_started_at is not None
            and self._runtime_ready_at is not None
        ):
            runtime_ready_ms = round(
                (self._runtime_ready_at - self._server_started_at) * 1000
            )

        return {
            "process_started_unix_ms": unix_ms(_PROCESS_STARTED_AT),
            "server_started_unix_ms": unix_ms(self._server_started_at),
            "model_load_started_unix_ms": unix_ms(self._model_load_started_at),
            "model_load_completed_unix_ms": unix_ms(self._model_load_completed_at),
            "runtime_ready_unix_ms": unix_ms(self._runtime_ready_at),
            "last_websocket_connected_unix_ms": unix_ms(
                self._last_websocket_connected_at
            ),
            "last_session_inference_started_unix_ms": unix_ms(
                self._last_session_ready_at
            ),
            "model_load_ms": model_load_ms,
            "server_to_runtime_ready_ms": runtime_ready_ms,
        }

    async def health_payload(self) -> dict[str, Any]:
        payload = await self._inner_health() if self._state == "ready" else {}
        if self._state == "loading":
            payload.setdefault("status", "loading")
        elif self._state == "failed":
            payload["status"] = "failed"
        else:
            payload.setdefault("status", "ok")
        payload["runtime_state"] = self._state
        payload["startup"] = self._startup_timings()
        if self._error_public is not None:
            payload["load_error"] = self._error_public
        return payload

    async def _load_runtime(self) -> None:
        started = time.perf_counter()
        self._model_load_started_at = time.time()
        LOGGER.info(
            "runtime startup: model load started unix_ms=%d",
            round(self._model_load_started_at * 1000),
        )
        try:
            inner = await asyncio.to_thread(self._loader)
            self._model_load_completed_at = time.time()
            LOGGER.info(
                "runtime startup: model load completed unix_ms=%d load_ms=%d",
                round(self._model_load_completed_at * 1000),
                round((time.perf_counter() - started) * 1000),
            )

            router = getattr(inner, "router", None)
            startup = getattr(router, "startup", None)
            if startup is not None:
                await startup()

            self._inner = inner
            self._runtime_ready_at = time.time()
            self._state = "ready"
            LOGGER.info(
                "runtime startup: ready unix_ms=%d server_to_ready_ms=%d",
                round(self._runtime_ready_at * 1000),
                round(
                    (
                        self._runtime_ready_at
                        - (self._server_started_at or self._runtime_ready_at)
                    )
                    * 1000
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"
            self._error_public = type(exc).__name__
            self._state = "failed"
            LOGGER.error(
                "runtime startup: model load failed type=%s message=%s "
                "loader=%r elapsed_ms=%d\n%s",
                type(exc).__name__,
                exc,
                self._loader,
                round((time.perf_counter() - started) * 1000),
                traceback.format_exc(),
            )
        finally:
            self._event().set()

    async def _shutdown_inner(self) -> None:
        inner = self._inner
        if inner is None:
            return
        router = getattr(inner, "router", None)
        shutdown = getattr(router, "shutdown", None)
        if shutdown is not None:
            await shutdown()

    async def _lifespan(self, receive: Any, send: Any) -> None:
        while True:
            message = await receive()
            message_type = message.get("type")
            if message_type == "lifespan.startup":
                self._server_started_at = time.time()
                LOGGER.info(
                    "runtime startup: server available unix_ms=%d",
                    round(self._server_started_at * 1000),
                )
                self._load_task = asyncio.create_task(
                    self._load_runtime(),
                    name="gnsis-runtime-background-load",
                )
                # Returning startup.complete is the port-first boundary: Uvicorn
                # can serve HTTP and complete websocket handshakes immediately.
                await send({"type": "lifespan.startup.complete"})
                continue
            if message_type == "lifespan.shutdown":
                if self._load_task is not None and not self._load_task.done():
                    self._load_task.cancel()
                    await asyncio.gather(self._load_task, return_exceptions=True)
                await self._shutdown_inner()
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _loading_duplex(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        first = await receive()
        if first.get("type") != "websocket.connect":
            return

        if not _came_through_the_front_door(scope):
            # The real app checks this too, but only once it exists. Without
            # the same check here, a caller who found this runtime's public
            # address held an accepted socket for the whole cold load and was
            # only turned away at handoff.
            await send({"type": "websocket.close", "code": 1008})
            return

        await send({"type": "websocket.accept"})
        self._last_websocket_connected_at = time.time()
        LOGGER.info(
            "runtime startup: websocket connected unix_ms=%d path=%s",
            round(self._last_websocket_connected_at * 1000),
            scope.get("path"),
        )

        async def send_loading_status() -> None:
            await send(
                {
                    "type": "websocket.send",
                    "text": json.dumps(
                        {
                            "type": "runtime.status",
                            "status": self._state,
                        },
                        separators=(",", ":"),
                    ),
                }
            )

        try:
            await send_loading_status()
            while self._state == "loading":
                try:
                    await asyncio.wait_for(
                        asyncio.shield(self._event().wait()),
                        timeout=self._heartbeat_interval_s,
                    )
                except asyncio.TimeoutError:
                    await send_loading_status()

            if self._state == "failed" or self._inner is None:
                await send(
                    {
                        "type": "websocket.send",
                        "text": json.dumps(
                            {
                                "type": "error",
                                "message": (
                                    "runtime failed to load"
                                    + (
                                        f" ({self._error_public})"
                                        if self._error_public
                                        else ""
                                    )
                                ),
                                "fatal": True,
                            },
                            separators=(",", ":"),
                        ),
                    }
                )
                await send({"type": "websocket.close", "code": 1011})
                return

            replay_connect = True
            suppress_accept = True

            async def inner_receive() -> dict[str, Any]:
                nonlocal replay_connect
                if replay_connect:
                    replay_connect = False
                    return {"type": "websocket.connect"}
                return await receive()

            async def inner_send(message: dict[str, Any]) -> None:
                nonlocal suppress_accept
                if message.get("type") == "websocket.accept" and suppress_accept:
                    suppress_accept = False
                    return
                if message.get("type") == "websocket.send":
                    raw = message.get("text")
                    if raw:
                        try:
                            payload = json.loads(raw)
                        except (TypeError, ValueError, json.JSONDecodeError):
                            payload = {}
                        if payload.get("type") == "ready":
                            self._last_session_ready_at = time.time()
                            LOGGER.info(
                                "runtime startup: session inference started "
                                "unix_ms=%d session_id=%s",
                                round(self._last_session_ready_at * 1000),
                                payload.get("session_id"),
                            )
                await send(message)

            await self._inner(scope, inner_receive, inner_send)
        except Exception:
            # A browser may disappear at any point during a cold start. That is
            # not a model failure and must not change the process readiness.
            LOGGER.info("cold-start websocket disconnected before handoff", exc_info=True)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        scope_type = scope.get("type")
        if scope_type == "lifespan":
            await self._lifespan(receive, send)
            return

        if scope_type == "http" and scope.get("path") == "/health":
            await self._bootstrap(scope, receive, send)
            return

        if self._state == "ready" and self._inner is not None:
            await self._inner(scope, receive, send)
            return

        if scope_type == "websocket" and scope.get("path") == "/ws/duplex":
            await self._loading_duplex(scope, receive, send)
            return

        await self._bootstrap(scope, receive, send)


def create_deferred_app(
    loader: Callable[[], Any],
    *,
    heartbeat_interval_s: float = 15.0,
) -> DeferredRuntimeApp:
    return DeferredRuntimeApp(
        loader,
        heartbeat_interval_s=heartbeat_interval_s,
    )
