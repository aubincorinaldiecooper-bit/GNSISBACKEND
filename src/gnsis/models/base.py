"""Provider-neutral model interface.

The message/response shapes mirror the OpenAI chat-completions format because
that is what GNSIS speaks on the wire (OpenRouter by default). Keeping a single
neutral representation lets the agent loop work identically against the
offline :class:`~gnsis.models.mock.MockModel` and a live model.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ToolCall:
    """A model's request to invoke a tool."""

    id: str
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Message:
    """One turn in a conversation.

    ``role`` is one of ``system`` | ``user`` | ``assistant`` | ``tool``.
    Assistant turns may carry ``tool_calls``; tool turns carry the
    ``tool_call_id`` they answer.
    """

    role: str
    content: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    tool_call_id: Optional[str] = None
    name: Optional[str] = None

    @classmethod
    def system(cls, content: str) -> "Message":
        return cls("system", content)

    @classmethod
    def user(cls, content: str) -> "Message":
        return cls("user", content)

    @classmethod
    def assistant(cls, content: str = "", tool_calls: Optional[List[ToolCall]] = None) -> "Message":
        return cls("assistant", content, tool_calls=tool_calls or [])

    @classmethod
    def tool(cls, tool_call_id: str, content: str, name: Optional[str] = None) -> "Message":
        return cls("tool", content, tool_call_id=tool_call_id, name=name)


@dataclass
class ModelResponse:
    """A single model completion."""

    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: Dict[str, Any] = field(default_factory=dict)
    raw: Any = None
    model: str = ""


class BaseModel(ABC):
    """The interface every model backend implements."""

    #: human-readable provider label, set by the registry
    provider: str = "base"
    #: model identifier
    model: str = ""

    @abstractmethod
    def generate(
        self,
        messages: List[Message],
        tools: Optional[List[dict]] = None,
        **kwargs: Any,
    ) -> ModelResponse:
        """Produce one completion for ``messages`` (optionally with ``tools``)."""
        raise NotImplementedError
