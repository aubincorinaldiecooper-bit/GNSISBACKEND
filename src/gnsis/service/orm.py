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

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class ExecutionModelCall(Base):
    """One model call made by a run through the restricted gateway (for the receipt)."""

    __tablename__ = "execution_model_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("execution_runs.id"), index=True)
    job_id: Mapped[str] = mapped_column(String(64), index=True)
    model: Mapped[str] = mapped_column(String(128), default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ExecutionEvent(Base):
    """An authenticated event/callback from a run. Idempotent per (run, key)."""

    __tablename__ = "execution_events"
    __table_args__ = (
        UniqueConstraint("run_id", "idempotency_key", name="uq_exec_event_idem"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("execution_runs.id"), index=True)
    job_id: Mapped[str] = mapped_column(String(64), index=True)
    workflow_run_attempt: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    sequence: Mapped[int] = mapped_column(Integer, default=0)
    idempotency_key: Mapped[str] = mapped_column(String(128), default="")
    kind: Mapped[str] = mapped_column(String(64), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AgentMemory(Base):
    """Long-term, repo-scoped agent memory. Only approved records are written."""

    __tablename__ = "agent_memory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repo: Mapped[str] = mapped_column(String(255), index=True)
    kind: Mapped[str] = mapped_column(String(64), default="note")
    content: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    approved: Mapped[bool] = mapped_column(Boolean, default=True)
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
