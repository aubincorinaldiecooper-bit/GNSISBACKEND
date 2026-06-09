"""The tool-calling agent — the 'Act' phase.

This is the one agent type Autogenesis ships as stable, and it is the heart of
the GNSIS MVP: drive a model in a loop, execute any tools it requests, feed the
results back, and stop when it produces a final answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional

from ..models.base import BaseModel, Message
from ..tools.registry import ToolRegistry
from ..tracer.tracer import Tracer


@dataclass
class AgentResult:
    output: str
    messages: List[Message] = field(default_factory=list)
    steps: int = 0
    tool_calls: int = 0
    used_tool: bool = False


class ToolCallingAgent:
    def __init__(
        self,
        model: BaseModel,
        tools: ToolRegistry,
        system_prompt: str,
        max_steps: int = 6,
        tracer: Optional[Tracer] = None,
        name: str = "tool_calling_agent",
    ) -> None:
        self.model = model
        self.tools = tools
        self.system_prompt = system_prompt
        self.max_steps = max_steps
        self.tracer = tracer
        self.name = name

    def run(self, task: str, files: Any = None, ctx: Any = None) -> AgentResult:
        messages: List[Message] = [
            Message.system(self.system_prompt),
            Message.user(task),
        ]
        tool_specs = self.tools.specs()
        total_tool_calls = 0
        used_tool = False

        if self.tracer:
            self.tracer.event("agent_start", {"agent": self.name, "task": task})

        for step in range(1, self.max_steps + 1):
            response = self.model.generate(messages, tools=tool_specs)
            if self.tracer:
                self.tracer.event(
                    "model_response",
                    {"step": step, "text": response.text, "tool_calls": len(response.tool_calls)},
                )
            messages.append(Message.assistant(response.text, response.tool_calls))

            if not response.tool_calls:
                if self.tracer:
                    self.tracer.event("agent_final", {"step": step, "output": response.text})
                return AgentResult(
                    output=response.text,
                    messages=messages,
                    steps=step,
                    tool_calls=total_tool_calls,
                    used_tool=used_tool,
                )

            for call in response.tool_calls:
                used_tool = True
                total_tool_calls += 1
                result = self.tools.run(call.name, call.arguments)
                if self.tracer:
                    self.tracer.event(
                        "tool_call",
                        {
                            "step": step,
                            "name": call.name,
                            "arguments": call.arguments,
                            "result": result.content,
                            "is_error": result.is_error,
                        },
                    )
                messages.append(Message.tool(call.id, result.content, name=call.name))

        # Step budget exhausted — force one final answer without tools.
        final = self.model.generate(messages, tools=None)
        messages.append(Message.assistant(final.text))
        if self.tracer:
            self.tracer.event("agent_final", {"step": self.max_steps, "output": final.text})
        return AgentResult(
            output=final.text,
            messages=messages,
            steps=self.max_steps,
            tool_calls=total_tool_calls,
            used_tool=used_tool,
        )
