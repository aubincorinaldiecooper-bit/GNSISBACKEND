import asyncio
import base64
import io
import json

import pytest
from PIL import Image

from gnsis_runtime.screen import LatestScreenFrameBuffer, ScreenFrame
from gnsis_runtime.visual.browser_bridge import (
    BrowserExecutor,
    BrowserHubPeer,
    StaleFrameError,
    build_action_request,
    parse_capture_frame,
    report_from_bridge,
    serve_in_thread,
)
from gnsis_runtime.visual.control import ActionAuthority, VisualStep
from gnsis_runtime.visual.real_runs import Point, RealRunRecord


def _frame(frame_id="f-1", size=(640, 400), tab_id=7) -> ScreenFrame:
    metadata = {"source": {"kind": "browser_tab", "tab_id": tab_id}} if tab_id else {}
    return ScreenFrame(frame_id, Image.new("RGB", size, "white"), captured_at_ms=1000, metadata=metadata)


def _authority(**overrides) -> ActionAuthority:
    values = dict(
        turn_id="turn-1",
        provenance="direct_user",
        policy_decision="allow",
        policy_reason="asked for directly",
        capability_manifest_id="browser-v1",
        allowed_actions=(
            "click",
            "type",
            "select",
            "scroll",
            "navigate",
            "open_url",
            "back",
            "reload",
            "wait",
            "done",
            "recover",
            "switch_tab",
            "close_tab",
        ),
        confirmation="not_required",
    )
    values.update(overrides)
    return ActionAuthority(**values)


def _step(**overrides) -> VisualStep:
    values = dict(
        run_id="r",
        case_id="c",
        goal="Pick CAD",
        action="click",
        frame_id="f-1",
        target=Point(320, 100),
        authority=_authority(),
    )
    values.update(overrides)
    return VisualStep(**values)


def _jpeg_b64(color="white") -> str:
    out = io.BytesIO()
    Image.new("RGB", (64, 40), color).save(out, format="JPEG")
    return base64.b64encode(out.getvalue()).decode()


# ------------------------------------------------------------------ requests


def test_request_uses_frame_pixels_and_the_frames_own_size():
    request = build_action_request(_step(), call_id="call_1", frame=_frame(), source_tab_id=7)
    assert request == {
        "type": "browser.action",
        "call_id": "call_1",
        "frame_id": "f-1",
        "source_tab_id": 7,
        "authority": _authority().to_json(),
        "decision": {"action": "click", "target": {"x": 320, "y": 100}, "viewport": {"width": 640, "height": 400}},
    }


def test_request_fails_closed_without_or_without_approved_authority():
    with pytest.raises(PermissionError):
        build_action_request(_step(authority=None), call_id="c", frame=_frame(), source_tab_id=7)
    with pytest.raises(PermissionError):
        build_action_request(
            _step(authority=_authority(policy_decision="confirm", confirmation="missing")),
            call_id="c",
            frame=_frame(),
            source_tab_id=7,
        )


def test_request_refuses_actions_outside_the_capability_manifest():
    with pytest.raises(PermissionError):
        build_action_request(
            _step(authority=_authority(allowed_actions=("wait",))),
            call_id="c",
            frame=_frame(),
            source_tab_id=7,
        )


def test_intent_not_traced_to_the_person_needs_explicit_approval_even_when_policy_allows():
    for provenance in ("unknown", "observed_untrusted"):
        assert _authority(provenance=provenance).execution_allowed is False
        with pytest.raises(PermissionError, match=f"{provenance} provenance"):
            build_action_request(
                _step(authority=_authority(provenance=provenance)), call_id="c", frame=_frame(), source_tab_id=7
            )
        assert _authority(provenance=provenance, confirmation="approved").execution_allowed is True
    # Where the person is on record, the policy decision stands.
    for provenance in ("direct_user", "mixed", "delegated_result"):
        assert _authority(provenance=provenance).execution_allowed is True


def test_a_request_needs_the_tab_its_frame_came_from():
    untied = {
        "back": {"target": None},
        "reload": {"target": None},
        "scroll": {"target": None, "direction": "down"},
        "click": {},
    }
    for action, extra in untied.items():
        with pytest.raises(StaleFrameError, match="observe again"):
            build_action_request(
                _step(action=action, **extra), call_id="c", frame=_frame(tab_id=None), source_tab_id=None
            )
    for action in ("wait", "done"):
        request = build_action_request(_step(action=action, target=None), call_id="c", frame=None, source_tab_id=None)
        assert request["source_tab_id"] is None


