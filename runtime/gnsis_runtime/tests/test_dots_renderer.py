from __future__ import annotations

import base64
import threading
from collections.abc import Iterator
from typing import Any

import numpy as np
import pytest
from gnsis_runtime.dots_sidecar import DotsRenderer, TextRequest
from mcpmft.infer.detached_talker import (
    DotsTalkerRuntime,
    SpeechSynthesisRequest,
    SynthesisCancelled,
)


def _audio_payload(values: list[float], *, patch_index: int) -> dict[str, Any]:
    audio = np.asarray(values, dtype="<f4")
    return {
        "audio_f32_b64": base64.b64encode(audio.tobytes()).decode("ascii"),
        "patch_index": patch_index,
    }


def test_dots_adapter_streams_tokens_and_marks_the_final_flush_chunk(monkeypatch):
    runtime = DotsTalkerRuntime(device="cuda:1")
    calls: list[tuple[str, dict[str, Any]]] = []
    finish_payloads: Iterator[dict[str, Any]] = iter(
        [
            _audio_payload([0.3, 0.4], patch_index=2),
            {"done": True},
        ]
    )

    def post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((path, payload))
        if path == "/v1/session/text":
            return {"token_ids": [101, 102]}
        if path == "/v1/session/push":
            if payload["token_id"] == 101:
                return {}
            return _audio_payload([0.1, 0.2], patch_index=1)
        if path == "/v1/session/finish":
            return next(finish_payloads)
        raise AssertionError(path)

    monkeypatch.setattr(runtime, "_post", post)
    emitted = []
    metrics = runtime.synthesize(
        SpeechSynthesisRequest(
            generation_id=7,
            unit_id=3,
            current_time=456,
            token_ids=(),
            hidden_states=None,
            end_of_turn=True,
            text="That is wonderful.",
        ),
        threading.Event(),
        emitted.append,
    )

    assert [event.sequence for event in emitted] == [1, 2]
    assert [event.end_of_turn for event in emitted] == [False, True]
    assert emitted[0].waveform.tolist() == pytest.approx([0.1, 0.2])
    assert emitted[1].waveform.tolist() == pytest.approx([0.3, 0.4])
    assert metrics["renderer"] == "dots"
    assert metrics["text_tokens"] == 2
    assert metrics["audio_chunks"] == 2
    assert [path for path, _ in calls] == [
        "/v1/session/text",
        "/v1/session/push",
        "/v1/session/push",
        "/v1/session/finish",
        "/v1/session/finish",
    ]


def test_dots_adapter_drops_output_when_cancelled_mid_push(monkeypatch):
    runtime = DotsTalkerRuntime(device="cuda:1")
    cancelled = threading.Event()
    emitted = []

    def post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if path == "/v1/session/text":
            return {"token_ids": [101]}
        if path == "/v1/session/push":
            cancelled.set()
            return _audio_payload([0.5], patch_index=1)
        raise AssertionError(path)

    monkeypatch.setattr(runtime, "_post", post)
    with pytest.raises(SynthesisCancelled):
        runtime.synthesize(
            SpeechSynthesisRequest(
                generation_id=8,
                unit_id=4,
                current_time=None,
                token_ids=(),
                hidden_states=None,
                end_of_turn=False,
                text="Stop",
            ),
            cancelled,
            emitted.append,
        )
    assert emitted == []


class _FakeTokenizer:
    def encode(self, text: str, *, add_special_tokens: bool) -> list[int]:
        assert add_special_tokens is False
        return [ord(value) for value in text]


class _FakeModel:
    tokenizer = _FakeTokenizer()


class _FakeSession:
    def push_text_token(self, token_id: int) -> None:
        return None

    def finish_text(self) -> Iterator[None]:
        return iter(())


class _FakeDotsRuntime:
    model = _FakeModel()
    sample_rate = 24_000

    def __init__(self) -> None:
        self.starts = 0

    def start_double_streaming(self, **_: Any) -> _FakeSession:
        self.starts += 1
        return _FakeSession()


def test_sidecar_holds_partial_words_and_reuses_generation_session():
    dots = _FakeDotsRuntime()
    renderer = DotsRenderer(
        runtime=dots,
        prompt_audio_path="/models/voice.wav",
        prompt_text="Reference transcript.",
    )

    first = renderer.append_text(
        TextRequest(generation_id=9, text="Hel", end_of_turn=False)
    )
    second = renderer.append_text(
        TextRequest(generation_id=9, text="lo world", end_of_turn=False)
    )
    final = renderer.append_text(
        TextRequest(generation_id=9, text="!", end_of_turn=True)
    )

    assert first["token_ids"] == []
    assert "".join(chr(value) for value in second["token_ids"]) == "Hello "
    assert "".join(chr(value) for value in final["token_ids"]) == "world!"
    assert dots.starts == 1


def test_dots_benchmark_config_keeps_gander_production_config_unchanged():
    from gnsis_runtime.cli import load_config

    dots = load_config("runtime/configs/gnsis-dots-voice.yaml")
    gander = load_config("runtime/configs/gnsis-voice.yaml")

    assert dots.duplex.speech_renderer == "dots"
    assert dots.duplex.talker_checkpoint is None
    assert dots.model.token2wav_dir is None
    assert dots.duplex.detached_talker_device == "cuda:1"
    assert dots.duplex.dots_endpoint == "http://127.0.0.1:7976"

    assert gander.duplex.speech_renderer == "gander"
    assert gander.duplex.talker_checkpoint == "/models/Gander/talker"
    assert gander.model.token2wav_dir.endswith("/token2wav")
