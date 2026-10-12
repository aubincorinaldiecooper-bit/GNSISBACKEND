from __future__ import annotations

from fastapi.testclient import TestClient

from gnsis_runtime.local_controller import Candidate, LocalDecisionEngine, Selection
from gnsis_runtime.local_controller_api import build_local_controller_app
from gnsis_runtime.local_speech import LocalKittenSpeech
from gnsis_runtime.local_whisper import LocalWhisper


class FixedBackend:
    def __init__(self, candidates, selection):
        self._candidates = candidates
        self._selection = selection

    def candidates(self, **_kwargs):
        return self._candidates

    def select(self, **_kwargs):
        return self._selection


def request(**extra):
    return {
        "requestId": "genesis2-1",
        "text": "Create a Receipts folder",
        "step": 0,
        "history": [],
        "tools": ["files"],
        "tool_schemas": [{"name": "files", "input_schema": {"type": "object"}}],
        **extra,
    }


def test_only_an_exact_host_offered_candidate_can_be_selected():
    engine = LocalDecisionEngine(
        FixedBackend(
            [
                Candidate(
                    key="one",
                    kind="tool",
                    tool="files",
                    args={"action": "new_folder", "name": "Receipts"},
                    description="Create the requested Receipts folder.",
                ),
                Candidate(
                    key="bad",
                    kind="tool",
                    tool="shell",
                    args={"command": "rm -rf ~"},
                    description="Run an unavailable command.",
                ),
            ],
            Selection(key="one", confidence=0.91, abstain=0.02),
        )
    )

    assert engine.decide(request()) == {
        "kind": "tool",
        "tool": "files",
        "args": {"action": "new_folder", "name": "Receipts"},
        "confidence": 0.91,
    }


def test_abstention_fails_closed():
    engine = LocalDecisionEngine(
        FixedBackend(
            [
                Candidate(
                    key="one",
                    kind="tool",
                    tool="files",
                    args={"action": "list", "path": "Desktop"},
                    description="List Desktop.",
                )
            ],
            Selection(key="one", confidence=0.31, abstain=0.55),
        )
    )

    assert engine.decide(request()) == {
        "kind": "fallback",
        "reason": "The local model abstained.",
    }


def test_loopback_api_requires_its_host_token():
    engine = LocalDecisionEngine(
        FixedBackend(
            [Candidate(key="done", kind="done", description="Finished.", message="Done.")],
            Selection(key="done", confidence=0.9),
        )
    )
    client = TestClient(build_local_controller_app(engine, "secret"))

    assert client.post("/v1/decide", json=request()).status_code == 401
    response = client.post(
        "/v1/decide",
        json=request(),
        headers={"Authorization": "Bearer secret"},
    )
    assert response.status_code == 200
    assert response.json() == {"kind": "done", "message": "Done."}


def test_whisper_stays_cpu_int8_and_accepts_pcm_in_memory():
    loaded = {}

    class Segment:
        text = " open Safari "

    class Model:
        def transcribe(self, audio, **options):
            loaded["audio"] = audio.read()
            loaded["options"] = options
            return [Segment()], object()

    def loader(name, **options):
        loaded["name"] = name
        loaded["load_options"] = options
        return Model()

    whisper = LocalWhisper("small.en", loader=loader)
    text = whisper.transcribe(b"\x00\x00" * 320)

    assert text == "open Safari"
    assert loaded["name"] == "small.en"
    assert loaded["load_options"] == {"device": "cpu", "compute_type": "int8"}
    assert loaded["audio"][:4] == b"RIFF"


def test_kitten_speech_loads_on_cpu_and_returns_pcm16():
    import numpy as np

    loaded = {}

    class Model:
        sample_rate = 24_000

        def generate(self, text, voice):
            loaded["text"] = text
            loaded["voice"] = voice
            return np.array([0.0, 0.5, -1.0], dtype=np.float32)

    def loader(name, **options):
        loaded["name"] = name
        loaded["options"] = options
        return Model()

    speech = LocalKittenSpeech(loader=loader)
    engine = LocalDecisionEngine(FixedBackend([], Selection(key=None, confidence=0.0)))
    client = TestClient(build_local_controller_app(engine, None, speech=speech))
    response = client.post("/v1/speak", json={"text": " Done. "})

    assert response.status_code == 200
    assert np.frombuffer(response.content, dtype="<i2").tolist() == [0, 16383, -32767]
    assert loaded == {
        "name": "KittenML/kitten-tts-2",
        "options": {"device": "cpu"},
        "text": "Done.",
        "voice": "Luna",
    }