def test_target_cleanup_is_off_unless_a_step_opts_in_and_is_capped_at_24px():
    plain = build_action_request(_step(), call_id="c", frame=_frame(), source_tab_id=7)
    assert "resolve_target" not in plain["decision"]
    opted = build_action_request(_step(resolve_target=True, max_radius_px=16), call_id="c", frame=_frame(), source_tab_id=7)
    assert opted["decision"]["resolve_target"] is True and opted["decision"]["max_radius_px"] == 16
    with pytest.raises(ValueError):
        _step(resolve_target=True, max_radius_px=40)


def test_every_surfaced_browser_action_can_be_requested_and_others_cannot():
    for action, extra in {
        "select": {"target": Point(10, 10), "option": "CAD"},
        "scroll": {"target": None, "direction": "right"},
        "open_url": {"target": None, "url": "https://example.com"},
        "reload": {"target": None},
        "switch_tab": {"target": None, "tab_id": 3},
        "close_tab": {"target": None, "tab_id": 3},
        "wait": {"target": None, "wait_ms": 300},
    }.items():
        request = build_action_request(_step(action=action, **extra), call_id="c", frame=_frame(), source_tab_id=7)
        assert request["decision"]["action"] == action
    with pytest.raises(ValueError):
        build_action_request(_step(action="drag"), call_id="c", frame=_frame(), source_tab_id=7)
    with pytest.raises(ValueError):
        build_action_request(_step(), call_id="c", frame=None, source_tab_id=7)


# ------------------------------------------------------------------- results


def _result(**evidence):
    base = {
        "context": "browser",
        "action": "click",
        "source_tab_id": 7,
        "executed_tab_id": 7,
        "started_at_ms": 5000,
        "completed_at_ms": 5040,
        "latency_ms": 40,
        "source_viewport": {"width": 640, "height": 400},
        "raw_target": {"x": 320, "y": 100},
        "resolve_target": False,
        "resolution_method": "raw-point",
        "resolved_target": None,
        "target_box": {"x": 600, "y": 180, "width": 120, "height": 40},
        "page_viewport": {"width": 1280, "height": 800},
    }
    base.update(evidence)
    return {"type": "browser.action.result", "call_id": "call_1", "frame_id": "f-1", "success": True, "done": False, "message": "Clicked", "evidence": base}


def test_evidence_becomes_a_canonical_report_in_frame_pixels():
    request = build_action_request(_step(), call_id="call_1", frame=_frame(), source_tab_id=7)
    report = report_from_bridge(_result(), request=request, frame_size=(640, 400))
    assert report.execution.actuator_success is True
    assert report.execution.executed_variant == "raw"
    assert report.execution.call_id == "call_1"
    assert report.acted_at_ms == 5000
    assert report.candidates["raw"].point == Point(320, 100)
    # The page box (CSS px, a 1280x800 layout viewport) lands in the 640x400 frame at half scale.
    box = report.target_box
    assert (box.x, box.y, box.width, box.height) == pytest.approx((300, 90, 60, 20))
    assert box.contains(report.candidates["raw"].point)


def test_r24_resolution_is_its_own_candidate_and_marks_the_executed_variant():
    request = build_action_request(_step(resolve_target=True), call_id="call_1", frame=_frame(), source_tab_id=7)
    report = report_from_bridge(
        _result(resolve_target=True, resolution_method="nearby", resolved_target={"x": 660, "y": 200}),
        request=request,
        frame_size=(640, 400),
    )
    assert report.execution.executed_variant == "raw+r24"
    assert report.candidates["raw+r24"].point == Point(330, 100)
    assert report.candidates["raw+r24"].method == "nearby"


def test_without_the_page_viewport_geometry_is_left_out_rather_than_guessed():
    request = build_action_request(_step(resolve_target=True), call_id="call_1", frame=_frame(), source_tab_id=7)
    report = report_from_bridge(
        _result(page_viewport=None, resolve_target=True, resolution_method="nearby", resolved_target={"x": 660, "y": 200}),
        request=request,
        frame_size=(640, 400),
    )
    assert report.target_box is None
    assert "raw+r24" not in report.candidates
    assert report.metadata["bridge"]["page_target_box"] == {"x": 600, "y": 180, "width": 120, "height": 40}


def test_a_browser_error_is_an_action_that_was_not_carried_out():
    request = build_action_request(_step(resolve_target=True), call_id="call_1", frame=_frame(), source_tab_id=7)
    report = report_from_bridge(
        {"type": "error", "call_id": "call_1", "message": "Actuator target resolution abstained: ambiguous nearby controls"},
        request=request,
        frame_size=(640, 400),
    )
    assert report.execution.actuator_success is False
    assert "abstained" in report.execution.error
    assert report.candidates["raw+r24"].status == "abstained"
    assert report.acted_at_ms is None


