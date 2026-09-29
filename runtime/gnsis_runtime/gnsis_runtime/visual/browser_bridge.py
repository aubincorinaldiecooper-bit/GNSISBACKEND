"""GNSIS's side of the browser: the peer the Gnsis-browser hub talks to (V2, V5).

The Gnsis-browser hub page is a WebSocket client of ``ws://localhost:<port>``.
Over that one socket it carries the two things the browser owns:

- **capture** — frames of the tab it is looking at (``capture.frame``). They
  enter GNSIS as ordinary screen frames on the canonical ``screen.frame`` path,
  with their provenance (tab, capture session, viewport, capture time) in the
  frame metadata. The browser keeps no perception timeline of its own.
- **execution** — one ``browser.action`` at a time through BrowserActionBridge,
  answered by ``browser.action.result`` with execution evidence, or ``error``.

GNSIS keeps perception history, decisions, verification and the learning
record. This module turns the bridge's evidence into the canonical
:class:`~.control.ExecutionReport` that the controller verifies and records,
including converting the bridge's page-CSS geometry into the pixel space of
the frame the target was chosen on.

Replay safety: BrowserActionBridge caches only *successful* results by
``call_id`` and would run a failed ``call_id`` again, so every attempt here
gets a fresh id and no id is ever resent.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import secrets
import threading
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from PIL import Image

from ..screen import LatestScreenFrameBuffer, ScreenFrame
from .control import ExecutionReport, VisualStep
from .real_runs import Box, Candidate, Execution, Point, find_frame

LOGGER = logging.getLogger(__name__)

# The extension id fixed by Gnsis-browser's manifest key.
DEFAULT_EXTENSION_ORIGIN = "chrome-extension://akldabonmimlicnjlflnapfeklbfemhj"
BRIDGE_ACTIONS = frozenset(
    {"click", "type", "select", "scroll", "navigate", "open_url", "back", "reload", "wait", "done", "recover", "switch_tab", "close_tab"}
)
POINT_ACTIONS = frozenset({"click", "type", "select", "recover"})
MAX_ERROR = 300
MAX_FRAME_BYTES = 4 * 1024 * 1024


# ------------------------------------------------------------------ the codec


def new_call_id() -> str:
    return f"call_{secrets.token_hex(8)}"


def frame_source(frame: ScreenFrame | None) -> Mapping[str, Any]:
    """The capture provenance a frame carried in, or an empty mapping."""

    if frame is None:
        return {}
    source = frame.metadata.get("source")
    return source if isinstance(source, Mapping) else {}


def build_action_request(
    step: VisualStep,
    *,
    call_id: str,
    frame: ScreenFrame | None,
    source_tab_id: int | None,
) -> dict[str, Any]:
    """The ``browser.action`` message for one step.

    Targets are pixels of the frame the step was chosen on; the frame's size is
    sent as the source viewport so the bridge can map them onto the page.
    """

    if step.action not in BRIDGE_ACTIONS:
        raise ValueError(f"the browser cannot execute {step.action!r}")
    decision: dict[str, Any] = {"action": step.action}
    if step.target is not None:
        if frame is None:
            raise ValueError("a targeted browser action needs the frame it was chosen on")
        width, height = frame.image.size
        decision["target"] = {"x": step.target.x, "y": step.target.y}
        decision["viewport"] = {"width": width, "height": height}
    for name in ("text", "option", "url", "direction", "tab_id", "wait_ms"):
        value = getattr(step, name)
        if value is not None:
            decision[name] = value
    if step.confidence is not None:
        decision["confidence"] = step.confidence
    if step.resolve_target:
        decision["resolve_target"] = True
        decision["max_radius_px"] = 24 if step.max_radius_px is None else step.max_radius_px
    authority = step.authority
    if authority is None:
        raise PermissionError("browser action has no trusted user-intent authority")
    if not authority.execution_allowed:
        raise PermissionError(
            f"browser action policy did not allow execution: {authority.policy_decision} ({authority.policy_reason})"
        )
    return {
        "type": "browser.action",
        "call_id": call_id,
        "frame_id": step.frame_id,
        "source_tab_id": source_tab_id,
        "authority": authority.to_json(),
        "decision": decision,
    }


def _page_to_frame(page: Any, frame_size: tuple[int, int] | None) -> Callable[[float, float], tuple[float, float]] | None:
    """Inverse of the bridge's mapping: page CSS pixels -> source frame pixels.

    The bridge sends ``target / frame_size * innerSize``. Without the page's
    layout viewport in the evidence the two spaces cannot be related.
    """

    if frame_size is None or not isinstance(page, Mapping):
        return None
    try:
        page_w, page_h = float(page["width"]), float(page["height"])
    except (KeyError, TypeError, ValueError):
        return None
    if page_w <= 0 or page_h <= 0:
        return None
    frame_w, frame_h = frame_size

    def convert(x: float, y: float) -> tuple[float, float]:
        return (
            round(min(frame_w - 0.001, max(0.0, float(x) / page_w * frame_w)), 3),
            round(min(frame_h - 0.001, max(0.0, float(y) / page_h * frame_h)), 3),
        )

    return convert


def report_from_bridge(
    message: Mapping[str, Any],
    *,
    request: Mapping[str, Any],
    frame_size: tuple[int, int] | None,
) -> ExecutionReport:
    """Turn the bridge's answer into the canonical execution report.

    The answer is external input: only known fields are read, and they are
    validated by the record types they land in.
    """

    decision = request["decision"]
    call_id = str(request["call_id"])
    targeted = decision["action"] in POINT_ACTIONS and "target" in decision
    raw = Candidate("resolved", Point(float(decision["target"]["x"]), float(decision["target"]["y"]))) if targeted else None

    if message.get("type") != "browser.action.result" or message.get("success") is not True:
        error = str(message.get("message") or "the browser did not report a result")[:MAX_ERROR]
        outcome_unknown = bool(message.get("outcome_unknown"))
        policy_blocked = bool(message.get("policy_blocked"))
        candidates: dict[str, Candidate] = {}
        if raw is not None:
            candidates["raw"] = raw
            if decision.get("resolve_target") and "abstained" in error.lower():
                candidates["raw+r24"] = Candidate("abstained", method="abstained")
        return ExecutionReport(
            execution=Execution(
                actuator_success=None if outcome_unknown else False,
                call_id=call_id,
                error=None if outcome_unknown else error,
            ),
            viewport=frame_size,
            candidates=candidates,
            metadata={
                "bridge": {"error": error},
                "outcome_unknown": outcome_unknown,
                "policy_blocked": policy_blocked,
            },
        )

    evidence = message.get("evidence") if isinstance(message.get("evidence"), Mapping) else {}
    method = evidence.get("resolution_method")
    method = str(method) if method is not None else None
    to_frame = _page_to_frame(evidence.get("page_viewport"), frame_size)
    candidates = {}
    target_box = None
    if raw is not None:
        candidates["raw"] = raw
        if evidence.get("resolve_target"):
            resolved = evidence.get("resolved_target")
            if isinstance(resolved, Mapping) and to_frame is not None:
                x, y = to_frame(resolved["x"], resolved["y"])
                candidates["raw+r24"] = Candidate("resolved", Point(x, y), method)
        box = evidence.get("target_box")
        if isinstance(box, Mapping) and to_frame is not None:
            x0, y0 = to_frame(box["x"], box["y"])
            x1, y1 = to_frame(float(box["x"]) + float(box["width"]), float(box["y"]) + float(box["height"]))
            if x1 > x0 and y1 > y0:
                target_box = Box(x0, y0, round(x1 - x0, 3), round(y1 - y0, 3))
    executed = None
    if raw is not None:
        executed = "raw+r24" if evidence.get("resolve_target") and method not in (None, "raw-point") else "raw"
    started = evidence.get("started_at_ms")
    execution = Execution(
        executed_variant=executed,
        actuator_success=True,
        source_tab_id=_tab(evidence.get("source_tab_id")),
        executed_tab_id=_tab(evidence.get("executed_tab_id")),
        started_at_ms=int(started) if started is not None else None,
        completed_at_ms=int(evidence["completed_at_ms"]) if evidence.get("completed_at_ms") is not None else None,
        latency_ms=float(evidence["latency_ms"]) if evidence.get("latency_ms") is not None else None,
        call_id=call_id,
    )
    return ExecutionReport(
        execution=execution,
        # The bridge and the tab capture both stamp with the browser's clock,
        # the same clock the frames' captured_at_ms uses.
        acted_at_ms=execution.started_at_ms,
        viewport=frame_size,
        candidates=candidates,
        target_box=target_box,
        metadata={
            "bridge": {
                "message": str(message.get("message") or "")[:MAX_ERROR],
                "done": bool(message.get("done")),
                "replayed": bool(message.get("replayed")),
                "resolution_method": method,
                "page_viewport": dict(evidence["page_viewport"]) if isinstance(evidence.get("page_viewport"), Mapping) else None,
                "page_target_box": dict(evidence["target_box"]) if isinstance(evidence.get("target_box"), Mapping) else None,
            }
        },
    )


def _tab(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        tab = int(value)
    except (TypeError, ValueError):
        return None
    return tab if tab > 0 else None


@dataclass(frozen=True, slots=True)
class CapturedFrame:
    """A ``capture.frame`` from the hub, validated, still encoded."""

    frame_id: str
    captured_at_ms: int
    encoded: bytes
    encoding: str
    source: dict[str, Any]

    def screen_header(self) -> dict[str, Any]:
        """The canonical ``screen.frame`` header, provenance in ``metadata``."""

        return {
            "type": "screen.frame",
            "frame_id": self.frame_id,
            "captured_at_ms": self.captured_at_ms,
            "encoding": self.encoding,
            "video_source": "screen",
            "metadata": {"source": self.source},
        }

    def to_screen_frame(self) -> ScreenFrame:
        image = Image.open(io.BytesIO(self.encoded)).convert("RGB")
        return ScreenFrame(
            self.frame_id,
            image,
            captured_at_ms=self.captured_at_ms,
            metadata={"source": self.source, "video_source": "screen", "width": image.width, "height": image.height},
        )


def parse_capture_frame(message: Mapping[str, Any]) -> CapturedFrame:
    frame_id = str(message.get("frame_id") or "").strip()
    if not frame_id or len(frame_id) > 256:
        raise ValueError("capture.frame needs a frame_id")
    captured_at = int(message["captured_at_ms"])
    if captured_at < 0:
        raise ValueError("captured_at_ms must not be negative")
    encoding = str(message.get("encoding") or "jpeg").lower()
    if encoding not in {"jpeg", "png", "webp"}:
        raise ValueError(f"unsupported capture encoding {encoding!r}")
    encoded = base64.b64decode(str(message.get("image_base64") or ""), validate=True)
    if not encoded or len(encoded) > MAX_FRAME_BYTES:
        raise ValueError("capture.frame image is empty or too large")
    raw_source = message.get("source") if isinstance(message.get("source"), Mapping) else {}
    source: dict[str, Any] = {"kind": "browser_tab"}
    tab = _tab(raw_source.get("tab_id"))
    if tab is not None:
        source["tab_id"] = tab
    for name in ("capture_session_id",):
        if raw_source.get(name) is not None:
            source[name] = str(raw_source[name])[:128]
    for name in ("width", "height", "source_width", "source_height"):
        value = raw_source.get(name, message.get(name))
        if value is not None:
            source[name] = int(value)
    source["captured_at_ms"] = captured_at
    return CapturedFrame(frame_id, captured_at, encoded, encoding, source)


# ------------------------------------------------------------------- the peer


FrameSink = Callable[[CapturedFrame], "Awaitable[None] | None"]
CaptureStopSink = Callable[[Mapping[str, Any]], "Awaitable[None] | None"]


class BrowserHubPeer:
    """WebSocket server the hub connects to; one hub, one action at a time.

    Bound to loopback only, and by default it accepts only the Gnsis-browser
    extension's origin, so another local page cannot pose as the hub and
    receive actions or send forged evidence.
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        allowed_origins: tuple[str, ...] | None = (DEFAULT_EXTENSION_ORIGIN,),
        on_frame: FrameSink | None = None,
        on_capture_stopped: CaptureStopSink | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.allowed_origins = allowed_origins
        self.on_frame = on_frame
        self.on_capture_stopped = on_capture_stopped
        self.hub_session_id: str | None = None
        self.hub_errors: list[str] = []
        self.capture_stops: list[dict[str, Any]] = []
        self.frames_received = 0
        self.frames_rejected = 0
        self._server: Any = None
        self._socket: Any = None
        self._connected: asyncio.Event | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._action_lock: asyncio.Lock | None = None
        self.loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> int:
        from websockets.asyncio.server import serve

        self.loop = asyncio.get_running_loop()
        self._connected = asyncio.Event()
        self._action_lock = asyncio.Lock()
        origins = list(self.allowed_origins) if self.allowed_origins is not None else None
        self._server = await serve(self._handle, self.host, self.port, origins=origins, max_size=8 * 1024 * 1024)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for future in self._pending.values():
            if not future.done():
                future.set_result({"type": "error", "message": "the browser connection closed"})
        self._pending.clear()

    async def wait_connected(self, timeout_s: float = 30.0) -> str:
        assert self._connected is not None, "start() first"
        await asyncio.wait_for(self._connected.wait(), timeout_s)
        assert self.hub_session_id is not None
        return self.hub_session_id

    async def _handle(self, websocket: Any) -> None:
        previous = self._socket
        self._socket = websocket
        if previous is not None:
            await previous.close(code=1000, reason="a newer hub connected")
        try:
            async for raw in websocket:
                if not isinstance(raw, str):
                    continue
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(message, dict):
                    await self._dispatch(message)
        finally:
            if self._socket is websocket:
                self._socket = None
                self.hub_session_id = None
                if self._connected is not None:
                    self._connected.clear()
                for future in self._pending.values():
                    if not future.done():
                        future.set_result({"type": "error", "message": "the browser connection closed"})

    async def _dispatch(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "ready":
            self.hub_session_id = str(message.get("session_id") or "") or None
            if self.hub_session_id and self._connected is not None:
                self._connected.set()
            return
        if kind == "capture.stopped":
            stop = {
                "reason": str(message.get("reason") or "failed"),
                "message": str(message.get("message") or "")[:MAX_ERROR],
                "capture_session_id": str(message.get("capture_session_id") or "")[:128] or None,
            }
            self.capture_stops.append(stop)
            if stop["reason"] == "failed":
                LOGGER.warning("browser capture stopped: %s", stop["message"] or "unknown failure")
            if self.on_capture_stopped is not None:
                result = self.on_capture_stopped(stop)
                if asyncio.iscoroutine(result):
                    await result
            return
        if kind == "capture.frame":
            try:
                frame = parse_capture_frame(message)
            except (KeyError, TypeError, ValueError) as exc:
                self.frames_rejected += 1
                LOGGER.warning("capture.frame rejected: %s", exc)
                return
            self.frames_received += 1
            if self.on_frame is not None:
                result = self.on_frame(frame)
                if asyncio.iscoroutine(result):
                    await result
            return
        if kind in {"browser.action.result", "error"}:
            call_id = message.get("call_id")
            future = self._pending.get(str(call_id)) if call_id is not None else None
            if future is not None and not future.done():
                future.set_result(message)
            elif kind == "error":
                self.hub_errors.append(str(message.get("message") or "")[:MAX_ERROR])
            return

    async def _send(self, message: Mapping[str, Any]) -> None:
        if self._socket is None:
            raise ConnectionError("no browser hub is connected")
        await self._socket.send(json.dumps(message))

    async def act(self, request: Mapping[str, Any], *, timeout_s: float = 30.0) -> dict[str, Any]:
        """Send one action and wait for its correlated answer.

        On timeout the action is cancelled and reported as not carried out; the
        same call id is never sent again.
        """

        assert self.loop is not None and self._action_lock is not None, "start() first"
        call_id = str(request["call_id"])
        if call_id in self._pending:
            raise ValueError(f"call id {call_id} is already in flight")
        async with self._action_lock:
            future: asyncio.Future[dict[str, Any]] = self.loop.create_future()
            self._pending[call_id] = future
            sent = False
            try:
                await self._send(request)
                sent = True
                return await asyncio.wait_for(future, timeout_s)
            except asyncio.TimeoutError:
                # A stop request is best-effort; without a terminal browser
                # acknowledgement we cannot know whether the side effect ran.
                try:
                    await self._send({"type": "stop", "call_id": call_id})
                except ConnectionError:
                    pass
                return {
                    "type": "error",
                    "call_id": call_id,
                    "message": f"no answer from the browser within {timeout_s:g}s; cancellation unconfirmed",
                    "outcome_unknown": True,
                }
            except ConnectionError as exc:
                return {
                    "type": "error",
                    "call_id": call_id,
                    "message": str(exc),
                    "outcome_unknown": sent,
                }
            finally:
                self._pending.pop(call_id, None)

    async def start_capture(self, *, fps: float = 2.0, max_edge: int = 1280, quality: float = 0.82) -> None:
        await self._send({"type": "capture.start", "fps": fps, "max_edge": max_edge, "quality": quality})

    async def stop_capture(self) -> None:
        await self._send({"type": "capture.stop"})

    def run(self, coro: Awaitable[Any], timeout_s: float) -> Any:
        """Run a coroutine on the peer's loop from another thread."""

        assert self.loop is not None, "start() first"
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout_s)


