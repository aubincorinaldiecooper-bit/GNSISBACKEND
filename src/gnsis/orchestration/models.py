"""Plain data shapes for the orchestration layer.

These are intentionally framework-free dataclasses so the engine, the pipeline,
the in-memory store, and the Postgres-backed store all speak the same language.
Persistence adapters (e.g. SQLAlchemy) map *to and from* these; nothing here
imports a database, a queue, or a web framework.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class JobSpec:
    """What the caller asks for when creating a job."""

    repo: str  # "owner/name"
    instruction: str  # natural-language description of the change
    base_branch: str = "main"
    engine: str = "claude"  # which PatchEngine to use
    branch: Optional[str] = None  # proposed head branch; defaults from job id
    context: Dict[str, Any] = field(default_factory=dict)


@dataclass
class JobRecord:
    """A job and its current state."""

    id: str
    repo: str
    instruction: str
    base_branch: str
    engine: str
    status: str
    branch: Optional[str] = None
    error: Optional[str] = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    context: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LogEntry:
    job_id: str
    phase: str
    level: str  # "info" | "warning" | "error"
    message: str
    data: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_now)


@dataclass
class Checkpoint:
    """A durable snapshot of one phase's output, so a restart never loses work."""

    job_id: str
    phase: str
    content: Any
    created_at: str = field(default_factory=_now)


@dataclass
class Diff:
    """The proposed change as a unified diff (the patch the worker will apply)."""

    job_id: str
    patch: str
    files_changed: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=_now)


@dataclass
class Approval:
    """A human decision at the approval gate."""

    job_id: str
    decision: str  # "approved" | "rejected"
    actor: str
    note: str = ""
    created_at: str = field(default_factory=_now)


@dataclass
class PRMetadata:
    """What `publish_pr` recorded after opening the pull request."""

    job_id: str
    number: int
    url: str
    branch: str
    head_sha: str = ""
    created_at: str = field(default_factory=_now)


@dataclass
class EngineResult:
    """What a PatchEngine produces for a single job."""

    plan: str = ""
    patch: str = ""  # unified diff
    tests: str = ""  # test report / notes
    summary: str = ""
    files_changed: List[str] = field(default_factory=list)
    success: bool = True
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PipelineResult:
    """Outcome of running the generation pipeline up to the approval gate."""

    job_id: str
    status: str
    engine_result: Optional[EngineResult] = None
