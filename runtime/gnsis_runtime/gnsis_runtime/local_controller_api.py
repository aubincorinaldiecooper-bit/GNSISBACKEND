"""Loopback-only HTTP boundary for GNSIS 0.5."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from collections.abc import Sequence
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response

from .local_controller import DecisionCancelled, LocalDecisionEngine
from .local_speech import LocalKittenSpeech
from .local_whisper import LocalWhisper

logger = logging.getLogger(__name__)


def _key_label(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:12]


def _safe_request_id(payload: dict[str, Any]) -> str:
    # Hash, don't sanitize: a caller can put a secret in requestId, and any
    # retained substring would land it in telemetry. The digest still lets an
    # operator correlate a known requestId to its log lines.
    raw = str(payload.get("requestId") or "")
    return hashlib.sha256(raw.encode()).hexdigest()[:12] if raw else "-"


def build_local_controller_app(
    engine: LocalDecisionEngine,
    host_token: str,
    whisper: LocalWhisper | None = None,
    speech: LocalKittenSpeech | None = None,
    extra_tokens: Sequence[str] = (),
) -> FastAPI:
    tokens = tuple(t for t in (host_token, *extra_tokens) if t)
    if not tokens:
        raise ValueError("GNSIS 0.5 needs a host token on every request.")
    app = FastAPI(title="GNSIS 0.5 Local Controller")

    def authorize(authorization: str | None) -> str:
        for token in tokens:
            if hmac.compare_digest(authorization or "", f"Bearer {token}"):
                return _key_label(token)
        raise HTTPException(status_code=401, detail="Unauthorized")

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/v1/decide")
    async def decide(
        payload: dict[str, Any],
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> Any:
        caller = authorize(authorization)
        logger.info("decide requestId=%s caller=%s", _safe_request_id(payload), caller)
        gone = asyncio.Event()

        async def watch_disconnect() -> None:
            while not gone.is_set():
                if await request.is_disconnected():
                    gone.set()
                    return
                await asyncio.sleep(0.25)

        watcher = asyncio.create_task(watch_disconnect())
        try:
            return await asyncio.to_thread(
                engine.decide, payload, cancelled=gone.is_set
            )
        except DecisionCancelled:
            return Response(status_code=499)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        finally:
            gone.set()
            watcher.cancel()

    @app.post("/v1/transcribe")
    async def transcribe(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, str]:
        authorize(authorization)
        if whisper is None:
            raise HTTPException(status_code=503, detail="Local Whisper is unavailable")
        try:
            return {"text": whisper.transcribe(await request.body())}
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/v1/speak")
    def speak(
        payload: dict[str, Any],
        authorization: str | None = Header(default=None),
    ) -> Response:
        authorize(authorization)
        if speech is None:
            raise HTTPException(status_code=503, detail="Local speech is unavailable")
        text = payload.get("text")
        voice = payload.get("voice")
        if not isinstance(text, str) or (
            voice is not None and not isinstance(voice, str)
        ):
            raise HTTPException(status_code=400, detail="Invalid speech request")
        try:
            pcm = speech.speak(text, voice)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return Response(content=pcm, media_type="application/octet-stream")

    return app
