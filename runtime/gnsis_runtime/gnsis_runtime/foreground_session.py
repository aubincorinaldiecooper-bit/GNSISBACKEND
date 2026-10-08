"""The one live-session contract the GNSIS live stack drives.

``online_duplex`` (the Host's ``/ws/duplex`` and ``/ws/screen`` sockets) and
``TaskToolsRealtimeCoordinator`` (Gateway tools, harness bridge, durable
memory, delivery gate, playback ACKs) talk to the foreground model through
``ForegroundSession`` and nothing else. Two implementations fill it:

- ``GNSISDuplexSession`` — the Thinker/Talker baseline, in process;
- ``NativeForegroundSession`` — any ``RealtimeSession`` provider (Venus
  today), adapted here.

``realtime.provider`` picks the implementation; every other component is the
same object, configured the same way, for both. What the model emits is
normalized to ``ForegroundModelEvent`` (what the model decided or said) and
the ``Speech*`` events (what the client should play), so the coordinator,
the timeline and the Host protocol never see a provider's wire format.
"""

from __future__ import annotations

import asyncio
import io
import logging
import queue
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

from .realtime_provider import ProviderEvent, RealtimeSession
from .screen import LatestScreenFrameBuffer, ScreenFrame

LOGGER = logging.getLogger("gnsis_runtime.foreground_session")

MEDIA_MODES = ("voice", "omni", "auto")

# A native input's receipt: who asked the model to read something and, for a
# worker delivery, the private claim the coordinator must see again on the
# model's answer before it may count the delivery as spoken.
NativeInputReceipt = dict[str, str | int | None]


@dataclass
class ForegroundModelEvent:
    """One model decision unit, provider-neutral.

    Field-compatible with the Thinker's ``DuplexStepEvent`` so the
    coordinator, the haptic splitter and the Host payload treat both the
    same. A native model that owns its own turn-taking reports what it did
    (spoke text, called a tool, yielded the turn) in this shape.
    """

    index: int
    is_listen: bool
    text: str
    end_of_turn: bool
    current_time: int | None
    audio_waveform: Any | None
    metrics: dict[str, Any]
    generated_token_ids: list[int] = field(default_factory=list)
    generation_id: int = 0
    unit_id: int | None = None
    interrupted: bool = False
    is_tool_call: bool = False
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_error: str | None = None
    raw_tool_text: str = ""
    tool_generation_complete: bool = False
    tool_parse_valid: bool = False
    tool_schema_valid: bool = False
    tool_response_expected: bool = False
    decision: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "type": (
                "tool.error"
                if self.is_tool_call and self.tool_error
                else ("tool.call" if self.is_tool_call else "chunk")
            ),
            "index": self.index,
            "is_listen": self.is_listen,
            "text": self.text,
            "end_of_turn": self.end_of_turn,
            "current_time": self.current_time,
            "audio": self.audio_waveform is not None,
            "generated_token_ids": self.generated_token_ids,
            "generation_id": self.generation_id,
            "unit_id": self.unit_id,
            "interrupted": self.interrupted,
            "decision": self.decision,
            "tool_calls": self.tool_calls,
            "tool_error": self.tool_error,
            "raw_tool_text": self.raw_tool_text,
            "tool_generation_complete": self.tool_generation_complete,
            "tool_parse_valid": self.tool_parse_valid,
            "tool_schema_valid": self.tool_schema_valid,
            "tool_response_expected": self.tool_response_expected,
            "metrics": self.metrics,
        }


@dataclass(frozen=True)
class SpeechChunk:
    """Model speech ready for the device: pcm16 at the session's output rate."""

    generation_id: int
    unit_id: int | None
    sequence: int
    pcm16: bytes
    current_time: int | None = None
    end_of_turn: bool = False
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SpeechDone:
    generation_id: int
    unit_id: int | None
    end_of_turn: bool = True
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SpeechCancel:
    """Output generation ``cancelled_generation_id`` is stale; play nothing of it."""

    generation_id: int
    cancelled_generation_id: int
    reason: str


@dataclass(frozen=True)
class SpeechError:
    generation_id: int
    unit_id: int | None
    message: str


