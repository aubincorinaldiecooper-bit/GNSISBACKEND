"""The PatchEngine seam — where "how the code actually gets written" plugs in.

The whole point of this interface is that the *orchestration* (queueing,
checkpointing, the approval gate, opening the PR) is independent of *which agent*
produces the change. Engine #1 is the Anthropic Claude Agent SDK
(:mod:`gnsis.engines.claude_agent`); an OpenRouter Agent SDK or a native GNSIS
engine can drop in later without touching the pipeline, the API, or the worker.

An engine receives a :class:`Workspace` (a checked-out copy of the repo) and a
:class:`PhaseSink` (to checkpoint/log as it goes), and returns an
:class:`~gnsis.orchestration.models.EngineResult`. It must **not** perform any
GitHub writes — pushing the branch and opening the PR is the worker's job, after
human approval.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Any, List, Optional, Protocol, runtime_checkable

from .models import EngineResult
from .status import Phase


@dataclass
class Workspace:
    """A checked-out working copy of the target repo on local disk."""

    path: str
    repo: str
    base_branch: str

    def git(self, *args: str, check: bool = True) -> str:
        """Run a git command inside the workspace and return stdout."""
        proc = subprocess.run(
            ["git", *args],
            cwd=self.path,
            capture_output=True,
            text=True,
        )
        if check and proc.returncode != 0:
            raise RuntimeError(
                f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}"
            )
        return proc.stdout

    def diff(self) -> str:
        """Unified diff of all working-tree changes against HEAD."""
        # -N includes intent-to-add (newly created) files in the diff.
        self.git("add", "-AN", check=False)
        return self.git("diff", "HEAD")

    def changed_files(self) -> List[str]:
        out = self.git("status", "--porcelain", check=False)
        files: List[str] = []
        for line in out.splitlines():
            name = line[3:].strip()
            if name:
                files.append(name)
        return files


class PhaseSink(Protocol):
    """How an engine reports progress so the worker can persist it durably."""

    def begin_phase(self, phase: str) -> None: ...

    def checkpoint(self, phase: str, content: Any) -> None: ...

    def log(self, message: str, level: str = "info", **data: Any) -> None: ...


@runtime_checkable
class PatchEngine(Protocol):
    name: str

    def generate(
        self,
        instruction: str,
        workspace: Optional[Workspace],
        sink: PhaseSink,
    ) -> EngineResult:
        """Produce plan → patch → tests → summary for ``instruction``."""
        ...


class MockEngine:
    """A deterministic, offline engine for tests and the zero-config demo.

    It does not call a model. If given a real workspace it writes a tiny marker
    file so the produced diff is a genuine (if trivial) patch; otherwise it
    fabricates a plausible unified diff. Either way it exercises the full
    phase/checkpoint/approval machinery without a network or an API key.
    """

    name = "mock"

    def __init__(self, marker_file: str = "GNSIS_CHANGE.md") -> None:
        self.marker_file = marker_file

    def generate(
        self,
        instruction: str,
        workspace: Optional[Workspace],
        sink: PhaseSink,
    ) -> EngineResult:
        sink.begin_phase(Phase.PLAN)
        plan = (
            f"Plan: address the request {instruction!r} by adding a marker file "
            f"and a note describing the intended change."
        )
        sink.checkpoint(Phase.PLAN, plan)

        sink.begin_phase(Phase.PATCH)
        files_changed: List[str] = [self.marker_file]
        if workspace is not None:
            target = os.path.join(workspace.path, self.marker_file)
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(f"# GNSIS change\n\n{instruction}\n")
            patch = workspace.diff()
            files_changed = workspace.changed_files() or files_changed
        else:
            patch = _fake_diff(self.marker_file, instruction)
        sink.checkpoint(Phase.PATCH, {"patch": patch, "files_changed": files_changed})

        sink.begin_phase(Phase.TESTS)
        tests = "No automated tests were run by the mock engine."
        sink.checkpoint(Phase.TESTS, tests)

        sink.begin_phase(Phase.SUMMARY)
        summary = f"Added {self.marker_file} to address: {instruction}"
        sink.checkpoint(Phase.SUMMARY, summary)

        return EngineResult(
            plan=plan,
            patch=patch,
            tests=tests,
            summary=summary,
            files_changed=files_changed,
            success=True,
            detail={"engine": self.name},
        )


def _fake_diff(path: str, instruction: str) -> str:
    body = f"# GNSIS change\n\n{instruction}\n"
    lines = body.splitlines()
    added = "\n".join(f"+{line}" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        f"new file mode 100644\n"
        f"--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
        f"{added}\n"
    )
