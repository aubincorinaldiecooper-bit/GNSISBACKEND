"""The model asks the connected desktop to do something, and hears back.

These run the real socket handler and the real coordinator; only the model is
scripted. Each test names what a person would notice: an action the model was
never told it could take, an answer that went to the wrong request, a request
that was never answered and left GNSIS waiting forever, a step that ran twice
after the connection dropped.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from mcpmft.infer.realtime import DuplexStepEvent

from gnsis_runtime import host_tools as host_tools_module
from gnsis_runtime import online_duplex
from gnsis_runtime.gateway import GNSISGateway, ProviderRegistry
from gnsis_runtime.supervision import TaskLedger

from test_duplex_lifecycle import _drain_until, _expect, _settle, connect

CATALOG_PATH = Path(__file__).resolve().parents[2] / "configs" / "gnsis-host-tools.json"
ONE_UNIT = b"\x00\x00" * 16000  # one second of silence at 16 kHz


def _tool_call(name: str, arguments: dict[str, Any], index: int = 1) -> DuplexStepEvent:
    return DuplexStepEvent(
        index=index,
        is_listen=False,
        text="",
        end_of_turn=False,
        current_time=index,
        audio_waveform=None,
        metrics={},
        is_tool_call=True,
        tool_calls=[{"name": name, "arguments": arguments}],
        tool_generation_complete=True,
        tool_parse_valid=True,
        tool_schema_valid=True,
        tool_response_expected=True,
    )


def _speech(text: str, index: int = 2) -> DuplexStepEvent:
    return DuplexStepEvent(
        index=index,
        is_listen=False,
        text=text,
        end_of_turn=True,
        current_time=index,
        audio_waveform=None,
        metrics={},
    )


class ScriptedModel:
    """Stands in for the live model session behind the real coordinator.

    Each second of audio plays the next scripted step. Tool responses follow
    the real session's contract: one may only answer a call that is waiting,
    and it is taken in with the next second of audio.
    """

    def __init__(self, steps: list[tuple[DuplexStepEvent, ...]]) -> None:
        self.steps = list(steps)
        self.responses: list[Any] = []
        self.awaiting_response = False
        self.closed = False
        self.screen_frames = None
        self._lock = threading.Lock()

    # --- audio in, events out ---------------------------------------------
    def feed_pcm16(self, _data: bytes, *, unit_capture_start_ms=None) -> tuple:
        with self._lock:
            if not self.steps:
                return ()
            events = self.steps.pop(0)
            for event in events:
                if event.is_tool_call and event.tool_response_expected:
                    self.awaiting_response = True
            return events

    def feed_tool_response(self, response: Any) -> None:
        with self._lock:
            if not self.awaiting_response:
                raise RuntimeError("there is no pending tool call for this response")
            self.responses.append(response)
            self.awaiting_response = False
        return None

    def feed_runtime_event(self, _event: Any, **_kwargs: Any) -> None:
        raise AssertionError("no worker deliveries in these tests")

    # --- what the handler and coordinator also touch ----------------------
    def take_native_input_receipt(self, _event: Any) -> None:
        return None

    def set_summary_needed_callback(self, _callback: Any) -> None:
        return None

    def set_task_slate(self, _slate: str) -> bool:
        return False

    def talker_state(self) -> dict[str, Any]:
        return {"generation_id": 0, "drained": True, "turn_ended": True}

    def interrupt_output(self) -> None:
        return None

    def close(self, *, drain_speech: bool = False) -> None:
        self.closed = True

    def flush_pending(self, *, unit_capture_start_ms=None) -> tuple:
        return ()

    def should_continue_draining(self, _steps: int) -> bool:
        return False

    def should_stop_after(self, _event: Any) -> bool:
        return True

    def wait_for_speech(self) -> None:
        return None

    def poll_output(self, _timeout: float) -> None:
        return None

    def drain_outputs(self) -> tuple:
        return ()

    def enqueue_screen_frame(self, _frame: Any) -> None:
        return None

    def set_media_mode(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _Params:
    chunk_ms = 1000
    generate_audio = False
    sliding_window_mode = "context_no_previous"
    context_max_units = 64
    context_previous_max_tokens = 0
    decode_mode = "sampling"
    speak_text_tokens_per_unit = 4


class _Bundle:
    class model:  # noqa: N801 - the runtime reads bundle.model.vpm/resampler
        vpm = object()
        resampler = object()


@pytest.fixture
def desktop_app(monkeypatch, tmp_path):
    """Build the real app with a scripted model and the real coordinator."""

    def _build(steps, **settings):
        models: list[ScriptedModel] = []
        opened: list[tuple[str, ...]] = []
        coordinators: list[Any] = []

        def fake_build_session(_runtime, *, host_tools=(), **_kwargs):
            opened.append(tuple(host_tools))
            model = ScriptedModel(list(steps))
            models.append(model)
            return model

        real_coordinator = online_duplex.TaskToolsRealtimeCoordinator

        def recording_coordinator(*args, **kwargs):
            coordinator = real_coordinator(*args, **kwargs)
            coordinators.append(coordinator)
            return coordinator

        monkeypatch.setattr(online_duplex, "_build_session", fake_build_session)
        monkeypatch.setattr(online_duplex, "_prepare_static_prefix", lambda _r: None)
        monkeypatch.setattr(
            online_duplex, "TaskToolsRealtimeCoordinator", recording_coordinator
        )
        app = online_duplex.create_online_duplex_app(
            _Bundle(),
            params=_Params(),
            gateway_factory=lambda _sid: GNSISGateway(
                coordinator=None,
                providers=ProviderRegistry(()),
                ledger=TaskLedger(":memory:"),
                mode="lean",
            ),
            provider_name=None,
            settings=online_duplex.OnlineDuplexSettings(
                **{
                    "reconnect_grace_sec": 2.0,
                    "host_tool_catalog": host_tools_module.load_host_tool_catalog(
                        CATALOG_PATH
                    ),
                    **settings,
                }
            ),
            media_dir=tmp_path / "media",
        )
        return app, models, opened, coordinators

    return _build


DESKTOP = "/ws/duplex?session_id=desk1&host_tools=files,open&host_tools_version=desktop-v2"


def _timeline(coordinator) -> list[tuple[str, str | None]]:
    return [
        (event.kind, event.correlation_id)
        for event in coordinator.timeline.snapshot()
        if event.component == "tools"
    ]


# --- negotiation ---------------------------------------------------------


def test_the_shipped_catalog_is_what_the_desktop_speaks():
    catalog = host_tools_module.load_host_tool_catalog(CATALOG_PATH)
    assert catalog is not None
    assert catalog.version == "desktop-v2"
    assert catalog.names() == ("open", "files", "browser", "input")


def test_a_desktop_is_shown_only_the_tools_it_offered():
    catalog = host_tools_module.load_host_tool_catalog(CATALOG_PATH)
    agreed = host_tools_module.negotiate(catalog, "files,open,teleport", "desktop-v2")
    # Catalog order, not the order offered; an unknown name is refused.
    assert agreed.accepted == ("open", "files")
    assert agreed.rejected == ("teleport",)
    assert agreed.reason == "not_in_catalog"


def test_a_desktop_built_against_another_catalog_gets_no_tools():
    catalog = host_tools_module.load_host_tool_catalog(CATALOG_PATH)
    # desktop-v1 is the catalog before browser gained new_tab: a desktop built
    # against it would refuse the new action, so it is shown none.
    agreed = host_tools_module.negotiate(catalog, "files", "desktop-v1")
    assert agreed.accepted == ()
    assert agreed.reason == "version_mismatch"


def test_a_web_page_that_offers_nothing_changes_nothing():
    catalog = host_tools_module.load_host_tool_catalog(CATALOG_PATH)
    assert host_tools_module.negotiate(catalog, None, None) is host_tools_module.NO_HOST_TOOLS
    assert host_tools_module.negotiate(None, "files", "desktop-v2").reason == "no_catalog"


def test_malformed_names_never_reach_the_prompt():
    names, malformed = host_tools_module.parse_offer("files,Files,rm -rf,<x>,open")
    assert names == ("files", "open")
    assert malformed == ("Files", "rm -rf", "<x>")


def test_ready_lists_the_session_tools_and_what_was_agreed(desktop_app):
    app, _models, opened, _coordinators = desktop_app([])
    with TestClient(app) as client:
        with connect(client, DESKTOP) as ws:
            ready = _settle(ws)
    assert opened == [("open", "files")]
    assert ready["host_tools"]["accepted"] == ["open", "files"]
    assert ready["tool_call_protocol"] == "call_id_v1"
    assert ready["tools"][:2] == ["open", "files"]
    assert "browser" not in ready["tools"] and "input" not in ready["tools"]


def test_a_web_session_is_not_shown_desktop_tools(desktop_app):
    app, _models, opened, _coordinators = desktop_app([])
    with TestClient(app) as client:
        with connect(client, "/ws/duplex?session_id=web1") as ws:
            ready = _settle(ws)
    assert opened == [()]
    assert not {"open", "files", "browser", "input"} & set(ready["tools"])
    assert ready["host_tools"]["accepted"] == []


# --- the round trip --------------------------------------------------------


def test_the_answer_reaches_the_same_model_and_it_carries_on(desktop_app):
    steps = [
        (_tool_call("files", {"action": "move", "path": "report.pdf", "to": "Projects"}),),
        (_speech("Done, it is in Projects."),),
    ]
    app, models, _opened, coordinators = desktop_app(steps)
    with TestClient(app) as client:
        with connect(client, DESKTOP) as ws:
            _settle(ws)
            ws.send_bytes(ONE_UNIT)
            call = _expect(ws, "tool.call")
            assert call["dispatch"] == "client"
            call_id = call["call_id"]
            assert call_id.startswith("call_")
            assert call["tool_calls"] == [
                {"name": "files", "arguments": {"action": "move", "path": "report.pdf", "to": "Projects"}}
            ]

            # An answer naming another request is refused and never fed.
            ws.send_text(json.dumps({"type": "tool.response", "call_id": "call_other", "content": {"status": "done"}}))
            stale = _expect(ws, "tool.response.stale")
            assert stale == {"type": "tool.response.stale", "call_id": "call_other", "reason": "not_pending"}
            assert models[0].responses == []

            result = {"status": "done", "moved": "report.pdf", "to": "~/Projects", "verified": "disk"}
            ws.send_text(json.dumps({"type": "tool.response", "call_id": call_id, "content": result}))
            queued = _expect(ws, "tool.response.queued")
            assert queued["call_id"] == call_id
            assert models[0].responses == [result]

            # The same answer again is a replay: refused, fed nothing.
            ws.send_text(json.dumps({"type": "tool.response", "call_id": call_id, "content": result}))
            assert _expect(ws, "tool.response.stale")["reason"] == "already_finished"
            assert models[0].responses == [result]

            # The next second of audio is where the model takes the answer in.
            ws.send_bytes(ONE_UNIT)
            follow = _expect(ws, "chunk")
            assert follow["text"] == "Done, it is in Projects."
    kinds = _timeline(coordinators[0])
    assert kinds == [
        ("tool.requested", call_id),
        ("tool.response.received", call_id),
        ("tool.response.injected", call_id),
    ]


def test_a_second_step_after_an_answer_is_a_new_request(desktop_app):
    """Open Downloads, then look for the PDF: the second call is not refused."""

    steps = [
        (_tool_call("open", {"target": "Downloads"}),),
        (_tool_call("files", {"action": "find", "path": "Downloads", "query": "pdf"}, index=2),),
    ]
    app, models, _opened, _coordinators = desktop_app(steps)
    with TestClient(app) as client:
        with connect(client, DESKTOP) as ws:
            _settle(ws)
            ws.send_bytes(ONE_UNIT)
            first = _expect(ws, "tool.call")
            ws.send_text(json.dumps({"type": "tool.response", "call_id": first["call_id"], "content": {"status": "done"}}))
            _expect(ws, "tool.response.queued")
            ws.send_bytes(ONE_UNIT)
            second = _expect(ws, "tool.call")
            assert second["call_id"] != first["call_id"]
            assert second["tool_calls"][0]["name"] == "files"
            ws.send_text(json.dumps({"type": "tool.response", "call_id": second["call_id"], "content": {"status": "done", "found": []}}))
            _expect(ws, "tool.response.queued")
    # Two answers fed, and no "still pending" error was fed in between.
    assert models[0].responses == [{"status": "done"}, {"status": "done", "found": []}]


def test_an_unanswered_request_tells_the_model_it_timed_out(desktop_app):
    steps = [(_tool_call("open", {"target": "Spotify"}),)]
    app, models, _opened, coordinators = desktop_app(
        steps, external_tool_timeout_sec=0.3, external_tool_max_wait_sec=0.3
    )
    with TestClient(app) as client:
        with connect(client, DESKTOP) as ws:
            _settle(ws)
            ws.send_bytes(ONE_UNIT)
            call = _expect(ws, "tool.call")
            timeout = _expect(ws, "tool.timeout")
            assert timeout["call_id"] == call["call_id"]
            # A late answer never reaches the model as the result.
            ws.send_text(json.dumps({"type": "tool.response", "call_id": call["call_id"], "content": {"status": "done"}}))
            assert _expect(ws, "tool.response.stale")["reason"] == "already_finished"
    assert len(models[0].responses) == 1
    assert models[0].responses[0]["status"] == "timeout"
    assert "may or may not have happened" in models[0].responses[0]["message"]
    assert ("tool.timeout", call["call_id"]) in _timeline(coordinators[0])


def test_waiting_on_the_person_keeps_the_request_alive(desktop_app):
    steps = [(_tool_call("files", {"action": "move", "path": "a.txt", "to": "B"}),)]
    app, models, _opened, coordinators = desktop_app(
        steps, external_tool_timeout_sec=0.5, external_tool_max_wait_sec=5.0
    )
    with TestClient(app) as client:
        with connect(client, DESKTOP) as ws:
            _settle(ws)
            ws.send_bytes(ONE_UNIT)
            call = _expect(ws, "tool.call")
            for _ in range(3):
                time.sleep(0.3)
                ws.send_text(json.dumps({"type": "tool.progress", "call_id": call["call_id"], "state": "awaiting_confirmation"}))
            ws.send_text(json.dumps({"type": "tool.response", "call_id": call["call_id"], "content": {"status": "done"}}))
            _expect(ws, "tool.response.queued")
    assert models[0].responses == [{"status": "done"}]
    assert ("tool.timeout", call["call_id"]) not in _timeline(coordinators[0])


def test_a_request_is_sent_again_after_a_reconnect_with_the_same_id(desktop_app):
    steps = [(_tool_call("files", {"action": "rename", "path": "draft", "name": "final"}),)]
    app, models, _opened, _coordinators = desktop_app(steps)
    with TestClient(app) as client:
        with connect(client, DESKTOP) as ws:
            ready = _settle(ws)
            ws.send_bytes(ONE_UNIT)
            call = _expect(ws, "tool.call")
        resume = f"{DESKTOP}&resume_token={ready['resume_token']}"
        with connect(client, resume) as ws:
            again = _drain_until(ws, "tool.call", limit=40)
            assert again["call_id"] == call["call_id"]
            assert again["redelivered"] is True
            assert again["tool_calls"] == call["tool_calls"]
            ws.send_text(json.dumps({"type": "tool.response", "call_id": call["call_id"], "content": {"status": "done"}}))
            _expect(ws, "tool.response.queued")
    assert models[0].responses == [{"status": "done"}]


def test_a_tool_outside_the_negotiated_set_is_never_dispatched(desktop_app):
    """The model core refuses a call to a tool it was not shown; if one slips
    through as an error unit, the client is not asked to run it."""

    bad = _tool_call("input", {"action": "type", "text": "hello"})
    bad.tool_error = "tool 'input' is not available"
    bad.tool_calls = []
    app, _models, _opened, _coordinators = desktop_app([(bad,)])
    with TestClient(app) as client:
        with connect(client, DESKTOP) as ws:
            _settle(ws)
            ws.send_bytes(ONE_UNIT)
            error = _expect(ws, "tool.error")
            assert "call_id" not in error


# --- prefix snapshots -------------------------------------------------------


def test_each_tool_set_prefills_once_and_is_reused(monkeypatch):
    """A snapshot restores the tools it was built with, so a desktop session
    must never borrow the web snapshot; the first desktop session prefills
    and every later one reuses that."""

    built: list[dict[str, Any]] = []

    class FakeRunner:
        def __init__(self, tools):
            self.tools = tools

        def capture_prefix_snapshot(self):
            return type("Snap", (), {"token_count": 100 + len(self.tools)})()

    class FakeLive:
        def __init__(self, _bundle, *, tools, prefix_snapshot, **_kwargs):
            built.append({"tools": [t["name"] for t in tools], "snapshot": prefix_snapshot})
            self.runner = FakeRunner(tools)

    import mcpmft.infer.realtime as realtime

    monkeypatch.setattr(realtime, "DuplexLiveSession", FakeLive)
    monkeypatch.setattr(online_duplex, "GNSISDuplexSession", lambda live, **_kw: live)
    runtime = online_duplex._Runtime(
        bundle=None,
        params=_Params(),
        settings=online_duplex.OnlineDuplexSettings(
            host_tool_catalog=host_tools_module.load_host_tool_catalog(CATALOG_PATH)
        ),
        media_dir=Path("."),
        gateway_factory=lambda _sid: None,
        provider_name=None,
    )
    base = object()
    runtime.prefix_snapshot = base
    online_duplex._build_session(runtime, screen_frames=object())
    online_duplex._build_session(runtime, screen_frames=object(), host_tools=("open", "files"))
    online_duplex._build_session(runtime, screen_frames=object(), host_tools=("open", "files"))
    assert built[0]["snapshot"] is base
    assert "files" not in built[0]["tools"]
    assert built[1]["snapshot"] is None, "a new tool set is prefilled, not restored from the web snapshot"
    assert built[1]["tools"][:2] == ["open", "files"]
    assert built[2]["snapshot"] is runtime.prefix_snapshots[("open", "files")]


def test_a_catalog_that_reuses_a_built_in_name_fails_the_boot(tmp_path):
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"version": "x", "tools": [{"name": "haptic", "description": "", "parameters": {"type": "object", "properties": {}}}]}))
    with pytest.raises(ValueError, match="reserved"):
        online_duplex.create_online_duplex_app(
            _Bundle(),
            params=_Params(),
            gateway_factory=lambda _sid: None,
            provider_name=None,
            settings=online_duplex.OnlineDuplexSettings(
                host_tool_catalog=host_tools_module.load_host_tool_catalog(catalog)
            ),
            media_dir=tmp_path,
        )


def test_a_catalog_over_the_tool_budget_turns_actions_off_not_the_runtime(monkeypatch, tmp_path):
    """If the desktop tools do not fit the model's allowance, voice still
    starts; no session is offered the tools, and /health says why."""

    from mcpmft.tool_protocol import ToolProtocolError

    prepared: list[list[str]] = []

    class FakeRunner:
        def __init__(self, *_args, **_kwargs):
            self.tools = []

        def prepare(self, *, tools, **_kwargs):
            names = [t["name"] for t in tools]
            if "files" in names:
                raise ToolProtocolError("realtime tool schemas use 2100 tokens; maximum is 1792")
            prepared.append(names)

        def capture_prefix_snapshot(self):
            return type("Snap", (), {"token_count": 100})()

    import mcpmft.infer.online as online

    monkeypatch.setattr(online, "OnlineRunner", FakeRunner)
    runtime = online_duplex._Runtime(
        bundle=None,
        params=_Params(),
        settings=online_duplex.OnlineDuplexSettings(
            host_tool_catalog=host_tools_module.load_host_tool_catalog(CATALOG_PATH),
            warm_first_unit=False,
        ),
        media_dir=tmp_path,
        gateway_factory=lambda _sid: None,
        provider_name=None,
    )
    online_duplex._prepare_static_prefix(runtime)
    assert runtime.prefix_snapshot is not None, "the voice prefix is still prepared"
    assert "2100 tokens" in runtime.host_tools_error
    assert prepared and "files" not in prepared[0]
