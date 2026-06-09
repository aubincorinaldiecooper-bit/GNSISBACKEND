"""A deterministic, offline model for tests and zero-config dogfooding.

The mock is intentionally *prompt-sensitive*: it only reaches for a tool when
the system prompt actually instructs it to. That single behaviour is what makes
the self-evolution loop observable without a network — a weak prompt produces a
wrong answer, and the optimizer can earn a better score by strengthening the
tool-use instruction. The mock never pretends to be smart; it just makes the
mechanics of evolution legible.
"""

from __future__ import annotations

import re
from typing import Any, List, Optional

from .base import BaseModel, Message, ModelResponse, ToolCall

_TOOL_WORDS = ("use", "always", "must", "call", "compute", "rely")
# A run of arithmetic characters; we then keep runs that have a digit and an
# operator. Matching a whole run (rather than a clever sub-pattern) keeps
# parentheses balanced, e.g. "(12 + 30) * 2".
_EXPR_RUN = re.compile(r"[0-9+\-*/().\s]+")


def _system_text(messages: List[Message]) -> str:
    return "\n".join(m.content for m in messages if m.role == "system")


def _last_user(messages: List[Message]) -> str:
    for message in reversed(messages):
        if message.role == "user":
            return message.content
    return ""


def _latest_tool_result(messages: List[Message]) -> Optional[str]:
    for message in reversed(messages):
        if message.role == "tool":
            return message.content
        if message.role == "assistant" and message.tool_calls:
            # Reached the assistant turn that requested tools without a result.
            return None
    return None


def _wants_tools(system_text: str) -> bool:
    lowered = system_text.lower()
    return "tool" in lowered and any(word in lowered for word in _TOOL_WORDS)


def _extract_expression(text: str) -> Optional[str]:
    candidates = []
    for match in _EXPR_RUN.finditer(text):
        run = match.group(0).strip()
        if any(op in run for op in "+-*/") and any(ch.isdigit() for ch in run):
            candidates.append(run)
    if not candidates:
        return None
    return max(candidates, key=len)


def _first_number(text: str) -> Optional[str]:
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return match.group(0) if match else None


class MockModel(BaseModel):
    provider = "mock"
    model = "mock-deterministic"

    def __init__(self, model: str = "mock-deterministic") -> None:
        self.model = model

    def generate(
        self,
        messages: List[Message],
        tools: Optional[List[dict]] = None,
        **kwargs: Any,
    ) -> ModelResponse:
        # If a tool already ran, fold its result into a final answer.
        tool_result = _latest_tool_result(messages)
        if tool_result is not None:
            return ModelResponse(
                text=f"The answer is {tool_result}.",
                finish_reason="stop",
                model=self.model,
            )

        system_text = _system_text(messages)
        task = _last_user(messages)
        expression = _extract_expression(task)

        if tools and expression and _wants_tools(system_text):
            call = ToolCall(id="call_mock_1", name="calculator", arguments={"expression": expression})
            return ModelResponse(
                text="",
                tool_calls=[call],
                finish_reason="tool_calls",
                model=self.model,
            )

        # No tool use: answer directly — and, lacking a calculator, guess badly.
        guess = _first_number(task) or "unknown"
        return ModelResponse(
            text=f"I think the answer is {guess}.",
            finish_reason="stop",
            model=self.model,
        )
