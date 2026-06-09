"""SEPL — the Self-Evolution Protocol Layer.

The loop realizes Autogenesis's cycle over a *versioned prompt resource*:

    Act      run the current prompt on the task
    Observe  score the result, capture feedback
    Optimize propose candidate prompts, assess them, commit the best
    Remember persist what won, with lineage, into memory

Each accepted improvement is a new resource version whose parent is the prompt
it improved on — so the whole history is auditable and any step is reversible
via :meth:`ResourceStore.rollback`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from ..agent.tool_calling import AgentResult
from ..tracer.tracer import Tracer
from .optimizer import PromptOptimizer
from .task import EvalResult, Task

DEFAULT_SEED_PROMPT = "You are a helpful assistant. Answer the user's question."

PROMPT_KIND = "prompt"


@dataclass
class Iteration:
    index: int
    version: int
    score: float
    used_tool: bool
    accepted: bool
    prompt: str
    feedback: str


@dataclass
class EvolutionReport:
    task_name: str
    prompt_name: str
    iterations: List[Iteration] = field(default_factory=list)
    best_version: int = 0
    best_score: float = 0.0
    best_prompt: str = ""

    @property
    def start_score(self) -> float:
        return self.iterations[0].score if self.iterations else 0.0

    @property
    def improved(self) -> bool:
        return self.best_score > self.start_score


class SelfEvolutionLoop:
    def __init__(
        self,
        runtime: "object",
        optimizer: Optional[PromptOptimizer] = None,
        prompt_name: str = "agent_system_prompt",
        tracer: Optional[Tracer] = None,
    ) -> None:
        self.runtime = runtime
        self.store = runtime.store
        self.memory = runtime.memory
        self.optimizer = optimizer or PromptOptimizer(model=runtime.model)
        self.prompt_name = prompt_name
        self.tracer = tracer

    def _evaluate(self, prompt: str, task: Task) -> Tuple[AgentResult, EvalResult]:
        agent = self.runtime.build_agent(prompt, tracer=self.tracer)
        result = agent.run(task.prompt)
        return result, task.evaluate(result)

    def run(
        self,
        task: Task,
        iterations: int = 5,
        accept_threshold: float = 1.0,
        seed_prompt: Optional[str] = None,
    ) -> EvolutionReport:
        # --- establish the starting prompt (a versioned resource) ---------
        head = self.store.head(PROMPT_KIND, self.prompt_name)
        if head is None:
            content = seed_prompt or DEFAULT_SEED_PROMPT
            head = self.store.commit(PROMPT_KIND, self.prompt_name, content, message="seed")
        current_prompt = head.content
        best_version = head.version

        # --- Act + Observe on the current head ----------------------------
        _, best_eval = self._evaluate(current_prompt, task)
        best_score = best_eval.score
        report = EvolutionReport(task.name, self.prompt_name)
        report.iterations.append(
            Iteration(0, best_version, best_score, best_eval.detail.get("used_tool", False),
                      True, current_prompt, best_eval.feedback)
        )
        self._remember(task, 0, best_version, best_eval, accepted=True)

        for i in range(1, iterations + 1):
            if best_score >= accept_threshold:
                break

            # --- Optimize: propose candidates, assess, keep first improver ---
            candidates = self.optimizer.propose(current_prompt, best_eval.feedback, n=3)
            improvement: Optional[Tuple[str, EvalResult]] = None
            for candidate in candidates:
                _, candidate_eval = self._evaluate(candidate, task)
                if candidate_eval.score > best_score:
                    improvement = (candidate, candidate_eval)
                    break

            if improvement is None:
                report.iterations.append(
                    Iteration(i, best_version, best_score, best_eval.detail.get("used_tool", False),
                              False, current_prompt, "no improving candidate found")
                )
                break

            # --- Optimize: commit the winner as a new, lineaged version ------
            candidate, candidate_eval = improvement
            version = self.store.commit(
                PROMPT_KIND,
                self.prompt_name,
                candidate,
                message=f"evolve v{best_version}->: score {best_score:.2f}->{candidate_eval.score:.2f}",
                parent_version=best_version,
            )
            current_prompt = candidate
            best_eval = candidate_eval
            best_score = candidate_eval.score
            best_version = version.version
            report.iterations.append(
                Iteration(i, version.version, best_score,
                          candidate_eval.detail.get("used_tool", False),
                          True, candidate, candidate_eval.feedback)
            )
            self._remember(task, i, version.version, candidate_eval, accepted=True)

        report.best_version = best_version
        report.best_score = best_score
        report.best_prompt = current_prompt
        return report

    def _remember(self, task: Task, index: int, version: int, ev: EvalResult, accepted: bool) -> None:
        self.memory.remember(
            {
                "kind": "evolution_step",
                "task": task.name,
                "prompt_name": self.prompt_name,
                "iteration": index,
                "version": version,
                "score": ev.score,
                "accepted": accepted,
                "feedback": ev.feedback,
            }
        )
