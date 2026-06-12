"""Run the tool-calling agent once — the stable Autogenesis example, GNSIS-style.

    python examples/run_tool_calling_agent.py --config configs/tool_calling_agent.py

Works offline with the deterministic mock model; set OPENROUTER_API_KEY to run
against a real model via OpenRouter.
"""

from __future__ import annotations

import argparse
import os
import sys

# Allow running from a checkout without installing.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from gnsis import Config, Runtime, Tracer  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/tool_calling_agent.py")
    parser.add_argument("--task", default=None)
    args = parser.parse_args()

    config = Config.fromfile(args.config)
    runtime = Runtime.from_config(config)
    tracer = Tracer(workdir=runtime.workdir)
    agent = runtime.build_agent(config.get("system_prompt"), tracer=tracer)

    task = args.task or config.get("task")
    print(f"provider={runtime.model.provider} model={runtime.model.model}")
    print(f"task: {task}\n")

    result = agent.run(task)
    for event in tracer.events:
        if event.kind == "tool_call":
            d = event.data
            print(f"  tool {d['name']}({d['arguments']}) -> {d['result']}")
    print(f"\nanswer: {result.output}")
    print(f"used_tool={result.used_tool} steps={result.steps}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
