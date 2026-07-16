"""Execution vocabulary and framework-free record shapes.

Mirrors the orchestration layer's convention: plain dataclasses that the store
maps to/from ORM rows, so nothing outside :mod:`.store` touches SQLAlchemy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional


class ExecutionStatus:
    """Lifecycle of a remote execution run."""

    PENDING = "pending"            # record created, about to dispatch
    DISPATCHED = "dispatched"      # workflow_dispatch accepted by GitHub
    AUTHENTICATED = "authenticated"  # OIDC exchanged; run token issued
    RUNNING = "running"            # spec/source fetched; agent executing
    VALIDATING = "validating"      # completion received; validating outputs
    COMPLETED = "completed"        # outputs validated; job -> awaiting_approval
    FAILED = "failed"
    CANCELLED = "cancelled"

    #: no further automatic transition happens from these
    TERMINAL = frozenset({COMPLETED, FAILED, CANCELLED})
    #: a run token is only usable while the run is in one of these
    TOKEN_ACTIVE = frozenset({AUTHENTICATED, RUNNING, VALIDATING})


class FailureCategory:
    """Why a run failed — recorded for the receipt and reconciliation."""

    DISPATCH = "dispatch_failed"
    OIDC = "oidc_failed"
    TIMEOUT = "timeout"
    EXECUTOR_ERROR = "executor_error"
    VALIDATION = "validation_failed"
    SECURITY = "security_validation_failed"
    BUDGET = "budget_exceeded"
    CANCELLED = "cancelled"
    ORPHANED = "orphaned"
    LOST_CALLBACK = "lost_callback"
    STALE_ATTEMPT = "stale_attempt"


@dataclass(frozen=True)
class Budgets:
    """Per-run limits enforced by the gateway and the store."""

    max_model_calls: int
    max_input_tokens: int
    max_output_tokens: int
    max_cost_usd: float


@dataclass(frozen=True)
class Usage:
    """Accrued model usage for a run."""

    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


@dataclass(frozen=True)
class ExecutionRunRecord:
    """A remote execution run and its current state (never carries plaintext tokens)."""

    id: str
    job_id: str
    workspace_id: Optional[str]
    repository_id: Optional[str]
    provider: str
    base_branch: str
    base_sha: str
    executor_owner: str
    executor_repository: str
    executor_repository_id: Optional[int]
    executor_workflow: str
    executor_ref: str
    trusted_workflow_sha: str
    workflow_run_id: Optional[int]
    workflow_run_attempt: Optional[int]
    workflow_run_url: Optional[str]
    status: str
    nonce_consumed: bool
    token_hashed: bool
    token_revoked: bool
    token_expired: bool
    source_downloaded: bool
    patch_sha256: Optional[str]
    artifact_hashes: Dict[str, str]
    budgets: Budgets
    usage: Usage
    cancellation_requested: bool
    failure_category: Optional[str]
    security_validation: Optional[str]
    created_at: str = ""
    updated_at: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.status in ExecutionStatus.TERMINAL


@dataclass
class RunSpec:
    """The authenticated job specification handed to the executor VM.

    Deliberately minimal — no control-plane secret, no GitHub credential. The
    model gateway URL and run token are delivered separately (token via the OIDC
    exchange response, gateway URL derived from the public API URL).
    """

    job_id: str
    instruction: str
    repository_full_name: str
    repository_id: Optional[int]
    base_sha: str
    base_branch: str
    model: str
    allowed_models: list
    budgets: Budgets
    model_gateway_url: str
    network_policy: str
    deadline_seconds: int
    output_max_bytes: Dict[str, int] = field(default_factory=dict)

    def to_public_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "instruction": self.instruction,
            "repository": {
                "full_name": self.repository_full_name,
                "id": self.repository_id,
            },
            "base_sha": self.base_sha,
            "base_branch": self.base_branch,
            "model": self.model,
            "allowed_models": list(self.allowed_models),
            "budgets": {
                "max_model_calls": self.budgets.max_model_calls,
                "max_input_tokens": self.budgets.max_input_tokens,
                "max_output_tokens": self.budgets.max_output_tokens,
                "max_cost_usd": self.budgets.max_cost_usd,
            },
            "model_gateway_url": self.model_gateway_url,
            "network_policy": self.network_policy,
            "deadline_seconds": self.deadline_seconds,
            "output_limits_bytes": dict(self.output_max_bytes),
            "output_files": ["patch.diff", "tests.json", "receipt.json", "events.jsonl"],
        }
