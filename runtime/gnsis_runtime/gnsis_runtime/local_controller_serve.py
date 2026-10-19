from __future__ import annotations

import argparse
import os
from collections.abc import Sequence

from .local_controller import LocalDecisionEngine
from .local_controller_api import build_local_controller_app
from .local_phonon import LocalPhonon
from .local_speech import LocalKittenSpeech
from .local_whisper import LocalWhisper
from .omnijev_local import backend_from_env


def _local_transcriber():
    """GNSIS_ASR=phonon (default, ~164 MB) or whisper (faster-whisper small.en)."""
    if os.environ.get("GNSIS_ASR", "phonon").strip().lower() == "whisper":
        return LocalWhisper(os.environ.get("GNSIS_WHISPER_MODEL", "small.en"))
    return LocalPhonon(os.environ.get("GNSIS_PHONON_MODEL", "phonon-2"))


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Serve GNSIS 0.5 locally.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8766, type=int)
    args = parser.parse_args(argv)
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        parser.error("The local controller may bind only to loopback.")

    token = os.environ.get("GNSIS_LOCAL_CONTROLLER_TOKEN", "").strip()
    if not token:
        parser.error("Set GNSIS_LOCAL_CONTROLLER_TOKEN; every request must carry it.")

    import uvicorn

    app = build_local_controller_app(
        LocalDecisionEngine(backend_from_env()),
        token,
        _local_transcriber(),
        LocalKittenSpeech(
            os.environ.get("GNSIS_KITTEN_MODEL", "KittenML/kitten-tts-2"),
            voice=os.environ.get("GNSIS_KITTEN_VOICE", "Luna"),
        ),
    )
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
