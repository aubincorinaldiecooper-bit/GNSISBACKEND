"""Engine #3 — the native GNSIS engine: OpenRouter + our own tool-calling loop.

Unlike :mod:`.claude_agent` (closed SDK, one provider) and :mod:`.openhands`
(open, but an opaque headless subprocess), this engine is *our* code driving
*our* :class:`~gnsis.agent.tool_calling.ToolCallingAgent` against
:class:`~gnsis.models.openrouter.OpenRouterModel`. Two things fall out of that:

* **Model-agnostic by construction.** ``OpenRouterModel`` just speaks the
  OpenAI-compatible chat-completions wire format against a configurable
  ``base_url``. Point ``OPENROUTER_BASE_URL`` at a LiteLLM proxy instead of
  OpenRouter directly and every call — and its cost — flows through LiteLLM,
  with zero changes to this file.
* **True per-step observation.** Every model turn and every tool call passes
  through :class:`~gnsis.tracer.Tracer`, which this engine bridges live into
  the job's :class:`~gnsis.orchestration.engine.PhaseSink` — so
  ``GET /jobs/{id}/logs`` (and the frontend's Activity panel) shows what the
  agent is actually doing step by step, not just phase boundaries.

The agent runs one continuous loop over the whole task (it has read/write/edit/
run tools, so planning and implementing naturally happen together) under the
Ponytail-derived policy in :mod:`gnsis.agent.policy`; tests and the summary are
separate, deterministic steps like every other engine.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from ..agent.policy import build_system_prompt
from ..agent.tool_calling import ToolCallingAgent
from ..models.base import BaseModel, Message, ModelResponse
from ..models.openrouter import OpenRouterModel
from ..orchestration.engine import PhaseSink, Workspace
from ..orchestration.models import EngineResult
from ..orchestration.status import Phase
from ..tools.registry import ToolRegistry
from ..tools.workspace import workspace_tools
from ..tracer.tracer import Tracer
from .common import run_tests

DEFAULT_MODEL = "anthropic/claude-opus-4.8"
_PREVIEW_CHARS = 240


class _UsageTrackingModel(BaseModel):
    """Wraps any model to accumulate token usage across every call it makes."""

    def __init__(self, inner: BaseModel) -> None:
        self.inner = inner
        self.provider = inner.provider
        self.model = inner.model
        self.calls: List[Dict[str, Any]] = []

    def generate(
        self, messages: List[Message], tools: Optional[List[dict]] = None, **kwargs: Any
    ) -> ModelResponse:
        response = self.inner.generate(messages, tools=tools, **kwargs)
        if response.usage:
            self.calls.append(dict(response.usage))
        return response

    def total_usage(self) -> Dict[str, int]:
        totals: Dict[str, int] = {}
        for usage in self.calls:
            for key, value in usage.items():
                if isinstance(value, (int, float)):
                    totals[key] = totals.get(key, 0) + int(value)
        return totals


class _SinkTracer(Tracer):
    """A Tracer that also streams each event into the job's PhaseSink live.

    The base Tracer only accumulates events in memory (for the CLI's local
    trace files); this subclass is what makes those events show up in
    ``GET /jobs/{id}/logs`` while the run is still in progress, not just after
    the fact.
    """

    def __init__(self, sink: PhaseSink) -> None:
        super().__init__()
        self._sink = sink

    def event(self, kind: str, data: Optional[Dict[str, Any]] = None):
        evt = super().event(kind, data)
        message = _describe_event(kind, evt.data)
        if message:
            self._sink.log(message, level="info")
        return evt


def _describe_event(kind: str, data: Dict[str, Any]) -> Optional[str]:
    if kind == "model_response":
        step = data.get("step")
        calls = data.get("tool_calls") or 0
        if calls:
            return f"step {step}: model requested {calls} tool call(s)"
        text = (data.get("text") or "").strip().replace("\n", " ")
        return f"step {step}: model responded — {text[:_PREVIEW_CHARS]}"
    if kind == "tool_call":
        name = data.get("name", "?")
        result = str(data.get("result", "")).strip().replace("\n", " ")
        status = "error" if data.get("is_error") else "ok"
        return f"tool {name} ({status}): {result[:_PREVIEW_CHARS]}"
    if kind == "agent_final":
        text = (data.get("output") or "").strip().replace("\n", " ")
        return f"agent finished: {text[:_PREVIEW_CHARS]}"
    return None


class GnsisNativeEngine:
    """A :class:`~gnsis.orchestration.engine.PatchEngine` on our own agent loop."""

    name = "gnsis"

    def __init__(
        self,
        model: Optional[str] = None,
        max_steps: int = 20,
        max_test_seconds: int = 600,
    ) -> None:
        self.model = model or os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL)
        self.max_steps = max_steps
        self.max_test_seconds = max_test_seconds

    def generate(
        self,
        instruction: str,
        workspace: Optional[Workspace],
        sink: PhaseSink,
    ) -> EngineResult:
        if workspace is None:
            raise ValueError("GnsisNativeEngine requires a checked-out workspace")

        base_model = OpenRouterModel(model=self.model)
        usage_model = _UsageTrackingModel(base_model)
        tools = ToolRegistry()
        for tool in workspace_tools(workspace.path):
            tools.register(tool)

        # plan + patch — one continuous agent loop with full workspace tools.
        sink.begin_phase(Phase.PLAN)
        tracer = _SinkTracer(sink)
        agent = ToolCallingAgent(
            model=usage_model,
            tools=tools,
            system_prompt=build_system_prompt(),
            max_steps=self.max_steps,
            tracer=tracer,
            name=self.name,
        )
        sink.checkpoint(Phase.PLAN, f"Delegated to the native agent ({self.model}).")

        sink.begin_phase(Phase.PATCH)
        result = agent.run(instruction)
        patch = workspace.diff()
        files_changed = workspace.changed_files()
        sink.checkpoint(Phase.PATCH, {"patch": patch, "files_changed": files_changed})

        if not patch.strip():
            sink.log("engine produced no changes", level="warning")
            return EngineResult(
                plan=f"Delegated to the native agent ({self.model}).",
                patch="",
                success=False,
                detail={"engine": self.name, "model": self.model, "steps": result.steps},
            )

        # tests — deterministic, same as every other engine.
        sink.begin_phase(Phase.TESTS)
        tests = run_tests(workspace, sink, self.max_test_seconds)
        sink.checkpoint(Phase.TESTS, tests)

        # summary — a short, separate, tool-free call.
        sink.begin_phase(Phase.SUMMARY)
        summary = self._summarize(usage_model, instruction, patch)
        sink.checkpoint(Phase.SUMMARY, summary)

        return EngineResult(
            plan=f"Delegated to the native agent ({self.model}).",
            patch=patch,
            tests=tests,
            summary=summary,
            files_changed=files_changed,
            success=True,
            detail={
                "engine": self.name,
                "model": self.model,
                "steps": result.steps,
                "tool_calls": result.tool_calls,
                "usage": usage_model.total_usage(),
            },
        )

    def _summarize(self, model: BaseModel, instruction: str, patch: str) -> str:
        messages = [
            Message.system(
                "Summarize the change in 3-6 sentences suitable for a pull request "
                "description. Be concrete about what changed and why."
            ),
            Message.user(f"Task:\n{instruction}\n\nDiff:\n{patch[:12000]}"),
        ]
        response = model.generate(messages, tools=None)
        return response.text.strip() or "No summary produced."
