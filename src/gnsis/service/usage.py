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
    # Ledger-integrity fields (PR-G1).
    idempotency_key: Optional[str] = None
    provider_request_id: Optional[str] = None
    error_category: Optional[str] = None
    genesis_calculated_cost: Optional[str] = None
    cost_source: str = "provider_reported"
    reconciliation_state: str = "resolved"
    project_id: Optional[str] = None
    virtual_key_id: Optional[str] = None


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
    project_id: Optional[str] = None
    virtual_key_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    provider_request_id: Optional[str] = None
    genesis_calculated_cost: Optional[str] = None
    cost_source: str = "provider_reported"
    reconciliation_state: str = "resolved"
    reconciliation_reason: Optional[str] = None
    pricing_version_id: Optional[str] = None
    error_category: Optional[str] = None
    created_at: str = ""

    @property
    def upstream_cost_decimal(self) -> Decimal:
        return Decimal(self.upstream_cost or "0")

    @property
    def needs_reconciliation(self) -> bool:
        return self.reconciliation_state == "needs_reconciliation"


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

    # Cost provenance: never silently treat a missing provider cost as $0. A
    # *successful* request whose cost is unknown is flagged for reconciliation
    # (the real cost is computed from versioned pricing later, or resolved by an
    # operator); a *failed* request legitimately carries no charge.
    raw_cost = body.get("upstream_cost")
    cost_present = raw_cost not in (None, "")
    status = str(body.get("request_status") or "success")
    succeeded = status in ("success", "succeeded", "ok", "completed")
    if cost_present:
        cost_source, reconciliation_state = "provider_reported", "resolved"
    elif not succeeded:
        cost_source, reconciliation_state = "unknown", "resolved"
    else:
        cost_source, reconciliation_state = "unknown", "needs_reconciliation"

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
        request_status=status,
        upstream_cost=_to_decimal_str(raw_cost, "upstream_cost"),
        currency=str(body.get("currency") or "USD"),
        retry_of=_opt(body.get("retry_of")),
        idempotency_key=_opt(md.get("idempotency_key") or body.get("idempotency_key")),
        provider_request_id=_opt(body.get("provider_request_id") or md.get("provider_request_id")),
        error_category=_opt(body.get("error_category")),
        cost_source=cost_source,
        reconciliation_state=reconciliation_state,
    )


def _to_view(row: orm.UsageRecord) -> UsageRecordView:
    return UsageRecordView(
        id=row.id,
        litellm_request_id=row.litellm_request_id,
        workspace_id=row.workspace_id,
        user_id=row.user_id,
        team_id=row.team_id,
        project_id=row.project_id,
        virtual_key_id=row.virtual_key_id,
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
        idempotency_key=row.idempotency_key,
        provider_request_id=row.provider_request_id,
        genesis_calculated_cost=row.genesis_calculated_cost,
        cost_source=row.cost_source,
        reconciliation_state=row.reconciliation_state,
        reconciliation_reason=row.reconciliation_reason,
        pricing_version_id=row.pricing_version_id,
        error_category=row.error_category,
        created_at=row.created_at.isoformat() if row.created_at else "",
    )


class UsageStore:
    """Durable, idempotent, workspace-isolated access to ``usage_records``."""

    def _find_existing(self, s, usage: MeasuredUsage):
        """A prior row for the same provider/callback id OR the same explicit
        caller idempotency key — either means "already recorded"."""
        row = (
            s.query(orm.UsageRecord)
            .filter(orm.UsageRecord.litellm_request_id == usage.litellm_request_id)
            .one_or_none()
        )
        if row is None and usage.idempotency_key:
            row = (
                s.query(orm.UsageRecord)
                .filter(orm.UsageRecord.idempotency_key == usage.idempotency_key)
                .one_or_none()
            )
        return row

    def record(self, usage: MeasuredUsage) -> tuple[UsageRecordView, bool]:
        """Persist a measurement. Returns ``(record, created)``.

        Idempotent on ``litellm_request_id`` *and* the explicit
        ``idempotency_key``: a duplicate callback (provider retry or webhook
        redelivery) returns the existing record with ``created=False`` and never
        inserts a second billable row.
        """
        with session_scope() as s:
            existing = self._find_existing(s, usage)
            if existing is not None:
                return _to_view(existing), False
            row = orm.UsageRecord(
                id=new_id("usage"),
                litellm_request_id=usage.litellm_request_id,
                idempotency_key=usage.idempotency_key,
                provider_request_id=usage.provider_request_id,
                workspace_id=usage.workspace_id,
                user_id=usage.user_id,
                team_id=usage.team_id,
                project_id=usage.project_id,
                virtual_key_id=usage.virtual_key_id,
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
                genesis_calculated_cost=usage.genesis_calculated_cost,
                currency=usage.currency,
                cost_source=usage.cost_source,
                reconciliation_state=usage.reconciliation_state,
                error_category=usage.error_category,
                retry_of=usage.retry_of,
            )
            s.add(row)
            try:
                s.flush()
            except IntegrityError:
                # Lost a concurrent race on a unique constraint — re-read.
                s.rollback()
                existing = self._find_existing(s, usage)
                if existing is None:
                    raise
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

    def list_needs_reconciliation(
        self, workspace_id: str, *, limit: int = 100
    ) -> List[UsageRecordView]:
        """Usage rows flagged for reconciliation (unknown cost, etc.)."""
        with session_scope() as s:
            rows = (
                s.query(orm.UsageRecord)
                .filter(
                    orm.UsageRecord.workspace_id == workspace_id,
                    orm.UsageRecord.reconciliation_state == "needs_reconciliation",
                )
                .order_by(orm.UsageRecord.created_at.desc())
                .limit(limit)
                .all()
            )
            return [_to_view(r) for r in rows]

    def count_needs_reconciliation(self, workspace_id: str) -> int:
        with session_scope() as s:
            return (
                s.query(orm.UsageRecord)
                .filter(
                    orm.UsageRecord.workspace_id == workspace_id,
                    orm.UsageRecord.reconciliation_state == "needs_reconciliation",
                )
                .count()
            )
