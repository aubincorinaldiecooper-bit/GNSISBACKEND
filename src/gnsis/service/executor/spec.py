"""Build the authenticated job specification handed to the executor VM.

Only what the run needs to execute — never a control-plane secret or a GitHub
credential. The model gateway URL and the run token replace any provider key.
"""

from __future__ import annotations

from .dispatch import budgets_from_settings
from .models import ExecutionRunRecord, RunSpec

MODEL_GATEWAY_PATH = "/internal/model/v1/chat/completions"
NETWORK_POLICY = "npm+pypi"  # public-beta: controlled npm + Python registries only


def model_gateway_url(settings) -> str:
    base = (settings.public_api_url or "").rstrip("/")
    return f"{base}{MODEL_GATEWAY_PATH}"


def build_run_spec(settings, job, run: ExecutionRunRecord) -> RunSpec:
    model = job.engine if job.engine in ("gnsis",) else "gnsis"
    selected_model = (
        settings.run_allowed_models[0]
        if settings.run_allowed_models
        else "anthropic/claude-opus-4.8"
    )
    return RunSpec(
        job_id=job.id,
        instruction=job.instruction,
        repository_full_name=job.repo,
        repository_id=None,
        base_sha=run.base_sha,
        base_branch=run.base_branch,
        model=selected_model,
        allowed_models=list(settings.run_allowed_models),
        budgets=budgets_from_settings(settings),
        model_gateway_url=model_gateway_url(settings),
        network_policy=NETWORK_POLICY,
        deadline_seconds=settings.executor_timeout_seconds,
        run_id=run.workflow_run_id,
        run_attempt=run.workflow_run_attempt,
        source_max_bytes=settings.executor_source_max_bytes,
        output_max_bytes={
            "patch.diff": settings.executor_patch_max_bytes,
            "tests.json": settings.executor_event_max_bytes,
            "receipt.json": settings.executor_event_max_bytes,
            "events.jsonl": settings.executor_event_max_bytes,
        },
    )
