#!/usr/bin/env python3
"""Behavioral evaluation for GNSIS recent visual context.

Drives a *deployed* live runtime over its two real WebSockets — the same path
the phone takes — so a passing scenario proves the model can resolve natural
references against the units already sitting in its bounded context window
(~context_max_units seconds of multimodal history).

This is intentionally not a unit test: the question being answered is whether
the *model* uses retained visual history, which only a real GPU session can
show. Run it where credentials exist, after `deploy_live_runtime`:

    python scripts/eval_recent_visual.py \
        --url wss://<runtime-host> \
        --edge-secret "$GNSIS_EDGE_SECRET" \
        --scenario eval/recent_visual/object_reference.json

Scenario JSON — a list of steps:

    [
      {"show":  "frames/keyboard.jpg",   "seconds": 4},
      {"show":  "frames/roku_remote.jpg","seconds": 4},
      {"show":  "frames/television.jpg", "seconds": 4},
      {"ask":   "questions/which_roku.wav", "tail_seconds": 20},
      {"expect_any": ["remote", "roku"], "expect_none": ["laptop"]}
    ]

Step kinds:

- ``show``  — publish one JPEG on /ws/screen (it stays the live view while
  its ``seconds`` of silence audio advance the model clock), then verify
  ``screen.frame.accepted``. This is the "camera was here" evidence.
- ``ask``   — stream a mono s16le WAV question on /ws/duplex, then feed
  ``tail_seconds`` of silence while the model answers.
- ``expect_any`` / ``expect_all`` / ``expect_none`` — assertions on the
  transcript collected since the last ask. ``expect_any`` passes if any
  needle appears; ``expect_all`` requires every needle; ``expect_none``
  fails on any needle.

WAV files must be 16 kHz, mono, PCM s16le (``ffmpeg -i q.mp3 -ar 16000 -ac 1
-f s16le q.wav``; the script also accepts a RIFF WAV header and strips it).
Audio is paced at real time — the model consumes one unit per second, so a
scenario takes roughly as long as it would on the phone.

Exits 0 when every expect step passes, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import wave
from pathlib import Path

import websockets.sync.client as ws_client

AUDIO_SAMPLE_RATE = 16000
AUDIO_CHUNK_SEC = 0.25
SILENCE = b"\x00\x00" * int(AUDIO_SAMPLE_RATE * AUDIO_CHUNK_SEC)


def _recv_until(ws, wanted: str, *, limit: int = 30, sink: list | None = None) -> dict:
    for _ in range(limit):
        raw = ws.recv()
        if isinstance(raw, (bytes, bytearray)):
            continue  # audio.chunk payloads
        message = json.loads(raw)
        if sink is not None:
            sink.append(message)
        if message.get("type") == wanted:
            return message
        if message.get("type") == "error" and message.get("fatal"):
            raise RuntimeError(f"fatal error before {wanted!r}: {message.get('message')}")
    raise RuntimeError(f"never received {wanted!r}")


def _pcm16(path: Path) -> bytes:
    data = path.read_bytes()
    if data[:4] == b"RIFF":
        with wave.open(str(path), "rb") as wav:
            if wav.getsampwidth() != 2 or wav.getnchannels() != 1:
                raise ValueError(f"{path}: need mono s16le WAV")
            if wav.getframerate() != AUDIO_SAMPLE_RATE:
                raise ValueError(f"{path}: need {AUDIO_SAMPLE_RATE} Hz audio")
            return wav.readframes(wav.getnframes())
    return data


class AudioFeed:
    """Pace PCM16 onto the duplex socket at real time."""

    def __init__(self, ws, start_ms: float) -> None:
        self.ws = ws
        self.sequence = 0
        self.start_sample = 0
        self.captured_at_ms = start_ms
        self.started = time.monotonic()

    def send(self, pcm: bytes, *, pace: bool = True) -> None:
        self.sequence += 1
        samples = len(pcm) // 2
        self.ws.send(json.dumps({
            "type": "audio.frame",
            "sequence": self.sequence,
            "start_sample": self.start_sample,
            "sample_count": samples,
            "captured_at_ms": int(self.captured_at_ms),
        }))
        self.ws.send(pcm)
        self.start_sample += samples
        self.captured_at_ms += samples / AUDIO_SAMPLE_RATE * 1000
        if pace:
            delay = self.captured_at_ms / 1000 - (time.monotonic() - self.started)
            if delay > 0:
                time.sleep(delay)

    def silence(self, seconds: float) -> None:
        chunks = max(1, round(seconds / AUDIO_CHUNK_SEC))
        for _ in range(chunks):
            self.send(SILENCE)


def _drain_transcript(collected: list[dict]) -> str:
    """Speak text from chunk events recorded so far."""
    parts = []
    for message in collected:
        if message.get("type") == "chunk" and not message.get("is_listen"):
            text = str(message.get("text") or "").strip()
            if text:
                parts.append(text)
    return " ".join(parts)


def run(url: str, edge_secret: str, scenario: list[dict], base: Path) -> int:
    headers = {"x-gnsis-edge": edge_secret} if edge_secret else {}
    session_id = f"eval_{int(time.time())}"
    transcript: list[str] = []
    results: list[bool] = []

    with ws_client.connect(
        f"{url}/ws/duplex?session_id={session_id}",
        additional_headers=headers,
        open_timeout=60,
    ) as duplex:
        ready = _recv_until(duplex, "ready")
        screen_info = ready.get("screen") or {}
        if not screen_info.get("enabled"):
            raise RuntimeError("screen channel is not enabled for this session")

        duplex.send(json.dumps(
            {"type": "media.mode", "video": True, "source": "camera"}
        ))
        _recv_until(duplex, "media.mode.done")

        feed = AudioFeed(duplex, start_ms=0.0)
        screen_url = (
            f"{url}/ws/screen?session_id={session_id}"
            f"&token={screen_info['token']}"
        )
        with ws_client.connect(screen_url, additional_headers=headers) as screen:
            _recv_until(screen, "screen.ready")

            last_answer = ""
            for index, step in enumerate(scenario):
                if "show" in step:
                    image = base / step["show"]
                    frame_id = f"eval-{index}-{image.stem}"
                    screen.send(json.dumps({
                        "type": "screen.frame",
                        "frame_id": frame_id,
                        "captured_at_ms": int(feed.captured_at_ms),
                        "encoding": "jpeg",
                        "video_source": "camera",
                    }))
                    screen.send(image.read_bytes())
                    accepted = _recv_until(screen, "screen.frame.accepted")
                    print(
                        f"[show] {image.name} accepted "
                        f"(frame_id={accepted.get('frame_id')})",
                        flush=True,
                    )
                    feed.silence(float(step.get("seconds", 3)))
                elif "ask" in step:
                    feed.send(_pcm16(base / step["ask"]))
                    print(f"[ask] {step['ask']}", flush=True)
                    answer_events: list[dict] = []
                    deadline = time.monotonic() + float(step.get("tail_seconds", 20))
                    # Collect speech while silence keeps the model clock moving.
                    while time.monotonic() < deadline:
                        feed.silence(AUDIO_CHUNK_SEC)
                        try:
                            raw = duplex.recv(timeout=0.1)
                        except Exception:
                            continue
                        if isinstance(raw, (bytes, bytearray)):
                            continue  # audio.chunk payloads
                        answer_events.append(json.loads(raw))
                    last_answer = _drain_transcript(answer_events)
                    transcript.append(last_answer)
                    print(f"[heard] {last_answer or '(silence)'}", flush=True)
                else:
                    text = last_answer.lower()
                    ok = True
                    if "expect_any" in step:
                        ok = ok and any(
                            needle.lower() in text for needle in step["expect_any"]
                        )
                    if "expect_all" in step:
                        ok = ok and all(
                            needle.lower() in text for needle in step["expect_all"]
                        )
                    if "expect_none" in step:
                        ok = ok and all(
                            needle.lower() not in text for needle in step["expect_none"]
                        )
                    results.append(ok)
                    print(f"[expect] {'PASS' if ok else 'FAIL'}: {step}", flush=True)

            duplex.send(json.dumps({"type": "stop"}))

    passed = sum(results)
    print(f"\n{passed}/{len(results)} expectations passed")
    return 0 if results and all(results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", required=True, help="runtime base, e.g. wss://host")
    parser.add_argument("--edge-secret", default="")
    parser.add_argument("--scenario", required=True, type=Path)
    args = parser.parse_args()

    scenario_path: Path = args.scenario
    steps = json.loads(scenario_path.read_text(encoding="utf-8"))
    if not isinstance(steps, list):
        raise SystemExit("scenario must be a JSON list of steps")
    return run(args.url.rstrip("/"), args.edge_secret, steps, scenario_path.parent)


if __name__ == "__main__":
    sys.exit(main())
