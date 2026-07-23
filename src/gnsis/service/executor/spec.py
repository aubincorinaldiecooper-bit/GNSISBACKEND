"""Build the authenticated job specification handed to the executor VM.

Only what the run needs to execute — never a control-plane secret or a GitHub
credential. The model gateway URL and the run token replace any provider key.

The trusted system policy and the repository memory are *reconstructed here*,
at fetch time, from what was pinned onto the run at dispatch — so a retry (which
re-fetches the spec) rebuilds byte-identical context, and a historical run always
reproduces exactly the policy version + memory it originally used.
"""

from __future__ import annotations

import logging

from .dispatch import budgets_from_settings
from .models import ExecutionRunRecord, RunSpec

MODEL_GATEWAY_PATH = "/internal/model/v1/chat/completions"
NETWORK_POLICY = "npm+pypi"  # public-beta: controlled npm + Python registries only

logger = logging.getLogger("gnsis.executor.spec")


def model_gateway_url(settings) -> str:
    base = (settings.public_api_url or "").rstrip("/")
    return f"{base}{MODEL_GATEWAY_PATH}"


def _reconstruct_policy(run: ExecutionRunRecord):
    """Rebuild the exact pinned policy version. None if the run pinned none."""
    if run.policy_version is None:
        return None
    from ..policy_store import get_policy_version

    resolved = get_policy_version(run.policy_version)
    if resolved is None:
        logger.error(
            "run %s pinned policy v%s but it is missing from the store",
            run.id, run.policy_version,
        )
        return None
    # Defensive: the pinned hash must match the reconstructed content. A mismatch
    # means the stored policy version was altered — refuse to serve stale/tampered
    # policy rather than silently hand the executor something unverifiable.
    if run.policy_hash and resolved.content_hash != run.policy_hash:
        logger.error(
            "run %s policy v%s hash mismatch (pinned=%s store=%s)",
            run.id, run.policy_version, run.policy_hash, resolved.content_hash,
        )
        return None
    return resolved.to_public_dict()


def _reconstruct_memory(job, run: ExecutionRunRecord) -> list:
    """Rebuild the exact pinned, tenant-scoped memory. Empty on any failure."""
    if not run.memory_ids:
        return []
    try:
        from ..codememory import CodeMemory

        items = CodeMemory().get_records_by_ids(
            memory_ids=list(run.memory_ids),
            workspace_id=run.workspace_id,
            repository_id=run.repository_id,
            repo=job.repo,
        )
        return [item.to_public_dict() for item in items]
    except Exception:  # noqa: BLE001 - memory is enhancement, never fail the spec
        logger.exception("failed to reconstruct memory context for run %s", run.id)
        return []


def build_run_spec(settings, job, run: ExecutionRunRecord) -> RunSpec:
    # The user-selected model, re-validated against the server allowlist at
    # dispatch (a stale/removed model falls back to the configured default —
    # the allowlist is never widened here). ``allowed_models`` handed to the
    # executor stays the full server-controlled set.
    from ..model_catalog import default_model, resolve_allowed_model

    selected_model = (
        resolve_allowed_model(settings, getattr(job, "model", None))
        or default_model(settings)
        or "anthropic/claude-opus-4.8"
    )
    # The Advisor is validated against the SAME server allowlist as the primary
    # so the openrouter:advisor server tool can invoke it exactly the way the
    # primary is invoked. A historical row with no Advisor recorded falls back
    # to the configured default — never widens the allowlist.
    selected_advisor = (
        resolve_allowed_model(settings, getattr(job, "advisor_model", None))
        or default_model(settings)
        or selected_model
    )
    return RunSpec(
        job_id=job.id,
        instruction=job.instruction,
        repository_full_name=job.repo,
        repository_id=None,
        base_sha=run.base_sha,
        base_branch=run.base_branch,
        model=selected_model,
        advisor_model=selected_advisor,
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
        policy=_reconstruct_policy(run),
        memory_context=_reconstruct_memory(job, run),
    )
