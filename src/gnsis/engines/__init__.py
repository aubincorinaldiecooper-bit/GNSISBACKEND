"""Concrete PatchEngines and a tiny registry.

Engine #1 is the Anthropic Claude Agent SDK. The deterministic ``mock`` engine
(from the orchestration core) is always available for offline tests and the
demo. Future engines — an OpenRouter Agent SDK adapter, or a native GNSIS coding
agent — register here without any change to the pipeline, API, or worker.
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
    raise ValueError(f"unknown engine: {name!r}")


__all__ = ["get_engine"]
