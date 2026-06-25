"""Job lifecycle — the states a code-change job moves through.

A job is created over HTTP (``queued``), picked up by a worker, driven through
the generation phases, and then **parked at ``awaiting_approval``** until a human
decides. Only after approval does a separate publish step run. Keeping the state
machine in one place (and free of any framework dependency) lets both the API
and the worker reason about it identically.
"""

from __future__ import annotations

from typing import Dict, FrozenSet


class Phase:
    """The generation phases a job's engine produces, in order."""

    PLAN = "plan"
    PATCH = "patch"
    TESTS = "tests"
    SUMMARY = "summary"
    PUBLISH = "publish"

    ORDER = (PLAN, PATCH, TESTS, SUMMARY)


class JobStatus:
    """All states a job can occupy."""

    QUEUED = "queued"
    PLANNING = "planning"
    PATCHING = "patching"
    TESTING = "testing"
    SUMMARIZING = "summarizing"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    PUBLISHING = "publishing"
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"


#: status the worker sets while a given phase runs
PHASE_STATUS: Dict[str, str] = {
    Phase.PLAN: JobStatus.PLANNING,
    Phase.PATCH: JobStatus.PATCHING,
    Phase.TESTS: JobStatus.TESTING,
    Phase.SUMMARY: JobStatus.SUMMARIZING,
    Phase.PUBLISH: JobStatus.PUBLISHING,
}

#: states from which no further automatic transition happens
TERMINAL: FrozenSet[str] = frozenset(
    {JobStatus.COMPLETED, JobStatus.REJECTED, JobStatus.FAILED}
)

#: the gate: a job sits here until a human approves or rejects
APPROVAL_GATE = JobStatus.AWAITING_APPROVAL


def is_terminal(status: str) -> bool:
    return status in TERMINAL
