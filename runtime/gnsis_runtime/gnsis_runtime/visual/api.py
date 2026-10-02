from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.websockets import WebSocketDisconnect, WebSocketDisconnected

from ..screen_transport import ScreenFrameHeader, decode_screen_frame
from .service import VisualService, VisualServiceError

MAX_FRAME_HEADER_BYTES = 4096
MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_FRAME_PIXELS = 16_777_216


@dataclass(frozen=True)
class VisualAPISettings:
    host_token: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.host_token:
            raise ValueError("visual API host token must not be empty")


class TaskRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=4_000)
    allowed_actions: list[str] | None = None


class DecisionRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=256)


class AttemptRequest(BaseModel):
    decision_id: str = Field(min_length=1, max_length=256)


def create_visual_api(
    service: VisualService,
    settings: VisualAPISettings,
) -> FastAPI:
    app = FastAPI(
        title="Smaller GNSIS Visual Service",
        version="1.0.0",
        description=(
            "Agent-independent visual decision service. Capture and actuation remain "
            "owned by authenticated hosts."
        ),
    )

    def bearer_credential(authorization: str | None) -> str:
        scheme, _, credential = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not credential:
            raise HTTPException(
                status_code=401,
                detail={"code": "unauthorized", "message": "invalid bearer token"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        return credential

    def unauthorized() -> None:
        raise HTTPException(
            status_code=401,
            detail={"code": "unauthorized", "message": "invalid bearer token"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    def forbidden() -> None:
        raise VisualServiceError(
            "forbidden",
            "planner credentials cannot perform this operation",
            status_code=403,
        )

    def require_host(authorization: str | None = Header(default=None)) -> None:
        credential = bearer_credential(authorization)
        if secrets.compare_digest(credential, settings.host_token):
            return
        if service.is_planner_token(credential):
            forbidden()
        unauthorized()

    def require_session_bearer(
        session_id: str,
        authorization: str | None = Header(default=None),
    ) -> None:
        credential = bearer_credential(authorization)
        if secrets.compare_digest(credential, settings.host_token):
            return
        if service.authenticate_planner(session_id, credential):
            return
        if service.is_planner_token(credential) and service.has_session(session_id):
            forbidden()
        unauthorized()

    @app.exception_handler(VisualServiceError)
    async def visual_service_error(
        _request: Any,
        exc: VisualServiceError,
    ) -> Any:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": str(exc)}},
        )

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return service.health()

    @app.post("/v1/visual/sessions", dependencies=[Depends(require_host)])
    async def create_session() -> dict[str, Any]:
        credentials = service.create_session()
        return {
            "session_id": credentials.session_id,
            "stream": {
                "path": f"/v1/visual/sessions/{credentials.session_id}/stream",
                "token": credentials.stream_token,
                "protocol": "screen-frame-v1",
            },
            "planner": {"token": credentials.planner_token},
        }

    @app.delete(
        "/v1/visual/sessions/{session_id}",
        dependencies=[Depends(require_host)],
    )
    async def close_session(session_id: str) -> dict[str, bool]:
        service.close_session(session_id)
        return {"closed": True}

    @app.put(
        "/v1/visual/sessions/{session_id}/task",
        dependencies=[Depends(require_session_bearer)],
    )
    async def set_task(session_id: str, payload: TaskRequest) -> dict[str, Any]:
        actions = (
            tuple(payload.allowed_actions)
            if payload.allowed_actions is not None
            else None
        )
        return service.set_task(
            session_id,
            payload.goal,
            allowed_actions=actions,
        )

    @app.delete(
        "/v1/visual/sessions/{session_id}/task",
        dependencies=[Depends(require_session_bearer)],
    )
    async def reset_task(session_id: str) -> dict[str, Any]:
        return service.reset_task(session_id)

    @app.post(
        "/v1/visual/sessions/{session_id}/decisions",
        dependencies=[Depends(require_session_bearer)],
    )
    async def decide(
        session_id: str,
        payload: DecisionRequest,
    ) -> dict[str, Any]:
        return service.decide(session_id, payload.request_id)

    @app.post(
        "/v1/visual/sessions/{session_id}/attempts",
        dependencies=[Depends(require_host)],
    )
    async def record_attempt(
        session_id: str,
        payload: AttemptRequest,
    ) -> dict[str, Any]:
        return service.record_attempt(session_id, payload.decision_id)

    @app.get(
        "/v1/visual/sessions/{session_id}",
        dependencies=[Depends(require_session_bearer)],
    )
    async def state(session_id: str) -> dict[str, Any]:
        return service.state(session_id)

    @app.websocket("/v1/visual/sessions/{session_id}/stream")
    async def stream(
        websocket: WebSocket,
        session_id: str,
        token: str = "",
    ) -> None:
        try:
            authenticated = service.authenticate_stream(session_id, token)
        except VisualServiceError:
            await websocket.close(code=4404)
            return
        if not authenticated:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        pending: ScreenFrameHeader | None = None
        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    return
                text = message.get("text")
                body = message.get("bytes")
                if text is not None:
                    if len(text.encode("utf-8")) > MAX_FRAME_HEADER_BYTES:
                        pending = None
                        await _stream_error(
                            websocket,
                            f"screen frame metadata exceeds {MAX_FRAME_HEADER_BYTES} bytes",
                        )
                        continue
                    if pending is not None:
                        await _stream_error(
                            websocket,
                            "frame metadata requires a following binary payload",
                        )
                        pending = None
                        continue
                    try:
                        value = json.loads(text)
                        if not isinstance(value, dict):
                            raise ValueError("screen frame metadata must be an object")
                        pending = ScreenFrameHeader.from_payload(value)
                    except (ValueError, TypeError) as exc:
                        await _stream_error(websocket, str(exc))
                elif body is not None:
                    if pending is None:
                        await _stream_error(
                            websocket,
                            "binary frame requires preceding metadata",
                        )
                        continue
                    header = pending
                    pending = None
                    try:
                        decoded = decode_screen_frame(
                            header,
                            body,
                            max_bytes=MAX_FRAME_BYTES,
                            max_pixels=MAX_FRAME_PIXELS,
                        )
                        accepted = service.publish_frame(session_id, decoded.frame)
                    except (ValueError, VisualServiceError) as exc:
                        code = (
                            exc.code
                            if isinstance(exc, VisualServiceError)
                            else "invalid_frame"
                        )
                        await websocket.send_json(
                            {
                                "type": "screen.frame.rejected",
                                "error": {"code": code, "message": str(exc)},
                            }
                        )
                        continue
                    await websocket.send_json(
                        {"type": "screen.frame.accepted", **accepted}
                    )
        except (WebSocketDisconnect, WebSocketDisconnected):
            return

    return app


async def _stream_error(websocket: WebSocket, message: str) -> None:
    await websocket.send_json(
        {
            "type": "screen.frame.rejected",
            "error": {"code": "invalid_protocol", "message": message},
        }
    )
