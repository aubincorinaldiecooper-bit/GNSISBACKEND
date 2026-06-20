"""The job domain model.

Everything the worker checkpoints and the API serves: state, per-phase
checkpoints, logs, the human approval decision, and PR metadata. Plain
dataclasses with explicit ``to_dict``/``from_dict`` so the same objects round-trip
through a JSON file today and Postgres columns/JSONB tomorrow.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_job_id() -> str:
    return "job_" + uuid.uuid4().hex[:12]


class JobState(str, Enum):
    QUEUED = "queued"
    PLANNING = "planning"
    PATCHING = "patching"
    TESTING = "testing"
    SUMMARIZING = "summarizing"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    PUBLISHING = "publishing"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"


#: The Hermes artifacts, in order. Each maps to a checkpoint.
WORKING_PHASES = ("plan", "patch", "tests", "summary")


@dataclass
class Checkpoint:
    phase: str
    data: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_now)


@dataclass
class LogEntry:
    message: str
    level: str = "info"
    phase: Optional[str] = None
    ts: str = field(default_factory=_now)


@dataclass
class Approval:
    decision: str  # "approved" | "rejected"
    actor: str = ""
    note: str = ""
    created_at: str = field(default_factory=_now)


@dataclass
class PRMetadata:
    branch: str = ""
    number: Optional[int] = None
    url: str = ""
    head_sha: str = ""


@dataclass
class Job:
    id: str = field(default_factory=new_job_id)
    repo: str = ""  # "owner/name"
    base_branch: str = "main"
    task: str = ""  # the instruction Hermes works from
    state: JobState = JobState.QUEUED
    checkpoints: List[Checkpoint] = field(default_factory=list)
    logs: List[LogEntry] = field(default_factory=list)
    approval: Optional[Approval] = None
    pr: Optional[PRMetadata] = None
    error: Optional[str] = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    # -- mutation helpers (the worker uses these) -------------------------
    def touch(self) -> None:
        self.updated_at = _now()

    def log(self, message: str, level: str = "info", phase: Optional[str] = None) -> None:
        self.logs.append(LogEntry(message=message, level=level, phase=phase))
        self.touch()

    def checkpoint(self, phase: str, data: Dict[str, Any]) -> Checkpoint:
        cp = Checkpoint(phase=phase, data=data)
        self.checkpoints.append(cp)
        self.touch()
        return cp

    def artifact(self, phase: str) -> Optional[Dict[str, Any]]:
        """The most recent checkpoint payload for a phase, if any."""
        for cp in reversed(self.checkpoints):
            if cp.phase == phase:
                return cp.data
        return None

    # -- serialization ----------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "repo": self.repo,
            "base_branch": self.base_branch,
            "task": self.task,
            "state": self.state.value,
            "checkpoints": [asdict(c) for c in self.checkpoints],
            "logs": [asdict(entry) for entry in self.logs],
            "approval": asdict(self.approval) if self.approval else None,
            "pr": asdict(self.pr) if self.pr else None,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Job":
        return cls(
            id=data["id"],
            repo=data.get("repo", ""),
            base_branch=data.get("base_branch", "main"),
            task=data.get("task", ""),
            state=JobState(data.get("state", "queued")),
            checkpoints=[Checkpoint(**c) for c in data.get("checkpoints", [])],
            logs=[LogEntry(**entry) for entry in data.get("logs", [])],
            approval=Approval(**data["approval"]) if data.get("approval") else None,
            pr=PRMetadata(**data["pr"]) if data.get("pr") else None,
            error=data.get("error"),
            created_at=data.get("created_at", _now()),
            updated_at=data.get("updated_at", _now()),
        )
