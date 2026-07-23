"""Postgres schema — the durable home for everything that must survive a restart.

Maps the framework-free orchestration dataclasses onto tables: jobs and their
history, evolution/phase logs, per-phase checkpoints, diffs, approvals, and PR
metadata — plus the versioned resource store (prompts and their lineage). On
Railway, container/sandbox teardown loses local disk; this is what makes that
safe.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# -- tenancy: one personal workspace per Better Auth user ----------------------


class Workspace(Base):
    """A user's personal Genesis workspace. One per Better Auth subject."""

    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_auth_subject: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), default="Personal")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    installations: Mapped[list["GitHubInstallation"]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )
    repositories: Mapped[list["Repository"]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )


class GitHubInstallation(Base):
    """A GitHub App installation claimed by (and scoped to) one workspace."""

    __tablename__ = "github_installations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id"), index=True
    )
    github_installation_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, index=True
    )
    github_account_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    github_account_login: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    github_account_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
    suspended_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    workspace: Mapped[Workspace] = relationship(back_populates="installations")
    repositories: Mapped[list["Repository"]] = relationship(
        back_populates="installation", cascade="all, delete-orphan"
    )


class Repository(Base):
    """A repository authorized through a workspace's GitHub App installation."""

    __tablename__ = "repositories"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "github_repository_id", name="uq_repo_per_workspace"
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    github_installation_record_id: Mapped[str] = mapped_column(
        ForeignKey("github_installations.id"), index=True
    )
    github_repository_id: Mapped[int] = mapped_column(BigInteger, index=True)
    owner: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(511), index=True)
    default_branch: Mapped[str] = mapped_column(String(255), default="main")
    private: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    workspace: Mapped[Workspace] = relationship(back_populates="repositories")
    installation: Mapped[GitHubInstallation] = relationship(
        back_populates="repositories"
    )


