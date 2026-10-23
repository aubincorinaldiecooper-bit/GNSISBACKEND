"""Require the site's edge secret on every route, not only the live sockets.

The runtime's own check covers ``/ws/duplex`` and ``/ws/screen``. On Modal the
rest of the surface (``/api/asr/transcribe``, the static live page, assets) sat
behind Modal's proxy auth, so nothing else needed guarding. A RunPod pod has no
such proxy: its address is public, and anyone who found it could run speech
recognition on the server or read the runtime's pages.

``EdgeGuard`` closes that gap. With it on, a request is served only when it
carries the same ``X-GNSIS-Edge`` header the site already adds when it
forwards. ``GET /health`` stays open so the control plane and the host's own
health checks can see whether the runtime is up; it reports state, not data.

It is opt-in through ``GNSIS_EDGE_ALL_ROUTES`` so Modal deployments keep their
current behaviour, and it fails closed: switched on without a secret, the
runtime refuses to start rather than serve an unguarded surface.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Awaitable, Callable

from .deferred_app import _came_through_the_front_door

LOGGER = logging.getLogger(__name__)

ENABLE_ENV = "GNSIS_EDGE_ALL_ROUTES"
SECRET_ENV = "GNSIS_EDGE_SECRET"
OPEN_PATHS = frozenset({"/health"})

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


def edge_all_routes_enabled() -> bool:
    return (os.environ.get(ENABLE_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


class EdgeGuard:
    """ASGI wrapper: refuse any request without the edge secret, except health."""

    def __init__(self, app: Any) -> None:
        if not (os.environ.get(SECRET_ENV) or "").strip():
            raise RuntimeError(
                f"{ENABLE_ENV} is on but {SECRET_ENV} is empty. Every route would be "
                "open to anyone who finds this runtime's address. Set the same "
                f"{SECRET_ENV} the site forwards with, or turn {ENABLE_ENV} off."
            )
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        kind = scope.get("type")
        if kind not in {"http", "websocket"}:
            # Lifespan and anything else the server sends pass straight through.
            await self._app(scope, receive, send)
            return
        is_open_health = (
            kind == "http"
            and scope.get("path") in OPEN_PATHS
            and scope.get("method") in {"GET", "HEAD"}
        )
        if is_open_health or _came_through_the_front_door(scope):
            await self._app(scope, receive, send)
            return
        LOGGER.warning(
            "edge_guard refused %s %s: missing or wrong X-GNSIS-Edge",
            kind,
            scope.get("path"),
        )
        if kind == "websocket":
            # Closing before accept makes the server answer the handshake with 403.
            await send({"type": "websocket.close", "code": 1008})
            return
        body = b'{"error":"forbidden"}'
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def guard_if_enabled(app: Any) -> Any:
    """Wrap ``app`` in :class:`EdgeGuard` when ``GNSIS_EDGE_ALL_ROUTES`` is on."""

    if not edge_all_routes_enabled():
        return app
    LOGGER.info("edge_guard on: every route except GET /health requires X-GNSIS-Edge")
    return EdgeGuard(app)