SpeechEvent = SpeechChunk | SpeechDone | SpeechCancel | SpeechError
SPEECH_AUDIO_EVENT_NAMES = frozenset(
    {
        "SpeechChunk",
        "SpeechDone",
        "SpeechError",
        "SpeechSynthesisChunk",
        "SpeechSynthesisDone",
        "SpeechSynthesisError",
    }
)
SPEECH_CANCEL_EVENT_NAMES = frozenset({"SpeechCancel", "PlaybackCancel"})


@runtime_checkable
class ForegroundSession(Protocol):
    """What the live sockets and the coordinator need from a model session.

    Methods are synchronous and thread-safe: the live loop runs them off the
    event loop with ``asyncio.to_thread`` (model inference may block), except
    the cheap state reads and ``close``/``enqueue_screen_frame``, which it
    may call on the loop thread.
    """

    screen_frames: LatestScreenFrameBuffer

    # --- vision -----------------------------------------------------------
    def enqueue_screen_frame(self, frame: ScreenFrame) -> None: ...
    def latest_screen_frame(self) -> ScreenFrame | None: ...
    def screen_frame_at_or_before(self, captured_at_ms: int) -> ScreenFrame | None: ...
    def recent_screen_frames(
        self, *, limit: int | None = None, within_ms: float | None = None
    ) -> tuple[ScreenFrame, ...]: ...
    def set_media_mode(self, mode: str) -> str: ...

    # --- audio in, decisions out ---------------------------------------------
    def feed_pcm16(
        self, data: bytes, *, unit_capture_start_ms: tuple[float, ...] | None = None
    ) -> list[Any]: ...
    def flush_pending(
        self, *, unit_capture_start_ms: float | None = None
    ) -> list[Any]: ...
    def step_silence(self) -> Any: ...
    def should_continue_draining(self, trailing_steps: int) -> bool: ...
    def should_stop_after(self, event: Any) -> bool: ...

    # --- things the runtime makes the model read -----------------------------
    def feed_tool_response(self, response: Any) -> Any | None: ...
    def feed_runtime_event(
        self,
        event: Any,
        *,
        delivery_id: str | None = None,
        claim_token: str | None = None,
        delivery_attempt: int | None = None,
    ) -> Any | None: ...
    def feed_memory_episode(self, episode: Any) -> bool: ...
    def set_task_slate(self, slate: str) -> bool: ...
    def set_summary_needed_callback(self, callback: Any) -> None: ...
    def take_native_input_receipt(self, event: Any) -> NativeInputReceipt | None: ...

    # --- speech out ----------------------------------------------------------
    def poll_output(self, timeout: float = 0.0) -> Any | None: ...
    def drain_outputs(self) -> list[Any]: ...
    def wait_for_speech(self, timeout: float | None = None) -> bool: ...
    def talker_state(self) -> dict[str, Any]: ...
    def context_window_snapshot(self) -> dict[str, Any] | None: ...

    # --- interruption and lifecycle --------------------------------------------
    def acknowledge_playback(
        self, output_id: str, *, phase: str, chunks_played: int
    ) -> None: ...

    def set_break(self) -> None: ...
    def interrupt_output(self) -> None: ...
    def clear_break(self) -> None: ...
    def close(self, *, drain_speech: bool = False) -> Any: ...
    @property
    def closed(self) -> bool: ...


@dataclass(frozen=True)
class NativeForegroundParams:
    """The model-clock knobs the live runtime reads, for a native provider.

    The Thinker's ``DuplexParams`` carries these for the in-process model; a
    remote native model has no decode parameters here, but the live runtime
    still paces audio units, bounds the visual history and chooses the
    context channels from them.
    """

    chunk_ms: int = 1000
    generate_audio: bool = True
    sliding_window_mode: str = "context_memory"
    context_max_units: int = 64
    context_previous_max_tokens: int = 0
    speak_text_tokens_per_unit: int = 4
    decode_mode: str = "sampling"


