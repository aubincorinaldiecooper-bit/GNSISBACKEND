from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import json
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from websockets.asyncio.server import ServerConnection, serve

from .client import VisualClient, VisualSession
from .stream import FrameStream

BROWSER_ACTIONS = ("click", "type", "scroll", "navigate", "back", "wait", "done")
CAPABILITY_MANIFEST_ID = "gnsis-browser-host-v1"


class BrowserHubError(RuntimeError):
    pass


class BrowserHubSocket(Protocol):
    async def recv(self) -> str | bytes: ...

    async def send(self, message: str) -> None: ...


class _BrowserCapturePump:
    def __init__(
        self,
        socket: BrowserHubSocket,
        stream: FrameStream,
        decode_frame: Callable[[dict[str, Any]], bytes],
    ) -> None:
        self.socket = socket
        self.stream = stream
        self.decode_frame = decode_frame
        self.frame_seq = 0
        self.latest_frame: dict[str, Any] | None = None
        self._recent_frame_ids: deque[str] = deque(maxlen=64)
        self._recent_frames: dict[str, dict[str, Any]] = {}
        self._condition = asyncio.Condition()
        self._action_results: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._failure: BrowserHubError | None = None
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("browser capture pump is already started")
        self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        task = self._task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        for future in self._action_results.values():
            if not future.done():
                future.cancel()
        self._action_results.clear()
        self._task = None

    async def next_frame(self, after_seq: int) -> tuple[int, dict[str, Any]]:
        async with self._condition:
            await self._condition.wait_for(
                lambda: self.frame_seq > after_seq or self._failure is not None
            )
            self._raise_failure()
            assert self.latest_frame is not None
            return self.frame_seq, self.latest_frame

    def expect_action(self, call_id: str) -> asyncio.Future[dict[str, Any]]:
        self._raise_failure()
        if call_id in self._action_results:
            raise BrowserHubError(f"duplicate browser action call {call_id}")
        future = asyncio.get_running_loop().create_future()
        self._action_results[call_id] = future
        return future

    def frame(self, frame_id: str) -> dict[str, Any]:
        frame = self._recent_frames.get(frame_id)
        if frame is None:
            raise BrowserHubError(
                f"visual decision references unavailable frame {frame_id!r}"
            )
        return frame

    async def _run(self) -> None:
        try:
            while True:
                message = await BrowserHubConnector._receive(self.socket)
                message_type = message.get("type")
                if message_type == "capture.frame":
                    await self._publish_frame(message)
                    continue
                if message_type == "capture.started":
                    continue
                if message_type == "browser.action.result":
                    call_id = message.get("call_id")
                    future = (
                        self._action_results.pop(call_id, None)
                        if isinstance(call_id, str)
                        else None
                    )
                    if future is not None and not future.done():
                        future.set_result(message)
                    continue
                if message_type == "capture.stopped":
                    reason = str(message.get("reason", "failed"))
                    detail = str(
                        message.get("message", "browser capture stopped unexpectedly")
                    )
                    if reason == "failed":
                        raise BrowserHubError(detail)
                    raise BrowserHubError("browser capture stopped unexpectedly")
                if message_type == "error":
                    call_id = message.get("call_id")
                    error = BrowserHubError(
                        str(message.get("message", "browser hub error"))
                    )
                    future = (
                        self._action_results.pop(call_id, None)
                        if isinstance(call_id, str)
                        else None
                    )
                    if future is not None and not future.done():
                        future.set_exception(error)
                        continue
                    raise error
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = (
                exc if isinstance(exc, BrowserHubError) else BrowserHubError(str(exc))
            )
            async with self._condition:
                self._failure = failure
                self._condition.notify_all()
            for future in self._action_results.values():
                if not future.done():
                    future.set_exception(failure)
            self._action_results.clear()

    async def _publish_frame(self, frame: dict[str, Any]) -> None:
        source = BrowserHubConnector._required_mapping(frame, "source")
        await self.stream.send_frame(
            BrowserHubConnector._required_string(frame, "frame_id"),
            BrowserHubConnector._required_non_negative_int(
                frame,
                "captured_at_ms",
            ),
            self.decode_frame(frame),
            encoding=str(frame.get("encoding", "jpeg")),
            video_source="screen",
            metadata={
                "source_kind": "browser_tab",
                "source_tab_id": BrowserHubConnector._required_positive_int(
                    source,
                    "tab_id",
                ),
                "capture_session_id": BrowserHubConnector._required_string(
                    source,
                    "capture_session_id",
                ),
                "source_width": BrowserHubConnector._required_positive_int(
                    source,
                    "width",
                ),
                "source_height": BrowserHubConnector._required_positive_int(
                    source,
                    "height",
                ),
            },
        )
        async with self._condition:
            frame_id = BrowserHubConnector._required_string(frame, "frame_id")
            if len(self._recent_frame_ids) == self._recent_frame_ids.maxlen:
                expired_id = self._recent_frame_ids.popleft()
                self._recent_frames.pop(expired_id, None)
            self._recent_frame_ids.append(frame_id)
            self._recent_frames[frame_id] = frame
            self.frame_seq += 1
            self.latest_frame = frame
            self._condition.notify_all()

    def _raise_failure(self) -> None:
        if self._failure is not None:
            raise self._failure


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


