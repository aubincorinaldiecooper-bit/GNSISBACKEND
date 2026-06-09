"""Model construction and provider auto-detection."""

from __future__ import annotations

import os
from typing import Any

from .base import BaseModel
from .mock import MockModel
from .openrouter import OpenRouterModel


def resolve_provider(provider: str = "auto") -> str:
    """Resolve ``auto`` to a concrete provider based on the environment."""
    provider = (provider or "auto").lower()
    if provider != "auto":
        return provider
    if os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY"):
        return "openrouter"
    return "mock"


def create_model(provider: str = "auto", **kwargs: Any) -> BaseModel:
    """Build a model backend.

    ``provider="auto"`` selects OpenRouter when an API key is present and falls
    back to the deterministic mock otherwise — so the runtime always works.
    """
    resolved = resolve_provider(provider)
    if resolved == "mock":
        # The mock always self-identifies; it ignores a configured live-model
        # slug so the banner never implies a real model was called.
        model = MockModel()
    elif resolved in ("openrouter", "openai", "openai-compatible"):
        model = OpenRouterModel(**kwargs)
    else:
        raise ValueError(f"unknown provider: {resolved!r}")
    model.provider = resolved
    return model
