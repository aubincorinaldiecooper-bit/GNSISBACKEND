"""Lifecycle tests for the online duplex endpoints.

Each test names a thing a person can observe: whether Start works after Stop,
whether one bad frame ends their screen share, whether GNSIS answers before
the Brain is warm.
"""
from __future__ import annotations

import contextlib
import json
import threading
import time

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


@contextlib.contextmanager
def connect(client, url: str, **kwargs):
    """Open a websocket, tolerating a handler that finishes before we close.

    The duplex handler returns as soon as the client is gone, so by the time
    the test client sends its own close frame the server task can already be
    finished. Starlette surfaces that as a cancelled portal future, which says
    nothing about the session under test - the assertions in each test, and
    the server's own log lines, are what decide whether it behaved.
    """

    connection = client.websocket_connect(url, **kwargs)
    websocket = connection.__enter__()
    try:
        yield websocket
    finally:
        try:
            connection.__exit__(None, None, None)
        except Exception:
            pass


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _drain_until(ws, wanted: str, limit: int = 20) -> dict:
    """Read frames until one of type ``wanted`` arrives."""

    for _ in range(limit):
        message = ws.receive_json()
        if message.get("type") == wanted:
            return message
    raise AssertionError(f"never received {wanted!r}")


def _expect(ws, wanted: str, timeout: float = 10.0) -> dict:
    """Read frames until ``wanted``, failing rather than hanging.

    ``receive_json`` blocks with no deadline of its own, so a regression that
    stops a frame ever being sent would hang the suite instead of failing it —
    which is how the session-limit tests behaved before this existed. The read
    happens on a worker thread and the deadline is enforced here.
    """

    received: dict = {}
    failure: list[BaseException] = []

    def read() -> None:
        try:
            received.update(_drain_until(ws, wanted, limit=60))
        except BaseException as exc:  # reported on the calling thread below
            failure.append(exc)

    worker = threading.Thread(target=read, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise AssertionError(f"timed out after {timeout}s waiting for {wanted!r}")
    if failure:
        raise failure[0]
    return received


def _settle(ws) -> dict:
    """Wait for `ready` and for the background Brain warmup to finish.

    Warmup reports itself after `ready`, so a test that acts before it settles
    is racing a background writer. Tests that are specifically about warmup
    timing read the frames themselves instead of calling this.
    """

    ready = _drain_until(ws, "ready")
    for _ in range(40):
        message = ws.receive_json()
        if message.get("type") == "brain.status" and message["status"] in {
            "ready",
            "error",
        }:
            return ready
    raise AssertionError("brain warmup never settled")


def test_ready_does_not_wait_for_the_brain(harness):
    """GNSIS sees and hears before Ornith answers (audit 14)."""

    gate = threading.Event()
    h = harness(warmup_gate=gate)
    try:
        with TestClient(h.app) as client:
            with connect(client, "/ws/duplex?session_id=s1") as ws:
                ready = _drain_until(ws, "ready")
                assert ready["session_id"] == "s1"
                # `ready` arrived while warmup is still blocked on the gate.
                assert not gate.is_set()
                warming = _drain_until(ws, "brain.status")
                assert warming["status"] == "warming"
    finally:
        gate.set()


def test_warmup_failure_still_reaches_ready(harness):
    """An unreachable Brain is reported, not fatal (audit 14)."""

    h = harness(warmup_fails=True)
    with TestClient(h.app) as client:
        with connect(client, "/ws/duplex?session_id=s1") as ws:
            _drain_until(ws, "ready")
            statuses = []
            for _ in range(6):
                message = ws.receive_json()
                if message.get("type") == "brain.status":
                    statuses.append(message["status"])
                    if message["status"] in {"ready", "error"}:
                        break
            assert statuses[-1] == "error"
            # The session is still usable: a ping still answers.
            ws.send_text(json.dumps({"type": "ping", "id": 7}))
            pong = _drain_until(ws, "pong")
            assert pong["id"] == 7


def test_control_error_is_recoverable(harness):
    """A bad control event does not end the session (audit 12)."""

    h = harness()
    with TestClient(h.app) as client:
        with connect(client, "/ws/duplex?session_id=s1") as ws:
            _settle(ws)
            ws.send_text(json.dumps({"type": "nonsense"}))
            error = _drain_until(ws, "error")
            assert error["fatal"] is False
            # Still alive afterwards.
            ws.send_text(json.dumps({"type": "ping", "id": 1}))
            assert _drain_until(ws, "pong")["id"] == 1


def test_malformed_json_control_is_recoverable(harness):
    """Unparseable control text is reported without ending the session."""

    h = harness()
    with TestClient(h.app) as client:
        with connect(client, "/ws/duplex?session_id=s1") as ws:
            _settle(ws)
            ws.send_text("{not json")
            error = _drain_until(ws, "error")
            assert error["fatal"] is False
            assert "invalid json" in error["message"]


def _read_rejection(client, url: str) -> dict:
    """First frame of a connection the server rejects and then closes.

    The server closes straight after replying, so the connection is unwound by
    hand: letting the context manager close an already-closed socket surfaces
    as a cancelled portal future rather than anything meaningful.
    """

    connection = client.websocket_connect(url)
    websocket = connection.__enter__()
    try:
        return websocket.receive_json()
    finally:
        try:
            connection.__exit__(None, None, None)
        except Exception:
            pass


def test_second_connection_is_told_to_retry(harness):
    """One browser, one Thinker - and the other is asked to wait, not fail."""

    h = harness()
    with TestClient(h.app) as client:
        with connect(client, "/ws/duplex?session_id=s1") as ws:
            _settle(ws)
            busy = _read_rejection(client, "/ws/duplex?session_id=s2")
            assert busy["type"] == "error"
            assert "busy" in busy["message"]
            # Busy is a retry, not a reason to tear the page down.
            assert busy["fatal"] is False
            assert busy["retry"] is True


def test_invalid_session_id_is_fatal(harness):
    """A malformed session id is the client's bug, and ends the attempt."""

    h = harness()
    with TestClient(h.app) as client:
        rejected = _read_rejection(client, "/ws/duplex?session_id=../escape")
        assert rejected["type"] == "error"
        assert rejected["fatal"] is True


def _wait_resumable(client, session_id: str, timeout: float = 5.0) -> None:
    """Wait until the server has finished parking a dropped session.

    Parking happens in the handler's teardown, so a client that reconnects
    instantly can arrive before it finishes and be told the model is busy.
    Real clients retry on that reply; the tests wait instead.
    """

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if session_id in client.get("/health").json()["resumable_sessions"]:
            return
        # Yield: polling flat out competes with the app's own event loop.
        time.sleep(0.02)
    raise AssertionError(f"{session_id} never became resumable")


def _stop(ws) -> None:
    """Send the explicit Stop control and wait for the runtime to finish."""

    ws.send_text(json.dumps({"type": "stop"}))
    _drain_until(ws, "session.done")


def test_stop_then_immediate_connect_succeeds(harness, caplog):
    """Start straight after Stop must not report the model busy (audit 16)."""

    caplog.set_level("INFO")
    h = harness()
    with TestClient(h.app) as client:
        with connect(client, "/ws/duplex?session_id=s1") as ws:
            _settle(ws)
            _stop(ws)
        # Stop is explicit, so nothing is held: the slot is free right away.
        assert client.get("/health").json()["busy"] is False
        with connect(client, "/ws/duplex?session_id=s2") as ws:
            assert _settle(ws)["session_id"] == "s2"
            _stop(ws)

    releases = [
        record for record in caplog.records
        if "released model slot" in record.getMessage()
    ]
    assert len(releases) == 2, "each session frees the slot exactly once"


def test_slot_is_released_exactly_once(harness, caplog):
    """The single slot is freed once per session, never twice (audit 16)."""

    caplog.set_level("INFO")
    h = harness()
    with TestClient(h.app) as client:
        with connect(client, "/ws/duplex?session_id=s1") as ws:
            _settle(ws)
            _stop(ws)
    releases = [
        record for record in caplog.records
        if "released model slot for s1" in record.getMessage()
    ]
    assert len(releases) == 1


def test_a_dropped_socket_is_held_then_released_once(harness, caplog):
    """A drop is held for the grace window, then torn down once (audit 11)."""

    caplog.set_level("INFO")
    h = harness(reconnect_grace_sec=0.4)
    with TestClient(h.app) as client:
        with connect(client, "/ws/duplex?session_id=s1") as ws:
            _settle(ws)
        # Still held: the client may yet come back.
        assert client.get("/health").json()["busy"] is True
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if not client.get("/health").json()["busy"]:
                break
            time.sleep(0.02)
        assert client.get("/health").json()["busy"] is False

    releases = [
        record for record in caplog.records
        if "released model slot for s1" in record.getMessage()
    ]
    assert len(releases) == 1, "the grace timer frees the slot exactly once"
    assert h.thinkers[0].close_count == 1


def test_same_session_resume_keeps_the_thinker(harness):
    """Coming back inside the window reuses the session (audit 11)."""

    h = harness(reconnect_grace_sec=5.0)
    with TestClient(h.app) as client:
        with connect(client, "/ws/duplex?session_id=s1") as ws:
            first = _settle(ws)
            token = first["resume_token"]
            assert first["resumed"] is False
        _wait_resumable(client, "s1")
        # Same id and token, inside the window: same Thinker, no new open.
        with connect(
            client, f"/ws/duplex?session_id=s1&resume_token={token}"
        ) as ws:
            again = _drain_until(ws, "ready")
            assert again["resumed"] is True
            assert again["session_id"] == "s1"
            _stop(ws)
    assert len(h.thinkers) == 1, "resume must not open a second Thinker"


def test_resume_without_the_token_is_refused(harness):
    """Another tab cannot adopt a held session by guessing its id."""

    h = harness(reconnect_grace_sec=5.0)
    with TestClient(h.app) as client:
        with connect(client, "/ws/duplex?session_id=s1") as ws:
            _settle(ws)
        _wait_resumable(client, "s1")
        busy = _read_rejection(client, "/ws/duplex?session_id=s1")
        assert busy["type"] == "error"
        assert busy["retry"] is True
        wrong = _read_rejection(
            client, "/ws/duplex?session_id=s1&resume_token=not-the-token"
        )
        assert wrong["retry"] is True


def test_screen_channel_survives_a_duplex_drop(harness):
    """The share is not interrupted while the duplex reconnects (audit 11)."""

    h = harness(media_mode="omni", reconnect_grace_sec=5.0)
    with TestClient(h.app) as client:
        duplex = client.websocket_connect("/ws/duplex?session_id=s1")
        ws = duplex.__enter__()
        ready = _settle(ws)
        token = ready["resume_token"]
        with connect(
            client,
            f"/ws/screen?session_id=s1&token={ready['screen']['token']}",
        ) as screen:
            _drain_until(screen, "screen.ready")
            # Drop the duplex socket only.
            try:
                duplex.__exit__(None, None, None)
            except Exception:
                pass
            _wait_resumable(client, "s1")
            # The screen channel is still up and still accepting frames.
            screen.send_text(json.dumps(_screen_header("f1")))
            screen.send_bytes(_jpeg())
            assert _drain_until(screen, "screen.frame.accepted")["frame_id"] == "f1"
            with connect(
                client, f"/ws/duplex?session_id=s1&resume_token={token}"
            ) as resumed:
                assert _drain_until(resumed, "ready")["resumed"] is True
                _stop(resumed)


def test_health_reports_the_slot_free_after_stop(harness):
    """`busy` clears on Stop, so the client can Start again immediately."""

    h = harness()
    with TestClient(h.app) as client:
        with connect(client, "/ws/duplex?session_id=s1") as ws:
            _settle(ws)
            assert client.get("/health").json()["busy"] is True
            _stop(ws)
        assert client.get("/health").json()["busy"] is False


def _jpeg(width: int = 32, height: int = 24) -> bytes:
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (width, height), (10, 120, 200)).save(buffer, format="JPEG")
    return buffer.getvalue()


