"""``gnsis-smoke`` — a one-command live test of the engine loop.

This validates the one thing unit tests can't: that a real model + engine
actually produces a real patch on a real repo. It clones the repo, runs the
chosen engine through plan → patch → tests → summary against it, and prints the
result. It needs **no Postgres, Redis, FastAPI, or Celery**, performs **no GitHub
writes**, and does not publish — it stops at the change, like the approval gate.

Default engine is OpenHands via OpenRouter (no Anthropic key required)::

    export OPENROUTER_API_KEY=sk-or-...
    export GNSIS_OPENHANDS_MODEL=openrouter/<provider>/<model>
    gnsis-smoke --repo owner/name --instruction "Add a /health endpoint"

Other handy forms::

    gnsis-smoke --repo /path/to/local/repo -i "..."     # clone a local repo
    gnsis-smoke --repo owner/name -i "..." --engine mock # offline plumbing check
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any, List, Optional

from .engines import get_engine
from .orchestration.engine import PhaseSink, Workspace


class _PrintSink(PhaseSink):
    """Streams phase progress to the console as the engine works."""

    def begin_phase(self, phase: str) -> None:
        print(f"\n=== phase: {phase} ===", flush=True)

    def checkpoint(self, phase: str, content: Any) -> None:
        print(f"[checkpoint:{phase}] saved", flush=True)

    def log(self, message: str, level: str = "info", **data: Any) -> None:
        print(f"[{level}] {message}", flush=True)


def _clone(source: str, base_branch: str, token: Optional[str], dest: str) -> None:
    """Clone ``source`` (owner/name, URL, or local path) into ``dest``."""
    if "://" in source or source.startswith("git@"):
        url = source
    elif os.path.isdir(source):
        url = os.path.abspath(source)  # local repo
    else:
        creds = f"x-access-token:{token}@" if token else ""
        url = f"https://{creds}github.com/{source}.git"
    subprocess.run(
        ["git", "clone", "--branch", base_branch, url, dest],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(["git", "config", "user.email", "gnsis@local"], cwd=dest, check=True)
    subprocess.run(["git", "config", "user.name", "GNSIS"], cwd=dest, check=True)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="gnsis-smoke", description=__doc__)
    parser.add_argument("--repo", "-r", required=True, help="owner/name, git URL, or local path")
    parser.add_argument("--instruction", "-i", required=True, help="what to change")
    parser.add_argument("--engine", "-e", default="openhands", help="claude | openhands | mock")
    parser.add_argument("--base-branch", "-b", default="main")
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"), help="token for private clone")
    parser.add_argument("--workspace", default=None, help="dir to clone into (default: temp)")
    parser.add_argument("--keep", action="store_true", help="keep the workspace afterwards")
    parser.add_argument("--max-diff", type=int, default=20000, help="chars of diff to print")
    args = parser.parse_args(argv)

    workdir = args.workspace or tempfile.mkdtemp(prefix="gnsis-smoke-")
    print(f"GNSIS smoke test")
    print(f"  repo:        {args.repo}")
    print(f"  engine:      {args.engine}")
    print(f"  workspace:   {workdir}")
    print(f"  instruction: {args.instruction}")

    try:
        print("\nCloning…", flush=True)
        _clone(args.repo, args.base_branch, args.token, workdir)
        workspace = Workspace(path=workdir, repo=args.repo, base_branch=args.base_branch)

        engine = get_engine(args.engine)
        print(f"Running engine '{engine.name}' (no GitHub writes, no publish)…", flush=True)
        result = engine.generate(args.instruction, workspace, _PrintSink())
    except Exception as exc:  # noqa: BLE001
        print(f"\n✗ smoke failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)

    print("\n" + "=" * 60)
    print(f"success:       {result.success}")
    print(f"files_changed: {result.files_changed}")
    print(f"\n--- PLAN ---\n{result.plan}")
    print(f"\n--- TESTS ---\n{result.tests}")
    print(f"\n--- SUMMARY ---\n{result.summary}")
    patch = result.patch or "(empty)"
    if len(patch) > args.max_diff:
        patch = patch[: args.max_diff] + f"\n… (truncated, {len(result.patch)} chars total)"
    print(f"\n--- DIFF ---\n{patch}")

    if not result.success or not (result.patch or "").strip():
        print("\n✗ engine produced no usable patch", file=sys.stderr)
        return 1
    print("\n✓ smoke passed — engine produced a patch")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
