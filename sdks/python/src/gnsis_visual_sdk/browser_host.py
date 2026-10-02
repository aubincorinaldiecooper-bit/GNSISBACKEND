from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import json
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from websockets.asyncio.server import ServerConnection, serve

from .client import VisualClient, VisualSession
from .stream import FrameStream

BROWSER_ACTIONS = ("click", "type", "scroll", "navigate", "back", "wait", "done")
CAPABILITY_MANIFEST_ID = "gnsis-browser-host-v1"


def _now_ms() -> int:
    return time.monotonic_ns() // 1_000_000


class BrowserHubError(RuntimeError):
    pass


class BrowserHubSocket(Protocol):
    async def recv(self) -> str | bytes: ...

    async def send(self, message: str) -> None: ...


@dataclass(frozen=True, slots=True)
class BrowserHostConfig:
    base_url: str
    host_token: str
    task: str
    allowed_actions: tuple[str, ...] = BROWSER_ACTIONS
    max_steps: int = 40
    capture_fps: float = 2
    capture_max_edge: int = 1280
    capture_quality: float = 0.82
    turn_id: str = ""

    def __post_init__(self) -> None:
        task = self.task.strip()
        if not task:
            raise ValueError("task must not be empty")
        object.__setattr__(self, "task", task)
        allowed = tuple(dict.fromkeys(self.allowed_actions))
        if not allowed or any(action not in BROWSER_ACTIONS for action in allowed):
            raise ValueError("allowed_actions contains an unsupported browser action")
        object.__setattr__(self, "allowed_actions", allowed)
        if self.max_steps <= 0:
            raise ValueError("max_steps must be positive")
        if self.capture_fps <= 0:
            raise ValueError("capture_fps must be positive")
        if self.capture_max_edge <= 0:
            raise ValueError("capture_max_edge must be positive")
        if not 0 < self.capture_quality <= 1:
            raise ValueError("capture_quality must be within (0, 1]")
        if not self.turn_id:
            object.__setattr__(self, "turn_id", uuid.uuid4().hex)


@dataclass(frozen=True, slots=True)
class BrowserTaskResult:
    success: bool
    message: str
    steps: int
    trace: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class PlannerObservation:
    """Everything a planner may see: the production contract and nothing else."""

    step: int
    task: str
    allowed_actions: tuple[str, ...]
    viewport: tuple[int, int]
    service_decision: dict[str, Any]
    history: tuple[dict[str, Any], ...]


class ActionPlanner(Protocol):
    """Chooses the bounded action the host is asked to execute.

    A planner never executes anything; the host keeps permission checks,
    execution, attempt recording, and verification.
    """

    async def plan(self, observation: PlannerObservation) -> dict[str, Any]: ...


