"""Tool protocol.

Tools are callable capabilities exposed to an agent. We describe them with a
JSON Schema and emit OpenAI-compatible ``function`` specs, since GNSIS talks to
models through an OpenAI-style chat-completions API (OpenRouter by default).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict


@dataclass
class ToolResult:
    """The outcome of a single tool invocation."""

    content: str
    is_error: bool = False


class Tool(ABC):
    """Base class for a callable tool.

    Subclasses set ``name``, ``description``, and ``parameters`` (a JSON Schema
    object) and implement :meth:`run`.
    """

    name: str = ""
    description: str = ""
    parameters: Dict[str, Any] = {"type": "object", "properties": {}}

    def openai_spec(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    @abstractmethod
    def run(self, **kwargs: Any) -> ToolResult:
        """Execute the tool and return a :class:`ToolResult`."""
        raise NotImplementedError
