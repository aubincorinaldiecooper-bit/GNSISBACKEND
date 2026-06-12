"""The optimizer — proposes improved prompts (the 'propose' of propose→assess→commit).

Two strategies, same interface:

* **rule-based** (default, deterministic): append targeted guidance drawn from
  a small library of improvement clauses. This makes the offline loop converge
  predictably and keeps tests hermetic.
* **model-driven**: when a real model is supplied, ask it to rewrite the prompt
  given the failure feedback. Falls back to rule-based on any error.

The loop *assesses* every candidate by actually running it, so the optimizer
only has to generate plausible directions, not guarantee improvement.
"""

from __future__ import annotations

import json
import re
from typing import List, Optional

from ..models.base import BaseModel, Message

# Ordered from gentlest to strongest — the loop hill-climbs through them.
_IMPROVEMENT_CLAUSES = [
    "Always use the available tools to compute answers instead of guessing.",
    "You must call the calculator tool for any arithmetic before answering.",
    "Work step by step and rely on tools for exact computation; never estimate numbers.",
]


class PromptOptimizer:
    def __init__(self, model: Optional[BaseModel] = None) -> None:
        self.model = model

    def propose(self, current_prompt: str, feedback: str, n: int = 3) -> List[str]:
        if self.model is not None and getattr(self.model, "provider", "mock") != "mock":
            try:
                candidates = self._propose_with_model(current_prompt, feedback, n)
                if candidates:
                    return candidates
            except Exception:  # noqa: BLE001 - any failure falls back to rule-based
                pass
        return self._propose_rule_based(current_prompt, n)

    def _propose_rule_based(self, current_prompt: str, n: int) -> List[str]:
        candidates: List[str] = []
        for clause in _IMPROVEMENT_CLAUSES:
            if clause.lower() not in current_prompt.lower():
                candidates.append(f"{current_prompt.rstrip()}\n{clause}")
            if len(candidates) >= n:
                break
        return candidates

    def _propose_with_model(self, current_prompt: str, feedback: str, n: int) -> List[str]:
        instruction = (
            "You improve system prompts for a tool-calling agent. The current "
            "prompt underperformed.\n\n"
            f"CURRENT PROMPT:\n{current_prompt}\n\n"
            f"EVALUATION FEEDBACK:\n{feedback}\n\n"
            f"Propose {n} improved system prompts that address the feedback. "
            "Respond ONLY with a JSON array of strings."
        )
        response = self.model.generate(
            [Message.system("You are a prompt optimizer."), Message.user(instruction)],
            tools=None,
        )
        return self._parse_candidates(response.text, n)

    @staticmethod
    def _parse_candidates(text: str, n: int) -> List[str]:
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if not match:
            return []
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
        return [str(item) for item in data if isinstance(item, str)][:n]
