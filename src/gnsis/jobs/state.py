"""The job state machine.

Transitions are explicit and guarded so the worker can never publish a PR that
a human did not approve: the only path out of ``awaiting_approval`` is
``approved`` (then ``publishing``) or ``rejected``. This guard is the heart of
the human-in-the-loop guarantee.
"""

from __future__ import annotations

from typing import Dict, Set

from .models import Job, JobState


class InvalidTransition(Exception):
    """Raised when a job is moved between states that are not adjacent."""


_ALLOWED: Dict[JobState, Set[JobState]] = {
    JobState.QUEUED: {JobState.PLANNING, JobState.FAILED},
    JobState.PLANNING: {JobState.PATCHING, JobState.FAILED},
    JobState.PATCHING: {JobState.TESTING, JobState.FAILED},
    JobState.TESTING: {JobState.SUMMARIZING, JobState.FAILED},
    JobState.SUMMARIZING: {JobState.AWAITING_APPROVAL, JobState.FAILED},
    # The approval gate: no path to PUBLISHING except through APPROVED.
    JobState.AWAITING_APPROVAL: {JobState.APPROVED, JobState.REJECTED},
    JobState.APPROVED: {JobState.PUBLISHING, JobState.FAILED},
    JobState.PUBLISHING: {JobState.COMPLETED, JobState.FAILED},
    JobState.COMPLETED: set(),
    JobState.FAILED: set(),
    JobState.REJECTED: set(),
}


def can_transition(current: JobState, target: JobState) -> bool:
    return target in _ALLOWED.get(current, set())


def is_terminal(state: JobState) -> bool:
    return len(_ALLOWED.get(state, set())) == 0


def transition(job: Job, target: JobState) -> Job:
    """Mutate ``job`` to ``target`` if the transition is legal, else raise."""
    if not can_transition(job.state, target):
        raise InvalidTransition(f"{job.state.value} -> {target.value} is not allowed")
    job.state = target
    job.touch()
    return job
