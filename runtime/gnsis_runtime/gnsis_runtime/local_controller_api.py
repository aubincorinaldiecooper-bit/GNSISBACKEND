"""Loopback-only HTTP boundary for GNSIS 0.5."""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response

from .local_controller import LocalDecisionEngine
from .local_speech import LocalKittenSpeech
from .local_whisper import LocalWhisper


def build_local_controller_app(
    engine: LocalDecisionEngine,
    host_token: str,
    whisper: LocalWhisper | None = None,
    speech: LocalKittenSpeech | None = None,
) -> FastAPI:
    if not host_token:
        raise ValueError("GNSIS 0.5 needs a host token on every request.")
    app = FastAPI(title="GNSIS 0.5 Local Controller")

    def authorize(authorization: str | None) -> None:
        expected = f"Bearer {host_token}"
        if authorization is None or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="Unauthorized")

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/v1/decide")
    def decide(
        payload: dict[str, Any],
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        authorize(authorization)
        try:
            return engine.decide(payload)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

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
        if not isinstance(text, str) or (voice is not None and not isinstance(voice, str)):
            raise HTTPException(status_code=400, detail="Invalid speech request")
        try:
            pcm = speech.speak(text, voice)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return Response(content=pcm, media_type="application/octet-stream")

    return app
