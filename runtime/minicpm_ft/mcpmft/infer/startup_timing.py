"""Cold/warm startup and first-session timing for the GNSIS realtime runtime.

One ``startup_attempt`` ID is minted per process boot; every ``gnsis_startup``
stage line carries it until ``runtime_ready`` so a single container's boot can
be correlated end to end. First-session milestones use the
``gnsis_session_timing`` prefix with the session's own ``session_id`` and a
``startup_class`` of ``cold`` (the socket connected before the runtime was
ready) or ``warm`` (it connected to an already-ready runtime).

Durations always come from ``time.monotonic``. Wall-clock is added only where
cross-service correlation needs it. The module is deliberately dependency-free
at import: torch is touched lazily inside resource snapshots so importing it
never costs startup time.

Nothing here may log secrets, websocket tokens, raw audio, or image bytes —
stage and event names are fixed strings and every value is a number or a
session/attempt identifier.
"""

from __future__ import annotations

import logging
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

LOGGER = logging.getLogger(__name__)

# Elapsed baseline: the first instrumented point in this process. It is the
# nearest truthful "container process start" we can observe from inside —
# interpreter boot and import time before this module load are unknowable here
# and are documented as such.
_PROCESS_T0 = time.monotonic()

_ATTEMPT_ID = secrets.token_hex(4)


@dataclass
class _Stage:
    name: str
    gpu: int | None
    start_ms: int
    end_ms: int | None = None


_STAGES: list[_Stage] = []
_FIRST_EVENTS: dict[str, float] = {}


def process_ms() -> int:
    """Milliseconds since the first instrumented point (monotonic)."""

    return round((time.monotonic() - _PROCESS_T0) * 1000)


def attempt_id() -> str:
    return _ATTEMPT_ID


def mark(stage: str, event: str, *, gpu: int | None = None, **fields: Any) -> int:
    """Emit one ``gnsis_startup`` line and return its elapsed_ms.

    ``event`` is ``start`` or ``end`` for paired stages, or a one-shot marker
    name (``ready``/``snapshot`` style) for instants. Extra ``key=value``
    fields may be appended; values must be plain numbers/strings, never
    payload bytes.
    """

    elapsed = process_ms()
    if event == "start":
        _STAGES.append(_Stage(stage, gpu, elapsed))
    else:
        # Close the most recent still-open stage with this name, if any.
        for rec in reversed(_STAGES):
            if rec.name == stage and rec.end_ms is None:
                rec.end_ms = elapsed
                break
    parts = [
        "gnsis_startup",
        f"startup_attempt={_ATTEMPT_ID}",
        f"stage={stage}",
        f"event={event}",
        f"elapsed_ms={elapsed}",
    ]
    if gpu is not None:
        parts.append(f"gpu={gpu}")
    if event == "end":
        duration = stage_duration_ms(stage)
        if duration is not None:
            parts.append(f"stage_duration_ms={duration}")
    for key, value in fields.items():
        parts.append(f"{key}={value}")
    LOGGER.info("%s", " ".join(parts))
    return elapsed


@contextmanager
def stage(name: str, *, gpu: int | None = None, **end_fields: Any) -> Iterator[None]:
    """Instrument a ``start``/``end`` stage pair around a block."""

    mark(name, "start", gpu=gpu)
    try:
        yield
    finally:
        mark(name, "end", gpu=gpu, **end_fields)


def stage_duration_ms(name: str) -> int | None:
    for rec in reversed(_STAGES):
        if rec.name == name and rec.end_ms is not None:
            return rec.end_ms - rec.start_ms
    return None


def observe_once(name: str) -> float:
    """Record the first instant a named process event fired; return its time.

    Used for events whose exact instant lives inside a component that does not
    know the session (e.g. the detached Talker worker accepting a generation),
    so the session layer can still report elapsed time truthfully.
    """

    if name not in _FIRST_EVENTS:
        _FIRST_EVENTS[name] = time.monotonic()
    return _FIRST_EVENTS[name]


def first_event_ms(name: str) -> int | None:
    value = _FIRST_EVENTS.get(name)
    if value is None:
        return None
    return round((value - _PROCESS_T0) * 1000)


