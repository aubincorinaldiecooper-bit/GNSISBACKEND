"""Compare realtime providers under the same session/timeline plumbing.

Drives one provider through a fixed scenario — stream mic audio, optionally
interrupt mid-response — while every normalized provider event lands on a
SessionTimeline. The summary makes Thinker vs Venus comparable:

- session open latency;
- first-audio latency (open -> first ``audio`` event);
- audio chunk count / audio bytes emitted;
- interrupt -> first post-interrupt event latency;
- per-kind event counts.

Usage:

    python tools/realtime_compare/run_compare.py \
        --provider venus --venus-url http://127.0.0.1:8077 \
        --audio /path/to/utterance.wav --interrupt-after-ms 400 \
        --out /tmp/compare/venus

Thinker needs a session factory — pass a callable as
``module:function`` via --thinker-factory that returns a GNSISDuplexSession.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import logging
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "runtime/gnsis_runtime"))

from gnsis_runtime.providers.thinker import ThinkerRealtimeProvider  # noqa: E402
from gnsis_runtime.providers.venus import VenusRealtimeProvider  # noqa: E402
from gnsis_runtime.realtime_provider import ProviderSessionConfig  # noqa: E402
from gnsis_runtime.timeline import SessionTimeline  # noqa: E402

LOGGER = logging.getLogger("realtime_compare")
_CHUNK_MS = 20


def _pcm_chunks(path: Path) -> list[bytes]:
    if path.suffix == ".wav":
        with wave.open(str(path), "rb") as wav:
            assert wav.getsampwidth() == 2 and wav.getnchannels() == 1
            return [
                wav.readframes(wav.getframerate() * _CHUNK_MS // 1000)
                for _ in range(wav.getnframes() // (wav.getframerate() * _CHUNK_MS // 1000))
            ]
    raw = path.read_bytes()
    frame_bytes = 16000 * _CHUNK_MS // 1000 * 2
    return [raw[i : i + frame_bytes] for i in range(0, len(raw), frame_bytes)]


async def run_scenario(provider, args) -> dict:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    timeline = SessionTimeline(
        f"compare-{provider.provider_name}", log_path=out / "timeline.jsonl"
    )
    t0 = time.monotonic()
    config = ProviderSessionConfig(
        session_id=timeline.session_id,
        system_prompt=args.system_prompt,
    )
    session = await provider.open_session(config)
    open_ms = (time.monotonic() - t0) * 1000

    chunks = _pcm_chunks(Path(args.audio))
    interrupt_at = args.interrupt_after_ms / 1000 if args.interrupt_after_ms else None
    interrupt_sent = interrupt_ms = first_audio_ms = None
    counts: dict[str, int] = {}
    audio_bytes = 0
    pushed = 0
    last_interrupt_event_ms = None

    async def drain(deadline_s: float) -> None:
        nonlocal first_audio_ms, audio_bytes, last_interrupt_event_ms
        while time.monotonic() - t0 < deadline_s:
            try:
                event = await session.next_event(timeout_s=0.05)
            except TimeoutError:
                continue
            counts[event.kind] = counts.get(event.kind, 0) + 1
            timeline.emit(
                f"model.{event.kind}",
                component=provider.provider_name,
                output_epoch=event.epoch,
                correlation_id=event.correlation_id,
                fields={"payload_keys": sorted(event.payload.keys())},
            )
            if event.kind == "audio":
                if first_audio_ms is None:
                    first_audio_ms = (time.monotonic() - t0) * 1000
                audio_bytes += len(event.payload.get("pcm16") or b"")
                if interrupt_sent and last_interrupt_event_ms is None:
                    last_interrupt_event_ms = (time.monotonic() - t0) * 1000

    pushed_start = time.monotonic()
    for i, chunk in enumerate(chunks):
        await session.push_audio(chunk, capture_ts_ms=int(i * _CHUNK_MS))
        pushed += 1
        elapsed = time.monotonic() - pushed_start
        await drain(elapsed)
        if interrupt_at is not None and not interrupt_sent and elapsed >= interrupt_at:
            interrupt_ms = (time.monotonic() - t0) * 1000
            await session.push_control({"kind": "interrupt"})
            interrupt_sent = True
            timeline.emit("user.interruption", component="compare")
        await asyncio.sleep(_CHUNK_MS / 1000)

    await drain(time.monotonic() - t0 + args.tail_s)
    await session.close()

    summary = {
        "provider": provider.provider_name,
        "open_ms": round(open_ms, 1),
        "first_audio_ms": None if first_audio_ms is None else round(first_audio_ms, 1),
        "interrupt_ms": None if interrupt_ms is None else round(interrupt_ms, 1),
        "post_interrupt_first_event_ms": (
            None
            if last_interrupt_event_ms is None or interrupt_ms is None
            else round(last_interrupt_event_ms - interrupt_ms, 1)
        ),
        "audio_bytes": audio_bytes,
        "audio_chunks_pushed": pushed,
        "event_counts": counts,
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def _load_provider(args):
    if args.provider == "venus":
        return VenusRealtimeProvider(args.venus_url, timeout_s=args.timeout_s)
    module_name, _, func = args.thinker_factory.partition(":")
    if not module_name or not func:
        raise ValueError("--thinker-factory must be module:function")
    factory = getattr(importlib.import_module(module_name), func)
    return ThinkerRealtimeProvider(factory)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")
    p = argparse.ArgumentParser(description="Thinker vs Venus realtime comparison")
    p.add_argument("--provider", choices=["thinker", "venus"], required=True)
    p.add_argument("--venus-url", default="http://127.0.0.1:8077")
    p.add_argument("--thinker-factory", default="")
    p.add_argument("--audio", required=True, help="16kHz mono pcm16 or .wav file")
    p.add_argument("--interrupt-after-ms", type=int, default=0)
    p.add_argument("--tail-s", type=float, default=5.0)
    p.add_argument("--timeout-s", type=float, default=30.0)
    p.add_argument("--system-prompt", default=None)
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)
    summary = asyncio.run(run_scenario(_load_provider(args), args))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