def _screen_header(frame_id: str) -> dict:
    return {
        "type": "screen.frame",
        "frame_id": frame_id,
        "captured_at_ms": 1_000,
        "encoding": "jpeg",
        "video_source": "screen",
    }


def _open_screen(client, ws_duplex):
    """Attach a screen channel to a duplex session that is already ready."""

    ready = _settle(ws_duplex)
    screen = ready["screen"]
    assert screen["enabled"] is True
    return connect(
        client,
        f"/ws/screen?session_id={ready['session_id']}&token={screen['token']}",
    )


def test_one_bad_frame_does_not_close_the_screen(harness):
    """A malformed frame is dropped; the share survives it (audit 13)."""

    h = harness(media_mode="omni")
    with TestClient(h.app) as client:
        with connect(client, "/ws/duplex?session_id=s1") as ws:
            with _open_screen(client, ws) as screen:
                _drain_until(screen, "screen.ready")

                # Metadata that is not JSON at all.
                screen.send_text("{not json")
                dropped = _drain_until(screen, "screen.frame.dropped")
                assert dropped["frame_id"] is None
                assert dropped["reason"]

                # A header that parses but names an encoding we do not accept.
                bad = _screen_header("f2") | {"encoding": "tiff"}
                screen.send_text(json.dumps(bad))
                dropped = _drain_until(screen, "screen.frame.dropped")
                assert "encoding" in dropped["reason"]

                # The socket is still usable: a good frame is accepted.
                screen.send_text(json.dumps(_screen_header("f3")))
                screen.send_bytes(_jpeg())
                accepted = _drain_until(screen, "screen.frame.accepted")
                assert accepted["frame_id"] == "f3"
                assert accepted["width"] == 32


