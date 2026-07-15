"""Filesystem/shell tools scoped to a checked-out workspace.

These are the tools the native ``gnsis`` engine gives the tool-calling agent so
it can actually read and edit a repository — the built-in demo tools
(calculator, string reverse) are deliberately toy capabilities for the
CLI's self-evolution example, not real coding tools.

Every tool is constructed with a ``root`` directory (the workspace path) and
refuses to touch anything outside it — a relative path that escapes via ``..``,
or an absolute path elsewhere on disk, is rejected rather than silently
resolved. This is a workspace boundary, not a security sandbox: it stops an
agent from wandering outside the repo it was asked to change, not a malicious
actor from breaking out of the process. Real isolation is the job of
:mod:`gnsis.service.sandbox` (``GNSIS_SANDBOX=docker``).
"""

from __future__ import annotations

import os
import subprocess
from typing import Any, List

from .base import Tool, ToolResult

_MAX_OUTPUT_CHARS = 8000
_MAX_READ_CHARS = 20000


class WorkspaceBoundaryError(ValueError):
    """A tool argument tried to reference a path outside the workspace root."""


def resolve_in_root(root: str, path: str) -> str:
    """Resolve ``path`` (relative or absolute) and require it stay under ``root``."""
    root_real = os.path.realpath(root)
    candidate = path if os.path.isabs(path) else os.path.join(root_real, path)
    resolved = os.path.realpath(candidate)
    if resolved != root_real and not resolved.startswith(root_real + os.sep):
        raise WorkspaceBoundaryError(
            f"path {path!r} resolves outside the workspace ({root_real})"
        )
    return resolved


class ReadFileTool(Tool):
    name = "read_file"
    description = "Read a text file from the repository. Returns up to 20,000 characters."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path relative to the repo root."}
        },
        "required": ["path"],
    }

    def __init__(self, root: str) -> None:
        self.root = root

    def run(self, **kwargs: Any) -> ToolResult:
        path = str(kwargs.get("path", "")).strip()
        if not path:
            return ToolResult("error: no path provided", is_error=True)
        try:
            full = resolve_in_root(self.root, path)
        except WorkspaceBoundaryError as exc:
            return ToolResult(f"error: {exc}", is_error=True)
        if not os.path.isfile(full):
            return ToolResult(f"error: no such file: {path}", is_error=True)
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as handle:
                content = handle.read(_MAX_READ_CHARS + 1)
        except OSError as exc:
            return ToolResult(f"error: could not read {path}: {exc}", is_error=True)
        if len(content) > _MAX_READ_CHARS:
            content = content[:_MAX_READ_CHARS] + "\n... (truncated)"
        return ToolResult(content)


class ListFilesTool(Tool):
    name = "list_files"
    description = "List files under a directory in the repository (non-recursive by default)."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory relative to the repo root. Defaults to '.'."},
            "recursive": {"type": "boolean", "description": "Recurse into subdirectories."},
        },
    }

    def __init__(self, root: str) -> None:
        self.root = root

    def run(self, **kwargs: Any) -> ToolResult:
        path = str(kwargs.get("path", ".") or ".").strip()
        recursive = bool(kwargs.get("recursive", False))
        try:
            full = resolve_in_root(self.root, path)
        except WorkspaceBoundaryError as exc:
            return ToolResult(f"error: {exc}", is_error=True)
        if not os.path.isdir(full):
            return ToolResult(f"error: no such directory: {path}", is_error=True)

        entries: List[str] = []
        if recursive:
            for dirpath, dirnames, filenames in os.walk(full):
                dirnames[:] = [d for d in dirnames if d != ".git"]
                for name in filenames:
                    rel = os.path.relpath(os.path.join(dirpath, name), self.root)
                    entries.append(rel)
                    if len(entries) >= 2000:
                        break
                if len(entries) >= 2000:
                    break
        else:
            for name in sorted(os.listdir(full)):
                if name == ".git":
                    continue
                entries.append(os.path.relpath(os.path.join(full, name), self.root))

        if not entries:
            return ToolResult("(empty)")
        return ToolResult("\n".join(sorted(entries)))