def test_unknown_bridge_outcome_is_not_misreported_as_not_carried_out():
    request = build_action_request(_step(), call_id="call_1", frame=_frame(), source_tab_id=7)
    report = report_from_bridge(
        {
            "type": "error",
            "call_id": "call_1",
            "message": "cancellation unconfirmed",
            "outcome_unknown": True,
        },
        request=request,
        frame_size=(640, 400),
    )
    assert report.execution.actuator_success is None
    assert report.execution.error is None
    assert report.metadata["outcome_unknown"] is True
    assert report.metadata["bridge"]["error"] == "cancellation unconfirmed"


@pytest.mark.parametrize(
    ("evidence", "field"),
    [
        ({"target_box": {"x": 600, "y": 180, "height": 40}}, "target_box"),
        ({"target_box": "600,180,120,40"}, "target_box"),
        ({"resolve_target": True, "resolution_method": "nearby", "resolved_target": {"x": "left", "y": 200}}, "resolved_target"),
        ({"page_viewport": {"width": "wide", "height": 800}}, "page_viewport"),
        ({"started_at_ms": "soon"}, "started_at_ms"),
        ({"completed_at_ms": 4000}, "completed_at_ms"),
        ({"latency_ms": -3}, "latency_ms"),
    ],
)
def test_malformed_evidence_from_an_action_that_ran_is_left_out_and_named(evidence, field):
    request = build_action_request(_step(resolve_target=True), call_id="call_1", frame=_frame(), source_tab_id=7)
    report = report_from_bridge(_result(**evidence), request=request, frame_size=(640, 400))
    assert report.execution.actuator_success is True
    assert report.candidates["raw"].point == Point(320, 100)
    assert report.metadata["bridge"]["malformed_evidence"] == [field]


def test_reports_fit_the_canonical_record():
    request = build_action_request(_step(), call_id="call_1", frame=_frame(), source_tab_id=7)
    report = report_from_bridge(_result(), request=request, frame_size=(640, 400))
    record = RealRunRecord(
        run_id="r",
        case_id="c",
        captured_at_ms=1,
        context="browser",
        frame_id="f-1",
        goal="g",
        action="click",
        viewport=report.viewport,
        candidates=dict(report.candidates),
        execution=report.execution,
        target_box=report.target_box,
        verification_status="ambiguous",
        verification_reason="not judged in this test",
    )
    assert RealRunRecord.from_json(json.loads(json.dumps(record.to_json()))) == record


# -------------------------------------------------------------------- frames


def test_captured_frames_become_canonical_screen_frames_with_provenance():
    frame = parse_capture_frame(
        {
            "type": "capture.frame",
            "frame_id": "0b6f-uuid",
            "captured_at_ms": 1790000000000,
            "encoding": "jpeg",
            "image_base64": _jpeg_b64(),
            "source": {"tab_id": 7, "capture_session_id": "cap-1", "width": 64, "height": 40, "stray": "ignored"},
        }
    )
    header = frame.screen_header()
    assert header["type"] == "screen.frame" and header["frame_id"] == "0b6f-uuid"
    assert header["metadata"]["source"] == {
        "kind": "browser_tab",
        "tab_id": 7,
        "capture_session_id": "cap-1",
        "width": 64,
        "height": 40,
        "captured_at_ms": 1790000000000,
    }
    screen = frame.to_screen_frame()
    assert screen.image.size == (64, 40)
    assert screen.metadata["source"]["tab_id"] == 7
    for bad in ({"frame_id": "", "captured_at_ms": 1, "image_base64": _jpeg_b64()}, {"frame_id": "f", "captured_at_ms": 1, "image_base64": "%%%"}):
        with pytest.raises((ValueError, KeyError)):
            parse_capture_frame(bad)


# ---------------------------------------------------------------------- peer


class FakeHub:
    """Speaks the hub side of the protocol, like hub-ws.ts."""

    def __init__(self, port, *, origin=None, reply=True):
        self.port = port
        self.origin = origin
        self.reply = reply
        self.received = []

    async def run(self, frames=()):
        from websockets.asyncio.client import connect

        headers = {"Origin": self.origin} if self.origin else None
        async with connect(f"ws://127.0.0.1:{self.port}", additional_headers=headers) as ws:
            await ws.send(json.dumps({"type": "ready", "session_id": "hub-1"}))
            for frame in frames:
                await ws.send(json.dumps(frame))
            async for raw in ws:
                message = json.loads(raw)
                self.received.append(message)
                if message["type"] == "browser.action" and self.reply:
                    await ws.send(json.dumps({**_result(), "call_id": message["call_id"]}))
                if message["type"] == "stop":
                    return