class BrowserHubConnector:
    def __init__(
        self,
        config: BrowserHostConfig,
        *,
        client_factory: Callable[[str], VisualClient] | None = None,
        stream_factory: Callable[[VisualSession], Awaitable[FrameStream]] | None = None,
    ) -> None:
        self.config = config
        self._client_factory = client_factory or (
            lambda token: VisualClient(config.base_url, token)
        )
        self._stream_factory = stream_factory or (
            lambda session: FrameStream.connect(config.base_url, session)
        )

    async def run(self, socket: BrowserHubSocket) -> BrowserTaskResult:
        ready = await self._receive(socket)
        if ready.get("type") != "ready":
            raise BrowserHubError("browser hub did not send ready")

        host = self._client_factory(self.config.host_token)
        planner: VisualClient | None = None
        stream: FrameStream | None = None
        pump: _BrowserCapturePump | None = None
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
            pump = _BrowserCapturePump(socket, stream, self._decode_frame)
            pump.start()
            observed_seq = 0

            for step in range(1, self.config.max_steps + 1):
                observed_seq, _ = await pump.next_frame(observed_seq)
                response = await asyncio.to_thread(planner.decide, session.session_id)
                decision_id = self._required_string(response, "decision_id")
                decision = self._required_mapping(response, "decision")
                frame = pump.frame(self._required_string(decision, "frame_id"))
                request = self._browser_action(response, frame)
                action_result = pump.expect_action(request["call_id"])
                await self._send(socket, request)
                try:
                    result = await action_result
                finally:
                    await asyncio.to_thread(
                        host.record_attempt,
                        session.session_id,
                        decision_id,
                    )
                if result.get("done") is True:
                    return BrowserTaskResult(
                        success=True,
                        message=str(result.get("message", "Task is complete.")),
                        steps=step,
                    )
                if result.get("success") is not True:
                    raise BrowserHubError(
                        str(result.get("message", "browser action failed"))
                    )
                observed_seq = pump.frame_seq

            return BrowserTaskResult(
                success=False,
                message="Browser task exceeded the configured step limit.",
                steps=self.config.max_steps,
            )
        finally:
            if capture_active:
                await self._send_best_effort(socket, {"type": "capture.stop"})
            try:
                if pump is not None:
                    await pump.close()
            finally:
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

    def _browser_action(
        self,
        response: dict[str, Any],
        frame: dict[str, Any],
    ) -> dict[str, Any]:
        decision = response.get("decision")
        if not isinstance(decision, dict):
            raise BrowserHubError("visual API returned an invalid decision")
        action = self._required_string(decision, "action")
        if action not in self.config.allowed_actions:
            raise BrowserHubError(f"visual API returned disallowed action {action!r}")
        source = frame.get("source")
        if not isinstance(source, dict):
            raise BrowserHubError("capture frame is missing browser provenance")
        width = self._required_positive_int(source, "width")
        height = self._required_positive_int(source, "height")
        tab_id = self._required_positive_int(source, "tab_id")

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
        return {
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

    @staticmethod
    def _required_non_negative_int(value: dict[str, Any], key: str) -> int:
        item = value.get(key)
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            raise BrowserHubError(f"{key} must be a non-negative integer")
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
