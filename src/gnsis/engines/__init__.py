"""Concrete PatchEngines and a tiny registry.

Engine #1 is the Anthropic Claude Agent SDK, engine #2 is OpenHands, and
``gnsis`` is our own engine: OpenRouter (or any OpenAI-compatible endpoint,
e.g. a LiteLLM proxy) driving our own tool-calling loop, so every model call
and tool call is ours to observe. The deterministic ``mock`` engine (from the
orchestration core) is always available for offline tests and the demo.
"""

from __future__ import annotations

from ..orchestration.engine import MockEngine, PatchEngine


def get_engine(name: str, **kwargs) -> PatchEngine:
    """Construct a PatchEngine by name. Heavy engines import lazily."""
    key = (name or "claude").lower()
    if key == "mock":
        return MockEngine(**kwargs)
    if key == "claude":
        from .claude_agent import ClaudeAgentEngine

        return ClaudeAgentEngine(**kwargs)
    if key == "openhands":
        from .openhands import OpenHandsEngine

        return OpenHandsEngine(**kwargs)
    if key == "gnsis":
        from .gnsis_native import GnsisNativeEngine

        return GnsisNativeEngine(**kwargs)
    raise ValueError(f"unknown engine: {name!r}")


__all__ = ["get_engine"]
