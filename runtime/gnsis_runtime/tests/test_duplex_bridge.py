from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from gnsis_runtime.duplex_bridge import GNSISDuplexSession
from gnsis_runtime.foreground_session import ForegroundSession


class _Live:
    def __init__(self) -> None:
        self.runner = SimpleNamespace(params=SimpleNamespace(decode_mode="greedy"))
        self.preferences: list[dict[str, Any]] = []

    def set_frame_source(self, _frames: Any) -> None:
        return None

    def set_voice_preferences(self, **values: Any) -> None:
        self.preferences.append(values)


def test_thinker_session_forwards_account_voice_preferences():
    live = _Live()
    session = GNSISDuplexSession(live)

    session.set_voice_preferences(
        voice_profile_id="voice_private",
        voice_pace="quick",
        prompt_audio_wav_b64="UklGRg==",
        prompt_text="Reference transcript.",
    )

    assert live.preferences == [
        {
            "voice_profile_id": "voice_private",
            "voice_pace": "quick",
            "prompt_audio_wav_b64": "UklGRg==",
            "prompt_text": "Reference transcript.",
        }
    ]


def test_thinker_session_has_every_foreground_session_method():
    required = {
        name
        for name in dir(ForegroundSession)
        if not name.startswith("_") and callable(getattr(ForegroundSession, name))
    }
    assert required <= set(dir(GNSISDuplexSession))
