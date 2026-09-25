"""Model-agnostic realtime provider contract.

Every foreground realtime model (Gander Thinker/Talker baseline,
Realtime-Venus, any future native-Omni model) hides behind this surface so
the session/timeline/delivery plumbing stays identical when the model
changes. The contract is deliberately not shaped around Gander's detached
Talker: a provider that generates speech natively inside one realtime loop
fits it without pretending to have a separate talker.

`ProviderEvent` is the normalized output unit. `kind` maps onto shared
timeline event kinds so every provider reports into the same event plane:

- ``audio``       a chunk of model speech (pcm16 bytes in ``payload["pcm16"]``)
- ``text``        model text output
- ``turn``        turn/listen-state transition (``payload["state"]``)
- ``interrupt``   the model decided the user interrupted / it yielded
- ``tool_call``   the model wants a tool run
- ``control``     provider-specific control worth recording verbatim
- ``closed``      session ended

``epoch`` is the model's output/generation epoch when it has one
(Venus ``generation_epoch``, Gander response unit), else ``None``. The
delivery gate's output-epoch invalidation applies the same way regardless
of provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

EventKind = Literal[
    "audio",
    "text",
    "turn",
    "interrupt",
    "tool_call",
    "control",
    "closed",
]


@dataclass(frozen=True)
class ProviderEvent:
    """One normalized model output unit."""

    kind: EventKind
    payload: dict[str, Any]
    epoch: int | None = None
    seq: int | None = None
    correlation_id: str | None = None
    raw: dict[str, Any] | None = None


@dataclass
class ProviderSessionConfig:
    """Per-session knobs every provider accepts."""

    session_id: str
    input_sample_rate: int = 16000
    output_sample_rate: int = 24000
    system_prompt: str | None = None
    ref_audio_path: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class RealtimeSession(Protocol):
    """One live model session."""

    @property
    def session_id(self) -> str: ...

    async def push_audio(
        self, pcm16: bytes, *, capture_ts_ms: int | None = None
    ) -> None:
        """Feed raw mic audio. ASR never sits in front of this path."""
        ...

    async def push_video_frame(
        self, data: bytes, *, mime_type: str = "image/jpeg", ts_ms: int | None = None
    ) -> None:
        ...

    async def push_control(self, control: dict[str, Any]) -> None:
        """Provider-specific control (prefill, tool result, interruption...)."""
        ...

    async def next_event(self, timeout_s: float | None = None) -> ProviderEvent:
        """Next normalized output event; raises TimeoutError on timeout."""
        ...

    async def acknowledge_playback(
        self, output_id: str, *, chunks_played: int
    ) -> bool:
        """Device playback acknowledgement — the authoritative delivered signal."""
        ...

    async def cancel_output(self, reason: str = "cancelled") -> None:
        ...

    async def close(self) -> None:
        ...


@runtime_checkable
class RealtimeProvider(Protocol):
    """Factory of model sessions behind one provider identity."""

    @property
    def provider_name(self) -> str: ...

    async def open_session(self, config: ProviderSessionConfig) -> RealtimeSession:
        ...

    async def health(self) -> dict[str, Any]:
        ...

    async def close(self) -> None:
        ...
