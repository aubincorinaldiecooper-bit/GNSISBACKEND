"""Runtime — the small composition root that wires the pieces together.

Autogenesis spreads this across a stack of async "managers" (version, model,
prompt, memory, tool, agent). For a stable MVP we consolidate that into one
synchronous object that owns the model, tools, resource store, and memory, and
knows how to build an agent from a system prompt.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .agent.tool_calling import ToolCallingAgent
from .config import Config
from .memory.memory import Memory
from .models.base import BaseModel
from .models.registry import create_model
from .resources.store import ResourceStore
from .tools.registry import ToolRegistry, default_registry
from .tracer.tracer import Tracer


class Runtime:
    def __init__(
        self,
        config: Optional[Config] = None,
        model: Optional[BaseModel] = None,
        tools: Optional[ToolRegistry] = None,
        store: Optional[ResourceStore] = None,
        memory: Optional[Memory] = None,
        workdir: str = "workdir",
    ) -> None:
        cfg = config or Config({})
        self.config = cfg
        self.workdir = cfg.get("workdir", workdir)

        model_cfg: Dict[str, Any] = dict(cfg.get("model", {}))
        self.model = model or create_model(cfg.get("provider", "auto"), **model_cfg)
        self.tools = tools or default_registry()
        self.store = store or ResourceStore(self.workdir)
        self.memory = memory or Memory(self.workdir, cfg.get("memory_namespace", "default"))
        self.agent_cfg: Dict[str, Any] = dict(cfg.get("agent", {}))

    @classmethod
    def from_config(cls, config: Config) -> "Runtime":
        return cls(config=config)

    def build_agent(self, system_prompt: str, tracer: Optional[Tracer] = None) -> ToolCallingAgent:
        return ToolCallingAgent(
            model=self.model,
            tools=self.tools,
            system_prompt=system_prompt,
            max_steps=self.agent_cfg.get("max_steps", 6),
            tracer=tracer,
            name=self.agent_cfg.get("name", "tool_calling_agent"),
        )
