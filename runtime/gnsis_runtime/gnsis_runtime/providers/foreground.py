"""Foreground realtime provider selection.

One configuration key chooses which model sits behind the shared
``RealtimeProvider`` seam: the Thinker baseline (the in-process
Thinker/Talker stack) or Realtime-Venus (a remote native-Omni model server).
Everything downstream drives the result through the same ``RealtimeSession``
calls, so a deployment or a matched benchmark can switch models without
touching session, timeline or delivery plumbing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal

from ..realtime_provider import ProviderSessionConfig, RealtimeProvider
from .thinker import ThinkerRealtimeProvider
from .venus import VenusRealtimeProvider

ForegroundProviderName = Literal["thinker", "venus"]

FOREGROUND_PROVIDERS: tuple[ForegroundProviderName, ...] = ("thinker", "venus")
DEFAULT_FOREGROUND_PROVIDER: ForegroundProviderName = "thinker"

ThinkerSessionFactory = Callable[[str, ProviderSessionConfig], Any]


@dataclass(frozen=True)
class RealtimeConfig:
    """The ``realtime`` section of a release config."""

    provider: ForegroundProviderName = DEFAULT_FOREGROUND_PROVIDER
    # Venus runs as its own model server; the runtime stays a thin client.
    venus_url: str | None = None
    venus_timeout_sec: float = 30.0


def validate_realtime_config(config: RealtimeConfig) -> None:
    if config.provider not in FOREGROUND_PROVIDERS:
        raise ValueError(
            f"realtime.provider must be one of {', '.join(FOREGROUND_PROVIDERS)}; "
            f"got {config.provider!r}"
        )
    if config.venus_timeout_sec <= 0:
        raise ValueError("realtime.venus_timeout_sec must be positive")
    if config.provider == "venus":
        if not config.venus_url:
            raise ValueError("realtime.provider venus requires realtime.venus_url")
        try:
            VenusRealtimeProvider(config.venus_url, timeout_s=config.venus_timeout_sec)
        except ValueError as exc:
            raise ValueError(f"realtime.venus_url: {exc}") from exc


def build_realtime_provider(
    config: RealtimeConfig,
    *,
    thinker_session_factory: ThinkerSessionFactory | None = None,
) -> RealtimeProvider:
    """Build the configured foreground provider.

    ``thinker_session_factory`` owns model weights and devices; it is only
    required, and only consulted, when the Thinker is the configured provider,
    so selecting Venus never loads the Thinker.
    """

    validate_realtime_config(config)
    provider: RealtimeProvider
    if config.provider == "thinker":
        if thinker_session_factory is None:
            raise ValueError(
                "realtime.provider thinker requires a thinker session factory"
            )
        provider = ThinkerRealtimeProvider(thinker_session_factory)
    else:
        assert config.venus_url is not None
        provider = VenusRealtimeProvider(
            config.venus_url, timeout_s=config.venus_timeout_sec
        )
    if provider.provider_name != config.provider:
        raise TypeError(
            f"foreground provider for {config.provider!r} reported "
            f"{provider.provider_name!r}"
        )
    return provider
