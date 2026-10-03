"""Matched-run harness for foreground realtime providers.

Feeds one recorded input — raw 16 kHz pcm16 audio plus optional timestamped
frames — to whichever provider the config selects, through the normalized
``RealtimeSession`` calls only, and records every event it emits with its
wall-clock offset. Run the same input against the Thinker baseline and
Venus and the two reports are directly comparable: same calls, same pacing,
same clock.

The bench never decides anything about the output; it measures. Default
pacing is real time, because a full-duplex model's latency numbers mean
nothing when audio is pushed faster than it was spoken.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
import wave
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Awaitable, Callable

from .realtime_provider import (
    ProviderEvent,
    ProviderSessionConfig,
    RealtimeProvider,
)

LOGGER = logging.getLogger(__name__)

FRAME_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")
MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


@dataclass(frozen=True)
class BenchFrame:
    data: bytes
    ts_ms: int
    mime_type: str = "image/jpeg"


@dataclass(frozen=True)
class BenchInput:
    pcm16: bytes
    sample_rate: int = 16000
    chunk_ms: int = 1000
    frames: tuple[BenchFrame, ...] = ()
    # Stop once the input is exhausted and the model has said nothing for this
    # long; a full-duplex model has no end-of-turn the bench could wait for.
    trailing_idle_sec: float = 3.0
    max_duration_sec: float = 120.0
    # Push audio at the rate it was spoken. Off only for unit tests.
    realtime: bool = True

    @property
    def duration_ms(self) -> int:
        return int(len(self.pcm16) / (self.sample_rate * 2) * 1000)

    @property
    def chunk_bytes(self) -> int:
        return int(self.sample_rate * self.chunk_ms / 1000) * 2


@dataclass(frozen=True)
class BenchEvent:
    t_ms: int
    kind: str
    epoch: int | None
    seq: int | None
    correlation_id: str | None
    pcm16_bytes: int
    text: str | None


@dataclass
class BenchReport:
    provider: str
    session_id: str
    input_ms: int
    frames: int
    open_ms: int | None = None
    input_done_ms: int | None = None
    duration_ms: int = 0
    first_event_ms: int | None = None
    first_audio_ms: int | None = None
    first_text_ms: int | None = None
    first_turn_ms: int | None = None
    events_by_kind: dict[str, int] = field(default_factory=dict)
    audio_pcm16_bytes: int = 0
    events: list[BenchEvent] = field(default_factory=list)
    stop_reason: str = "idle"
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


def _event_text(event: ProviderEvent) -> str | None:
    for key in ("text", "value"):
        value = event.payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _event_pcm16_bytes(event: ProviderEvent) -> int:
    pcm = event.payload.get("pcm16")
    return len(pcm) if isinstance(pcm, (bytes, bytearray)) else 0


async def run_bench(
    provider: RealtimeProvider,
    inputs: BenchInput,
    *,
    session_config: ProviderSessionConfig,
    clock: Clock = time.monotonic,
    sleep: Sleep = asyncio.sleep,
    poll_timeout_s: float = 0.25,
) -> BenchReport:
    """Drive one provider session with ``inputs`` and record what it emits."""

    report = BenchReport(
        provider=provider.provider_name,
        session_id=session_config.session_id,
        input_ms=inputs.duration_ms,
        frames=len(inputs.frames),
    )
    t0 = clock()

    def now_ms() -> int:
        return round((clock() - t0) * 1000)

    input_done = asyncio.Event()
    last_activity_ms = 0

    async def pump() -> None:
        nonlocal last_activity_ms
        frames = sorted(inputs.frames, key=lambda frame: frame.ts_ms)
        next_frame = 0
        chunk_bytes = inputs.chunk_bytes
        offsets = range(0, len(inputs.pcm16), chunk_bytes)
        for index, offset in enumerate(offsets):
            ts_ms = index * inputs.chunk_ms
            if inputs.realtime:
                wait_s = ts_ms / 1000 - (clock() - t0)
                if wait_s > 0:
                    await sleep(wait_s)
            while next_frame < len(frames) and frames[next_frame].ts_ms <= ts_ms:
                frame = frames[next_frame]
                await session.push_video_frame(
                    frame.data, mime_type=frame.mime_type, ts_ms=frame.ts_ms
                )
                next_frame += 1
            await session.push_audio(
                inputs.pcm16[offset : offset + chunk_bytes], capture_ts_ms=ts_ms
            )
        for frame in frames[next_frame:]:
            await session.push_video_frame(
                frame.data, mime_type=frame.mime_type, ts_ms=frame.ts_ms
            )
        report.input_done_ms = now_ms()
        last_activity_ms = max(last_activity_ms, report.input_done_ms)
        input_done.set()

    def record(event: ProviderEvent) -> None:
        nonlocal last_activity_ms
        t_ms = now_ms()
        last_activity_ms = max(last_activity_ms, t_ms)
        pcm16_bytes = _event_pcm16_bytes(event)
        report.events.append(
            BenchEvent(
                t_ms=t_ms,
                kind=event.kind,
                epoch=event.epoch,
                seq=event.seq,
                correlation_id=event.correlation_id,
                pcm16_bytes=pcm16_bytes,
                text=_event_text(event),
            )
        )
        report.audio_pcm16_bytes += pcm16_bytes
        if report.first_event_ms is None:
            report.first_event_ms = t_ms
        if event.kind == "audio" and report.first_audio_ms is None and pcm16_bytes:
            report.first_audio_ms = t_ms
        if event.kind == "text" and report.first_text_ms is None:
            report.first_text_ms = t_ms
        if event.kind == "turn" and report.first_turn_ms is None:
            report.first_turn_ms = t_ms

    session = None
    pump_task: asyncio.Task[None] | None = None
    try:
        session = await provider.open_session(session_config)
        report.open_ms = now_ms()
        pump_task = asyncio.create_task(pump(), name="gnsis-realtime-bench-pump")
        while True:
            if pump_task.done():
                # Surface a failed push now rather than after the idle wait.
                pump_task.result()
            if now_ms() >= inputs.max_duration_sec * 1000:
                report.stop_reason = "max_duration"
                break
            if (
                input_done.is_set()
                and now_ms() - last_activity_ms >= inputs.trailing_idle_sec * 1000
            ):
                report.stop_reason = "idle"
                break
            try:
                event = await session.next_event(timeout_s=poll_timeout_s)
            except TimeoutError:
                if not inputs.realtime and input_done.is_set():
                    # Unpaced runs have no wall clock to wait out; an empty
                    # poll after the last push is the end of the output.
                    report.stop_reason = "idle"
                    break
            else:
                record(event)
                if event.kind == "closed":
                    report.stop_reason = "closed"
                    break
            # A poll that answers without suspending would otherwise starve
            # the pump; the model must keep receiving input while it speaks.
            await asyncio.sleep(0)
    except Exception as exc:  # the report is the deliverable, failure included
        report.error = f"{type(exc).__name__}: {exc}"
        report.stop_reason = "error"
        LOGGER.exception("realtime bench failed for provider %s", provider.provider_name)
    finally:
        if pump_task is not None and not pump_task.done():
            pump_task.cancel()
            await asyncio.gather(pump_task, return_exceptions=True)
        report.duration_ms = now_ms()
        report.events_by_kind = dict(Counter(event.kind for event in report.events))
        if session is not None:
            try:
                await session.close()
            except Exception:
                LOGGER.warning("provider session close failed", exc_info=True)
        await provider.close()
    return report


def load_pcm16_wav(path: str | Path) -> tuple[bytes, int]:
    """Read a mono 16-bit PCM WAV; returns ``(pcm16, sample_rate)``."""

    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise ValueError(f"{path}: bench audio must be mono 16-bit PCM")
        return handle.readframes(handle.getnframes()), handle.getframerate()


def load_frames(directory: str | Path, *, fps: float) -> tuple[BenchFrame, ...]:
    """Frames from a directory of images, in name order, ``fps`` apart."""

    if fps <= 0:
        raise ValueError("fps must be positive")
    files = sorted(
        path
        for path in Path(directory).iterdir()
        if path.suffix.lower() in FRAME_SUFFIXES
    )
    return tuple(
        BenchFrame(
            data=path.read_bytes(),
            ts_ms=int(index * 1000 / fps),
            mime_type=MIME_BY_SUFFIX[path.suffix.lower()],
        )
        for index, path in enumerate(files)
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Drive one foreground realtime provider with a recorded input and "
            "write a comparable event report"
        )
    )
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--provider",
        choices=("thinker", "venus"),
        help="override realtime.provider from the config",
    )
    parser.add_argument("--venus-url", help="override realtime.venus_url")
    parser.add_argument("--audio", required=True, help="mono 16 kHz 16-bit WAV")
    parser.add_argument("--frames", help="directory of frames, pushed in name order")
    parser.add_argument("--fps", type=float, default=1.0)
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--idle-sec", type=float, default=3.0)
    parser.add_argument("--max-sec", type=float, default=120.0)
    parser.add_argument(
        "--no-realtime",
        action="store_true",
        help="push audio as fast as the provider accepts it (latency numbers are then meaningless)",
    )
    parser.add_argument("--out", required=True, help="report JSON path")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    from .cli import build_foreground_provider, load_config, preflight_config

    args = _parse_args(argv)
    config = load_config(args.config)
    realtime = config.realtime
    if args.provider:
        realtime = replace(realtime, provider=args.provider)
    if args.venus_url:
        realtime = replace(realtime, venus_url=args.venus_url)
    config = replace(config, realtime=realtime)
    logging.basicConfig(
        level=config.server.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if config.realtime.provider == "thinker":
        preflight_config(config)
        if config.server.cuda_visible_devices:
            os.environ["CUDA_VISIBLE_DEVICES"] = config.server.cuda_visible_devices
    else:
        from .providers.foreground import validate_realtime_config

        validate_realtime_config(config.realtime)

    pcm16, sample_rate = load_pcm16_wav(args.audio)
    frames = load_frames(args.frames, fps=args.fps) if args.frames else ()
    inputs = BenchInput(
        pcm16=pcm16,
        sample_rate=sample_rate,
        frames=frames,
        trailing_idle_sec=args.idle_sec,
        max_duration_sec=args.max_sec,
        realtime=not args.no_realtime,
    )
    session_id = args.session_id or f"bench-{config.realtime.provider}-{int(time.time())}"
    session_config = ProviderSessionConfig(
        session_id=session_id,
        input_sample_rate=sample_rate,
        system_prompt=config.duplex.system_prompt,
        ref_audio_path=config.duplex.ref_audio_path,
        extra={"media_mode": "omni" if frames else "voice"},
    )

    provider = build_foreground_provider(config)
    report = asyncio.run(
        run_bench(provider, inputs, session_config=session_config)
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary = {
        key: value
        for key, value in report.to_dict().items()
        if key != "events"
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if report.error:
        raise SystemExit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
