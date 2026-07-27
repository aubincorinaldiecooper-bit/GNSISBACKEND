"""Evidence-based descriptions shared by executor failure paths."""

from __future__ import annotations

from typing import Optional

from .models import ExecutionStatus, FailureCategory


def classify_failure(run, *, failure_category: Optional[str] = None) -> dict:
    """Describe the furthest stage proven by durable execution evidence."""
    category = failure_category or run.failure_category
    model_called = run.usage.model_calls > 0
    execution_started = run.status in (
        ExecutionStatus.RUNNING, ExecutionStatus.VALIDATING,
    ) or model_called or bool(run.patch_sha256)
    if (
        run.patch_sha256
        or run.status == ExecutionStatus.VALIDATING
        or category == FailureCategory.VALIDATION
    ):
        return {
            "stage": "output_validation", "execution_started": True,
            "model_called": model_called,
            "message": "The executor produced changes, but GNSIS could not validate the result.",
            "retryable": False,
            "next_action": "Review the validation details and retry the run.",
        }
    if run.source_downloaded and execution_started:
        return {
            "stage": "execution", "execution_started": True,
            "model_called": model_called,
            "message": "Execution began but stopped before producing a validated result.",
            "retryable": True,
            "next_action": "Retry the run; contact support if it stops again.",
        }
    if run.token_hashed:
        return {
            "stage": "source_loading", "execution_started": False,
            "model_called": False,
            "message": "The executor connected successfully but could not load the repository source.",
            "retryable": True,
            "next_action": "Verify repository access and retry the run.",
        }
    return {
        "stage": "executor_authentication", "execution_started": False,
        "model_called": False,
        "message": "The trusted executor started, but it could not connect to or authenticate with GNSIS.",
        "retryable": True,
        "next_action": "Retry the run; contact support if authentication continues to fail.",
    }
