"""Tools: callable capabilities exposed to agents."""

from .base import Tool, ToolResult
from .builtin import CalculatorTool, StringReverseTool, safe_arithmetic
from .registry import ToolRegistry, default_registry

__all__ = [
    "Tool",
    "ToolResult",
    "CalculatorTool",
    "StringReverseTool",
    "safe_arithmetic",
    "ToolRegistry",
    "default_registry",
]
