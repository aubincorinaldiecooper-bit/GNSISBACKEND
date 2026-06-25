"""Engine #1 — the Anthropic Claude Agent SDK.

This wraps the same agent harness that powers Claude Code and drives it against a
checked-out :class:`~gnsis.orchestration.engine.Workspace`. It maps the SDK onto
the GNSIS phases:

* **plan**  — a read-only SDK run (``permission_mode="plan"``) that states an
  approach without touching files.
* **patch** — an editing SDK run (``permission_mode="acceptEdits"``) that makes
  the change in the workspace; the *patch* is then the workspace's ``git diff``.
* **tests** — best-effort discovery and execution of the project's test command.
* **summary** — a short read-only SDK run summarizing the diff.

The SDK needs ``ANTHROPIC_API_KEY`` in the environment. The import is lazy so the
rest of GNSIS (and CI) works without the SDK installed. This engine performs **no
GitHub writes** — that is the worker's job after approval.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from typing import Any, List, Optional

from ..orchestration.engine import PhaseSink, Workspace
from ..orchestration.models import EngineResult
from ..orchestration.status import Phase

DEFAULT_MODEL = "claude-opus-4-8"
_CODING_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]


class ClaudeAgentEngine:
    """A :class:`~gnsis.orchestration.engine.PatchEngine` backed by the SDK."""

    name = "claude"

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        max_test_seconds: int = 600,
    ) -> None:
        self.model = model
        self.max_test_seconds = max_test_seconds

    # -- PatchEngine -------------------------------------------------------
    def generate(
        self,
        instruction: str,
        workspace: Optional[Workspace],
        sink: PhaseSink,
    ) -> EngineResult:
        if workspace is None:
            raise ValueError("ClaudeAgentEngine requires a checked-out workspace")
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set; the Claude Agent SDK cannot run"
            )

        # plan ------------------------------------------------------------
        sink.begin_phase(Phase.PLAN)
        plan = self._run_sdk(
            workspace.path,
            permission_mode="plan",
            prompt=(
                "You are working in a git repository. Read whatever you need, then "
                "write a short, concrete plan to accomplish this task. Do NOT edit "
                f"any files yet.\n\nTask:\n{instruction}"
            ),
        )
        sink.checkpoint(Phase.PLAN, plan)

        # patch -----------------------------------------------------------
        sink.begin_phase(Phase.PATCH)
        self._run_sdk(
            workspace.path,
            permission_mode="acceptEdits",
            prompt=(
                "Implement the following task by editing files in this repository. "
                "Make focused, working changes and add or update tests as "
                f"appropriate. Do not commit, push, or open a PR.\n\n"
                f"Plan:\n{plan}\n\nTask:\n{instruction}"
            ),
        )
        patch = workspace.diff()
        files_changed = workspace.changed_files()
        sink.checkpoint(
            Phase.PATCH, {"patch": patch, "files_changed": files_changed}
        )
        if not patch.strip():
            sink.log("engine produced no changes", level="warning")
            return EngineResult(
                plan=plan, patch="", success=False, detail={"engine": self.name}
            )

        # tests -----------------------------------------------------------
        sink.begin_phase(Phase.TESTS)
        tests = self._run_tests(workspace, sink)
        sink.checkpoint(Phase.TESTS, tests)

        # summary ---------------------------------------------------------
        sink.begin_phase(Phase.SUMMARY)
        summary = self._run_sdk(
            workspace.path,
            permission_mode="plan",
            prompt=(
                "Summarize the change you just made in 3-6 sentences suitable for a "
                "pull request description. Here is the diff:\n\n"
                f"{patch[:12000]}"
            ),
        )
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

    # -- SDK plumbing ------------------------------------------------------
    def _run_sdk(self, cwd: str, prompt: str, permission_mode: str) -> str:
        return asyncio.run(self._run_sdk_async(cwd, prompt, permission_mode))

    async def _run_sdk_async(
        self, cwd: str, prompt: str, permission_mode: str
    ) -> str:
        try:
            from claude_agent_sdk import ClaudeAgentOptions, query
        except ImportError as exc:  # pragma: no cover - dep is optional
            raise RuntimeError(
                "claude-agent-sdk is not installed; install the 'service' extra"
            ) from exc

        options = ClaudeAgentOptions(
            cwd=cwd,
            permission_mode=permission_mode,
            model=self.model,
            allowed_tools=_CODING_TOOLS,
        )
        chunks: List[str] = []
        async for message in query(prompt=prompt, options=options):
            chunks.append(_message_text(message))
        return "\n".join(c for c in chunks if c).strip()

    # -- tests -------------------------------------------------------------
    def _run_tests(self, workspace: Workspace, sink: PhaseSink) -> str:
        command = _detect_test_command(workspace.path)
        if command is None:
            sink.log("no test command detected; skipping test run", level="warning")
            return "No test command detected."
        sink.log(f"running tests: {' '.join(command)}")
        try:
            proc = subprocess.run(
                command,
                cwd=workspace.path,
                capture_output=True,
                text=True,
                timeout=self.max_test_seconds,
            )
        except subprocess.TimeoutExpired:
            return f"Tests timed out after {self.max_test_seconds}s."
        tail = (proc.stdout + "\n" + proc.stderr).strip()[-8000:]
        verdict = "passed" if proc.returncode == 0 else f"failed (exit {proc.returncode})"
        return f"Tests {verdict}.\n\n{tail}"


def _message_text(message: Any) -> str:
    """Best-effort text extraction across SDK message/content shapes."""
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
        if parts:
            return "\n".join(parts)
    text = getattr(message, "text", None)
    if isinstance(text, str):
        return text
    return ""


def _detect_test_command(path: str) -> Optional[List[str]]:
    """Pick a sensible test command for the project, or ``None``."""
    if os.path.exists(os.path.join(path, "pyproject.toml")) or _glob(path, "test_*.py"):
        if os.path.isdir(os.path.join(path, "tests")):
            return ["python", "-m", "pytest", "-q"]
        return ["python", "-m", "pytest", "-q"]
    if os.path.exists(os.path.join(path, "package.json")):
        return ["npm", "test", "--silent"]
    return None


def _glob(path: str, pattern: str) -> bool:
    import glob as _g

    return bool(_g.glob(os.path.join(path, "**", pattern), recursive=True))
