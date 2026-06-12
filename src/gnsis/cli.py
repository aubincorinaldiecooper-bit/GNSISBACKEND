"""The ``gnsis`` command-line interface — the primary dogfooding surface.

Subcommands
-----------
  run       Run the tool-calling agent once on a task.
  evolve    Run the self-evolution loop and watch a prompt improve.
  demo      A zero-config, offline end-to-end demo (uses the mock model).
  history   Show the version lineage of an evolved prompt.
  rollback  Roll a prompt resource back to an earlier version.
  version   Print the GNSIS version.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from typing import List, Optional

from .__about__ import __version__
from .config import Config
from .evolution.loop import PROMPT_KIND, SelfEvolutionLoop
from .evolution.optimizer import PromptOptimizer
from .evolution.task import calculator_task
from .models.registry import resolve_provider
from .runtime import Runtime
from .tracer.tracer import Tracer

DEFAULT_AGENT_PROMPT = (
    "You are a precise assistant. Use the available tools — especially the "
    "calculator — to compute exact answers, then reply with the final answer."
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader so OPENROUTER_API_KEY just works (no dependency)."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def _make_runtime(args: argparse.Namespace) -> Runtime:
    model_cfg = {}
    if getattr(args, "model", None):
        model_cfg["model"] = args.model
    config = Config(
        {
            "provider": getattr(args, "provider", "auto"),
            "workdir": args.workdir,
            "model": model_cfg,
            "agent": {"max_steps": getattr(args, "max_steps", 6)},
        }
    )
    if getattr(args, "config", None):
        config = Config.fromfile(args.config).merge(config.to_dict())
    return Runtime.from_config(config)


def _banner(runtime: Runtime) -> str:
    return f"provider={runtime.model.provider} model={runtime.model.model} workdir={runtime.workdir}"


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_run(args: argparse.Namespace) -> int:
    runtime = _make_runtime(args)
    tracer = Tracer(workdir=runtime.workdir)
    agent = runtime.build_agent(args.system or DEFAULT_AGENT_PROMPT, tracer=tracer)
    print(f"[gnsis] {_banner(runtime)}")
    print(f"[gnsis] task: {args.task}")
    result = agent.run(args.task)
    for event in tracer.events:
        if event.kind == "tool_call":
            d = event.data
            print(f"  ↳ tool {d['name']}({d['arguments']}) = {d['result']}")
    trace_path = tracer.save()
    print(f"\n[answer] {result.output}")
    print(
        f"[stats] steps={result.steps} tool_calls={result.tool_calls} "
        f"used_tool={result.used_tool}"
    )
    if trace_path:
        print(f"[trace] {trace_path}")
    return 0


def cmd_evolve(args: argparse.Namespace) -> int:
    runtime = _make_runtime(args)
    if args.fresh:
        runtime.store.delete(PROMPT_KIND, args.prompt_name)
    task = calculator_task(args.task)
    loop = SelfEvolutionLoop(
        runtime,
        optimizer=PromptOptimizer(model=runtime.model),
        prompt_name=args.prompt_name,
    )
    print(f"[gnsis] {_banner(runtime)}")
    print(f"[gnsis] evolving prompt '{args.prompt_name}' on task '{task.name}': {task.prompt}\n")
    report = loop.run(task, iterations=args.iterations, seed_prompt=args.seed)

    print(f"{'iter':>4}  {'ver':>3}  {'score':>5}  {'tool':>4}  {'ok':>2}  note")
    print("  " + "-" * 60)
    for it in report.iterations:
        print(
            f"{it.index:>4}  {it.version:>3}  {it.score:>5.2f}  "
            f"{('yes' if it.used_tool else 'no'):>4}  {('✓' if it.accepted else '·'):>2}  "
            f"{it.feedback[:48]}"
        )
    arrow = "↑ improved" if report.improved else "no change"
    print(
        f"\n[result] {report.start_score:.2f} → {report.best_score:.2f} "
        f"({arrow}); best is v{report.best_version}"
    )
    print(f"[prompt] {report.best_prompt!r}")
    print(
        f"[stored] resource '{PROMPT_KIND}:{args.prompt_name}' in "
        f"{runtime.store._path(PROMPT_KIND, args.prompt_name)}"
    )
    print(f"[hint] inspect lineage: gnsis history --name {args.prompt_name} --workdir {runtime.workdir}")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    workdir = args.workdir or tempfile.mkdtemp(prefix="gnsis-demo-")
    print("=" * 64)
    print(" GNSIS demo — watch a tool-calling agent evolve its own prompt")
    print(" (offline: deterministic mock model, no API key needed)")
    print("=" * 64)
    ns = argparse.Namespace(
        provider="mock",
        model=None,
        workdir=workdir,
        config=None,
        max_steps=6,
        task="(12 + 30) * 2",
        iterations=5,
        prompt_name="agent_system_prompt",
        seed=None,
        fresh=True,
    )
    rc = cmd_evolve(ns)
    print(
        "\nWhat happened: the seed prompt never used the calculator, so it "
        "guessed and scored 0.00.\nThe optimizer proposed stronger tool-use "
        "instructions; the loop committed the first\nversion that actually "
        "called the tool and reached the correct answer (1.00).\n"
    )
    print(f"Artifacts (resources, memory, traces) are under: {workdir}")
    print("Run the real thing against Claude via OpenRouter:")
    print("  export OPENROUTER_API_KEY=sk-or-...")
    print("  gnsis evolve --provider openrouter --fresh")
    return rc


def cmd_history(args: argparse.Namespace) -> int:
    runtime = _make_runtime(args)
    versions = runtime.store.history(PROMPT_KIND, args.prompt_name)
    if not versions:
        print(f"[gnsis] no history for '{PROMPT_KIND}:{args.prompt_name}' in {runtime.workdir}")
        return 0
    print(f"[gnsis] lineage of '{PROMPT_KIND}:{args.prompt_name}':")
    for v in versions:
        parent = "—" if v.parent_version is None else f"v{v.parent_version}"
        print(f"  v{v.version:<3} (parent {parent}, {v.short_hash})  {v.message}")
        print(f"        {v.content!r}")
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    runtime = _make_runtime(args)
    new_version = runtime.store.rollback(PROMPT_KIND, args.prompt_name, args.to)
    print(
        f"[gnsis] rolled '{PROMPT_KIND}:{args.prompt_name}' back to v{args.to}; "
        f"new head is v{new_version.version}"
    )
    print(f"[prompt] {new_version.content!r}")
    return 0


def cmd_version(_: argparse.Namespace) -> int:
    print(f"gnsis {__version__}")
    return 0


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gnsis", description=__doc__)
    parser.add_argument("--version", action="version", version=f"gnsis {__version__}")
    sub = parser.add_subparsers(dest="command")

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--provider", default="auto", help="auto | mock | openrouter")
        p.add_argument("--model", default=None, help="model slug, e.g. anthropic/claude-opus-4.8")
        p.add_argument("--workdir", default="workdir", help="where resources/memory/traces live")
        p.add_argument("--config", default=None, help="optional .py/.json config file")

    p_run = sub.add_parser("run", help="run the tool-calling agent once")
    add_common(p_run)
    p_run.add_argument("--task", required=True, help="the task/question for the agent")
    p_run.add_argument("--system", default=None, help="system prompt (defaults to a tool-using prompt)")
    p_run.add_argument("--max-steps", type=int, default=6, dest="max_steps")
    p_run.set_defaults(func=cmd_run)

    p_evolve = sub.add_parser("evolve", help="run the self-evolution loop")
    add_common(p_evolve)
    p_evolve.add_argument("--task", default="(12 + 30) * 2", help="arithmetic expression to solve")
    p_evolve.add_argument("--iterations", type=int, default=5)
    p_evolve.add_argument("--prompt-name", default="agent_system_prompt", dest="prompt_name")
    p_evolve.add_argument("--seed", default=None, help="seed system prompt (defaults to a weak one)")
    p_evolve.add_argument("--fresh", action="store_true", help="discard any existing prompt history first")
    p_evolve.add_argument("--max-steps", type=int, default=6, dest="max_steps")
    p_evolve.set_defaults(func=cmd_evolve)

    p_demo = sub.add_parser("demo", help="offline end-to-end demo (no API key)")
    p_demo.add_argument("--workdir", default=None, help="defaults to a temp directory")
    p_demo.set_defaults(func=cmd_demo)

    p_hist = sub.add_parser("history", help="show a prompt's version lineage")
    add_common(p_hist)
    p_hist.add_argument("--prompt-name", "--name", default="agent_system_prompt", dest="prompt_name")
    p_hist.set_defaults(func=cmd_history)

    p_rb = sub.add_parser("rollback", help="roll a prompt back to an earlier version")
    add_common(p_rb)
    p_rb.add_argument("--prompt-name", "--name", default="agent_system_prompt", dest="prompt_name")
    p_rb.add_argument("--to", type=int, required=True, help="version number to roll back to")
    p_rb.set_defaults(func=cmd_rollback)

    p_ver = sub.add_parser("version", help="print version")
    p_ver.set_defaults(func=cmd_version)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    _load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    # Surface auto-resolution early so users know which backend ran.
    if getattr(args, "provider", None) == "auto" and args.command in ("run", "evolve"):
        args.provider = resolve_provider("auto")
    try:
        return args.func(args)
    except KeyboardInterrupt:  # pragma: no cover
        print("\n[gnsis] interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - friendly top-level error
        print(f"[gnsis] error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