class WebhookDelivery(Base):
    """Idempotency ledger: one row per processed GitHub webhook delivery."""

    __tablename__ = "webhook_deliveries"

    delivery_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    event: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    repo: Mapped[str] = mapped_column(String(255), index=True)
    instruction: Mapped[str] = mapped_column(Text)
    base_branch: Mapped[str] = mapped_column(String(255), default="main")
    engine: Mapped[str] = mapped_column(String(64), default="claude")
    # The user-selected OpenRouter model for this job, validated against the
    # server allowlist at creation. Nullable: legacy jobs and jobs created before
    # model selection fall back to the configured default at dispatch.
    model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    branch: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    # Tenancy — nullable so legacy/internal rows created before this migration
    # (and internal-API-key runs) remain valid; user runs always set both.
    workspace_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    repository_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    logs: Mapped[list["JobLog"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    checkpoints: Mapped[list["JobCheckpoint"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    approvals: Mapped[list["JobApproval"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )


class JobLog(Base):
    __tablename__ = "job_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    phase: Mapped[str] = mapped_column(String(32), default="")
    level: Mapped[str] = mapped_column(String(16), default="info")
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    job: Mapped[Job] = relationship(back_populates="logs")


class JobCheckpoint(Base):
    __tablename__ = "job_checkpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    phase: Mapped[str] = mapped_column(String(32), index=True)
    content: Mapped[Any] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    job: Mapped[Job] = relationship(back_populates="checkpoints")


class JobDiff(Base):
    __tablename__ = "job_diffs"

    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), primary_key=True)
    patch: Mapped[str] = mapped_column(Text)
    files_changed: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class JobApproval(Base):
    __tablename__ = "job_approvals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    decision: Mapped[str] = mapped_column(String(16))
    actor: Mapped[str] = mapped_column(String(255))
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    job: Mapped[Job] = relationship(back_populates="approvals")


class PullRequest(Base):
    __tablename__ = "pull_requests"

    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), primary_key=True)
    number: Mapped[int] = mapped_column(Integer)
    url: Mapped[str] = mapped_column(String(512))
    branch: Mapped[str] = mapped_column(String(255))
    head_sha: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


# -- public-beta remote execution (GitHub Actions executor) --------------------


class ExecutionRun(Base):
    """One remote execution of a user job in the private GitHub Actions executor.

    This is the control-plane's durable record of a run: what customer commit it
    is pinned to, which fixed executor workflow/attempt it dispatched, the *hash*
    of the single-use dispatch nonce and of the short-lived executor token (never
    the plaintext of either), the enforced budgets and accrued usage, the
    server-computed patch hash, and the lifecycle timestamps reconciliation
    relies on. No raw OIDC token, plaintext executor/installation token, provider
    master key, or GitHub App private key is ever stored here.
    """

    __tablename__ = "execution_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    workspace_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    repository_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    provider: Mapped[str] = mapped_column(String(32), default="github_actions")

    # Immutable customer target.
    base_branch: Mapped[str] = mapped_column(String(255), default="main")
    base_sha: Mapped[str] = mapped_column(String(64), default="")

    # Single-use dispatch nonce — stored only as a hash, consumed atomically.
    dispatch_nonce_hash: Mapped[str] = mapped_column(String(64), index=True)
    nonce_consumed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Fixed executor identity + the exact trusted workflow commit.
    executor_owner: Mapped[str] = mapped_column(String(255), default="")
    executor_repository: Mapped[str] = mapped_column(String(255), default="")
    executor_repository_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    executor_workflow: Mapped[str] = mapped_column(String(255), default="execute.yml")
    executor_ref: Mapped[str] = mapped_column(String(255), default="main")
    trusted_workflow_sha: Mapped[str] = mapped_column(String(64), default="")

    # GitHub-assigned run identity, persisted from the dispatch/lookup response.
    workflow_run_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, index=True)
    workflow_run_attempt: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    workflow_run_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)

    # Short-lived executor token — hash only, plus its lifecycle.
    token_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    token_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    token_revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    source_downloaded_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Server-computed output hashes.
    patch_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    artifact_hashes: Mapped[dict] = mapped_column(JSON, default=dict)

    # Budgets (snapshot at dispatch) and accrued usage.
    max_model_calls: Mapped[int] = mapped_column(Integer, default=0)
    max_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    max_output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    max_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    model_calls: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    # Lifecycle timestamps.
    dispatched_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    cancellation_requested_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_reconciled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    failure_category: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    security_validation: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Intelligence context pinned at dispatch, so a run permanently retains the
    # exact trusted policy version and the exact memory it was allowed to see —
    # deterministic across retries and auditable historically. Nullable: legacy
    # runs and runs dispatched before the intelligence loop carry none.
    policy_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    policy_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    policy_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    memory_ids: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # Compact, immutable snapshot of the run's test outcome, captured from the
    # (already validated) tests.json at completion so the run receipt can report
    # it without re-running anything. Nullable: legacy runs and runs that emitted
    # no tests.json carry none. Keys: runner, status, passed, failed, skipped.
    tests_summary: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class ExecutionModelCall(Base):
    """One model call made by a run through the restricted gateway (for the receipt)."""

    __tablename__ = "execution_model_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("execution_runs.id"), index=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    model: Mapped[str] = mapped_column(String(128), default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    # Deterministic correlation key attached to the LiteLLM request metadata, so
    # the LiteLLM usage callback can be tied back to this exact model call.
    event_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class UsageRecord(Base):
    """One measured model request, as reported by LiteLLM and attributed to GNSIS.

    This is the metering fact: what was consumed (tokens/cost/provider/model,
    exactly as LiteLLM measured it) and who/what consumed it (workspace, user,
    and — for native coding runs — run + trace event + repository; for external
    virtual-key usage — application). It references the existing GNSIS records; it
    is not a second trace system. Money is stored as an exact decimal string,
    never binary floating point. ``litellm_request_id`` is unique so a replayed
    callback is idempotent.
    """

    __tablename__ = "usage_records"
    __table_args__ = (
        UniqueConstraint("litellm_request_id", name="uq_usage_litellm_request_id"),
        # Explicit caller-supplied idempotency (distinct from the provider/callback
        # dedup key above). Nullable so most rows leave it unset; unique when set.
        UniqueConstraint("idempotency_key", name="uq_usage_idempotency_key"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    litellm_request_id: Mapped[str] = mapped_column(String(128), index=True)
    # Caller-supplied logical-operation key (e.g. one attempt of one model call).
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(191), nullable=True)
    # The provider's own request id (distinct from litellm_request_id); useful for
    # provider-side reconciliation and distinguishing a retry from a new call.
    provider_request_id: Mapped[Optional[str]] = mapped_column(String(191), nullable=True, index=True)

    # Attribution to existing GNSIS records.
    workspace_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(255), index=True)
    team_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    project_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    # The Genesis virtual key the request was made with (for per-key attribution
    # + limits). Null for non-gateway (native run / callback) usage.
    virtual_key_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    run_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    trace_event_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    repository_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    application_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    engine: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    phase: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    environment: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # Measured usage (from LiteLLM).
    provider: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    reasoning_tokens: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    request_status: Mapped[str] = mapped_column(String(32), default="success", index=True)
    # Provider-reported cost, exactly as received, as a decimal string (never
    # float). Kept verbatim; the Genesis-calculated cost is stored separately so
    # neither overwrites the other and discrepancies can be flagged.
    upstream_cost: Mapped[str] = mapped_column(String(40), default="0")
    genesis_calculated_cost: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    # Where the provider cost came from: "provider_reported" or "unknown". An
    # "unknown" cost is NEVER silently treated as $0 — the row is flagged below.
    cost_source: Mapped[str] = mapped_column(String(24), default="provider_reported")
    # "resolved" | "needs_reconciliation". Unknown pricing / cost, or a meaningful
    # provider-vs-calculated discrepancy, must surface here rather than mis-bill.
    reconciliation_state: Mapped[str] = mapped_column(String(24), default="resolved", index=True)
    # Why a row needs reconciliation: unknown_cost / unknown_pricing / cost_discrepancy.
    reconciliation_reason: Mapped[Optional[str]] = mapped_column(String(48), nullable=True)
    # The model_pricing row used to compute genesis_calculated_cost (historical).
    pricing_version_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    # Classified failure bucket (e.g. provider_timeout, rate_limited, auth_error).
    error_category: Mapped[Optional[str]] = mapped_column(String(48), nullable=True, index=True)
    # For a retry, the litellm_request_id of the original request it retries.
    retry_of: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ExecutionEvent(Base):
    """An authenticated event/callback from a run. Idempotent per (run, key)."""

    __tablename__ = "execution_events"
    __table_args__ = (
        UniqueConstraint("run_id", "idempotency_key", name="uq_exec_event_idem"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("execution_runs.id"), index=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    workflow_run_attempt: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    sequence: Mapped[int] = mapped_column(Integer, default=0)
    idempotency_key: Mapped[str] = mapped_column(String(128), default="")
    kind: Mapped[str] = mapped_column(String(64), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


# -- billing: immutable charges, prepaid balance ledger, reservations ----------


class UsageCharge(Base):
    """An immutable retail charge derived from one measured usage record.

    Stores the *exact* pricing decision applied at the time (upstream, markup
    rate, service fee, retail, rate-card version) as decimal strings, so it is a
    historical fact that is never recomputed when the current markup changes. At
    most one charge exists per usage record (unique ``usage_record_id``).
    """

    __tablename__ = "usage_charges"
    __table_args__ = (
        UniqueConstraint("usage_record_id", name="uq_charge_usage_record"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    usage_record_id: Mapped[str] = mapped_column(String(64), index=True)
    litellm_request_id: Mapped[str] = mapped_column(String(128), index=True)
    workspace_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(255), index=True)
    run_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    trace_event_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    repository_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    application_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    # Exact applied pricing (decimal strings; never floats).
    upstream_cost: Mapped[str] = mapped_column(String(40), default="0")
    markup_rate: Mapped[str] = mapped_column(String(40), default="0")
    service_fee: Mapped[str] = mapped_column(String(40), default="0")
    retail_cost: Mapped[str] = mapped_column(String(40), default="0")
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    rate_card_version: Mapped[str] = mapped_column(String(64), default="")
    billing_status: Mapped[str] = mapped_column(String(32), default="charged", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class BalanceTransaction(Base):
    """One signed movement in a workspace's prepaid balance ledger.

    The current balance is derivable as the sum of ``signed_amount`` over all
    rows for the workspace. Money moves only through this table; nothing edits a
    prior row. Idempotency is enforced by a unique ``idempotency_key`` and, for
    Stripe-sourced rows, a unique ``stripe_event_id`` (nullable — non-Stripe rows
    leave it null).
    """

    __tablename__ = "balance_transactions"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_txn_idempotency_key"),
        UniqueConstraint("stripe_event_id", name="uq_txn_stripe_event_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), index=True)
    transaction_type: Mapped[str] = mapped_column(String(32), index=True)
    signed_amount: Mapped[str] = mapped_column(String(40), default="0")  # +credit / -debit
    usage_charge_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    stripe_event_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    stripe_payment_reference: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(191))
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class BalanceReservation(Base):
    """A short-lived hold placed before an upstream request whose cost is unknown.

    ``available = ledger balance − sum(active reservations)``. The usage callback
    settles the reservation into the actual debit; a failed request releases it.
    Keyed by the gateway's per-request correlation id so settlement is idempotent.
    """

    __tablename__ = "balance_reservations"
    __table_args__ = (
        UniqueConstraint("reservation_key", name="uq_reservation_key"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), index=True)
    reservation_key: Mapped[str] = mapped_column(String(128))
    amount: Mapped[str] = mapped_column(String(40), default="0")
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)  # active/settled/released
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class WorkspaceBilling(Base):
    """Per-workspace lock anchor, so balance reservations serialise safely."""

    __tablename__ = "workspace_billing"

    workspace_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class BetaCreditGrant(Base):
    """An operator-issued beta credit grant — the audit record for manual credits.

    The money itself lives in ``balance_transactions`` (the source of truth); this
    row records *who* granted *what*, *why*, and *when*, plus its reversal. A grant
    and its ledger transaction are written together in one transaction. A unique
    ``idempotency_key`` makes a re-sent grant a safe no-op; a reversal is a
    compensating negative transaction, never an edit of the original.
    """

    __tablename__ = "beta_credit_grants"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_beta_grant_idempotency_key"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), index=True)
    amount: Mapped[str] = mapped_column(String(40), default="0")  # decimal string, > 0
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    reason: Mapped[str] = mapped_column(Text, default="")
    operator: Mapped[str] = mapped_column(String(255), default="")  # attested operator id
    idempotency_key: Mapped[str] = mapped_column(String(191))
    status: Mapped[str] = mapped_column(String(16), default="granted", index=True)  # granted/reversed
    transaction_id: Mapped[str] = mapped_column(String(64))
    reversal_transaction_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    reversed_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    reversed_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    reversed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class LimitPolicy(Base):
    """A configurable spending-limit policy over one scope.

    Deterministic + auditable: the engine finds every applicable policy for a
    request and applies the most restrictive valid one. Enforcement is
    configurable per policy (observe / warn / block) so limits are never globally
    disabled — they are opt-in and tunable.
    """

    __tablename__ = "limit_policies"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), index=True)  # owning workspace
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    scope_type: Mapped[str] = mapped_column(String(24), index=True)  # workspace/project/environment/user/team/virtual_key
    scope_id: Mapped[str] = mapped_column(String(64), index=True)
    limit_type: Mapped[str] = mapped_column(String(16))              # per_run/daily/monthly/total
    amount: Mapped[str] = mapped_column(String(40), default="0")
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    warning_threshold: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)  # 0..1
    enforcement_mode: Mapped[str] = mapped_column(String(16), default="block")  # observe_only/warn/block
    reset_period: Mapped[str] = mapped_column(String(12), default="month")      # run/day/month/never
    effective_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class LimitDecision(Base):
    """Immutable audit of one policy evaluation for one request."""

    __tablename__ = "limit_decisions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    workspace_id: Mapped[str] = mapped_column(String(64), index=True)
    policy_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    policy_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # for key-inline limits
    scope_type: Mapped[str] = mapped_column(String(24))
    scope_id: Mapped[str] = mapped_column(String(64))
    limit_type: Mapped[str] = mapped_column(String(16))
    amount: Mapped[str] = mapped_column(String(40), default="0")
    previous_usage: Mapped[str] = mapped_column(String(40), default="0")
    reserved_amount: Mapped[str] = mapped_column(String(40), default="0")
    actual_usage: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    enforcement_mode: Mapped[str] = mapped_column(String(16))
    result: Mapped[str] = mapped_column(String(12), index=True)  # ok/warn/block
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class LimitReservation(Base):
    """A per-scope, per-window in-flight hold so concurrent requests cannot all
    spend the same remaining allowance before their charges land."""

    __tablename__ = "limit_reservations"
    __table_args__ = (
        # One hold per request per (scope, window). ``window_key`` is part of the
        # key because a single request legitimately holds against several windows
        # of the same scope (e.g. a workspace daily *and* monthly cap); this mirrors
        # how active holds are summed (scope_type + scope_id + window_key).
        UniqueConstraint(
            "reservation_key", "scope_type", "scope_id", "window_key", name="uq_limit_resv"
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    reservation_key: Mapped[str] = mapped_column(String(64), index=True)  # the Genesis request id
    workspace_id: Mapped[str] = mapped_column(String(64), index=True)
    scope_type: Mapped[str] = mapped_column(String(24))
    scope_id: Mapped[str] = mapped_column(String(64), index=True)
    window_key: Mapped[str] = mapped_column(String(48), index=True)
    amount: Mapped[str] = mapped_column(String(40), default="0")
    status: Mapped[str] = mapped_column(String(12), default="active", index=True)  # active/released/settled
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ModelPricing(Base):
    """A versioned price for one (provider, model) over a time window.

    The row id **is** the pricing version referenced by each usage event, so a
    price change never rewrites historical cost: an event keeps the version that
    was effective when it happened. Per-token prices are exact decimal strings
    (never float). ``effective_end`` NULL means "current".
    """

    __tablename__ = "model_pricing"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # == pricing_version_id
    provider: Mapped[str] = mapped_column(String(64), index=True)
    model: Mapped[str] = mapped_column(String(128), index=True)
    # Per-token prices as decimal strings.
    input_price: Mapped[str] = mapped_column(String(40), default="0")
    output_price: Mapped[str] = mapped_column(String(40), default="0")
    cached_input_price: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    reasoning_price: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    effective_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    effective_end: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class VirtualKey(Base):
    """A Genesis-native scoped inference credential (``gns_live_/gns_test_``).

    Genesis issues, hashes, and validates these itself — the full secret is
    returned exactly once at creation and is NEVER stored or retrievable
    afterwards; only a SHA-256 (optionally peppered) ``key_hash`` and a non-secret
    ``key_prefix`` for display/logging are kept. Keys carry attribution scopes
    (workspace/project/environment/user/team), provider/model allowlists, and
    per-scope spend limits. Prefer ``disable``/``rotate`` over destructive delete.
    """

    __tablename__ = "virtual_keys"
    __table_args__ = (
        UniqueConstraint("key_hash", name="uq_virtual_key_hash"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    key_hash: Mapped[str] = mapped_column(String(128), index=True)  # sha256 hex; never the secret
    key_prefix: Mapped[str] = mapped_column(String(32), default="")  # e.g. "gns_live_ab12cd…"
    mode: Mapped[str] = mapped_column(String(8), default="live")     # live | test
    name: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)  # active/disabled/rotated

    # Attribution scopes (workspace required; the rest optional).
    workspace_id: Mapped[str] = mapped_column(String(64), index=True)
    project_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    environment_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    user_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)
    team_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)

    # Restrictions ("" / null = unrestricted). CSV of provider / "provider/model".
    allowed_providers: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    allowed_models: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Per-key spend limits (decimal strings; null = unset). Enforced by the limits
    # engine (a later PR); stored here as the key's own policy inputs.
    soft_limit: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    hard_limit: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    per_run_limit: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    daily_limit: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    monthly_limit: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)

    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    rotated_to: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # successor key id
    key_metadata: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    disabled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentMemory(Base):
    """Long-term, repo-scoped agent memory. Only approved records are written.

    Scoping is layered: ``repo`` (the globally-unique ``owner/name``) has always
    namespaced a row to one repository; ``workspace_id`` + ``repository_id`` add
    tenant-strict isolation on top, so CodeMemory can guarantee one workspace's
    memory is never surfaced to another even if two ever shared a name. ``memory_id``
    is a stable public handle (distinct from the autoincrement PK) that can be
    pinned onto a run and echoed in its receipt without exposing the row id;
    ``source_job_id`` records which job's *approved* outcome produced the memory.
    All four are nullable so pre-existing rows remain valid (additive migration).
    """

    __tablename__ = "agent_memory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repo: Mapped[str] = mapped_column(String(255), index=True)
    kind: Mapped[str] = mapped_column(String(64), default="note")
    content: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    approved: Mapped[bool] = mapped_column(Boolean, default=True)
    # Tenant-strict scoping + provenance (nullable for legacy rows).
    workspace_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    repository_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    memory_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    source_job_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class MemoryProvenance(Base):
    """Auditable link from durable intelligence back to its reviewed source."""

    __tablename__ = "memory_provenance"
    __table_args__ = (
        UniqueConstraint(
            "outcome_id", "item_key", name="uq_memory_provenance_outcome_item_key"
        ),
        UniqueConstraint("memory_id", name="uq_memory_provenance_memory_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    memory_id: Mapped[str] = mapped_column(String(64), index=True)
    item_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    source_run_id: Mapped[str] = mapped_column(ForeignKey("execution_runs.id"), index=True)
    source_job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    outcome_id: Mapped[int] = mapped_column(ForeignKey("job_approvals.id"), index=True)
    outcome_decision: Mapped[str] = mapped_column(String(16), index=True)
    workspace_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    repository_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class MemoryConsumption(Base):
    """Auditable link from a later execution run to intelligence supplied to it."""

    __tablename__ = "memory_consumptions"
    __table_args__ = (
        UniqueConstraint("run_id", "memory_id", name="uq_memory_consumption_run_memory"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("execution_runs.id"), index=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    memory_id: Mapped[str] = mapped_column(String(64), index=True)
    workspace_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    repository_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


# -- durable resource store (RSPL on Postgres) ---------------------------------


class ResourceRecord(Base):
    __tablename__ = "resources"

    resource_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(255), index=True)

    versions: Mapped[list["ResourceVersionRecord"]] = relationship(
        back_populates="resource",
        cascade="all, delete-orphan",
        order_by="ResourceVersionRecord.version",
    )


class ResourceVersionRecord(Base):
    __tablename__ = "resource_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    resource_id: Mapped[str] = mapped_column(
        ForeignKey("resources.resource_id"), index=True
    )
    kind: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255))
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[Any] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    parent_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(String(64))

    resource: Mapped[ResourceRecord] = relationship(back_populates="versions")
