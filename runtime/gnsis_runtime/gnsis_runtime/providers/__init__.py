"""Worker provider implementations and their configuration registry.

The foreground realtime model is selected separately, in ``foreground``: the
worker registry below is for background action providers, not for the model
that sees and hears the live session.
"""

from .foreground import (
    DEFAULT_FOREGROUND_PROVIDER,
    FOREGROUND_PROVIDERS,
    RealtimeConfig,
    build_realtime_provider,
    validate_realtime_config,
)
from .registry import (
    ProviderBuildContext,
    ProviderFactoryRegistry,
    ProviderRegistration,
)


def builtin_provider_registry() -> ProviderFactoryRegistry:
    from .codex import CODEX_PROVIDER_REGISTRATION
    from .ornith import ORNITH_PROVIDER_REGISTRATION

    registry = ProviderFactoryRegistry()
    registry.register(CODEX_PROVIDER_REGISTRATION)
    registry.register(ORNITH_PROVIDER_REGISTRATION)
    return registry


__all__ = [
    "DEFAULT_FOREGROUND_PROVIDER",
    "FOREGROUND_PROVIDERS",
    "ProviderBuildContext",
    "ProviderFactoryRegistry",
    "ProviderRegistration",
    "RealtimeConfig",
    "build_realtime_provider",
    "builtin_provider_registry",
    "validate_realtime_config",
]
