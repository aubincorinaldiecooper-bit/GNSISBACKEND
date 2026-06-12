"""Tasks and evaluation — how the loop scores an agent's work.

A :class:`Task` bundles the input prompt with a grader. The grader returns a
scalar score in [0, 1] *and* free-text feedback; the feedback is what the
optimizer uses to propose a better prompt, so it should describe the gap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from ..agent.tool_calling import AgentResult
from ..tools.builtin import safe_arithmetic


@dataclass
class EvalResult:
    score: float
    feedback: str
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Task:
    name: str
    prompt: str
    evaluate: Callable[[AgentResult], EvalResult]


def _extract_number(text: str) -> Optional[float]:
    match = re.findall(r"-?\d+(?:\.\d+)?", text or "")
    if not match:
        return None
    return float(match[-1])  # the answer usually comes last


def calculator_task(expression: str, name: str = "calculator") -> Task:
    """Build a task that asks for an arithmetic result and rewards tool use.

    Scoring:
      * correct answer computed via the tool  -> 1.0
      * correct answer but no tool was used   -> 0.6 (right, but not reliably)
      * wrong / missing answer                -> 0.0
    """
    expected = safe_arithmetic(expression)
    if isinstance(expected, float) and expected.is_integer():
        expected = int(expected)
    prompt = f"What is {expression}? Reply with the final numeric answer."

    def _evaluate(result: AgentResult) -> EvalResult:
        answer = _extract_number(result.output)
        correct = answer is not None and abs(answer - float(expected)) < 1e-9
        detail = {"expected": expected, "answer": answer, "used_tool": result.used_tool}
        if correct and result.used_tool:
            return EvalResult(1.0, "Correct, and computed with the calculator tool.", detail)
        if correct:
            return EvalResult(
                0.6,
                "Answer is correct but was not computed with the calculator tool; "
                "guessing arithmetic is unreliable. Instruct the agent to use tools.",
                detail,
            )
        return EvalResult(
            0.0,
            "Answer is wrong because the agent did not use the available calculator "
            "tool. The system prompt should require using tools for computation.",
            detail,
        )

    return Task(name=name, prompt=prompt, evaluate=_evaluate)
