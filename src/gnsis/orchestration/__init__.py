"""Orchestration — the framework-free core of the GNSIS job service.

Job lifecycle, the persistence boundary (:class:`JobStore`), the pluggable
:class:`PatchEngine` seam, and the :class:`JobPipeline` that drives a job up to
the approval gate. Nothing here imports a web framework, a queue, or a database,
so it is unit-testable offline; the Railway service layer plugs concrete
implementations into these contracts.
"""

from __future__ import annotations

from .engine import MockEngine, PatchEngine, PhaseSink, Workspace
from .models import (
    Approval,
    Checkpoint,
    Diff,
    EngineResult,
    JobRecord,
    JobSpec,
    LogEntry,
    PipelineResult,
    PRMetadata,
)
from .pipeline import JobPipeline, Publisher, publish, reject_job
from .status import APPROVAL_GATE, PHASE_STATUS, TERMINAL, JobStatus, Phase, is_terminal
from .store import InMemoryJobStore, JobNotFound, JobStore

__all__ = [
    "JobStatus",
    "Phase",
    "PHASE_STATUS",
    "TERMINAL",
    "APPROVAL_GATE",
    "is_terminal",
    "JobSpec",
    "JobRecord",
    "LogEntry",
    "Checkpoint",
    "Diff",
    "Approval",
    "PRMetadata",
    "EngineResult",
    "PipelineResult",
    "JobStore",
    "InMemoryJobStore",
    "JobNotFound",
    "PatchEngine",
    "PhaseSink",
    "Workspace",
    "MockEngine",
    "JobPipeline",
    "Publisher",
    "publish",
    "reject_job",
]