class BrowserExecutor:
    """The controller's executor for the browser environment.

    The step's frame is looked up in the shared history so its size and its
    capture provenance (the tab it came from) travel with the action; the
    bridge refuses to act if that tab is no longer the one it would act on.
    """

    def __init__(self, peer: BrowserHubPeer, screen_frames: LatestScreenFrameBuffer, *, timeout_s: float = 30.0) -> None:
        self.peer = peer
        self.screen_frames = screen_frames
        self.timeout_s = timeout_s
        self.sent: list[dict[str, Any]] = []

    def execute(self, step: VisualStep) -> ExecutionReport:
        frame = find_frame(self.screen_frames, step.frame_id)
        source_tab_id = _tab(frame_source(frame).get("tab_id"))
        call_id = new_call_id()
        try:
            request = build_action_request(step, call_id=call_id, frame=frame, source_tab_id=source_tab_id)
        except PermissionError as exc:
            return ExecutionReport(
                execution=Execution(actuator_success=False, call_id=call_id, error=str(exc)[:MAX_ERROR]),
                viewport=frame.image.size if frame is not None else None,
                metadata={
                    "policy_blocked": True,
                    "authority": step.authority.to_json() if step.authority is not None else None,
                },
            )
        self.sent.append(request)
        message = self.peer.run(self.peer.act(request, timeout_s=self.timeout_s), self.timeout_s + 5)
        report = report_from_bridge(message, request=request, frame_size=frame.image.size if frame is not None else None)
        return ExecutionReport(
            execution=report.execution,
            acted_at_ms=report.acted_at_ms,
            viewport=report.viewport,
            candidates=report.candidates,
            target_box=report.target_box,
            source_ref=report.source_ref,
            metadata={**dict(report.metadata), "authority": step.authority.to_json() if step.authority is not None else None},
        )


