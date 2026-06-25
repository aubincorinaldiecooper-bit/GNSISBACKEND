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

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
