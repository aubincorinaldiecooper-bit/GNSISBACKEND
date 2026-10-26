from __future__ import annotations

import pytest
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


class _WordTokenizer:
    """Roughly a BPE tokenizer: words and punctuation each count as one."""

    def encode(self, text: str, add_special_tokens: bool = False) -> list[int]:
        import re

        return list(range(len(re.findall(r"\w+|[^\w\s]", text))))


class _BudgetLive(_Live):
    def __init__(self, limit: int = 256) -> None:
        super().__init__()
        self.runner = SimpleNamespace(
            params=SimpleNamespace(
                decode_mode="greedy",
                max_tool_response_tokens=limit,
            ),
            bundle=SimpleNamespace(tokenizer=_WordTokenizer()),
        )


def _delivery(**fields: Any) -> dict[str, Any]:
    return {"type": "worker_delivery", "delivery_id": "d-1", **fields}


def test_a_delivery_with_bulk_outside_content_is_cut_instead_of_refused():
    """The live failure: "tool response has 265 tokens; maximum is 256".

    The old code trimmed only ``content``, so a delivery whose weight sat in
    ``error`` still overflowed and raised — even though most of the answer
    could have been kept.
    """

    session = GNSISDuplexSession(_BudgetLive())
    oversized = _delivery(
        status="failed",
        error="navigation failed: " + "timeout waiting for selector " * 120,
        content="",
    )
    fitted = session._fit_runtime_event(oversized)

    assert fitted is not oversized, "it was over budget, so it must be cut"
    assert fitted["status"] == "failed", "the outcome survives"
    assert fitted["error"].startswith("navigation failed")
    assert fitted.get("more"), "the model is told the answer was shortened"
    assert fitted["delivery_id"] == "d-1", "correlation is kept"


def test_a_delivery_inside_the_budget_is_passed_through_untouched():
    session = GNSISDuplexSession(_BudgetLive())
    small = _delivery(status="done", content="Notes is open.")
    assert session._fit_runtime_event(small) is small


def test_a_budget_nothing_can_fit_still_refuses():
    session = GNSISDuplexSession(_BudgetLive(limit=3))
    with pytest.raises(ValueError, match="max_tool_response_tokens"):
        session._fit_runtime_event(
            _delivery(status="done", entries=[f"entry {i}" for i in range(50)])
        )
