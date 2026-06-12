"""Run the self-evolution loop and print the report.

    python examples/run_self_evolution.py --config configs/self_evolution.py

Watch a weak seed prompt evolve, version by version, into one that reliably
uses the calculator tool. Offline by default; set OPENROUTER_API_KEY to evolve
against a real model.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from gnsis import Config, PromptOptimizer, Runtime, SelfEvolutionLoop, calculator_task  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/self_evolution.py")
    parser.add_argument("--fresh", action="store_true", help="discard prior prompt history")
    args = parser.parse_args()

    config = Config.fromfile(args.config)
    runtime = Runtime.from_config(config)
    prompt_name = config.get("prompt_name", "agent_system_prompt")
    if args.fresh:
        runtime.store.delete("prompt", prompt_name)

    task = calculator_task(config.get("task_expression", "(12 + 30) * 2"))
    loop = SelfEvolutionLoop(
        runtime,
        optimizer=PromptOptimizer(model=runtime.model),
        prompt_name=prompt_name,
    )

    print(f"provider={runtime.model.provider} model={runtime.model.model}\n")
    report = loop.run(
        task,
        iterations=config.get("iterations", 5),
        seed_prompt=config.get("seed_prompt"),
    )

    for it in report.iterations:
        flag = "accept" if it.accepted else "  ----"
        print(f"  iter {it.index} v{it.version} score={it.score:.2f} [{flag}] {it.feedback[:50]}")
    print(f"\n{report.start_score:.2f} -> {report.best_score:.2f}  (best v{report.best_version})")
    print(f"evolved prompt:\n  {report.best_prompt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
