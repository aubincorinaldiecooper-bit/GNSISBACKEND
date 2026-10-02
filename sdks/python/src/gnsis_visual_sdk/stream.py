from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import websockets

from .client import VisualSession
from .errors import VisualServiceError


class FrameStream:
    def __init__(self, websocket: Any, stream_token: str) -> None:
        self._websocket = websocket
        self._stream_token = stream_token
        self._send_lock = asyncio.Lock()

    def __repr__(self) -> str:
        return "FrameStream(<connected>)"

    @classmethod
    async def connect(cls, base_url: str, session: VisualSession) -> FrameStream:
        url = _stream_url(base_url, session)
        try:
            websocket = await websockets.connect(url)
        except Exception:
            raise VisualServiceError(
                "stream_connect_error",
                "could not connect to the visual frame stream",
                retryable=True,
            ) from None
        return cls(websocket, session.stream_token)

    async def __aenter__(self) -> FrameStream:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._websocket.close()

    async def send_frame(
        self,
        frame_id: str,
        captured_at_ms: int,
        image: bytes,
        *,
        encoding: str = "jpeg",
        video_source: str = "screen",
    ) -> dict[str, Any]:
        header = {
            "type": "screen.frame",
            "frame_id": frame_id,
            "captured_at_ms": captured_at_ms,
            "encoding": encoding,
            "video_source": video_source,
        }
        async with self._send_lock:
            try:
                await self._websocket.send(json.dumps(header, separators=(",", ":")))
                await self._websocket.send(image)
                response = await self._websocket.recv()
            except Exception:
                raise VisualServiceError(
                    "stream_error",
                    "visual frame stream communication failed",
                    retryable=True,
                ) from None
        try:
            payload = json.loads(response)
        except (TypeError, ValueError):
            raise VisualServiceError(
                "invalid_protocol",
                "visual frame stream returned an invalid response",
            ) from None
        if not isinstance(payload, dict):
            raise VisualServiceError(
                "invalid_protocol",
                "visual frame stream returned an invalid response",
            )
        if payload.get("type") == "screen.frame.accepted":
            return payload
        if payload.get("type") == "screen.frame.rejected":
            error = payload.get("error")
            if isinstance(error, dict):
                code = str(error.get("code", "frame_rejected"))
                message = str(error.get("message", "visual frame was rejected"))
            else:
                code = "frame_rejected"
                message = "visual frame was rejected"
            raise VisualServiceError(
                _redact(code, self._stream_token),
                _redact(message, self._stream_token),
            )
        raise VisualServiceError(
            "invalid_protocol",
            "visual frame stream returned an unexpected response",
        )


def _stream_url(base_url: str, session: VisualSession) -> str:
    base = urlsplit(base_url)
    scheme = {"http": "ws", "https": "wss"}.get(base.scheme.lower())
    if scheme is None or not base.netloc:
        raise VisualServiceError(
            "invalid_base_url",
            "visual API base URL must use http or https",
        )
    stream = urlsplit(session.stream_path)
    query = parse_qsl(stream.query, keep_blank_values=True)
    query.append(("token", session.stream_token))
    return urlunsplit((scheme, base.netloc, stream.path, urlencode(query), ""))


def _redact(value: str, secret: str) -> str:
    return value.replace(secret, "[redacted]") if secret else value