def test_undecodable_image_is_dropped_not_fatal(harness):
    """Bytes that are not an image drop one frame, not the channel."""

    h = harness(media_mode="omni")
    with TestClient(h.app) as client:
        with connect(client, "/ws/duplex?session_id=s1") as ws:
            with _open_screen(client, ws) as screen:
                _drain_until(screen, "screen.ready")
                screen.send_text(json.dumps(_screen_header("f1")))
                screen.send_bytes(b"this is not a jpeg")
                dropped = _drain_until(screen, "screen.frame.dropped")
                assert dropped["frame_id"] == "f1"

                screen.send_text(json.dumps(_screen_header("f2")))
                screen.send_bytes(_jpeg())
                assert _drain_until(screen, "screen.frame.accepted")["frame_id"] == "f2"


def test_disconnect_during_startup_frees_the_slot(harness, caplog):
    """A client that gives up while the model opens frees it (audit 15).

    The slot comes back as soon as the abandoned open has finished and been
    closed, rather than only when the whole startup sequence would have run.
    A client reconnecting in that gap is told the model is busy and retries,
    which is why this waits for `busy` to clear first.
    """

    caplog.set_level("INFO")
    gate = threading.Event()
    h = harness(open_gate=gate)
    with TestClient(h.app) as client:
        with connect(client, "/ws/duplex?session_id=s1"):
            # Leave immediately, while _open_session is still blocked.
            pass
        gate.set()
        assert _wait_until(lambda: not client.get("/health").json()["busy"])
        with connect(client, "/ws/duplex?session_id=s2") as ws:
            assert _settle(ws)["session_id"] == "s2"
            _stop(ws)