class BrowserHubConnector:
    def __init__(
        self,
        config: BrowserHostConfig,
        *,
        client_factory: Callable[[str], VisualClient] | None = None,
        stream_factory: Callable[[VisualSession], Awaitable[FrameStream]] | None = None,
        planner: ActionPlanner | None = None,
    ) -> None:
        self.config = config
        self._client_factory = client_factory or (
            lambda token: VisualClient(config.base_url, token)
        )
        self._stream_factory = stream_factory or (
            lambda session: FrameStream.connect(config.base_url, session)
        )
        self._planner = planner

    async def run(self, socket: BrowserHubSocket) -> BrowserTaskResult:
        ready = await self._receive(socket)
        if ready.get("type") != "ready":
            raise BrowserHubError("browser hub did not send ready")

        host = self._client_factory(self.config.host_token)
        planner: VisualClient | None = None
        stream: FrameStream | None = None
        session: VisualSession | None = None
        capture_active = False
        try:
            session = await asyncio.to_thread(host.create_session)
            planner = self._client_factory(session.planner_token)
            stream = await self._stream_factory(session)
            await asyncio.to_thread(
                planner.set_task,
                session.session_id,
                self.config.task,
                self.config.allowed_actions,
            )

            trace: list[dict[str, Any]] = []
            for step in range(1, self.config.max_steps + 1):
                await self._send(
                    socket,
                    {
                        "type": "capture.start",
                        "fps": self.config.capture_fps,
                        "max_edge": self.config.capture_max_edge,
                        "quality": self.config.capture_quality,
                    },
                )
                capture_active = True
                frame = await self._next_frame(socket)
                await self._send(socket, {"type": "capture.stop"})
                await self._wait_for_capture_stop(socket)
                capture_active = False

                image = self._decode_frame(frame)
                await stream.send_frame(
                    str(frame["frame_id"]),
                    int(frame["captured_at_ms"]),
                    image,
                    encoding=str(frame.get("encoding", "jpeg")),
                    video_source="screen",
                    metadata={
                        "source_kind": "browser_tab",
                        "source_tab_id": self._required_positive_int(
                            self._required_mapping(frame, "source"),
                            "tab_id",
                        ),
                        "capture_session_id": self._required_string(
                            self._required_mapping(frame, "source"),
                            "capture_session_id",
                        ),
                        "source_width": self._required_positive_int(
                            self._required_mapping(frame, "source"),
                            "width",
                        ),
                        "source_height": self._required_positive_int(
                            self._required_mapping(frame, "source"),
                            "height",
                        ),
                    },
                )
                decide_started_ms = _now_ms()
                response = await asyncio.to_thread(planner.decide, session.session_id)
                decide_latency_ms = _now_ms() - decide_started_ms
                decision_id = self._required_string(response, "decision_id")
                service_decision = response.get("decision")
                if not isinstance(service_decision, dict):
                    raise BrowserHubError("visual API returned an invalid decision")
                request, plan_trace = await self._browser_action(
                    response,
                    frame,
                    step=step,
                    history=tuple(trace),
                )
                await self._send(socket, request)
                execute_started_ms = _now_ms()
                try:
                    result = await self._wait_for_action(socket, request["call_id"])
                finally:
                    await asyncio.to_thread(
                        host.record_attempt,
                        session.session_id,
                        decision_id,
                    )
                entry: dict[str, Any] = {
                    "step": step,
                    "frame_id": str(frame["frame_id"]),
                    "decision_id": decision_id,
                    "service_decision": service_decision,
                    "service_decide_latency_ms": decide_latency_ms,
                    "executed_decision": request["decision"],
                    "host_latency_ms": _now_ms() - execute_started_ms,
                    "success": result.get("success") is True,
                    "done": result.get("done") is True,
                    "message": str(result.get("message", "")),
                    "evidence": result.get("evidence"),
                }
                if plan_trace is not None:
                    entry["planner"] = plan_trace
                trace.append(entry)
                if result.get("done") is True:
                    return BrowserTaskResult(
                        success=True,
                        message=str(result.get("message", "Task is complete.")),
                        steps=step,
                        trace=tuple(trace),
                    )
                if result.get("success") is not True:
                    raise BrowserHubError(
                        str(result.get("message", "browser action failed"))
                    )

            return BrowserTaskResult(
                success=False,
                message="Browser task exceeded the configured step limit.",
                steps=self.config.max_steps,
                trace=tuple(trace),
            )
        finally:
            if capture_active:
                await self._send_best_effort(socket, {"type": "capture.stop"})
            try:
                if stream is not None:
                    await stream.close()
            finally:
                try:
                    if session is not None:
                        await asyncio.to_thread(
                            host.close_session,
                            session.session_id,
                        )
                finally:
                    if planner is not None:
                        planner.close()
                    host.close()

    async def _next_frame(self, socket: BrowserHubSocket) -> dict[str, Any]:
        while True:
            message = await self._receive(socket)
            message_type = message.get("type")
            if message_type == "capture.frame":
                return message
            if message_type == "capture.stopped" and message.get("reason") == "failed":
                raise BrowserHubError(
                    str(message.get("message", "browser capture failed"))
                )
            if message_type == "error":
                raise BrowserHubError(str(message.get("message", "browser hub error")))

    async def _wait_for_capture_stop(self, socket: BrowserHubSocket) -> None:
        while True:
            message = await self._receive(socket)
            message_type = message.get("type")
            if message_type == "capture.stopped":
                if message.get("reason") == "failed":
                    raise BrowserHubError(
                        str(message.get("message", "browser capture failed"))
                    )
                return
            if message_type == "error":
                raise BrowserHubError(str(message.get("message", "browser hub error")))

    async def _wait_for_action(
        self,
        socket: BrowserHubSocket,
        call_id: str,
    ) -> dict[str, Any]:
        while True:
            message = await self._receive(socket)
            message_type = message.get("type")
            if (
                message_type == "browser.action.result"
                and message.get("call_id") == call_id
            ):
                return message
            if message_type == "error" and message.get("call_id") in {None, call_id}:
                raise BrowserHubError(
                    str(message.get("message", "browser action failed"))
                )

    async def _browser_action(
        self,
        response: dict[str, Any],
        frame: dict[str, Any],
        *,
        step: int,
        history: tuple[dict[str, Any], ...],
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        decision = response.get("decision")
        if not isinstance(decision, dict):
            raise BrowserHubError("visual API returned an invalid decision")
        source = frame.get("source")
        if not isinstance(source, dict):
            raise BrowserHubError("capture frame is missing browser provenance")
        width = self._required_positive_int(source, "width")
        height = self._required_positive_int(source, "height")
        tab_id = self._required_positive_int(source, "tab_id")

        plan_trace: dict[str, Any] | None = None
        if self._planner is not None:
            planned = await self._planner.plan(
                PlannerObservation(
                    step=step,
                    task=self.config.task,
                    allowed_actions=self.config.allowed_actions,
                    viewport=(width, height),
                    service_decision=dict(decision),
                    history=history,
                )
            )
            if not isinstance(planned, dict):
                raise BrowserHubError("planner returned an invalid decision")
            plan_trace = dict(planned.get("trace") or {})
            decision = {
                key: value
                for key, value in planned.items()
                if key != "trace" and value is not None
            }

        action = self._required_string(decision, "action")
        if action not in self.config.allowed_actions:
            raise BrowserHubError(f"visual API returned disallowed action {action!r}")

        browser_decision: dict[str, Any] = {
            key: decision[key]
            for key in ("action", "confidence", "target", "text", "url", "direction")
            if key in decision
        }
        browser_decision["viewport"] = {"width": width, "height": height}
        browser_decision["resolve_target"] = False
        if action == "wait":
            browser_decision["wait_ms"] = 500

        decision_id = self._required_string(response, "decision_id")
        request = {
            "type": "browser.action",
            "call_id": decision_id,
            "frame_id": frame["frame_id"],
            "source_tab_id": tab_id,
            "authority": {
                "turn_id": self.config.turn_id,
                "provenance": "mixed",
                "policy_decision": "allow",
                "policy_reason": (
                    "Action is bounded by the user's explicit browser task and "
                    "current untrusted page observation."
                ),
                "capability_manifest_id": CAPABILITY_MANIFEST_ID,
                "allowed_actions": list(self.config.allowed_actions),
                "confirmation": "not_required",
            },
            "decision": browser_decision,
        }
        return request, plan_trace

    @staticmethod
    async def _receive(socket: BrowserHubSocket) -> dict[str, Any]:
        raw = await socket.recv()
        if not isinstance(raw, str):
            raise BrowserHubError("browser hub sent a non-text message")
        try:
            message = json.loads(raw)
        except ValueError:
            raise BrowserHubError("browser hub sent invalid JSON") from None
        if not isinstance(message, dict):
            raise BrowserHubError("browser hub sent an invalid message")
        return message

    @staticmethod
    async def _send(socket: BrowserHubSocket, message: dict[str, Any]) -> None:
        await socket.send(json.dumps(message, separators=(",", ":")))

    @classmethod
    async def _send_best_effort(
        cls,
        socket: BrowserHubSocket,
        message: dict[str, Any],
    ) -> None:
        try:
            await cls._send(socket, message)
        except Exception:
            return

    @staticmethod
    def _decode_frame(frame: dict[str, Any]) -> bytes:
        encoded = frame.get("image_base64")
        if not isinstance(encoded, str) or not encoded:
            raise BrowserHubError("capture frame is missing image data")
        try:
            return base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise BrowserHubError("capture frame contains invalid base64") from None

    @staticmethod
    def _required_string(value: dict[str, Any], key: str) -> str:
        item = value.get(key)
        if not isinstance(item, str) or not item.strip():
            raise BrowserHubError(f"{key} must be a non-empty string")
        return item

    @staticmethod
    def _required_mapping(value: dict[str, Any], key: str) -> dict[str, Any]:
        item = value.get(key)
        if not isinstance(item, dict):
            raise BrowserHubError(f"{key} must be an object")
        return item

    @staticmethod
    def _required_positive_int(value: dict[str, Any], key: str) -> int:
        item = value.get(key)
        if not isinstance(item, int) or isinstance(item, bool) or item <= 0:
            raise BrowserHubError(f"{key} must be a positive integer")
        return item


async def run_server(
    config: BrowserHostConfig,
    *,
    listen_host: str,
    listen_port: int,
) -> BrowserTaskResult:
    result: asyncio.Future[BrowserTaskResult] = (
        asyncio.get_running_loop().create_future()
    )

    async def handler(socket: ServerConnection) -> None:
        if result.done():
            await socket.close(code=1013, reason="browser host is already in use")
            return
        try:
            result.set_result(await BrowserHubConnector(config).run(socket))
        except Exception as exc:
            result.set_exception(exc)

    async with serve(handler, listen_host, listen_port):
        return await result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Connect GNSIS Browser's existing Hub protocol to Smaller GNSIS."
    )
    parser.add_argument("task", help="Trusted user task for the browser host.")
    parser.add_argument("--base-url", required=True, help="Smaller GNSIS API base URL.")
    parser.add_argument(
        "--host-token", required=True, help="Visual host token or grant."
    )
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=8766)
    parser.add_argument("--max-steps", type=int, default=40)
    parser.add_argument("--fps", type=float, default=2)
    parser.add_argument("--max-edge", type=int, default=1280)
    parser.add_argument("--quality", type=float, default=0.82)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    config = BrowserHostConfig(
        base_url=args.base_url,
        host_token=args.host_token,
        task=args.task,
        max_steps=args.max_steps,
        capture_fps=args.fps,
        capture_max_edge=args.max_edge,
        capture_quality=args.quality,
    )
    outcome = asyncio.run(
        run_server(
            config,
            listen_host=args.listen_host,
            listen_port=args.listen_port,
        )
    )
    print(json.dumps(asdict(outcome), separators=(",", ":")))


if __name__ == "__main__":
    main()
