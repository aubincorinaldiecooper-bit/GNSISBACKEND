from __future__ import annotations

from fastapi.testclient import TestClient

from gnsis_runtime.local_controller import (
    Candidate,
    DecisionCancelled,
    LocalDecisionEngine,
    Selection,
    schema_candidates,
)
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
        "requestId": "gnsis05-1",
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
            [
                Candidate(
                    key="done", kind="done", description="Finished.", message="Done."
                )
            ],
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
    client = TestClient(build_local_controller_app(engine, "secret", speech=speech))
    response = client.post(
        "/v1/speak",
        json={"text": " Done. "},
        headers={"Authorization": "Bearer secret"},
    )

    assert response.status_code == 200
    assert np.frombuffer(response.content, dtype="<i2").tolist() == [0, 16383, -32767]
    assert loaded == {
        "name": "KittenML/kitten-tts-2",
        "options": {"device": "cpu"},
        "text": "Done.",
        "voice": "Luna",
    }


def test_local_controller_refuses_to_run_without_a_token() -> None:
    import pytest

    with pytest.raises(ValueError):
        build_local_controller_app(
            LocalDecisionEngine(FixedBackend([], Selection(key=None, confidence=0.0))),
            "",
        )


SCHEMAS = [
    {
        "name": "open",
        "description": "Open an app, a web address, or a file or folder.",
        "parameters": {
            "type": "object",
            "required": ["target"],
            "properties": {"target": {"type": "string"}},
        },
    },
    {
        "name": "input",
        "description": "Type, press keys, click, or scroll on the desktop.",
        "parameters": {
            "type": "object",
            "required": ["action"],
            "properties": {"action": {"type": "string"}},
        },
    },
]


def test_schema_candidates_ground_the_offered_tools_in_the_request():
    candidates = schema_candidates(
        text="open Notes",
        tools=["open", "input"],
        tool_schemas=SCHEMAS,
    )
    by_tool = {c.tool: c for c in candidates if c.kind == "tool"}
    assert by_tool["open"].args == {"target": "Notes"}
    # Tools the words did not plainly name stay off the ballot.
    assert "input" not in by_tool
    kinds = [c.kind for c in candidates]
    assert kinds == ["tool", "done", "fallback"]


def test_schema_candidates_offer_every_tool_when_nothing_is_named():
    candidates = schema_candidates(
        text="what can you do for me today",
        tools=["open", "input"],
        tool_schemas=SCHEMAS,
    )
    assert {c.tool for c in candidates if c.kind == "tool"} == {"open", "input"}


def test_schema_candidates_extract_click_labels():
    candidates = schema_candidates(
        text="click the Apple menu",
        tools=["input"],
        tool_schemas=SCHEMAS,
    )
    tool = next(c for c in candidates if c.kind == "tool")
    assert tool.args == {"action": "click", "label": "Apple menu"}


def test_schema_candidates_only_cover_offered_tools():
    candidates = schema_candidates(
        text="open Notes",
        tools=["input"],
        tool_schemas=SCHEMAS,
    )
    assert all(c.tool != "open" for c in candidates)


def test_decide_stops_when_the_client_is_gone():
    reached = []

    class Watching:
        def candidates(self, **_kwargs):
            reached.append("candidates")
            return [
                Candidate(
                    key="one", kind="done", description="Finish.", message="Done."
                )
            ]

        def select(self, **_kwargs):
            reached.append("select")
            return Selection(key="one", confidence=0.9)

    engine = LocalDecisionEngine(Watching())
    import pytest

    with pytest.raises(DecisionCancelled):
        engine.decide(request(), cancelled=lambda: True)
    assert reached == []

    calls = iter([False, True])

    with pytest.raises(DecisionCancelled):
        engine.decide(request(), cancelled=lambda: next(calls))
    assert reached == ["candidates"]