def test_an_abandoned_model_open_is_closed_before_the_slot_is_freed(harness):
    """A client that leaves mid-open must not strand a half-built session.

    The open runs on a worker thread that cancellation cannot reach, so the
    slot has to stay held until that thread produces a session and it is
    closed. Freeing it earlier would let a second session build against the
    same model while the first is finished and never closed.
    """

    gate = threading.Event()
    h = harness(open_gate=gate)
    with TestClient(h.app) as client:
        with connect(client, "/ws/duplex?session_id=s1"):
            # Leave while _build_session is still blocked on the gate.
            pass
        # The slot is not free yet: the model is still being built.
        assert client.get("/health").json()["busy"] is True
        assert h.thinkers == []

        gate.set()
        assert _wait_until(lambda: not client.get("/health").json()["busy"]), (
            "the slot is freed once the abandoned open finishes"
        )
        assert len(h.thinkers) == 1
        assert h.thinkers[0].close_count == 1, "the abandoned session is closed"

        # And the slot really is usable again.
        with connect(client, "/ws/duplex?session_id=s2") as ws:
            assert _settle(ws)["session_id"] == "s2"
            _stop(ws)


def test_flooding_before_ready_is_refused(harness):
    """Buffering what a client sends during startup is bounded."""

    from gnsis_runtime.online_duplex import STARTUP_BUFFER_MAX_MESSAGES

    gate = threading.Event()
    h = harness(open_gate=gate)
    try:
        with TestClient(h.app) as client:
            with connect(client, "/ws/duplex?session_id=s1") as ws:
                # Nothing should be sent before `ready`; this is a client
                # holding the model slot and growing the server's memory.
                for index in range(STARTUP_BUFFER_MAX_MESSAGES + 4):
                    ws.send_text(json.dumps({"type": "ping", "id": index}))
                refused = _drain_until(ws, "error", limit=40)
                assert refused["fatal"] is True
                assert "before the session was ready" in refused["message"]
    finally:
        gate.set()


# --- Bounding what one visitor can consume -----------------------------------
#
# The live page is open to anyone with the link, and a duplex socket holds the
# model slot — and so a whole GPU container — for as long as it stays open.
# These cover the two things that keep that from becoming an unbounded bill:
# the session's own time limit, and the front-door secret that stops a caller
# reaching the runtime's public address directly.


def test_no_front_door_secret_configured_accepts_every_socket(harness):
    """Shipping this before the secret is set changes nothing.

    The check is off until both ends have the value, so the deploy order can
    never take the live page down.
    """

    h = harness()
    with TestClient(h.app) as client:
        with connect(client, "/ws/duplex?session_id=s1") as ws:
            assert _settle(ws)["session_id"] == "s1"
            _stop(ws)


