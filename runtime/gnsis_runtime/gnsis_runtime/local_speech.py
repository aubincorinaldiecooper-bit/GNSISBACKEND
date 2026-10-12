"""Local speech output for GNSIS 0.5, returned as in-memory 24 kHz PCM16."""

from __future__ import annotations

import threading
from collections.abc import Callable

SAMPLE_RATE = 24_000
MAX_SPEECH_CHARS = 2_000


class LocalKittenSpeech:
    def __init__(
        self,
        model_name: str = "KittenML/kitten-tts-2",
        *,
        voice: str = "Luna",
        loader: Callable[..., object] | None = None,
    ):
        self.model_name = model_name
        self.voice = voice
        self._loader = loader
        self._model: object | None = None
        self._lock = threading.Lock()

    def speak(self, text: str, voice: str | None = None) -> bytes:
        words = text.strip()
        if not words or len(words) > MAX_SPEECH_CHARS:
            raise ValueError("Speech text must be between 1 and 2000 characters")
        import numpy as np

        model = self._load()
        audio = np.asarray(model.generate(words, voice=voice or self.voice), dtype=np.float32)
        if int(getattr(model, "sample_rate", SAMPLE_RATE)) != SAMPLE_RATE:
            raise ValueError("Kitten TTS returned an unsupported sample rate")
        return (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()

    def _load(self) -> object:
        with self._lock:
            if self._model is None:
                loader = self._loader
                if loader is None:
                    from kittenml import KittenTTS

                    loader = KittenTTS
                self._model = loader(self.model_name, device="cpu")
            return self._model
