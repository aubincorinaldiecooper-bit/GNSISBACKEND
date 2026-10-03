"""Test harness for the online duplex app.

The Thinker and the worker gateway are stubbed: these tests are about session
lifecycle - who holds the single model slot, when it is freed, what survives a
bad frame - not about inference. Nothing here loads a model.
"""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import pytest


class StubThinker:
    """Stands in for ``GNSISDuplexSession``."""

    def __init__(self) -> None:
        self.closed = False
        self.close_count = 0
        self.interrupted = 0
        self.frames: list[Any] = []
        # Chunks fed to the model, and how long each one is made to take. A
        # test that needs the session's deadline to fall *during* a frame sets
        # feed_delay, since a stub otherwise consumes any frame instantly.
        self.fed: list[bytes] = []
        self.feed_delay = 0.0

    def feed_pcm16(self, data: bytes, *, unit_capture_start_ms=None) -> tuple:
        self.fed.append(data)
        if self.feed_delay:
            time.sleep(self.feed_delay)
        return ()

    def talker_state(self) -> dict[str, Any]:
        return {"generation_id": 0}

    def close(self, *, drain_speech: bool = False) -> None:
        self.close_count += 1
        self.closed = True

    # --- explicit-stop drain path ---------------------------------------
    def flush_pending(self, *, unit_capture_start_ms=None) -> tuple:
        return ()

    def should_continue_draining(self, _steps: int) -> bool:
        return False

    def step_silence(self):
        raise AssertionError("stub thinker never steps silence")

    def should_stop_after(self, _event) -> bool:
        return True

    def wait_for_speech(self) -> None:
        return None

    def interrupt_output(self) -> None:
        self.interrupted += 1

    def acknowledge_playback(self, output_id: str, *, phase: str, chunks_played: int) -> None:
        return None

    def poll_output(self, _timeout: float) -> None:
        return None

    def drain_outputs(self) -> tuple[Any, ...]:
        return ()

    def enqueue_screen_frame(self, frame: Any) -> None:
        self.frames.append(frame)

    def set_media_mode(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class ScriptedSession:
    """A native provider's session: records every seam call, replays events."""

    def __init__(self, config: Any) -> None:
        self.config = config
        self.calls: list[tuple[str, Any]] = []
        self.events: asyncio.Queue[Any] = asyncio.Queue()
        self.closed = False

    @property
    def session_id(self) -> str:
        return self.config.session_id

    async def push_audio(self, pcm16: bytes, *, capture_ts_ms: int | None = None) -> None:
        self.calls.append(("audio", (pcm16, capture_ts_ms)))

    async def push_video_frame(
        self, data: bytes, *, mime_type: str = "image/jpeg", ts_ms: int | None = None
    ) -> None:
        self.calls.append(("video", (data, mime_type, ts_ms)))

    async def push_control(self, control: dict[str, Any]) -> None:
        self.calls.append(("control", control))

    async def next_event(self, timeout_s: float | None = None) -> Any:
        try:
            return await asyncio.wait_for(self.events.get(), timeout_s)
        except asyncio.TimeoutError:
            raise TimeoutError from None

    async def acknowledge_playback(self, output_id: str, *, chunks_played: int) -> bool:
        self.calls.append(("ack", (output_id, chunks_played)))
        return True

    async def cancel_output(self, reason: str = "cancelled") -> None:
        self.calls.append(("cancel", reason))

    async def close(self) -> None:
        self.closed = True

    def calls_of(self, kind: str) -> list[Any]:
        return [value for name, value in self.calls if name == kind]


class ScriptedProvider:
    """Stands in for a remote native full-duplex model such as Venus."""

    provider_name = "venus"

    def __init__(self) -> None:
        self.sessions: list[ScriptedSession] = []

    async def open_session(self, config: Any) -> ScriptedSession:
        session = ScriptedSession(config)
        self.sessions.append(session)
        return session

    async def health(self) -> dict[str, Any]:
        return {"status": "ok"}

    async def close(self) -> None:
        return None


class StubProvider:
    def __init__(self, *, fail: bool = False, gate: threading.Event | None = None):
        self.fail = fail
        self.gate = gate
        self.warmups = 0

    async def warmup(self) -> None:
        self.warmups += 1
        if self.gate is not None:
            # Hold warmup open so a test can prove `ready` does not wait on it.
            # Polled rather than blocked: occupying a default-executor thread
            # would starve the app's own asyncio.to_thread calls.
            for _ in range(500):
                if self.gate.is_set():
                    break
                await asyncio.sleep(0.01)
        if self.fail:
            raise RuntimeError("ornith unreachable")


class StubGateway:
    def __init__(self, provider: StubProvider) -> None:
        self.providers = {"stub": provider}
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


class StubCoordinator:
    """Stands in for ``TaskToolsRealtimeCoordinator``."""

    def __init__(self, gateway: StubGateway, **_kwargs: Any) -> None:
        self.gateway = gateway
        self.started = 0
        self.model_jobs_stopped = 0
        self.closed = 0
        self._outputs: asyncio.Queue[Any] = asyncio.Queue()

    async def start(self) -> None:
        self.started += 1

    async def stop_model_jobs(self) -> None:
        self.model_jobs_stopped += 1

    async def close(self, *, discard_state: bool = False) -> None:
        self.closed += 1
        await self.gateway.close()

    async def next_output(self) -> Any:
        # Never resolves: the outbound loop just waits.
        return await self._outputs.get()

    def record_pcm16(self, _audio: bytes) -> None:
        return None

    def task_status(self) -> dict[str, Any]:
        return {"tasks": []}

    def remember_media(self, _media: Any) -> None:
        return None

    def model_output(self, _event: Any) -> Any:
        raise AssertionError("stub coordinator emits no model output")

    def observe_frontbrain(self, _event: Any) -> None:
        return None

    def pending_external_dispatch(self) -> None:
        # No external tool call is ever waiting on a stub.
        return None


@dataclass
class StubModel:
    vpm: object = field(default_factory=object)
    resampler: object = field(default_factory=object)


@dataclass
class StubBundle:
    model: StubModel = field(default_factory=StubModel)


@dataclass
class StubParams:
    """The `DuplexParams` fields the runtime reads, with the real defaults.

    A missing one is not a test failure but a fatal `error` on a live socket,
    which is how `speak_text_tokens_per_unit` was found: in a browser, after
    every test had passed.
    """

    chunk_ms: int = 1000
    generate_audio: bool = False
    sliding_window_mode: str = "context_no_previous"
    context_max_units: int = 64
    context_previous_max_tokens: int = 0
    decode_mode: str = "sampling"
    # Read when a client switches media mode (estimated_tokens_per_unit).
    speak_text_tokens_per_unit: int = 4


@dataclass
class Harness:
    app: Any
    provider: StubProvider
    gateway: StubGateway
    thinkers: list[StubThinker]
    coordinators: list[StubCoordinator]
    open_gate: threading.Event | None = None
    # Where the server would write frames, so a test can look and find nothing.
    media_dir: Any = None
    # The native provider behind the seam when foreground="venus".
    native: ScriptedProvider | None = None
    foreground: str = "thinker"


@pytest.fixture
def harness(monkeypatch, tmp_path):
    """Build the duplex app with the Thinker and gateway stubbed out.

    ``foreground="venus"`` builds the same app around a scripted native
    provider instead of a Thinker: nothing else in the harness changes, which
    is the point.
    """

    def _build(
        *,
        warmup_fails: bool = False,
        warmup_gate: threading.Event | None = None,
        open_gate: threading.Event | None = None,
        # None models a lean server: no coordinator, no worker provider, and
        # so nothing downstream that would ever read a persisted frame.
        provider_name: str | None = "stub",
        foreground: str = "thinker",
        real_coordinator: bool = False,
        **settings_kwargs: Any,
    ) -> Harness:
        from gnsis_runtime import online_duplex

        provider = StubProvider(fail=warmup_fails, gate=warmup_gate)
        gateway = StubGateway(provider)
        thinkers: list[StubThinker] = []
        coordinators: list[StubCoordinator] = []
        native = ScriptedProvider() if foreground == "venus" else None

        def fake_build_session(_runtime, **_kwargs):
            if open_gate is not None:
                # Simulate a slow model open so a test can disconnect during it.
                open_gate.wait(5.0)
            thinker = StubThinker()
            thinkers.append(thinker)
            return thinker

        def fake_coordinator(gw, **kwargs):
            coordinator = StubCoordinator(gw, **kwargs)
            coordinators.append(coordinator)
            return coordinator

        live_coordinator = online_duplex.TaskToolsRealtimeCoordinator

        def recording_coordinator(*args: Any, **kwargs: Any) -> Any:
            coordinator = live_coordinator(*args, **kwargs)
            coordinators.append(coordinator)
            return coordinator

        def gateway_factory(_session_id: str) -> Any:
            if not real_coordinator:
                return gateway
            from gnsis_runtime.gateway import GNSISGateway, ProviderRegistry
            from gnsis_runtime.supervision import TaskLedger

            return GNSISGateway(
                coordinator=None,
                providers=ProviderRegistry(()),
                ledger=TaskLedger(":memory:"),
                mode="lean",
            )

        monkeypatch.setattr(online_duplex, "_build_session", fake_build_session)
        monkeypatch.setattr(
            online_duplex,
            "TaskToolsRealtimeCoordinator",
            recording_coordinator if real_coordinator else fake_coordinator,
        )
        monkeypatch.setattr(
            online_duplex, "_prepare_static_prefix", lambda _runtime: None
        )

        app = online_duplex.create_online_duplex_app(
            None if native is not None else StubBundle(),
            params=StubParams(),
            gateway_factory=gateway_factory,
            provider_name=provider_name,
            settings=online_duplex.OnlineDuplexSettings(
                **{"reconnect_grace_sec": 0.3, **settings_kwargs}
            ),
            media_dir=tmp_path / "media",
            foreground_provider=native,
            foreground_system_prompt="native prompt" if native is not None else None,
        )
        return Harness(
            app=app,
            media_dir=tmp_path / "media",
            provider=provider,
            gateway=gateway,
            thinkers=thinkers,
            coordinators=coordinators,
            open_gate=open_gate,
            native=native,
            foreground=foreground,
        )

    return _build
