"""GNSIS — a stable, dogfoodable self-evolution runtime for tool-calling agents.

GNSIS is a clean-room MVP inspired by Autogenesis (https://github.com/DVampire/Autogenesis).
It keeps the two protocol ideas — RSPL (versioned resources) and SEPL (the
self-evolution loop) — but trades breadth for a core that actually runs.
"""

from .__about__ import __summary__, __title__, __version__
from .agent.tool_calling import AgentResult, ToolCallingAgent
from .config import Config
from .evolution.loop import EvolutionReport, Iteration, SelfEvolutionLoop
from .evolution.optimizer import PromptOptimizer
from .evolution.task import EvalResult, Task, calculator_task
from .memory.memory import Memory
from .models.base import BaseModel, Message, ModelResponse, ToolCall
from .models.registry import create_model, resolve_provider
from .resources.resource import Resource, ResourceVersion
from .resources.store import ResourceStore
from .runtime import Runtime
from .tools.registry import ToolRegistry, default_registry
from .tracer.tracer import Tracer

__all__ = [
    "__version__",
    "__title__",
    "__summary__",
    "Runtime",
    "Config",
    "BaseModel",
    "Message",
    "ModelResponse",
    "ToolCall",
    "create_model",
    "resolve_provider",
    "ToolRegistry",
    "default_registry",
    "Resource",
    "ResourceVersion",
    "ResourceStore",
    "Memory",
    "Tracer",
    "ToolCallingAgent",
    "AgentResult",
    "SelfEvolutionLoop",
    "EvolutionReport",
    "Iteration",
    "PromptOptimizer",
    "Task",
    "EvalResult",
    "calculator_task",
]
