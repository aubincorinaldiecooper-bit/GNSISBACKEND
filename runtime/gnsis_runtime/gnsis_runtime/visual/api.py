from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket
from pydantic import BaseModel, Field
from starlette.websockets import WebSocketDisconnect, WebSocketDisconnected

from ..screen_transport import ScreenFrameHeader, decode_screen_frame
from .service import VisualService, VisualServiceError

MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_FRAME_PIXELS = 16_777_216


@dataclass(frozen=True)
class VisualAPISettings:
    bearer_token: str

    def __post_init__(self) -> None:
        if not self.bearer_token:
            raise ValueError("visual API bearer token must not be empty")


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

    def require_bearer(authorization: str | None = Header(default=None)) -> None:
        scheme, _, credential = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(
            credential,
            settings.bearer_token,
        ):
            raise HTTPException(
                status_code=401,
                detail={"code": "unauthorized", "message": "invalid bearer token"},
                headers={"WWW-Authenticate": "Bearer"},
            )

    @app.exception_handler(VisualServiceError)
    async def visual_service_error(
        _request: Any,
        exc: VisualServiceError,
    ) -> Any:
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": str(exc)}},
        )

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return service.health()

    @app.post("/v1/visual/sessions", dependencies=[Depends(require_bearer)])
    async def create_session() -> dict[str, Any]:
        session_id, stream_token = service.create_session()
        return {
            "session_id": session_id,
            "stream": {
                "path": f"/v1/visual/sessions/{session_id}/stream",
                "token": stream_token,
                "protocol": "screen-frame-v1",
            },
        }

    @app.delete(
        "/v1/visual/sessions/{session_id}",
        dependencies=[Depends(require_bearer)],
    )
    async def close_session(session_id: str) -> dict[str, bool]:
        service.close_session(session_id)
        return {"closed": True}

    @app.put(
        "/v1/visual/sessions/{session_id}/task",
        dependencies=[Depends(require_bearer)],
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
        dependencies=[Depends(require_bearer)],
    )
    async def reset_task(session_id: str) -> dict[str, Any]:
        return service.reset_task(session_id)

    @app.post(
        "/v1/visual/sessions/{session_id}/decisions",
        dependencies=[Depends(require_bearer)],
    )
    async def decide(
        session_id: str,
        payload: DecisionRequest,
    ) -> dict[str, Any]:
        return service.decide(session_id, payload.request_id)

    @app.post(
        "/v1/visual/sessions/{session_id}/attempts",
        dependencies=[Depends(require_bearer)],
    )
    async def record_attempt(
        session_id: str,
        payload: AttemptRequest,
    ) -> dict[str, Any]:
        return service.record_attempt(session_id, payload.decision_id)

    @app.get(
        "/v1/visual/sessions/{session_id}",
        dependencies=[Depends(require_bearer)],
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
                text = message.get("text")
                body = message.get("bytes")
                if text is not None:
                    if pending is not None:
                        await _stream_error(
                            websocket,
                            "frame metadata requires a following binary payload",
                        )
                        pending = None
                        continue
                    try:
                        import json

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
