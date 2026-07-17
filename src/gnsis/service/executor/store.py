"""The ``execution_runs`` persistence boundary.

All atomicity that the security model depends on lives here: single-use nonce
consumption, single-use source download, token binding/revocation, and
budget-checked model-call accounting are expressed as *conditional* SQL updates
so two concurrent callers can never both win. Callers receive framework-free
:class:`ExecutionRunRecord` dataclasses and never a live ORM row.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional, Tuple

from .. import orm
from ..db import session_scope
from ...orchestration.models import new_id
from .models import Budgets, ExecutionRunRecord, ExecutionStatus, Usage


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> str:
    return dt.isoformat() if dt else ""


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalise to a UTC-aware datetime (SQLite hands back naive values)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _is_expired(dt: Optional[datetime]) -> bool:
    aware = _aware(dt)
    return aware is not None and aware < _utcnow()


def _to_record(row: orm.ExecutionRun) -> ExecutionRunRecord:
    return ExecutionRunRecord(
        id=row.id,
        job_id=row.job_id,
        workspace_id=row.workspace_id,
        repository_id=row.repository_id,
        provider=row.provider,
        base_branch=row.base_branch,
        base_sha=row.base_sha,
        executor_owner=row.executor_owner,
        executor_repository=row.executor_repository,
        executor_repository_id=row.executor_repository_id,
        executor_workflow=row.executor_workflow,
        executor_ref=row.executor_ref,
        trusted_workflow_sha=row.trusted_workflow_sha,
        workflow_run_id=row.workflow_run_id,
        workflow_run_attempt=row.workflow_run_attempt,
        workflow_run_url=row.workflow_run_url,
        status=row.status,
        nonce_consumed=row.nonce_consumed_at is not None,
        token_hashed=row.token_hash is not None,
        token_revoked=row.token_revoked_at is not None,
        token_expired=_is_expired(row.token_expires_at),
        source_downloaded=row.source_downloaded_at is not None,
        patch_sha256=row.patch_sha256,
        artifact_hashes=dict(row.artifact_hashes or {}),
        budgets=Budgets(
            max_model_calls=row.max_model_calls,
            max_input_tokens=row.max_input_tokens,
            max_output_tokens=row.max_output_tokens,
            max_cost_usd=row.max_cost_usd,
        ),
        usage=Usage(
            model_calls=row.model_calls,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            cost_usd=row.cost_usd,
        ),
        cancellation_requested=row.cancellation_requested_at is not None,
        failure_category=row.failure_category,
        security_validation=row.security_validation,
        created_at=_iso(row.created_at),
        updated_at=_iso(row.updated_at),
    )


class ExecutionStore:
    """Durable access to ``execution_runs`` and its child tables."""

    # -- creation / lookup ------------------------------------------------
    def create_run(
        self,
        *,
        job_id: str,
        workspace_id: Optional[str],
        repository_id: Optional[str],
        base_branch: str,
        base_sha: str,
        dispatch_nonce_hash: str,
        executor_owner: str,
        executor_repository: str,
        executor_repository_id: Optional[int],
        executor_workflow: str,
        executor_ref: str,
        trusted_workflow_sha: str,
        budgets: Budgets,
    ) -> ExecutionRunRecord:
        run_id = new_id("exec")
        with session_scope() as s:
            row = orm.ExecutionRun(
                id=run_id,
                job_id=job_id,
                workspace_id=workspace_id,
                repository_id=repository_id,
                provider="github_actions",
                base_branch=base_branch,
                base_sha=base_sha,
                dispatch_nonce_hash=dispatch_nonce_hash,
                executor_owner=executor_owner,
                executor_repository=executor_repository,
                executor_repository_id=executor_repository_id,
                executor_workflow=executor_workflow,
                executor_ref=executor_ref,
                trusted_workflow_sha=trusted_workflow_sha,
                status=ExecutionStatus.PENDING,
                max_model_calls=budgets.max_model_calls,
                max_input_tokens=budgets.max_input_tokens,
                max_output_tokens=budgets.max_output_tokens,
                max_cost_usd=budgets.max_cost_usd,
            )
            s.add(row)
            s.flush()
            return _to_record(row)

    def get_run(self, run_id: str) -> Optional[ExecutionRunRecord]:
        with session_scope() as s:
            row = s.get(orm.ExecutionRun, run_id)
            return _to_record(row) if row else None

    def get_run_for_job(self, job_id: str) -> Optional[ExecutionRunRecord]:
        """The most recent run for a job (jobs re-dispatch on reconciliation)."""
        with session_scope() as s:
            row = (
                s.query(orm.ExecutionRun)
                .filter(orm.ExecutionRun.job_id == job_id)
                .order_by(orm.ExecutionRun.created_at.desc())
                .first()
            )
            return _to_record(row) if row else None

    def get_run_by_token_hash(self, token_hash: str) -> Optional[ExecutionRunRecord]:
        with session_scope() as s:
            row = (
                s.query(orm.ExecutionRun)
                .filter(orm.ExecutionRun.token_hash == token_hash)
                .one_or_none()
            )
            return _to_record(row) if row else None

    # -- dispatch ---------------------------------------------------------
    def mark_dispatched(
        self,
        run_id: str,
        *,
        workflow_run_id: Optional[int],
        workflow_run_attempt: Optional[int],
        workflow_run_url: Optional[str],
    ) -> None:
        with session_scope() as s:
            row = s.get(orm.ExecutionRun, run_id)
            if row is None:
                raise KeyError(run_id)
            row.status = ExecutionStatus.DISPATCHED
            row.workflow_run_id = workflow_run_id
            row.workflow_run_attempt = workflow_run_attempt
            row.workflow_run_url = workflow_run_url
            row.dispatched_at = _utcnow()
            s.flush()

    def set_workflow_run(
        self,
        run_id: str,
        *,
        workflow_run_id: int,
        workflow_run_attempt: int,
        workflow_run_url: Optional[str] = None,
    ) -> None:
        with session_scope() as s:
            row = s.get(orm.ExecutionRun, run_id)
            if row is None:
                raise KeyError(run_id)
            row.workflow_run_id = workflow_run_id
            row.workflow_run_attempt = workflow_run_attempt
            if workflow_run_url:
                row.workflow_run_url = workflow_run_url
            s.flush()

    # -- nonce (single use, atomic) --------------------------------------
    def consume_nonce(self, run_id: str, nonce_hash: str) -> bool:
        """Atomically consume the dispatch nonce. Returns True exactly once.

        The conditional ``WHERE nonce_consumed_at IS NULL`` means a replayed
        exchange (or a concurrent one) matches zero rows and returns False.
        """
        with session_scope() as s:
            updated = (
                s.query(orm.ExecutionRun)
                .filter(
                    orm.ExecutionRun.id == run_id,
                    orm.ExecutionRun.dispatch_nonce_hash == nonce_hash,
                    orm.ExecutionRun.nonce_consumed_at.is_(None),
                    orm.ExecutionRun.status.in_(
                        [ExecutionStatus.PENDING, ExecutionStatus.DISPATCHED]
                    ),
                )
                .update(
                    {orm.ExecutionRun.nonce_consumed_at: _utcnow()},
                    synchronize_session=False,
                )
            )
            return updated == 1

    def nonce_matches(self, run_id: str, nonce_hash: str) -> bool:
        """Read-only check that a presented nonce hash matches (no consume)."""
        with session_scope() as s:
            row = s.get(orm.ExecutionRun, run_id)
            return bool(row and row.dispatch_nonce_hash == nonce_hash)

    # -- token binding ----------------------------------------------------
    def bind_token(
        self, run_id: str, *, token_hash: str, expires_at: datetime
    ) -> None:
        with session_scope() as s:
            row = s.get(orm.ExecutionRun, run_id)
            if row is None:
                raise KeyError(run_id)
            row.token_hash = token_hash
            row.token_expires_at = expires_at
            row.token_revoked_at = None
            row.status = ExecutionStatus.AUTHENTICATED
            s.flush()

    def revoke_token(self, run_id: str) -> None:
        with session_scope() as s:
            row = s.get(orm.ExecutionRun, run_id)
            if row is None:
                return
            if row.token_revoked_at is None:
                row.token_revoked_at = _utcnow()
            s.flush()

    def token_expires_at(self, run_id: str) -> Optional[datetime]:
        with session_scope() as s:
            row = s.get(orm.ExecutionRun, run_id)
            return row.token_expires_at if row else None

    # -- source (single use, atomic) -------------------------------------
    def claim_source_download(self, run_id: str) -> bool:
        """Atomically mark the immutable source as delivered. True exactly once."""
        with session_scope() as s:
            updated = (
                s.query(orm.ExecutionRun)
                .filter(
                    orm.ExecutionRun.id == run_id,
                    orm.ExecutionRun.source_downloaded_at.is_(None),
                )
                .update(
                    {orm.ExecutionRun.source_downloaded_at: _utcnow()},
                    synchronize_session=False,
                )
            )
            return updated == 1

    # -- status transitions ----------------------------------------------
    def set_status(
        self,
        run_id: str,
        status: str,
        *,
        failure_category: Optional[str] = None,
        security_validation: Optional[str] = None,
    ) -> None:
        with session_scope() as s:
            row = s.get(orm.ExecutionRun, run_id)
            if row is None:
                raise KeyError(run_id)
            row.status = status
            if failure_category is not None:
                row.failure_category = failure_category
            if security_validation is not None:
                row.security_validation = security_validation
            now = _utcnow()
            if status == ExecutionStatus.RUNNING and row.started_at is None:
                row.started_at = now
            if status == ExecutionStatus.COMPLETED:
                row.completed_at = now
            if status == ExecutionStatus.FAILED:
                row.completed_at = now
            if status == ExecutionStatus.CANCELLED:
                row.cancelled_at = now
            s.flush()

    def mark_started(self, run_id: str) -> None:
        self.set_status(run_id, ExecutionStatus.RUNNING)

    def set_patch_result(
        self,
        run_id: str,
        *,
        patch_sha256: str,
        artifact_hashes: dict,
        security_validation: str,
    ) -> None:
        with session_scope() as s:
            row = s.get(orm.ExecutionRun, run_id)
            if row is None:
                raise KeyError(run_id)
            row.patch_sha256 = patch_sha256
            row.artifact_hashes = dict(artifact_hashes)
            row.security_validation = security_validation
            s.flush()


    def merge_receipt_context(self, run_id: str, updates: dict) -> None:
        """Merge read-only observation metadata into the run receipt JSON.

        The existing schema has no separate receipt table; artifact_hashes already
        persists executor receipt-adjacent JSON, so CI observation is nested under
        stable keys without a migration.
        """
        with session_scope() as s:
            row = s.get(orm.ExecutionRun, run_id)
            if row is None:
                return
            row.artifact_hashes = {**(row.artifact_hashes or {}), **updates}
            s.flush()

    def touch_reconciled(self, run_id: str) -> None:
        with session_scope() as s:
            row = s.get(orm.ExecutionRun, run_id)
            if row is None:
                return
            row.last_reconciled_at = _utcnow()
            s.flush()

    # -- cancellation -----------------------------------------------------
    def request_cancellation(self, run_id: str) -> None:
        with session_scope() as s:
            row = s.get(orm.ExecutionRun, run_id)
            if row is None:
                return
            if row.cancellation_requested_at is None:
                row.cancellation_requested_at = _utcnow()
            s.flush()

    # -- budget-checked model accounting ---------------------------------
    def reserve_model_call(self, run_id: str) -> Tuple[bool, str]:
        """Atomically claim one model-call slot within budget.

        Returns ``(ok, reason)``. Only succeeds when the run is token-active, the
        token is not revoked, and the call count is below the limit. The
        increment and the check are one UPDATE, so concurrent gateway requests
        cannot together exceed the cap.
        """
        with session_scope() as s:
            row = s.get(orm.ExecutionRun, run_id)
            if row is None:
                return False, "unknown run"
            if row.status not in ExecutionStatus.TOKEN_ACTIVE:
                return False, "run is not active"
            if row.token_revoked_at is not None:
                return False, "token revoked"
            updated = (
                s.query(orm.ExecutionRun)
                .filter(
                    orm.ExecutionRun.id == run_id,
                    orm.ExecutionRun.token_revoked_at.is_(None),
                    orm.ExecutionRun.model_calls < orm.ExecutionRun.max_model_calls,
                    orm.ExecutionRun.input_tokens < orm.ExecutionRun.max_input_tokens,
                    orm.ExecutionRun.output_tokens < orm.ExecutionRun.max_output_tokens,
                    orm.ExecutionRun.cost_usd < orm.ExecutionRun.max_cost_usd,
                )
                .update(
                    {orm.ExecutionRun.model_calls: orm.ExecutionRun.model_calls + 1},
                    synchronize_session=False,
                )
            )
            if updated != 1:
                return False, "model budget exhausted"
            return True, ""

    def record_model_usage(
        self,
        run_id: str,
        *,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        event_id: Optional[str] = None,
    ) -> Tuple[bool, Usage]:
        """Add actual usage and report whether the run is still within budget.

        A returned ``ok=False`` means the run has now exceeded a token/cost limit
        and the caller must revoke the token. ``event_id`` is the correlation key
        attached to the LiteLLM request so the usage callback can be tied back to
        this exact model call.
        """
        with session_scope() as s:
            row = s.get(orm.ExecutionRun, run_id)
            if row is None:
                raise KeyError(run_id)
            row.input_tokens = (row.input_tokens or 0) + max(0, int(input_tokens))
            row.output_tokens = (row.output_tokens or 0) + max(0, int(output_tokens))
            row.cost_usd = (row.cost_usd or 0.0) + max(0.0, float(cost_usd))
            s.add(
                orm.ExecutionModelCall(
                    run_id=run_id,
                    job_id=row.job_id,
                    model=model,
                    input_tokens=int(input_tokens),
                    output_tokens=int(output_tokens),
                    cost_usd=float(cost_usd),
                    event_id=event_id,
                )
            )
            within = (
                row.input_tokens <= row.max_input_tokens
                and row.output_tokens <= row.max_output_tokens
                and row.cost_usd <= row.max_cost_usd
            )
            usage = Usage(
                model_calls=row.model_calls,
                input_tokens=row.input_tokens,
                output_tokens=row.output_tokens,
                cost_usd=row.cost_usd,
            )
            s.flush()
            return within, usage

    def model_calls_for(self, run_id: str) -> List[dict]:
        with session_scope() as s:
            rows = (
                s.query(orm.ExecutionModelCall)
                .filter(orm.ExecutionModelCall.run_id == run_id)
                .order_by(orm.ExecutionModelCall.id)
                .all()
            )
            return [
                {
                    "model": r.model,
                    "input_tokens": r.input_tokens,
                    "output_tokens": r.output_tokens,
                    "cost_usd": r.cost_usd,
                    "created_at": _iso(r.created_at),
                }
                for r in rows
            ]

    # -- events (idempotent) ---------------------------------------------
    def record_event(
        self,
        run_id: str,
        *,
        job_id: str,
        workflow_run_attempt: Optional[int],
        sequence: int,
        idempotency_key: str,
        kind: str,
        payload: dict,
    ) -> bool:
        """Persist an event. Returns False if it was a duplicate (idempotent)."""
        from sqlalchemy.exc import IntegrityError

        with session_scope() as s:
            existing = (
                s.query(orm.ExecutionEvent)
                .filter(
                    orm.ExecutionEvent.run_id == run_id,
                    orm.ExecutionEvent.idempotency_key == idempotency_key,
                )
                .one_or_none()
            )
            if existing is not None:
                return False
            s.add(
                orm.ExecutionEvent(
                    run_id=run_id,
                    job_id=job_id,
                    workflow_run_attempt=workflow_run_attempt,
                    sequence=sequence,
                    idempotency_key=idempotency_key,
                    kind=kind,
                    payload=payload,
                )
            )
            try:
                s.flush()
            except IntegrityError:
                s.rollback()
                return False
        return True

    def events_for(self, run_id: str) -> List[dict]:
        with session_scope() as s:
            rows = (
                s.query(orm.ExecutionEvent)
                .filter(orm.ExecutionEvent.run_id == run_id)
                .order_by(orm.ExecutionEvent.sequence, orm.ExecutionEvent.id)
                .all()
            )
            return [
                {
                    "sequence": r.sequence,
                    "kind": r.kind,
                    "payload": dict(r.payload or {}),
                    "created_at": _iso(r.created_at),
                }
                for r in rows
            ]

    # -- reconciliation queries ------------------------------------------
    def active_runs(self) -> List[ExecutionRunRecord]:
        """Runs not in a terminal state (candidates for reconciliation)."""
        with session_scope() as s:
            rows = (
                s.query(orm.ExecutionRun)
                .filter(
                    orm.ExecutionRun.status.notin_(list(ExecutionStatus.TERMINAL))
                )
                .order_by(orm.ExecutionRun.created_at)
                .all()
            )
            return [_to_record(r) for r in rows]
