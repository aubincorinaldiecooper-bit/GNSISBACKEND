from __future__ import annotations

import argparse
import base64
import hashlib
import io
import os
import re
import subprocess
import threading
import wave
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

_SAFE_TEXT_BOUNDARY = re.compile(r"[\s,.!?;:，。！？；：…]")


class GenerationRequest(BaseModel):
    generation_id: int


class TextRequest(GenerationRequest):
    text: str = ""
    end_of_turn: bool = False


class TokenRequest(GenerationRequest):
    token_id: int


class PreferencesRequest(BaseModel):
    voice_profile_id: str = "gnsis-default"
    voice_pace: str = "relaxed"
    prompt_audio_wav_b64: str | None = None
    prompt_text: str | None = None


@dataclass
class DotsRenderer:
    runtime: Any
    prompt_audio_path: str
    prompt_text: str
    output_sample_rate: int = 24_000

    def __post_init__(self) -> None:
        self._lock = threading.RLock()
        self._session: Any | None = None
        self._generation_id: int | None = None
        self._pending_text = ""
        self._finish_iterator: Iterator[Any] | None = None
        self._patch_index = 0
        self._voice_profile_id = "gnsis-default"
        self._voice_pace = "relaxed"
        self._active_prompt_audio_path = self.prompt_audio_path
        self._active_prompt_text = self.prompt_text

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "model_sample_rate": int(self.runtime.sample_rate),
            "output_sample_rate": self.output_sample_rate,
            "generation_id": self._generation_id,
            "voice_profile_id": self._voice_profile_id,
            "voice_pace": self._voice_pace,
        }

    def set_preferences(self, request: PreferencesRequest) -> dict[str, Any]:
        if request.voice_pace not in {"relaxed", "normal", "quick"}:
            raise HTTPException(422, "Unsupported voice pace")
        with self._lock:
            if self._session is not None:
                raise HTTPException(409, "Cannot change voice during active speech")
            prompt_audio_path = self.prompt_audio_path
            prompt_text = self.prompt_text
            if request.prompt_audio_wav_b64:
                try:
                    wav = base64.b64decode(
                        request.prompt_audio_wav_b64,
                        validate=True,
                    )
                    with wave.open(io.BytesIO(wav), "rb") as reader:
                        if reader.getsampwidth() != 2:
                            raise ValueError("reference must be 16-bit PCM")
                except (ValueError, wave.Error, EOFError) as exc:
                    raise HTTPException(422, "Invalid reference WAV") from exc
                digest = hashlib.sha256(wav).hexdigest()[:24]
                prompt_audio_path = f"/tmp/gnsis-dots-reference-{digest}.wav"
                if not os.path.exists(prompt_audio_path):
                    with open(prompt_audio_path, "wb") as handle:
                        handle.write(wav)
                prompt_text = (request.prompt_text or "").strip()
            prompt_audio_path = self._paced_reference(
                prompt_audio_path,
                request.voice_pace,
            )
            self._voice_profile_id = request.voice_profile_id
            self._voice_pace = request.voice_pace
            self._active_prompt_audio_path = prompt_audio_path
            self._active_prompt_text = prompt_text
            return {
                "ok": True,
                "voice_profile_id": self._voice_profile_id,
                "voice_pace": self._voice_pace,
            }

    @staticmethod
    def _paced_reference(path: str, pace: str) -> str:
        ratio = {"relaxed": 0.85, "normal": 1.0, "quick": 1.12}[pace]
        if ratio == 1.0:
            return path
        digest = hashlib.sha256(f"{path}:{pace}".encode()).hexdigest()[:24]
        output = f"/tmp/gnsis-dots-paced-{digest}.wav"
        if os.path.exists(output):
            return output
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    path,
                    "-filter:a",
                    f"atempo={ratio}",
                    output,
                ],
                check=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise HTTPException(500, "Unable to prepare voice pace") from exc
        return output

    def append_text(self, request: TextRequest) -> dict[str, Any]:
        with self._lock:
            self._ensure_session(request.generation_id)
            self._pending_text += request.text
            stable, self._pending_text = self._split_stable_text(
                self._pending_text,
                final=request.end_of_turn,
            )
            token_ids = self.runtime.model.tokenizer.encode(
                stable,
                add_special_tokens=False,
            )
            return {"token_ids": [int(value) for value in token_ids]}

    def push_token(self, request: TokenRequest) -> dict[str, Any]:
        with self._lock:
            session = self._require_session(request.generation_id)
            if self._finish_iterator is not None:
                raise HTTPException(409, "Dots session is already finishing")
            return self._audio_payload(session.push_text_token(request.token_id))

    def finish(self, request: GenerationRequest) -> dict[str, Any]:
        with self._lock:
            session = self._require_session(request.generation_id)
            if self._pending_text:
                raise HTTPException(409, "Dots session still has unsubmitted text")
            if self._finish_iterator is None:
                self._finish_iterator = iter(session.finish_text())
            try:
                chunk = next(self._finish_iterator)
            except StopIteration:
                self._clear_session()
                return {"done": True}
            return {**self._audio_payload(chunk), "done": False}

    def reset(self) -> dict[str, Any]:
        with self._lock:
            self._clear_session()
            return {"ok": True}

    def _ensure_session(self, generation_id: int) -> None:
        if self._generation_id == generation_id and self._session is not None:
            return
        self._clear_session()
        self._session = self.runtime.start_double_streaming(
            prompt_audio_path=self._active_prompt_audio_path,
            prompt_text=self._active_prompt_text or None,
        )
        self._generation_id = generation_id

    def _require_session(self, generation_id: int) -> Any:
        if self._session is None or self._generation_id != generation_id:
            raise HTTPException(409, "Dots generation is not active")
        return self._session

    def _clear_session(self) -> None:
        self._session = None
        self._generation_id = None
        self._pending_text = ""
        self._finish_iterator = None
        self._patch_index = 0

    def _audio_payload(self, chunk: Any | None) -> dict[str, Any]:
        if chunk is None:
            return {}
        import torchaudio.functional as audio_functional

        audio = chunk.detach().float().cpu().reshape(-1)
        source_rate = int(self.runtime.sample_rate)
        if source_rate != self.output_sample_rate:
            audio = audio_functional.resample(
                audio,
                source_rate,
                self.output_sample_rate,
            )
        array = audio.contiguous().numpy().astype("<f4", copy=False)
        self._patch_index += 1
        return {
            "audio_f32_b64": base64.b64encode(array.tobytes()).decode("ascii"),
            "patch_index": self._patch_index,
        }

    @staticmethod
    def _split_stable_text(text: str, *, final: bool) -> tuple[str, str]:
        if final or not text:
            return text, ""
        boundary = -1
        for match in _SAFE_TEXT_BOUNDARY.finditer(text):
            boundary = match.end()
        if boundary <= 0:
            return "", text
        return text[:boundary], text[boundary:]