class ScreenChannel:
    """A ``/ws/screen`` client: forwards captured frames on the canonical path.

    The encoded image is passed through untouched; the runtime decodes,
    validates and publishes it like any other screen frame.
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self.accepted: list[str] = []
        self.dropped: list[tuple[str, str]] = []
        self._socket: Any = None
        self._lock = asyncio.Lock()
        self._reader: asyncio.Task[None] | None = None
        self.ready = asyncio.Event()

    async def open(self) -> None:
        from websockets.asyncio.client import connect

        self._socket = await connect(self.url, max_size=8 * 1024 * 1024)
        self._reader = asyncio.create_task(self._read())
        await asyncio.wait_for(self.ready.wait(), 10)

    async def _read(self) -> None:
        async for raw in self._socket:
            if not isinstance(raw, str):
                continue
            message = json.loads(raw)
            kind = message.get("type")
            if kind == "screen.ready":
                self.ready.set()
            elif kind == "screen.frame.accepted":
                self.accepted.append(str(message.get("frame_id")))
            elif kind == "screen.frame.dropped":
                self.dropped.append((str(message.get("frame_id")), str(message.get("reason"))))

    async def send(self, frame: CapturedFrame) -> None:
        async with self._lock:
            await self._socket.send(json.dumps(frame.screen_header()))
            await self._socket.send(frame.encoded)

    async def close(self) -> None:
        if self._socket is not None:
            await self._socket.close()
        if self._reader is not None:
            self._reader.cancel()


def serve_in_thread(peer: BrowserHubPeer) -> threading.Thread:
    """Run the peer's event loop in a daemon thread (for synchronous hosts)."""

    started = threading.Event()

    def runner() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(peer.start())
        started.set()
        loop.run_forever()

    thread = threading.Thread(target=runner, name="gnsis-browser-peer", daemon=True)
    thread.start()
    started.wait(10)
    return thread
