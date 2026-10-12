"""CPU-first local Whisper transcription without filesystem audio artifacts."""

from __future__ import annotations

import io
import threading
import wave
from collections.abc import Callable

SAMPLE_RATE = 16_000


class LocalWhisper:
    def __init__(
        self,
        model_name: str = "small.en",
        *,
        loader: Callable[..., object] | None = None,
    ):
        self.model_name = model_name
        self._loader = loader
        self._model: object | None = None
        self._lock = threading.Lock()

    def transcribe(self, pcm: bytes) -> str:
        if not pcm or len(pcm) % 2:
            raise ValueError("Expected nonempty 16-bit PCM audio")
        model = self._load()
        audio = io.BytesIO()
        with wave.open(audio, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(pcm)
        audio.seek(0)
        segments, _info = model.transcribe(
            audio,
            beam_size=1,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        return " ".join(segment.text.strip() for segment in segments).strip()

    def _load(self) -> object:
        with self._lock:
            if self._model is None:
                loader = self._loader
                if loader is None:
                    from faster_whisper import WhisperModel

                    loader = WhisperModel
                self._model = loader(
                    self.model_name,
                    device="cpu",
                    compute_type="int8",
                )
            return self._model
