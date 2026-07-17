"""Measured usage — the metering fact for one model request.

Parses the LiteLLM usage callback (a documented GNSIS-shaped contract, not
LiteLLM's internal schema), attributes it deterministically to existing GNSIS
records via the metadata GNSIS itself attached to the request, and persists it
idempotently keyed on ``litellm_request_id``. Money is handled as
:class:`decimal.Decimal` and stored as an exact decimal string — never binary
floating point. A duplicate callback returns the existing record (idempotent
success) and never creates a second row.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import List, Optional

from sqlalchemy.exc import IntegrityError

from . import orm
from .db import session_scope
from ..orchestration.models import new_id


class UsageValidationError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _to_decimal_str(value: object, field: str) -> str:
    """Validate and canonicalise a monetary value to an exact decimal string."""
    if value is None or value == "":
        return "0"
    try:
        # str() first so a float like 0.1 is taken at its provided text form when
        # given as a string; a genuine float is converted through its repr.
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise UsageValidationError(f"{field} is not a valid decimal: {value!r}") from exc
    if dec.is_nan() or dec.is_infinite():
        raise UsageValidationError(f"{field} is not finite: {value!r}")
    if dec < 0:
        raise UsageValidationError(f"{field} must not be negative: {value!r}")
    return format(dec.normalize(), "f")


def _int(value: object, field: str) -> int:
    if value in (None, ""):
        return 0
    try:
        i = int(value)
    except (TypeError, ValueError) as exc:
        raise UsageValidationError(f"{field} must be an integer: {value!r}") from exc
    return max(0, i)


@dataclass(frozen=True)
class MeasuredUsage:
    """A validated, attributed usage measurement ready to persist."""

    litellm_request_id: str
    workspace_id: str
    user_id: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    reasoning_tokens: int
    duration_ms: int
    request_status: str
    upstream_cost: str
    currency: str
    team_id: Optional[str] = None
    run_id: Optional[str] = None
    trace_event_id: Optional[str] = None
    repository_id: Optional[str] = None
    application_name: Optional[str] = None
    engine: Optional[str] = None
    phase: Optional[str] = None
    environment: Optional[str] = None
    retry_of: Optional[str] = None


@dataclass(frozen=True)
class UsageRecordView:
    id: str
    litellm_request_id: str
    workspace_id: str
    user_id: str
    team_id: Optional[str]
    run_id: Optional[str]
    trace_event_id: Optional[str]
    repository_id: Optional[str]
    application_name: Optional[str]
    engine: Optional[str]
    phase: Optional[str]
    environment: Optional[str]
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    reasoning_tokens: int
    duration_ms: int
    request_status: str
    upstream_cost: str
    currency: str
    retry_of: Optional[str]
    created_at: str = ""

    @property
    def upstream_cost_decimal(self) -> Decimal:
        return Decimal(self.upstream_cost or "0")


def _opt(value: object) -> Optional[str]:
    if value in (None, ""):
        return None
    return str(value)


def parse_callback(body: dict) -> MeasuredUsage:
    """Validate a LiteLLM usage callback body into a :class:`MeasuredUsage`.

    Required: ``litellm_request_id`` and, from ``metadata``, ``workspace_id`` and
    ``user_id`` — attribution is by explicit id only (never timestamp/token/model
    matching). Native coding usage additionally carries ``run_id`` /
    ``trace_event_id`` / ``repository_id``; external virtual-key usage carries
    ``application_name``. Both flow through the same record.
    """
    if not isinstance(body, dict):
        raise UsageValidationError("callback body must be a JSON object")
    request_id = body.get("litellm_request_id") or body.get("litellm_call_id")
    if not request_id:
        raise UsageValidationError("litellm_request_id is required")

    md = body.get("metadata")
    if not isinstance(md, dict):
        raise UsageValidationError("metadata object is required")
    workspace_id = md.get("workspace_id")
    user_id = md.get("user_id")
    if not workspace_id or not user_id:
        raise UsageValidationError("metadata.workspace_id and metadata.user_id are required")

    return MeasuredUsage(
        litellm_request_id=str(request_id),
        workspace_id=str(workspace_id),
        user_id=str(user_id),
        team_id=_opt(md.get("team_id")),
        run_id=_opt(md.get("run_id")),
        trace_event_id=_opt(md.get("trace_event_id") or md.get("model_call_event_id")),
        repository_id=_opt(md.get("repository_id")),
        application_name=_opt(md.get("application_name")),
        engine=_opt(md.get("engine")),
        phase=_opt(md.get("phase")),
        environment=_opt(md.get("environment")),
        provider=str(body.get("provider") or body.get("custom_llm_provider") or ""),
        model=str(body.get("model") or ""),
        input_tokens=_int(body.get("input_tokens"), "input_tokens"),
        output_tokens=_int(body.get("output_tokens"), "output_tokens"),
        cached_tokens=_int(body.get("cached_tokens"), "cached_tokens"),
        reasoning_tokens=_int(body.get("reasoning_tokens"), "reasoning_tokens"),
        duration_ms=_int(body.get("duration_ms"), "duration_ms"),
        request_status=str(body.get("request_status") or "success"),
        upstream_cost=_to_decimal_str(body.get("upstream_cost"), "upstream_cost"),
        currency=str(body.get("currency") or "USD"),
        retry_of=_opt(body.get("retry_of")),
    )


def _to_view(row: orm.UsageRecord) -> UsageRecordView:
    return UsageRecordView(
        id=row.id,
        litellm_request_id=row.litellm_request_id,
        workspace_id=row.workspace_id,
        user_id=row.user_id,
        team_id=row.team_id,
        run_id=row.run_id,
        trace_event_id=row.trace_event_id,
        repository_id=row.repository_id,
        application_name=row.application_name,
        engine=row.engine,
        phase=row.phase,
        environment=row.environment,
        provider=row.provider,
        model=row.model,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        cached_tokens=row.cached_tokens,
        reasoning_tokens=row.reasoning_tokens,
        duration_ms=row.duration_ms,
        request_status=row.request_status,
        upstream_cost=row.upstream_cost,
        currency=row.currency,
        retry_of=row.retry_of,
        created_at=row.created_at.isoformat() if row.created_at else "",
    )


class UsageStore:
    """Durable, idempotent, workspace-isolated access to ``usage_records``."""

    def record(self, usage: MeasuredUsage) -> tuple[UsageRecordView, bool]:
        """Persist a measurement. Returns ``(record, created)``.

        Idempotent on ``litellm_request_id``: a duplicate callback returns the
        existing record with ``created=False`` and never inserts a second row.
        """
        with session_scope() as s:
            existing = (
                s.query(orm.UsageRecord)
                .filter(orm.UsageRecord.litellm_request_id == usage.litellm_request_id)
                .one_or_none()
            )
            if existing is not None:
                return _to_view(existing), False
            row = orm.UsageRecord(
                id=new_id("usage"),
                litellm_request_id=usage.litellm_request_id,
                workspace_id=usage.workspace_id,
                user_id=usage.user_id,
                team_id=usage.team_id,
                run_id=usage.run_id,
                trace_event_id=usage.trace_event_id,
                repository_id=usage.repository_id,
                application_name=usage.application_name,
                engine=usage.engine,
                phase=usage.phase,
                environment=usage.environment,
                provider=usage.provider,
                model=usage.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cached_tokens=usage.cached_tokens,
                reasoning_tokens=usage.reasoning_tokens,
                duration_ms=usage.duration_ms,
                request_status=usage.request_status,
                upstream_cost=usage.upstream_cost,
                currency=usage.currency,
                retry_of=usage.retry_of,
            )
            s.add(row)
            try:
                s.flush()
            except IntegrityError:
                # Lost a concurrent race on the unique constraint — re-read.
                s.rollback()
                existing = (
                    s.query(orm.UsageRecord)
                    .filter(orm.UsageRecord.litellm_request_id == usage.litellm_request_id)
                    .one()
                )
                return _to_view(existing), False
            return _to_view(row), True

    def get(self, record_id: str) -> Optional[UsageRecordView]:
        with session_scope() as s:
            row = s.get(orm.UsageRecord, record_id)
            return _to_view(row) if row else None

    def get_by_litellm_id(self, litellm_request_id: str) -> Optional[UsageRecordView]:
        with session_scope() as s:
            row = (
                s.query(orm.UsageRecord)
                .filter(orm.UsageRecord.litellm_request_id == litellm_request_id)
                .one_or_none()
            )
            return _to_view(row) if row else None

    def list_for_workspace(
        self, workspace_id: str, *, limit: int = 100
    ) -> List[UsageRecordView]:
        with session_scope() as s:
            rows = (
                s.query(orm.UsageRecord)
                .filter(orm.UsageRecord.workspace_id == workspace_id)
                .order_by(orm.UsageRecord.created_at.desc())
                .limit(limit)
                .all()
            )
            return [_to_view(r) for r in rows]

    def count_for_workspace(self, workspace_id: str) -> int:
        with session_scope() as s:
            return (
                s.query(orm.UsageRecord)
                .filter(orm.UsageRecord.workspace_id == workspace_id)
                .count()
            )
