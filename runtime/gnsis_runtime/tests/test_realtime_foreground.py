"""Foreground provider selection and the matched provider bench.

The Thinker baseline and Venus sit behind one `RealtimeProvider` seam; one
config key picks which. These tests prove the selection, its defaults and
refusals, and that the bench drives any provider through identical calls —
no model, no GPU, no network.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest
import yaml

from gnsis_runtime.providers.foreground import (
    DEFAULT_FOREGROUND_PROVIDER,
    FOREGROUND_PROVIDERS,
    RealtimeConfig,
    build_realtime_provider,
    validate_realtime_config,
)
from gnsis_runtime.providers.thinker import ThinkerRealtimeProvider
from gnsis_runtime.providers.venus import VenusRealtimeProvider
from gnsis_runtime.realtime_bench import (
    BenchFrame,
    BenchInput,
    load_frames,
    load_pcm16_wav,
    run_bench,
)
from gnsis_runtime.realtime_provider import (
    ProviderEvent,
    ProviderSessionConfig,
    RealtimeProvider,
    RealtimeSession,
)


# --- selection --------------------------------------------------------------


def test_the_thinker_is_the_default_and_both_names_are_selectable():
    assert DEFAULT_FOREGROUND_PROVIDER == "thinker"
    assert FOREGROUND_PROVIDERS == ("thinker", "venus")
    assert RealtimeConfig().provider == "thinker"


def test_venus_needs_an_absolute_url():
    with pytest.raises(ValueError, match="venus_url"):
        validate_realtime_config(RealtimeConfig(provider="venus"))
    with pytest.raises(ValueError, match="venus_url"):
        validate_realtime_config(
            RealtimeConfig(provider="venus", venus_url="venus.internal:8000")
        )
    with pytest.raises(ValueError, match="timeout"):
        validate_realtime_config(
            RealtimeConfig(
                provider="venus", venus_url="http://venus:8000", venus_timeout_sec=0
            )
        )
    validate_realtime_config(
        RealtimeConfig(provider="venus", venus_url="http://venus:8000/")
    )


def test_an_unknown_provider_is_refused():
    with pytest.raises(ValueError, match="realtime.provider"):
        validate_realtime_config(RealtimeConfig(provider="qwen"))  # type: ignore[arg-type]


def test_selecting_venus_never_touches_the_thinker():
    """Venus is a remote server: choosing it must not need Thinker weights."""

    def factory(session_id: str, config: ProviderSessionConfig) -> Any:
        raise AssertionError("the Thinker factory must not be consulted for Venus")

    provider = build_realtime_provider(
        RealtimeConfig(provider="venus", venus_url="http://venus:8000/", venus_timeout_sec=5),
        thinker_session_factory=factory,
    )
    assert isinstance(provider, VenusRealtimeProvider)
    assert provider.provider_name == "venus"
    assert provider.base_url == "http://venus:8000"
    assert provider.timeout_s == 5


def test_selecting_the_thinker_needs_its_session_factory():
    with pytest.raises(ValueError, match="session factory"):
        build_realtime_provider(RealtimeConfig())
    provider = build_realtime_provider(
        RealtimeConfig(), thinker_session_factory=lambda sid, cfg: None
    )
    assert isinstance(provider, ThinkerRealtimeProvider)
    assert provider.provider_name == "thinker"


# --- release config ---------------------------------------------------------


def _release_config(tmp_path, name: str, **realtime) -> str:
    model_dir = tmp_path / "model"
    model_dir.mkdir(exist_ok=True)
    checkpoint = tmp_path / "duplex.pt"
    checkpoint.write_bytes(b"")
    document: dict[str, Any] = {
        "model": {"model_name_or_path": str(model_dir)},
        "duplex": {"checkpoint": str(checkpoint)},
        "server": {"mode": "lean"},
        "worker": {"provider": "none", "cwd": str(tmp_path)},
    }
    if realtime:
        document["realtime"] = realtime
    path = tmp_path / f"{name}.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return str(path)


def test_a_config_without_a_realtime_section_runs_the_thinker(tmp_path):
    from gnsis_runtime.cli import load_config, preflight_config

    config = load_config(_release_config(tmp_path, "default"))
    assert config.realtime == RealtimeConfig()
    preflight_config(config)


def test_a_config_can_select_venus_and_preflight_checks_its_url(tmp_path):
    from gnsis_runtime.cli import load_config, preflight_config

    config = load_config(
        _release_config(
            tmp_path, "venus", provider="venus", venus_url="http://venus:8000"
        )
    )
    assert config.realtime.provider == "venus"
    assert config.realtime.venus_url == "http://venus:8000"
    preflight_config(config)

    broken = load_config(_release_config(tmp_path, "broken", provider="venus"))
    with pytest.raises(ValueError, match="venus_url"):
        preflight_config(broken)


def test_the_thinker_still_needs_its_checkpoint_but_venus_does_not(tmp_path):
    from gnsis_runtime.cli import load_config

    model_dir = tmp_path / "model"
    model_dir.mkdir()
    base: dict[str, Any] = {
        "model": {"model_name_or_path": str(model_dir)},
        "worker": {"provider": "none"},
    }
    thinker = tmp_path / "thinker.yaml"
    thinker.write_text(yaml.safe_dump(base), encoding="utf-8")
    with pytest.raises(ValueError, match="duplex.checkpoint"):
        load_config(str(thinker))

    venus = tmp_path / "venus.yaml"
    venus.write_text(
        yaml.safe_dump(
            {**base, "realtime": {"provider": "venus", "venus_url": "http://v:1"}}
        ),
        encoding="utf-8",
    )
    assert load_config(str(venus)).realtime.provider == "venus"


def test_an_unknown_realtime_key_is_rejected_like_every_other_section(tmp_path):
    from gnsis_runtime.cli import load_config

    with pytest.raises(ValueError):
        load_config(_release_config(tmp_path, "typo", provider="thinker", venus_ur="x"))


def test_the_worker_provider_and_the_foreground_provider_are_separate(tmp_path):
    """`worker.provider` picks the background action layer; `realtime.provider`
    picks the model that sees and hears. Setting one must not move the other."""

    from gnsis_runtime.cli import load_config

    config = load_config(
        _release_config(tmp_path, "both", provider="venus", venus_url="http://v:1")
    )
    assert config.worker.provider == "none"
    assert config.realtime.provider == "venus"


def test_serve_builds_the_native_app_for_venus_without_loading_the_thinker(
    tmp_path, monkeypatch
):
    from gnsis_runtime import cli

    def no_thinker(config):
        raise AssertionError("the Thinker must not load for realtime.provider: venus")

    monkeypatch.setattr(cli, "_load_thinker", no_thinker)
    config = cli.load_config("runtime/configs/gnsis-venus-bench.yaml")
    config = replace(
        config,
        server=replace(config.server, runtime_dir=str(tmp_path / "rt")),
    )
    app = cli.build_app(config)
    paths = {route.path for route in app.routes}
    assert {"/ws/duplex", "/ws/screen", "/health"} <= paths
    assert app.title == "GNSIS Native Duplex"


def test_the_shipped_configs_select_their_providers():
    from gnsis_runtime.cli import load_config, preflight_config

    voice = load_config("runtime/configs/gnsis-voice.yaml")
    assert voice.realtime.provider == "thinker"

    venus = load_config("runtime/configs/gnsis-venus-bench.yaml")
    assert venus.realtime.provider == "venus"
    assert venus.worker.provider == "none"
    validate_realtime_config(venus.realtime)
    # The bench config carries no Thinker paths, so serve's preflight — which
    # demands them — must not be what gates it.
    with pytest.raises(FileNotFoundError):
        preflight_config(venus)


def test_build_foreground_provider_builds_venus_without_loading_a_model(
    tmp_path, monkeypatch
):
    from gnsis_runtime import cli

    def no_load(config):
        raise AssertionError("Venus must not load the Thinker")

    monkeypatch.setattr(cli, "_load_thinker", no_load)
    config = cli.load_config(
        _release_config(tmp_path, "venus", provider="venus", venus_url="http://v:1")
    )
    provider = cli.build_foreground_provider(config)
    assert isinstance(provider, VenusRealtimeProvider)


def test_build_foreground_provider_hands_the_thinker_the_live_session_builder(
    tmp_path, monkeypatch
):
    """The Thinker behind the seam is the deployment's model, built by the
    live sockets' own session builder, not a second loader."""

    from gnsis_runtime import cli

    loads: list[Any] = []
    factories: list[dict[str, Any]] = []

    def fake_load(config):
        loads.append(config)
        return "bundle", "talker"

    def fake_factory(bundle, **kwargs):
        factories.append({"bundle": bundle, **kwargs})
        return lambda session_id, session_config: ("session", session_id)

    monkeypatch.setattr(cli, "_load_thinker", fake_load)
    monkeypatch.setattr(cli, "_duplex_params", lambda duplex: "params")
    monkeypatch.setattr(cli, "_duplex_settings", lambda config: "settings")
    import gnsis_runtime.online_duplex as online_duplex

    monkeypatch.setattr(online_duplex, "thinker_session_factory", fake_factory)

    config = cli.load_config(_release_config(tmp_path, "thinker"))
    config = replace(config, server=replace(config.server, runtime_dir=str(tmp_path)))
    provider = cli.build_foreground_provider(config)
    assert isinstance(provider, ThinkerRealtimeProvider)
    assert loads == [config]
    assert factories == [
        {
            "bundle": "bundle",
            "params": "params",
            "settings": "settings",
            "media_dir": tmp_path / "media",
            "detached_talker": "talker",
        }
    ]