def _validate_claim(
    delivery_id: str | None, claim_token: str | None, delivery_attempt: int | None
) -> None:
    claim_fields = (delivery_id, claim_token, delivery_attempt)
    if any(value is None for value in claim_fields) != all(
        value is None for value in claim_fields
    ):
        raise ValueError(
            "delivery_id, claim_token and delivery_attempt must be provided together"
        )
    if delivery_attempt is not None and (
        not isinstance(delivery_attempt, int)
        or isinstance(delivery_attempt, bool)
        or delivery_attempt < 1
    ):
        raise ValueError("delivery_attempt must be a positive integer")


def encode_frame_jpeg(frame: ScreenFrame, *, quality: int = 85) -> bytes:
    out = io.BytesIO()
    image = frame.image
    if image.mode not in {"RGB", "L"}:
        image = image.convert("RGB")
    image.save(out, format="JPEG", quality=quality)
    return out.getvalue()


class NativeForegroundSession:
    """A ``RealtimeSession`` provider wearing the ``ForegroundSession`` contract.

    The provider's async surface is driven from the live runtime's event
    loop; calls arriving on worker threads block on the loop, calls arriving
    on the loop itself are scheduled so they never stall it. Provider output
    is read continuously and translated into ``ForegroundModelEvent`` /
    ``Speech*`` events that the runtime's existing output pump delivers.

    Generation ids are the runtime's own, monotonic per session: a new
    provider output (correlation id or epoch) starts a new generation and an
    interruption retires the current one, so the Host's playback floor and
    the delivery gate's epoch checks work exactly as they do for the Thinker.
    """

    def __init__(
        self,
        session: RealtimeSession,
        *,
        loop: asyncio.AbstractEventLoop,
        screen_frames: LatestScreenFrameBuffer,
        media_mode: str = "voice",
        provider_name: str = "native",
        call_timeout_sec: float = 30.0,
    ) -> None:
        if media_mode not in MEDIA_MODES:
            raise ValueError(f"unsupported media_mode: {media_mode!r}")
        self._session = session
        self._loop = loop
        self._provider_name = provider_name
        self._call_timeout_sec = float(call_timeout_sec)
        self.screen_frames = screen_frames
        self._media_mode = media_mode
        self._lock = threading.RLock()
        self._outputs: queue.Queue[Any] = queue.Queue()
        self._pending_native_inputs: deque[NativeInputReceipt] = deque()
        self._native_input_receipts: dict[int, NativeInputReceipt] = {}
        self._summary_callback: Callable[[dict[str, Any]], None] | None = None
        self._closed = False
        self._break = False
        self._generation_id = 0
        self._generation_keys: dict[str, int] = {}
        self._speech_sequence = 0
        self._unit_index = 0
        self._drained = True
        self._turn_ended = True
        self._background: set[asyncio.Future[Any]] = set()
        self._reader = loop.create_task(
            self._read_events(), name=f"gnsis-native-foreground-{session.session_id}"
        )

    # ------------------------------------------------------------------ plumbing
    @property
    def session_id(self) -> str:
        return self._session.session_id

    @property
    def provider_name(self) -> str:
        return self._provider_name

    def _on_loop_thread(self) -> bool:
        try:
            return asyncio.get_running_loop() is self._loop
        except RuntimeError:
            return False

    def _call(self, coro: Any) -> Any:
        """Run a provider coroutine from wherever the runtime called us."""

        if self._loop.is_closed():
            coro.close()
            return None
        if self._on_loop_thread():
            task = self._loop.create_task(coro)
            self._background.add(task)
            task.add_done_callback(self._background.discard)
            task.add_done_callback(self._log_background_failure)
            return None
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(self._call_timeout_sec)

    @staticmethod
    def _log_background_failure(task: asyncio.Future[Any]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            LOGGER.warning("native foreground call failed: %s", error)

    async def _read_events(self) -> None:
        while not self._closed:
            try:
                event = await self._session.next_event(timeout_s=None)
            except asyncio.CancelledError:
                return
            except TimeoutError:
                await asyncio.sleep(0.05)
                continue
            except Exception as exc:
                LOGGER.warning(
                    "native foreground %s stopped reading: %s", self.session_id, exc
                )
                self._outputs.put(
                    SpeechError(
                        generation_id=self._generation_id,
                        unit_id=None,
                        message=str(exc),
                    )
                )
                return
            with self._lock:
                outputs = self._translate(event)
            for output in outputs:
                self._outputs.put(output)

    # ------------------------------------------------------------ normalization
    def _generation_for(self, event: ProviderEvent) -> int:
        key = (
            event.correlation_id
            if event.correlation_id is not None
            else (f"epoch:{event.epoch}" if event.epoch is not None else None)
        )
        if key is None:
            if self._generation_id == 0:
                self._generation_id = 1
            return self._generation_id
        known = self._generation_keys.get(key)
        if known is not None:
            return known
        self._generation_id += 1
        self._generation_keys[key] = self._generation_id
        self._speech_sequence = 0
        return self._generation_id

    def _model_event(
        self,
        *,
        generation_id: int,
        is_listen: bool,
        text: str = "",
        end_of_turn: bool = False,
        tool_calls: list[dict[str, Any]] | None = None,
        tool_error: str | None = None,
        raw: dict[str, Any] | None = None,
        decision: str | None = None,
    ) -> ForegroundModelEvent:
        self._unit_index += 1
        metrics: dict[str, Any] = {
            "provider": self._provider_name,
            "gnsis": {"provider": self._provider_name},
        }
        if raw is not None and "finish_reason" in raw:
            metrics["finish_reason"] = raw["finish_reason"]
        event = ForegroundModelEvent(
            index=self._unit_index,
            is_listen=is_listen,
            text=text,
            end_of_turn=end_of_turn,
            current_time=None,
            audio_waveform=None,
            metrics=metrics,
            generation_id=generation_id,
            unit_id=self._unit_index,
            is_tool_call=tool_calls is not None,
            decision=decision,
            tool_calls=list(tool_calls or ()),
            tool_error=tool_error,
            tool_generation_complete=tool_calls is not None,
            tool_parse_valid=tool_calls is not None and tool_error is None,
            tool_schema_valid=tool_calls is not None and tool_error is None,
            tool_response_expected=tool_calls is not None and tool_error is None,
        )
        if self._pending_native_inputs:
            # The first thing the model produces after reading a runtime
            # input is its answer to it, the same binding the Thinker makes
            # on the unit that consumed the input.
            receipt = self._pending_native_inputs.popleft()
            metrics["gnsis"]["native_input_kind"] = receipt["kind"]
            self._native_input_receipts[id(event)] = receipt
        return event

    def _translate(self, event: ProviderEvent) -> list[Any]:
        if event.kind == "closed":
            self._closed = True
            self._drained = True
            self._turn_ended = True
            return []
        generation_id = self._generation_for(event)
        payload = event.payload
        if event.kind == "audio":
            pcm16 = payload.get("pcm16")
            if not isinstance(pcm16, (bytes, bytearray)):
                raise TypeError("provider audio event must carry pcm16 bytes")
            self._speech_sequence += 1
            self._drained = False
            self._turn_ended = False
            outputs: list[Any] = [
                SpeechChunk(
                    generation_id=generation_id,
                    unit_id=self._unit_index or None,
                    sequence=self._speech_sequence,
                    pcm16=bytes(pcm16),
                    end_of_turn=bool(payload.get("turn_finished", False)),
                    metrics={"provider": self._provider_name},
                )
            ]
            if payload.get("turn_finished"):
                outputs.append(self._finish_speech(generation_id))
            return outputs
        if event.kind == "text":
            text = str(payload.get("text") or "")
            end_of_turn = bool(payload.get("turn_finished", False))
            self._turn_ended = end_of_turn
            return [
                self._model_event(
                    generation_id=generation_id,
                    is_listen=False,
                    text=text,
                    end_of_turn=end_of_turn,
                    raw=event.raw,
                    decision="speak",
                )
            ]
        if event.kind == "tool_call":
            calls = payload.get("tool_calls")
            if not isinstance(calls, list):
                calls = [
                    {
                        "name": payload.get("name"),
                        "arguments": payload.get("arguments") or {},
                    }
                ]
            self._turn_ended = False
            return [
                self._model_event(
                    generation_id=generation_id,
                    is_listen=False,
                    tool_calls=calls,
                    tool_error=payload.get("error"),
                    raw=event.raw,
                    decision="tool",
                )
            ]
        if event.kind == "interrupt":
            cancelled = generation_id
            self._generation_id += 1
            self._speech_sequence = 0
            self._drained = True
            self._turn_ended = True
            return [
                SpeechCancel(
                    generation_id=self._generation_id,
                    cancelled_generation_id=cancelled,
                    reason=str(payload.get("reason") or "model_interrupt"),
                ),
                self._model_event(
                    generation_id=self._generation_id,
                    is_listen=True,
                    end_of_turn=True,
                    raw=event.raw,
                    decision="interrupt",
                ),
            ]
        if event.kind == "turn":
            finished = payload.get("turn_finished")
            state = payload.get("state")
            outputs = []
            if finished or state in {"listen", "listening", "idle"}:
                if not self._drained:
                    outputs.append(self._finish_speech(generation_id))
                self._turn_ended = True
                outputs.append(
                    self._model_event(
                        generation_id=generation_id,
                        is_listen=True,
                        end_of_turn=True,
                        raw=event.raw,
                        decision="listen",
                    )
                )
            else:
                self._turn_ended = False
            return outputs
        # ``control``: provider bookkeeping (work ids, utterance ids). Worth a
        # debug line, not a client message.
        LOGGER.debug(
            "native foreground %s control: %s", self.session_id, sorted(payload)
        )
        return []

    def _finish_speech(self, generation_id: int) -> SpeechDone:
        self._drained = True
        self._turn_ended = True
        return SpeechDone(
            generation_id=generation_id,
            unit_id=self._unit_index or None,
            metrics={"provider": self._provider_name},
        )

    # ---------------------------------------------------------------- vision
    def enqueue_screen_frame(self, frame: ScreenFrame) -> None:
        with self._lock:
            if self._closed or self._media_mode == "voice":
                return
            self.screen_frames.publish(frame)
            # The provider sees every accepted frame, so it is consumed into
            # the bounded history at once: that history is what the
            # coordinator attaches to turns and what "a moment ago" means.
            self.screen_frames.consume_for_unit()
        self._call(self._forward_frame(frame))

    async def _forward_frame(self, frame: ScreenFrame) -> None:
        data = await asyncio.to_thread(encode_frame_jpeg, frame)
        await self._session.push_video_frame(
            data, mime_type="image/jpeg", ts_ms=frame.captured_at_ms
        )

    def latest_screen_frame(self) -> ScreenFrame | None:
        return self.screen_frames.latest_frame()

    def screen_frame_at_or_before(self, captured_at_ms: int) -> ScreenFrame | None:
        return self.screen_frames.frame_at_or_before(captured_at_ms)

    def recent_screen_frames(
        self, *, limit: int | None = None, within_ms: float | None = None
    ) -> tuple[ScreenFrame, ...]:
        return self.screen_frames.recent_frames(limit=limit, within_ms=within_ms)

    def set_media_mode(self, mode: str) -> str:
        if mode not in MEDIA_MODES:
            raise ValueError(f"unsupported media_mode: {mode!r}")
        with self._lock:
            previous = self._media_mode
            if mode != previous:
                self.screen_frames.reset()
                self._media_mode = mode
            return previous

    # -------------------------------------------------------------- audio in
    def feed_pcm16(
        self, data: bytes, *, unit_capture_start_ms: tuple[float, ...] | None = None
    ) -> list[Any]:
        if self._closed:
            raise RuntimeError("native foreground session is closed")
        capture_ts_ms = int(unit_capture_start_ms[0]) if unit_capture_start_ms else None
        self._call(self._session.push_audio(bytes(data), capture_ts_ms=capture_ts_ms))
        # Decisions arrive on the provider's own clock, through poll_output.
        return []

    def flush_pending(self, *, unit_capture_start_ms: float | None = None) -> list[Any]:
        return []

    def step_silence(self) -> Any:
        raise RuntimeError("a native full-duplex model owns its own silence")

    def should_continue_draining(self, trailing_steps: int) -> bool:
        return False

    def should_stop_after(self, event: Any) -> bool:
        return True

    # ------------------------------------------------- runtime inputs to model
    def _push_native_input(
        self, control: dict[str, Any], receipt: NativeInputReceipt
    ) -> None:
        if self._closed:
            raise RuntimeError("native foreground session is closed")
        with self._lock:
            self._pending_native_inputs.append(receipt)
        try:
            self._call(self._session.push_control(control))
        except Exception:
            with self._lock:
                if (
                    self._pending_native_inputs
                    and self._pending_native_inputs[-1] is receipt
                ):
                    self._pending_native_inputs.pop()
            raise

    def feed_tool_response(self, response: Any) -> Any | None:
        self._push_native_input(
            {"kind": "tool_response", "response": response},
            {"kind": "tool_response", "delivery_id": None, "claim_token": None},
        )
        return None

    def feed_runtime_event(
        self,
        event: Any,
        *,
        delivery_id: str | None = None,
        claim_token: str | None = None,
        delivery_attempt: int | None = None,
    ) -> Any | None:
        _validate_claim(delivery_id, claim_token, delivery_attempt)
        if not isinstance(event, dict) or event.get("type") != "worker_delivery":
            raise ValueError("runtime event must be a worker_delivery object")
        self._push_native_input(
            {"kind": "runtime_event", "event": event},
            {
                "kind": "worker_delivery",
                "delivery_id": delivery_id,
                "claim_token": claim_token,
                "delivery_attempt": delivery_attempt,
            },
        )
        return None

    def feed_memory_episode(self, episode: Any) -> bool:
        if self._closed:
            return False
        self._call(
            self._session.push_control({"kind": "memory_episode", "episode": episode})
        )
        return True

    def set_task_slate(self, slate: str) -> bool:
        if self._closed:
            return False
        self._call(self._session.push_control({"kind": "task_slate", "slate": slate}))
        return True

    def set_summary_needed_callback(self, callback: Any) -> None:
        # A native model manages its own context; nothing here ever asks the
        # runtime for a summary, so the callback is held and never fired.
        self._summary_callback = callback

    def take_native_input_receipt(self, event: Any) -> NativeInputReceipt | None:
        with self._lock:
            return self._native_input_receipts.pop(id(event), None)

    # --------------------------------------------------------------- speech out
    def poll_output(self, timeout: float = 0.0) -> Any | None:
        try:
            return (
                self._outputs.get(timeout=timeout)
                if timeout > 0
                else self._outputs.get_nowait()
            )
        except queue.Empty:
            return None

    def drain_outputs(self) -> list[Any]:
        drained: list[Any] = []
        while True:
            try:
                drained.append(self._outputs.get_nowait())
            except queue.Empty:
                return drained

    def wait_for_speech(self, timeout: float | None = None) -> bool:
        # Speech streams to the client as it is produced; there is no
        # synthesis backlog to wait on.
        return True

    def talker_state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "generation_id": self._generation_id,
                "drained": self._drained,
                "turn_ended": self._turn_ended,
                "provider": self._provider_name,
            }

    def context_window_snapshot(self) -> dict[str, Any] | None:
        return None

    # ------------------------------------------------- interruption, lifecycle
    def acknowledge_playback(
        self, output_id: str, *, phase: str, chunks_played: int
    ) -> None:
        if self._closed or phase not in {"started", "finished"}:
            return
        self._call(
            self._session.acknowledge_playback(output_id, chunks_played=chunks_played)
        )

    def set_break(self) -> None:
        with self._lock:
            self._break = True
        self.interrupt_output()

    def interrupt_output(self) -> None:
        with self._lock:
            if self._closed:
                return
            cancelled = self._generation_id
            self._generation_id += 1
            self._speech_sequence = 0
            self._drained = True
            self._turn_ended = True
            self._outputs.put(
                SpeechCancel(
                    generation_id=self._generation_id,
                    cancelled_generation_id=cancelled,
                    reason="client_break" if self._break else "interrupt",
                )
            )
        self._call(
            self._session.cancel_output("client_break" if self._break else "interrupt")
        )

    def clear_break(self) -> None:
        with self._lock:
            self._break = False

    def close(self, *, drain_speech: bool = False) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._loop.call_soon_threadsafe(self._reader.cancel)
        self._call(self._session.close())

    @property
    def closed(self) -> bool:
        return self._closed
