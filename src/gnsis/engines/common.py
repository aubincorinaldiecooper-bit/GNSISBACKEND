"""Shared helpers for concrete engines (test discovery + execution).

Kept here so every engine runs and reports tests the same way, and so the
orchestration core stays free of engine-specific concerns.
"""

from __future__ import annotations

import glob as _glob
import os
import subprocess
from typing import List, Optional

from ..orchestration.engine import PhaseSink, Workspace


def detect_test_command(path: str) -> Optional[List[str]]:
    """Pick a sensible test command for the project, or ``None``."""
    if os.path.exists(os.path.join(path, "pyproject.toml")) or _has(path, "test_*.py"):
        return ["python", "-m", "pytest", "-q"]
    if os.path.exists(os.path.join(path, "package.json")):
        return ["npm", "test", "--silent"]
    return None


def run_tests(workspace: Workspace, sink: PhaseSink, max_seconds: int) -> str:
    """Detect and run the project's tests; return a short report."""
    command = detect_test_command(workspace.path)
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
            timeout=max_seconds,
        )
    except subprocess.TimeoutExpired:
        return f"Tests timed out after {max_seconds}s."
    tail = (proc.stdout + "\n" + proc.stderr).strip()[-8000:]
    verdict = "passed" if proc.returncode == 0 else f"failed (exit {proc.returncode})"
    return f"Tests {verdict}.\n\n{tail}"


def _has(path: str, pattern: str) -> bool:
    return bool(_glob.glob(os.path.join(path, "**", pattern), recursive=True))
