"""Server-side validation of executor outputs against the clean source.

Two layers:

* :func:`validate_patch_structure` — pure, no I/O. Confirms the patch is a
  non-empty unified diff, every touched path stays inside the repository (no
  absolute paths, no ``..`` traversal), it never edits ``.github/workflows/**``,
  and it is within the size / file-count ceilings. Used to reject a malformed or
  hostile patch *before* any clone.

* :func:`patch_applies_to_base` — runs ``git apply --check`` against an untouched
  checkout of the exact base commit, so a patch that does not apply cleanly to
  the pinned SHA is refused before it can reach approval or publication.

Plus JSON/text schema helpers for ``tests.json``/``receipt.json`` and
deterministic output hashing. The trusted host never trusts a byte it did not
re-derive here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import List, Optional, Set

WORKFLOW_DIR_PREFIX = ".github/workflows/"
_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_PLUSMINUS_RE = re.compile(r"^[+-]{3} [ab]/(.+)$")

ALLOWED_OUTPUT_FILES = ("patch.diff", "tests.json", "receipt.json", "events.jsonl")


@dataclass
class PatchValidation:
    ok: bool
    reason: str = ""
    files: List[str] = field(default_factory=list)


def _unsafe_path(path: str) -> Optional[str]:
    if path in ("", "/dev/null"):
        return None  # the null side of an add/delete
    if path.startswith("/"):
        return f"absolute path: {path}"
    parts = path.split("/")
    if ".." in parts:
        return f"path traversal: {path}"
    if any(p in (".",) for p in parts):
        return f"unsafe path component: {path}"
    return None


def _collect_paths(patch: str) -> Set[str]:
    paths: Set[str] = set()
    for line in patch.splitlines():
        m = _DIFF_GIT_RE.match(line)
        if m:
            paths.add(m.group(1))
            paths.add(m.group(2))
            continue
        m = _PLUSMINUS_RE.match(line)
        if m:
            paths.add(m.group(1))
    return {p for p in paths if p and p != "/dev/null"}


def validate_patch_structure(
    patch: str,
    *,
    max_bytes: int,
    max_files: int = 500,
) -> PatchValidation:
    """Structural + safety validation of a unified diff (no I/O)."""
    if not patch or not patch.strip():
        return PatchValidation(False, "empty patch")
    raw = patch.encode("utf-8", "surrogatepass") if isinstance(patch, str) else patch
    if len(raw) > max_bytes:
        return PatchValidation(False, f"patch exceeds {max_bytes} bytes")
    if "diff --git" not in patch and not patch.lstrip().startswith("--- "):
        return PatchValidation(False, "not a unified diff")

    paths = _collect_paths(patch)
    if not paths:
        return PatchValidation(False, "no file paths in patch")
    if len(paths) > max_files:
        return PatchValidation(False, f"patch touches too many files (> {max_files})")

    for path in sorted(paths):
        problem = _unsafe_path(path)
        if problem:
            return PatchValidation(False, problem, sorted(paths))
        if path == ".github/workflows" or path.startswith(WORKFLOW_DIR_PREFIX):
            return PatchValidation(
                False, f"patch modifies workflow file: {path}", sorted(paths)
            )
    return PatchValidation(True, "", sorted(paths))


def patch_applies_to_base(base_dir: str, patch: str) -> PatchValidation:
    """True iff ``patch`` applies cleanly to the untouched base checkout."""
    if not os.path.isdir(os.path.join(base_dir, ".git")) and not os.path.isdir(base_dir):
        return PatchValidation(False, "base checkout missing")
    with tempfile.NamedTemporaryFile(
        "w", suffix=".patch", dir=base_dir, delete=False, encoding="utf-8"
    ) as handle:
        handle.write(patch if patch.endswith("\n") else patch + "\n")
        patch_path = handle.name
    try:
        proc = subprocess.run(
            ["git", "apply", "--check", "-p1", os.path.basename(patch_path)],
            cwd=base_dir,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            return PatchValidation(False, f"patch does not apply: {proc.stderr.strip()[:300]}")
        return PatchValidation(True, "")
    finally:
        try:
            os.remove(patch_path)
        except OSError:
            pass


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_tests_json(raw: str) -> PatchValidation:
    """`tests.json` must be a JSON object with a structurally valid shape."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        return PatchValidation(False, f"tests.json invalid JSON: {exc}")
    if not isinstance(data, dict):
        return PatchValidation(False, "tests.json must be an object")
    # A permissive-but-typed shape: optional counts + list of results.
    for key in ("passed", "failed", "skipped"):
        if key in data and not isinstance(data[key], int):
            return PatchValidation(False, f"tests.json.{key} must be an integer")
    results = data.get("results")
    if results is not None and not isinstance(results, list):
        return PatchValidation(False, "tests.json.results must be a list")
    return PatchValidation(True, "")


def validate_receipt_json(raw: str) -> PatchValidation:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        return PatchValidation(False, f"receipt.json invalid JSON: {exc}")
    if not isinstance(data, dict):
        return PatchValidation(False, "receipt.json must be an object")
    return PatchValidation(True, "")


def strip_control_sequences(text: str) -> str:
    """Remove ANSI/control sequences so trusted logs can't be injection vectors."""
    # Drop ESC-based CSI/OSC sequences, then any remaining C0 controls except \n\t.
    text = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", text)
    text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", text)
    return "".join(ch for ch in text if ch == "\n" or ch == "\t" or ord(ch) >= 0x20)
