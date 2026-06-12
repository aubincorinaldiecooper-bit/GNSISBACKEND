"""SEPL: the self-evolution loop, optimizer, and task/evaluation types."""

from .loop import (
    DEFAULT_SEED_PROMPT,
    PROMPT_KIND,
    EvolutionReport,
    Iteration,
    SelfEvolutionLoop,
)
from .optimizer import PromptOptimizer
from .task import EvalResult, Task, calculator_task

__all__ = [
    "SelfEvolutionLoop",
    "EvolutionReport",
    "Iteration",
    "PromptOptimizer",
    "Task",
    "EvalResult",
    "calculator_task",
    "DEFAULT_SEED_PROMPT",
    "PROMPT_KIND",
]
