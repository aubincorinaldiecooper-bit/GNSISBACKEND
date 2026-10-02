"""Idempotent visual-session usage ledger."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Mapping, Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from . import orm
from .db import session_scope
from ..orchestration.models import new_id

_REPORT_FIELDS = (
    "event_id",
    "workspace_id",
    "virtual_key_id",
    "project_id",
    "environment_id",
    "grant_id",
    "session_id",
    "report_seq",
    "frames_accepted",
    "frame_bytes",
    "decisions",
    "decisions_act",
    "decisions_abstain",
    "attempts_recorded",
    "inference_ms",
    "session_ms",
    "closed",
)


@dataclass(frozen=True)
class VisualUsageRecordView:
    id: str
    event_id: str
    workspace_id: str
    virtual_key_id: str
    project_id: Optional[str]
    environment_id: Optional[str]
    grant_id: str
    session_id: str
    report_seq: int
    frames_accepted: int
    frame_bytes: int
    decisions: int
    decisions_act: int
    decisions_abstain: int
    attempts_recorded: int
    inference_ms: int
    session_ms: int
    closed: bool
    created_at: str


def _to_view(row: orm.VisualUsageRecord) -> VisualUsageRecordView:
    return VisualUsageRecordView(
        id=row.id,
        event_id=row.event_id,
        workspace_id=row.workspace_id,
        virtual_key_id=row.virtual_key_id,
        project_id=row.project_id,
        environment_id=row.environment_id,
        grant_id=row.grant_id,
        session_id=row.session_id,
        report_seq=row.report_seq,
        frames_accepted=row.frames_accepted,
        frame_bytes=row.frame_bytes,
        decisions=row.decisions,
        decisions_act=row.decisions_act,
        decisions_abstain=row.decisions_abstain,
        attempts_recorded=row.attempts_recorded,
        inference_ms=row.inference_ms,
        session_ms=row.session_ms,
        closed=row.closed,
        created_at=row.created_at.isoformat() if row.created_at else "",
    )


class VisualUsageStore:
    """Append-only visual metering records, deduplicated by ``event_id``."""

    def _find_event(self, s, event_id: str):
        return (
            s.query(orm.VisualUsageRecord)
            .filter(orm.VisualUsageRecord.event_id == event_id)
            .one_or_none()
        )

    def record(
        self, report: Mapping[str, object]
    ) -> tuple[VisualUsageRecordView, bool]:
        values = {name: report[name] for name in _REPORT_FIELDS}
        with session_scope() as s:
            existing = self._find_event(s, values["event_id"])
            if existing is not None:
                return _to_view(existing), False
            row = orm.VisualUsageRecord(id=new_id("vusg"), **values)
            s.add(row)
            try:
                s.flush()
            except IntegrityError:
                s.rollback()
                existing = self._find_event(s, values["event_id"])
                if existing is None:
                    raise
                return _to_view(existing), False
            return _to_view(row), True

    def decisions_today(self, virtual_key_id: str) -> int:
        now = datetime.now(timezone.utc)
        start = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
        with session_scope() as s:
            total = (
                s.query(func.coalesce(func.sum(orm.VisualUsageRecord.decisions), 0))
                .filter(
                    orm.VisualUsageRecord.virtual_key_id == virtual_key_id,
                    orm.VisualUsageRecord.created_at >= start,
                )
                .scalar()
            )
            return int(total or 0)