# --- matched bench ----------------------------------------------------------


class FakeSession:
    """A provider session that answers each audio push with one audio event
    and ends the turn after the last push — the same for every provider."""

    def __init__(self, name: str, log: list[tuple[str, Any]]) -> None:
        self.name = name
        self.log = log
        self.events: list[ProviderEvent] = []
        self.closed = False

    @property
    def session_id(self) -> str:
        return f"{self.name}-session"

    async def push_audio(self, pcm16: bytes, *, capture_ts_ms: int | None = None) -> None:
        self.log.append(("audio", (len(pcm16), capture_ts_ms)))
        self.events.append(
            ProviderEvent(
                kind="audio",
                payload={"pcm16": b"\x00" * 480},
                epoch=1,
                seq=len(self.log),
                correlation_id="g-1",
            )
        )

    async def push_video_frame(
        self, data: bytes, *, mime_type: str = "image/jpeg", ts_ms: int | None = None
    ) -> None:
        self.log.append(("frame", (len(data), mime_type, ts_ms)))

    async def push_control(self, control: dict[str, Any]) -> None:
        self.log.append(("control", control))

    async def next_event(self, timeout_s: float | None = None) -> ProviderEvent:
        if self.events:
            return self.events.pop(0)
        raise TimeoutError("quiet")

    async def acknowledge_playback(self, output_id: str, *, chunks_played: int) -> bool:
        return True

    async def cancel_output(self, reason: str = "cancelled") -> None:
        self.log.append(("cancel", reason))

    async def close(self) -> None:
        self.closed = True
        self.log.append(("close", None))


