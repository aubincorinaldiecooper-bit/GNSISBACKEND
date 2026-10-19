"""Phonon (fermion-research) local speech recognition behind the LocalWhisper seam.

Phonon-2 is a ~164 MB model that runs on Apple silicon through MLX and on
Intel/ARM CPUs through its own low-bit engine. It exposes `transcribe_array`,
so PCM never touches the filesystem — same contract as `LocalWhisper`.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

SAMPLE_RATE = 16_000


class LocalPhonon:
    def __init__(
        self,
        model_name: str = "phonon-2",
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
        import numpy as np

        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        text, _decode_s, _audio_s = self._load().transcribe_array(samples)
        return str(text).strip()

    def _load(self) -> object:
        with self._lock:
            if self._model is None:
                loader = self._loader
                if loader is None:
                    import fermion

                    loader = fermion.load_speech
                self._model = loader(self.model_name)
            return self._model