def build_app(renderer: DotsRenderer) -> FastAPI:
    app = FastAPI(title="GNSIS Dots renderer", docs_url=None, redoc_url=None)

    @app.get("/health")
    @app.post("/health")
    def health() -> dict[str, Any]:
        return renderer.health()

    @app.post("/v1/session/text")
    def append_text(request: TextRequest) -> dict[str, Any]:
        return renderer.append_text(request)

    @app.post("/v1/session/preferences")
    def set_preferences(request: PreferencesRequest) -> dict[str, Any]:
        return renderer.set_preferences(request)

    @app.post("/v1/session/push")
    def push_token(request: TokenRequest) -> dict[str, Any]:
        return renderer.push_token(request)

    @app.post("/v1/session/finish")
    def finish(request: GenerationRequest) -> dict[str, Any]:
        return renderer.finish(request)

    @app.post("/v1/session/reset")
    def reset() -> dict[str, Any]:
        return renderer.reset()

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve Dots as a GNSIS renderer")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7976)
    parser.add_argument(
        "--model",
        default=os.environ.get(
            "GNSIS_DOTS_MODEL",
            "dots-studio/dots.tts-mf-2steps-stts",
        ),
    )
    parser.add_argument(
        "--prompt-audio",
        default=os.environ.get("GNSIS_DOTS_PROMPT_AUDIO", ""),
    )
    parser.add_argument(
        "--prompt-text",
        default=os.environ.get("GNSIS_DOTS_PROMPT_TEXT", ""),
    )
    parser.add_argument("--precision", default="bfloat16")
    parser.add_argument("--output-sample-rate", type=int, default=24_000)
    parser.add_argument("--no-optimize", action="store_true")
    args = parser.parse_args()
    if not args.prompt_audio:
        raise ValueError("--prompt-audio or GNSIS_DOTS_PROMPT_AUDIO is required")

    from dots_tts.runtime_double_streaming import DotsTtsRuntimeDoubleStreaming

    runtime = DotsTtsRuntimeDoubleStreaming.from_pretrained(
        args.model,
        precision=args.precision,
        optimize=not args.no_optimize,
        max_generate_length=500,
    )
    renderer = DotsRenderer(
        runtime=runtime,
        prompt_audio_path=args.prompt_audio,
        prompt_text=args.prompt_text,
        output_sample_rate=args.output_sample_rate,
    )

    import uvicorn

    uvicorn.run(build_app(renderer), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