class FakeProvider:
    def __init__(self, name: str) -> None:
        self.name = name
        self.log: list[tuple[str, Any]] = []
        self.sessions: list[FakeSession] = []
        self.closed = False

    @property
    def provider_name(self) -> str:
        return self.name

    async def open_session(self, config: ProviderSessionConfig) -> FakeSession:
        self.log.append(("open", config.session_id))
        session = FakeSession(self.name, self.log)
        self.sessions.append(session)
        return session

    async def health(self) -> dict[str, Any]:
        return {"ready": True}

    async def close(self) -> None:
        self.closed = True


def _inputs(**overrides) -> BenchInput:
    base = dict(
        pcm16=b"\x01\x00" * 16000 * 3,  # three seconds
        sample_rate=16000,
        chunk_ms=1000,
        frames=(
            BenchFrame(data=b"jpeg-0", ts_ms=0),
            BenchFrame(data=b"png-1", ts_ms=1500, mime_type="image/png"),
            BenchFrame(data=b"jpeg-late", ts_ms=9000),
        ),
        realtime=False,
    )
    base.update(overrides)
    return BenchInput(**base)


def test_fake_provider_satisfies_the_contract():
    assert isinstance(FakeProvider("x"), RealtimeProvider)
    assert isinstance(FakeSession("x", []), RealtimeSession)


