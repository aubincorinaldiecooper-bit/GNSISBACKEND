"""Engine #2 — OpenHands (open-source, Python, autonomous).

OpenHands is a strong open alternative to the Anthropic SDK: Python-native (so it
lives inside GNSIS), model-agnostic, and purpose-built for unattended, sandboxed
runs that complete a whole task and return a result — which matches the GNSIS
worker exactly. Because it's open, the GNSIS learning layer (repo-scoped memory +
evolved prompts/skills) can fully steer it, which the closed SDK can't allow.

This adapter drives OpenHands **headless** against the checked-out workspace and
derives the patch from ``git diff`` — the same contract every engine honors. The
exact OpenHands CLI changes across versions, so the invocation is configurable:

* ``GNSIS_OPENHANDS_CMD`` — a JSON list template; ``{task}`` and ``{workspace}``
  are substituted. Default: ``["python","-m","openhands.core.main","-t","{task}"]``
* ``GNSIS_OPENHANDS_MODEL`` — passed to OpenHands via ``LLM_MODEL``.

What's stable is the *contract* (run in the workspace, the diff is the result),
not the CLI string — tune the command for your OpenHands version without touching
the pipeline. Performs no GitHub writes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import List, Optional

from ..orchestration.engine import PhaseSink, Workspace
from ..orchestration.models import EngineResult
from ..orchestration.status import Phase
from .common import run_tests

DEFAULT_CMD = ["python", "-m", "openhands.core.main", "-t", "{task}"]
DEFAULT_MODEL = "anthropic/claude-opus-4-8"


class OpenHandsEngine:
    name = "openhands"

    def __init__(
        self,
        model: Optional[str] = None,
        command: Optional[List[str]] = None,
        max_run_seconds: int = 1800,
        max_test_seconds: int = 600,
    ) -> None:
        self.model = model or os.environ.get("GNSIS_OPENHANDS_MODEL", DEFAULT_MODEL)
        self.command = command or _command_from_env()
        self.max_run_seconds = max_run_seconds
        self.max_test_seconds = max_test_seconds

    def generate(
        self,
        instruction: str,
        workspace: Optional[Workspace],
        sink: PhaseSink,
    ) -> EngineResult:
        if workspace is None:
            raise ValueError("OpenHandsEngine requires a checked-out workspace")
        if shutil.which(self.command[0]) is None and self.command[0] not in ("python", "python3"):
            raise RuntimeError(f"OpenHands command not found: {self.command[0]!r}")

        # plan: OpenHands plans internally; record the delegated task.
        sink.begin_phase(Phase.PLAN)
        plan = f"Delegating to OpenHands ({self.model}): {instruction}"
        sink.checkpoint(Phase.PLAN, plan)

        # patch: run OpenHands headless in the workspace, then diff.
        sink.begin_phase(Phase.PATCH)
        transcript = self._run_openhands(instruction, workspace, sink)
        patch = workspace.diff()
        files_changed = workspace.changed_files()
        sink.checkpoint(Phase.PATCH, {"patch": patch, "files_changed": files_changed})
        if not patch.strip():
            sink.log("OpenHands produced no changes", level="warning")
            return EngineResult(
                plan=plan, patch="", success=False, detail={"engine": self.name}
            )

        # tests
        sink.begin_phase(Phase.TESTS)
        tests = run_tests(workspace, sink, self.max_test_seconds)
        sink.checkpoint(Phase.TESTS, tests)

        # summary: synthesized (no extra model call required).
        sink.begin_phase(Phase.SUMMARY)
        summary = _summarize(instruction, files_changed, transcript)
        sink.checkpoint(Phase.SUMMARY, summary)

        return EngineResult(
            plan=plan,
            patch=patch,
            tests=tests,
            summary=summary,
            files_changed=files_changed,
            success=True,
            detail={"engine": self.name, "model": self.model},
        )

    def _run_openhands(
        self, instruction: str, workspace: Workspace, sink: PhaseSink
    ) -> str:
        cmd = [
            part.replace("{task}", instruction).replace("{workspace}", workspace.path)
            for part in self.command
        ]
        env = dict(os.environ)
        env["LLM_MODEL"] = self.model
        sink.log(f"running OpenHands: {cmd[0]} …")
        try:
            proc = subprocess.run(
                cmd,
                cwd=workspace.path,
                env=env,
                capture_output=True,
                text=True,
                timeout=self.max_run_seconds,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"OpenHands timed out after {self.max_run_seconds}s"
            )
        if proc.returncode != 0:
            sink.log(
                f"OpenHands exited {proc.returncode}", level="warning",
            )
        return (proc.stdout + "\n" + proc.stderr).strip()[-8000:]


def _command_from_env() -> List[str]:
    raw = os.environ.get("GNSIS_OPENHANDS_CMD")
    if not raw:
        return list(DEFAULT_CMD)
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list) and parsed:
            return [str(p) for p in parsed]
    except json.JSONDecodeError:
        pass
    raise ValueError("GNSIS_OPENHANDS_CMD must be a non-empty JSON list")


def _summarize(instruction: str, files: List[str], transcript: str) -> str:
    head = f"Implemented via OpenHands: {instruction.strip().splitlines()[0]}"
    if files:
        head += f"\n\nFiles changed: {', '.join(files[:20])}"
    tail = transcript.strip().splitlines()[-5:] if transcript.strip() else []
    if tail:
        head += "\n\nAgent notes:\n" + "\n".join(tail)
    return head