class WriteFileTool(Tool):
    name = "write_file"
    description = (
        "Create a file or overwrite it entirely with new content. Prefer edit_file for "
        "changes to an existing file — only use write_file for new files or full rewrites."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path relative to the repo root."},
            "content": {"type": "string", "description": "The full file content."},
        },
        "required": ["path", "content"],
    }

    def __init__(self, root: str) -> None:
        self.root = root

    def run(self, **kwargs: Any) -> ToolResult:
        path = str(kwargs.get("path", "")).strip()
        content = kwargs.get("content", "")
        if not path:
            return ToolResult("error: no path provided", is_error=True)
        try:
            full = resolve_in_root(self.root, path)
        except WorkspaceBoundaryError as exc:
            return ToolResult(f"error: {exc}", is_error=True)
        try:
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as handle:
                handle.write(str(content))
        except OSError as exc:
            return ToolResult(f"error: could not write {path}: {exc}", is_error=True)
        return ToolResult(f"wrote {path} ({len(str(content))} chars)")


class EditFileTool(Tool):
    name = "edit_file"
    description = (
        "Replace an exact, unique substring in an existing file. Prefer this over "
        "write_file — it makes a targeted change instead of rewriting the whole file, "
        "which is cheaper and safer. old_string must match exactly once in the file."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path relative to the repo root."},
            "old_string": {"type": "string", "description": "The exact text to replace. Must be unique in the file."},
            "new_string": {"type": "string", "description": "The replacement text."},
        },
        "required": ["path", "old_string", "new_string"],
    }

    def __init__(self, root: str) -> None:
        self.root = root

    def run(self, **kwargs: Any) -> ToolResult:
        path = str(kwargs.get("path", "")).strip()
        old_string = kwargs.get("old_string", "")
        new_string = kwargs.get("new_string", "")
        if not path:
            return ToolResult("error: no path provided", is_error=True)
        if old_string == "":
            return ToolResult("error: old_string must not be empty", is_error=True)
        try:
            full = resolve_in_root(self.root, path)
        except WorkspaceBoundaryError as exc:
            return ToolResult(f"error: {exc}", is_error=True)
        if not os.path.isfile(full):
            return ToolResult(f"error: no such file: {path}", is_error=True)
        with open(full, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read()
        count = content.count(old_string)
        if count == 0:
            return ToolResult(f"error: old_string not found in {path}", is_error=True)
        if count > 1:
            return ToolResult(
                f"error: old_string matches {count} times in {path}; it must be unique "
                "— include more surrounding context",
                is_error=True,
            )
        content = content.replace(old_string, new_string, 1)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(content)
        return ToolResult(f"edited {path}")


class RunCommandTool(Tool):
    name = "run_command"
    description = (
        "Run a shell command inside the repository (e.g. to run tests or a linter). "
        "Output is truncated to 8,000 characters. Times out after 120 seconds."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to run."}
        },
        "required": ["command"],
    }

    def __init__(self, root: str, timeout_seconds: int = 120) -> None:
        self.root = root
        self.timeout_seconds = timeout_seconds

    def run(self, **kwargs: Any) -> ToolResult:
        command = str(kwargs.get("command", "")).strip()
        if not command:
            return ToolResult("error: no command provided", is_error=True)
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                f"error: command timed out after {self.timeout_seconds}s", is_error=True
            )
        output = (proc.stdout + "\n" + proc.stderr).strip()[-_MAX_OUTPUT_CHARS:]
        header = f"exit code: {proc.returncode}\n\n"
        return ToolResult(header + output, is_error=proc.returncode != 0)


def workspace_tools(root: str) -> List[Tool]:
    """The standard set of workspace-scoped tools for a coding agent."""
    return [
        ReadFileTool(root),
        ListFilesTool(root),
        WriteFileTool(root),
        EditFileTool(root),
        RunCommandTool(root),
    ]