def test_the_bench_drives_two_providers_with_identical_calls():
    thinker, venus = FakeProvider("thinker"), FakeProvider("venus")
    inputs = _inputs()
    config = ProviderSessionConfig(session_id="matched")
    reports = [
        asyncio.run(run_bench(provider, inputs, session_config=config))
        for provider in (thinker, venus)
    ]

    assert thinker.log == venus.log
    assert thinker.log == [
        ("open", "matched"),
        ("frame", (6, "image/jpeg", 0)),
        ("audio", (32000, 0)),
        ("audio", (32000, 1000)),
        # A frame captured at 1.5s is already on screen when the 2s chunk is
        # spoken, so it precedes that chunk.
        ("frame", (5, "image/png", 1500)),
        ("audio", (32000, 2000)),
        # Frames timestamped after the audio ends still arrive, in order.
        ("frame", (9, "image/jpeg", 9000)),
        ("close", None),
    ]
    assert [report.provider for report in reports] == ["thinker", "venus"]
    for report, provider in zip(reports, (thinker, venus)):
        assert report.error is None
        assert report.stop_reason == "idle"
        assert report.input_ms == 3000
        assert report.frames == 3
        assert report.events_by_kind == {"audio": 3}
        assert report.audio_pcm16_bytes == 3 * 480
        assert report.first_event_ms == report.first_audio_ms
        assert report.first_text_ms is None
        assert [event.correlation_id for event in report.events] == ["g-1"] * 3
        assert provider.closed and provider.sessions[0].closed
    # Same input, same calls: the two reports differ only in the provider.
    assert reports[0].to_dict() | {"provider": "venus", "session_id": "matched"} == (
        reports[1].to_dict() | {"session_id": "matched"}
    )


def test_the_bench_times_events_on_the_injected_clock():
    now = {"t": 100.0}

    def clock() -> float:
        return now["t"]

    class Session(FakeSession):
        async def push_audio(self, pcm16, *, capture_ts_ms=None):
            now["t"] += 0.25
            await super().push_audio(pcm16, capture_ts_ms=capture_ts_ms)

        async def next_event(self, timeout_s=None):
            now["t"] += 0.1
            return await super().next_event(timeout_s)

    class Provider(FakeProvider):
        async def open_session(self, config):
            now["t"] += 0.5
            session = Session(self.name, self.log)
            self.sessions.append(session)
            return session

    report = asyncio.run(
        run_bench(
            Provider("thinker"),
            _inputs(frames=()),
            session_config=ProviderSessionConfig(session_id="clocked"),
            clock=clock,
        )
    )
    # open 0.5s; one empty poll 0.1s; three pushes 0.25s each; then the three
    # queued events are polled 0.1s apart and one more empty poll ends the run.
    assert report.open_ms == 500
    assert report.input_done_ms == 1350
    assert report.first_audio_ms == 1450
    assert [event.t_ms for event in report.events] == [1450, 1550, 1650]
    assert report.duration_ms == 1750


def test_realtime_pacing_waits_for_each_chunks_capture_time():
    now = {"t": 0.0}
    waits: list[float] = []

    def clock() -> float:
        return now["t"]

    async def sleep(seconds: float) -> None:
        waits.append(round(seconds, 3))
        now["t"] += seconds

    class Provider(FakeProvider):
        async def open_session(self, config):
            session = FakeSession(self.name, self.log)

            async def next_event(timeout_s=None):
                if session.events:
                    return session.events.pop(0)
                # The real poll blocks for its timeout; the clock has to move
                # or the idle window could never elapse.
                now["t"] += timeout_s or 0.25
                raise TimeoutError("quiet")

            session.next_event = next_event  # type: ignore[method-assign]
            self.sessions.append(session)
            return session

    report = asyncio.run(
        run_bench(
            Provider("venus"),
            _inputs(frames=(), realtime=True, trailing_idle_sec=1.0),
            session_config=ProviderSessionConfig(session_id="paced"),
            clock=clock,
            sleep=sleep,
        )
    )
    assert report.error is None
    # The first empty poll took 0.25s, so chunk 0 is already due and goes at
    # once; chunk 1 waits the 0.75s left to its capture time, chunk 2 a full
    # second. Each chunk is pushed at its own capture offset, not back to back.
    assert waits == [0.75, 1.0]
    assert report.input_done_ms == 2000
    assert report.stop_reason == "idle"
    assert report.duration_ms == 3000