def test_peer_sends_one_action_and_returns_its_correlated_answer():
    async def scenario():
        frames = []
        peer = BrowserHubPeer(allowed_origins=None, on_frame=frames.append)
        port = await peer.start()
        hub = FakeHub(port)
        task = asyncio.create_task(
            hub.run(frames=[{"type": "capture.frame", "frame_id": "cap-1", "captured_at_ms": 10, "image_base64": _jpeg_b64(), "source": {"tab_id": 7}}])
        )
        assert await peer.wait_connected(5) == "hub-1"
        request = build_action_request(_step(), call_id="call_x", frame=_frame(), source_tab_id=7)
        answer = await peer.act(request, timeout_s=5)
        assert answer["type"] == "browser.action.result" and answer["call_id"] == "call_x"
        assert hub.received[0]["call_id"] == "call_x"
        assert [frame.frame_id for frame in frames] == ["cap-1"]
        await peer.close()
        task.cancel()

    asyncio.run(scenario())


def test_an_unanswered_action_stays_indeterminate_when_cancellation_is_unconfirmed():
    async def scenario():
        peer = BrowserHubPeer(allowed_origins=None)
        port = await peer.start()
        hub = FakeHub(port, reply=False)
        task = asyncio.create_task(hub.run())
        await peer.wait_connected(5)
        request = build_action_request(_step(), call_id="call_slow", frame=_frame(), source_tab_id=7)
        answer = await peer.act(request, timeout_s=0.2)
        assert answer["type"] == "error"
        assert answer["outcome_unknown"] is True
        assert "cancellation unconfirmed" in answer["message"]
        await asyncio.wait_for(task, 5)
        assert hub.received[-1] == {"type": "stop", "call_id": "call_slow"}
        await peer.close()

    asyncio.run(scenario())


def test_capture_stopped_failure_is_recorded_and_forwarded():
    async def scenario():
        stops = []
        peer = BrowserHubPeer(allowed_origins=None, on_capture_stopped=stops.append)
        await peer.start()
        await peer._dispatch(
            {
                "type": "capture.stopped",
                "reason": "failed",
                "message": "No frame arrived from the tab for 10 s.",
                "capture_session_id": "cap-1",
            }
        )
        assert peer.capture_stops == stops
        assert stops[0]["reason"] == "failed"
        assert stops[0]["capture_session_id"] == "cap-1"
        await peer.close()

    asyncio.run(scenario())


def test_only_the_extension_origin_may_connect():
    async def scenario():
        peer = BrowserHubPeer()
        port = await peer.start()
        with pytest.raises(Exception):
            await FakeHub(port, origin="http://localhost:3000").run()
        with pytest.raises(Exception):
            await FakeHub(port).run()
        await peer.close()

    asyncio.run(scenario())


def test_executor_blocks_a_step_without_authority_before_sending():
    peer = BrowserHubPeer(allowed_origins=None)
    buffer = LatestScreenFrameBuffer()
    buffer.publish(_frame(tab_id=11))
    buffer.consume_for_unit()
    executor = BrowserExecutor(peer, buffer, timeout_s=1)
    report = executor.execute(_step(authority=None))
    assert report.execution.actuator_success is False
    assert report.metadata["policy_blocked"] is True
    assert executor.sent == []


def test_executor_does_not_send_an_action_it_cannot_tie_to_a_tab():
    peer = BrowserHubPeer(allowed_origins=None)
    buffer = LatestScreenFrameBuffer()
    buffer.publish(_frame(tab_id=None))
    buffer.consume_for_unit()
    executor = BrowserExecutor(peer, buffer, timeout_s=1)
    # A frame with no browser tab behind it, and a frame that has left the history.
    for step in (_step(action="back", target=None), _step(frame_id="gone")):
        report = executor.execute(step)
        assert report.execution.actuator_success is False
        assert "observe again" in report.execution.error
        assert report.metadata["stale_frame"] is True
        assert "policy_blocked" not in report.metadata
    assert executor.sent == []


def test_executor_carries_frame_provenance_and_never_reuses_a_call_id():
    peer = BrowserHubPeer(allowed_origins=None)
    serve_in_thread(peer)
    hub = FakeHub(peer.port)
    hub_task = asyncio.run_coroutine_threadsafe(hub.run(), peer.loop)
    peer.run(peer.wait_connected(5), 10)
    buffer = LatestScreenFrameBuffer()
    buffer.publish(_frame(tab_id=11))
    buffer.consume_for_unit()
    executor = BrowserExecutor(peer, buffer, timeout_s=5)
    first = executor.execute(_step())
    second = executor.execute(_step())
    assert first.execution.actuator_success is True
    assert [sent["source_tab_id"] for sent in executor.sent] == [11, 11]
    assert executor.sent[0]["call_id"] != executor.sent[1]["call_id"]
    assert second.execution.call_id == executor.sent[1]["call_id"]
    hub_task.cancel()
    peer.run(peer.close(), 10)
