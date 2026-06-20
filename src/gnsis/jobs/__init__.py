"""Jobs — the unit of work the worker runs and the human approves.

A job moves through Hermes's phases (plan -> patch -> tests -> summary), halts
at ``awaiting_approval``, and — only after approval — is published as a PR. The
domain model here is storage-agnostic; ``FileJobStore`` backs it today and a
Postgres-backed store will implement the same :class:`JobStore` interface.
"""

from .models import (
    Approval,
    Checkpoint,
    Job,
    JobState,
    LogEntry,
    PRMetadata,
    new_job_id,
)
from .state import InvalidTransition, can_transition, is_terminal, transition
from .store import FileJobStore, JobStore

__all__ = [
    "Job",
    "JobState",
    "Checkpoint",
    "LogEntry",
    "Approval",
    "PRMetadata",
    "new_job_id",
    "JobStore",
    "FileJobStore",
    "transition",
    "can_transition",
    "is_terminal",
    "InvalidTransition",
]