def test_a_failed_push_ends_the_bench_with_the_error_in_the_report():
    class Provider(FakeProvider):
        async def open_session(self, config):
            session = FakeSession(self.name, self.log)

            async def push_audio(pcm16, *, capture_ts_ms=None):
                raise RuntimeError("venus POST /audio -> 503")

            session.push_audio = push_audio  # type: ignore[method-assign]
            self.sessions.append(session)
            return session

    provider = Provider("venus")
    report = asyncio.run(
        run_bench(
            provider,
            _inputs(frames=()),
            session_config=ProviderSessionConfig(session_id="broken"),
        )
    )
    assert report.stop_reason == "error"
    assert report.error == "RuntimeError: venus POST /audio -> 503"
    assert report.events == []
    # The session and provider are still closed: no leaked server-side session.
    assert provider.sessions[0].closed and provider.closed


def test_a_closed_event_ends_the_run():
    class Provider(FakeProvider):
        async def open_session(self, config):
            session = FakeSession(self.name, self.log)
            session.events.append(ProviderEvent(kind="closed", payload={}))
            self.sessions.append(session)
            return session

    report = asyncio.run(
        run_bench(
            Provider("thinker"),
            _inputs(frames=()),
            session_config=ProviderSessionConfig(session_id="closed"),
        )
    )
    assert report.stop_reason == "closed"
    assert report.events_by_kind["closed"] == 1


def test_bench_inputs_load_from_wav_and_a_frame_directory(tmp_path):
    import wave

    audio = tmp_path / "in.wav"
    with wave.open(str(audio), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x01" * 16000)
    pcm16, rate = load_pcm16_wav(audio)
    assert rate == 16000 and len(pcm16) == 32000
    assert BenchInput(pcm16=pcm16, sample_rate=rate).duration_ms == 1000

    stereo = tmp_path / "stereo.wav"
    with wave.open(str(stereo), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00" * 64)
    with pytest.raises(ValueError, match="mono"):
        load_pcm16_wav(stereo)

    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    (frames_dir / "002.png").write_bytes(b"c")
    (frames_dir / "001.jpg").write_bytes(b"b")
    (frames_dir / "000.jpeg").write_bytes(b"a")
    (frames_dir / "notes.txt").write_text("ignored")
    frames = load_frames(frames_dir, fps=4)
    assert [(f.data, f.ts_ms, f.mime_type) for f in frames] == [
        (b"a", 0, "image/jpeg"),
        (b"b", 250, "image/jpeg"),
        (b"c", 500, "image/png"),
    ]
    with pytest.raises(ValueError):
        load_frames(frames_dir, fps=0)


def test_each_provider_is_asked_for_the_same_behaviour_in_its_own_form():
    from mcpmft.prompts import GNSIS_DUPLEX_SYSTEM_PROMPT
    from gnsis_runtime.providers.foreground import foreground_system_prompt

    thinker = foreground_system_prompt(
        RealtimeConfig(), thinker_prompt=GNSIS_DUPLEX_SYSTEM_PROMPT
    )
    venus = foreground_system_prompt(
        RealtimeConfig(provider="venus", venus_url="http://v:1"),
        thinker_prompt=GNSIS_DUPLEX_SYSTEM_PROMPT,
    )
    # The Thinker keeps the prompt it was trained on, unit protocol included.
    assert thinker is GNSIS_DUPLEX_SYSTEM_PROMPT
    assert "<listen>" in thinker and "<tool_call>" in thinker
    # A native full-duplex model owns turn-taking: same guidance, no protocol.
    assert "<listen>" not in venus and "<speak>" not in venus
    assert "<tool_call>" not in venus
    for shared in (
        "RECENT VISUAL CONTEXT",
        "Do not invent continuity",
        "task_start",
        "Toronto, Canada",
    ):
        assert shared in thinker and shared in venus
    assert "混元" not in thinker
    # An explicit prompt wins for either provider.
    override = RealtimeConfig(system_prompt="say less")
    assert foreground_system_prompt(override, thinker_prompt="x") == "say less"
