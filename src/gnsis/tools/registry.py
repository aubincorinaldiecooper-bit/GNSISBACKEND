"""A registry that owns tool instances and dispatches calls to them."""

from __future__ import annotations

from typing import Dict, List

from .base import Tool, ToolResult
from .builtin import CalculatorTool, StringReverseTool


class ToolRegistry:
    """Holds the tools available to an agent and runs them by name."""

    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> "ToolRegistry":
        if not tool.name:
            raise ValueError("tool must define a name")
        self._tools[tool.name] = tool
        return self

    def get(self, name: str) -> Tool:
        return self._tools[name]

    def names(self) -> List[str]:
        return sorted(self._tools)

    def specs(self) -> List[dict]:
        """OpenAI-compatible ``tools`` array."""
        return [self._tools[name].openai_spec() for name in self.names()]

    def run(self, name: str, arguments: dict) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(f"error: unknown tool {name!r}", is_error=True)
        try:
            return tool.run(**(arguments or {}))
        except TypeError as exc:
            return ToolResult(f"error: bad arguments for {name!r}: {exc}", is_error=True)


def default_registry() -> ToolRegistry:
    """A registry pre-loaded with the built-in tools."""
    registry = ToolRegistry()
    registry.register(CalculatorTool())
    registry.register(StringReverseTool())
    return registry
