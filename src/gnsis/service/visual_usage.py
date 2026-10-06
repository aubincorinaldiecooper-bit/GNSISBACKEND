"""Idempotent visual-session usage ledger."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any, Mapping, Optional

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
    "perceptions",
    "attempts_recorded",
    "inference_ms",
    "session_ms",
    "inspections",
    "pixel_reads",
    "history_reads",
    "host_client",
    "planner_client",
    "closed",
    "reported_at",
)

#: Additive counters summed by the usage read API.
SUMMED_FIELDS = (
    "frames_accepted",
    "frame_bytes",
    "decisions",
    "decisions_act",
    "decisions_abstain",
    "perceptions",
    "attempts_recorded",
    "inference_ms",
    "session_ms",
    "inspections",
    "pixel_reads",
    "history_reads",
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
    perceptions: int
    attempts_recorded: int
    inference_ms: int
    session_ms: int
    inspections: int
    pixel_reads: int
    history_reads: int
    host_client: Optional[str]
    planner_client: Optional[str]
    closed: bool
    reported_at: Optional[datetime]
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
        perceptions=row.perceptions,
        attempts_recorded=row.attempts_recorded,
        inference_ms=row.inference_ms,
        session_ms=row.session_ms,
        inspections=row.inspections or 0,
        pixel_reads=row.pixel_reads or 0,
        history_reads=row.history_reads or 0,
        host_client=row.host_client,
        planner_client=row.planner_client,
        closed=row.closed,
        reported_at=row.reported_at,
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

    def summary(
        self,
        workspace_id: str,
        *,
        days: int,
        virtual_key_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Usage totals for the last ``days`` UTC days, split by day, key and client.

        The client is the agent holding the planner token when one reported
        itself, otherwise the host that opened the session.
        """

        now = datetime.now(timezone.utc)
        start = datetime.combine(
            now.date() - timedelta(days=days - 1), time.min, tzinfo=timezone.utc
        )
        record = orm.VisualUsageRecord
        event_time = func.coalesce(record.reported_at, record.created_at)
        day = func.date(event_time)
        client = func.coalesce(record.planner_client, record.host_client, "unknown")
        sums = [
            func.coalesce(func.sum(getattr(record, name)), 0) for name in SUMMED_FIELDS
        ]
        with session_scope() as s:
            filters = [record.workspace_id == workspace_id, event_time >= start]
            if virtual_key_id is not None:
                filters.append(record.virtual_key_id == virtual_key_id)
            aggregates = [func.count(func.distinct(record.session_id)), *sums]

            def counters(row) -> dict[str, int]:
                return dict(
                    zip(("sessions", *SUMMED_FIELDS), (int(v or 0) for v in row))
                )

            totals = counters(s.query(*aggregates).filter(*filters).one())

            def grouped(expression, label: str) -> list[dict[str, Any]]:
                rows = (
                    s.query(expression, *aggregates)
                    .filter(*filters)
                    .group_by(expression)
                    .order_by(expression)
                    .all()
                )
                return [{label: str(row[0]), **counters(row[1:])} for row in rows]

            by_day = grouped(day, "day")
            by_key = grouped(record.virtual_key_id, "virtual_key_id")
            by_client = grouped(client, "client")
        return {
            "start": start.isoformat(),
            "end": now.isoformat(),
            "totals": totals,
            "by_day": by_day,
            "by_key": by_key,
            "by_client": by_client,
        }

    def decisions_today(self, virtual_key_id: str) -> int:
        now = datetime.now(timezone.utc)
        start = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
        event_day = func.coalesce(
            orm.VisualUsageRecord.reported_at, orm.VisualUsageRecord.created_at
        )
        with session_scope() as s:
            total = (
                s.query(
                    func.coalesce(
                        func.sum(
                            orm.VisualUsageRecord.decisions
                            + orm.VisualUsageRecord.perceptions
                        ),
                        0,
                    )
                )
                .filter(
                    orm.VisualUsageRecord.virtual_key_id == virtual_key_id,
                    event_day >= start,
                )
                .scalar()
            )
            return int(total or 0)