def first_event_at(name: str) -> float | None:
    return _FIRST_EVENTS.get(name)


def _meminfo() -> dict[str, int]:
    info: dict[str, int] = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                info[key.strip()] = int(rest.strip().split()[0])  # kB
    except OSError:
        pass
    return info


def resource_snapshot(label: str) -> None:
    """One cheap ``gnsis_startup`` resource line: RSS, host RAM, GPU memory.

    Reads ``/proc`` directly and torch's allocator counters only when torch is
    already imported — no profiler, no extra CUDA work on the critical path.
    """

    rss_mb = None
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    rss_mb = round(int(line.split()[1]) / 1024, 1)
                    break
    except OSError:
        pass
    meminfo = _meminfo()
    host_ram_available_mb = (
        round(meminfo["MemAvailable"] / 1024, 1) if "MemAvailable" in meminfo else None
    )
    gpu_parts: list[str] = []
    try:
        import torch  # noqa: PLC0415 - lazy, optional

        if torch.cuda.is_available():
            for index in range(torch.cuda.device_count()):
                allocated = round(torch.cuda.memory_allocated(index) / 1e6, 1)
                reserved = round(torch.cuda.memory_reserved(index) / 1e6, 1)
                gpu_parts.append(
                    f"gpu{index}_alloc_mb={allocated} gpu{index}_reserved_mb={reserved}"
                )
    except Exception:  # pragma: no cover - diagnostics must never break startup
        pass
    fields: dict[str, Any] = {"label": label}
    if rss_mb is not None:
        fields["rss_mb"] = rss_mb
    if host_ram_available_mb is not None:
        fields["host_ram_available_mb"] = host_ram_available_mb
    mark("resources", "snapshot", **fields)
    if gpu_parts:
        LOGGER.info(
            "gnsis_startup startup_attempt=%s stage=resources event=gpu_snapshot "
            "label=%s %s",
            _ATTEMPT_ID,
            label,
            " ".join(gpu_parts),
        )


def covered_ms() -> tuple[int, int, int]:
    """Union of completed stage intervals: (covered, serial_sum, overlap)."""

    intervals = sorted(
        (rec.start_ms, rec.end_ms)
        for rec in _STAGES
        if rec.end_ms is not None
    )
    covered = 0
    serial = 0
    cursor: int | None = None
    span_start: int | None = None
    for start, end in intervals:
        serial += end - start
        if cursor is None or start > cursor:
            if cursor is not None and span_start is not None:
                covered += cursor - span_start
            span_start = start
            cursor = end
        elif end > cursor:
            cursor = end
    if cursor is not None and span_start is not None:
        covered += cursor - span_start
    return covered, serial, max(0, serial - covered)


# Stage names -> the /health ``startup_stage_seconds`` keys the report reads.
_HEALTH_STAGE_KEYS = {
    "thinker_model_load": "thinker",
    "detached_talker_init": "talker",
    "token2wav_init": "token2wav",
    "prefix_prepare": "prefix_prepare",
    "first_unit_warmup": "first_unit_warmup",
    "detached_speech_warmup": "detached_speech_warmup",
}


def health_fields() -> dict[str, Any]:
    """Truthful startup fields for ``/health`` — no secrets, no guesses."""

    stage_seconds = {
        key: round(stage_duration_ms(name) / 1000, 3)
        for name, key in _HEALTH_STAGE_KEYS.items()
        if stage_duration_ms(name) is not None
    }
    runtime_ready_ms = first_event_ms("runtime_ready")
    return {
        "startup_attempt": _ATTEMPT_ID,
        # The rule is documented: cold = this process loaded the model during
        # the current boot; warm = a session reusing an already-ready runtime
        # (which is what the session-level timing line reports). A completed
        # model load in-process is always a cold load.
        "startup_class": "cold",
        "startup_total_seconds": (
            round(runtime_ready_ms / 1000, 3) if runtime_ready_ms is not None else None
        ),
        "startup_stage_seconds": stage_seconds,
    }