def test_a_socket_without_the_front_door_secret_never_reaches_the_model(harness):
    """Going around the site gets a closed socket, not a GPU."""

    h = harness(edge_secret="front-door-secret")
    with TestClient(h.app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/duplex?session_id=s1"):
                pass
    assert h.thinkers == [], "a refused socket must not open a Thinker"


def test_a_wrong_front_door_secret_is_refused(harness):
    """Knowing the header's name is not knowing its value."""

    h = harness(edge_secret="front-door-secret")
    with TestClient(h.app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "/ws/duplex?session_id=s1",
                headers={"X-GNSIS-Edge": "front-door-secre"},
            ):
                pass
    assert h.thinkers == []


def test_the_front_door_secret_lets_a_real_visitor_through(harness):
    """A socket forwarded by the site works exactly as before."""

    h = harness(edge_secret="front-door-secret")
    with TestClient(h.app) as client:
        with connect(
            client,
            "/ws/duplex?session_id=s1",
            headers={"X-GNSIS-Edge": "front-door-secret"},
        ) as ws:
            assert _settle(ws)["session_id"] == "s1"
            _stop(ws)


def test_the_screen_socket_needs_the_front_door_secret_too(harness):
    """Both sockets reach the runtime, so both are covered."""

    h = harness(edge_secret="front-door-secret")
    with TestClient(h.app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/screen?session_id=s1&token=x"):
                pass


def test_a_session_ends_when_it_reaches_its_time_limit(harness):
    """An open socket cannot hold a GPU indefinitely.

    This is the abandoned tab: nobody is there, nothing is being sent, and
    before the limit existed the container stayed up until the client's
    browser gave up or the machine was noticed on a bill.
    """

    h = harness(max_session_sec=1.5)
    with TestClient(h.app) as client:
        with connect(client, "/ws/duplex?session_id=s1") as ws:
            _settle(ws)
            ended = _expect(ws, "error")
    assert ended["fatal"] is True
    assert "time limit" in ended["message"]


def test_the_slot_is_free_once_a_session_times_out(harness):
    """The point of the limit is the GPU coming back, not the message."""

    h = harness(max_session_sec=1.0, reconnect_grace_sec=5.0)
    with TestClient(h.app) as client:
        with connect(client, "/ws/duplex?session_id=s1") as ws:
            _settle(ws)
            _expect(ws, "error")
        # An expired session is not parked for a resume: it is over, so the
        # slot must not be held through the reconnect grace by a client that
        # has already been told it cannot come back.
        assert _wait_until(
            lambda: client.get("/health").json()["busy"] is False
        ), "the model slot was still held after the session timed out"


def test_a_resume_does_not_extend_the_time_limit(harness):
    """Dropping and reconnecting cannot buy more GPU time.

    The budget belongs to the session, not to the socket — otherwise holding a
    machine forever is just a reconnect loop.
    """

    h = harness(max_session_sec=1.5, reconnect_grace_sec=5.0)
    with TestClient(h.app) as client:
        with connect(client, "/ws/duplex?session_id=s1") as ws:
            token = _settle(ws)["resume_token"]
        _wait_resumable(client, "s1")
        time.sleep(0.8)
        with connect(
            client, f"/ws/duplex?session_id=s1&resume_token={token}"
        ) as ws:
            assert _drain_until(ws, "ready")["resumed"] is True
            # What remains is what the first connection did not spend.
            ended = _expect(ws, "error")
    assert "time limit" in ended["message"]


def test_a_huge_audio_frame_cannot_outlast_the_time_limit(harness):
    """One frame must not buy unbounded GPU time.

    Nothing bounds how much audio a single frame carries, and the runtime
    splits whatever arrives into chunks and feeds every one of them to the
    model. Checking the clock only between messages therefore checked it in
    the one place a determined client never goes: send one enormous frame just
    before the deadline and the model stays busy for as long as the frame is
    long. The deadline is checked between chunks for that reason.
    """

    h = harness(max_session_sec=1.0)
    with TestClient(h.app) as client:
        with connect(client, "/ws/duplex?session_id=s1") as ws:
            _settle(ws)
            thinker = h.thinkers[0]
            # 50 ms of model time per chunk, at 32 000 bytes a chunk (16 kHz,
            # 1 s units, 16-bit). Feeding all 60 would take about three times
            # the whole session's budget.
            thinker.feed_delay = 0.05
            chunks = 60
            ws.send_bytes(b"\x00" * (32_000 * chunks))
            ended = _expect(ws, "error")

    assert "time limit" in ended["message"]
    assert len(thinker.fed) < chunks, (
        f"fed {len(thinker.fed)} of {chunks} chunks — the frame outlived the "
        "deadline instead of being cut short by it"
    )
