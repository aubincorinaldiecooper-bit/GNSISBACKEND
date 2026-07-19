"""Configurable, concurrency-safe spending limits.

Policies are opt-in and tunable (never globally disabled): each has an
enforcement mode — ``observe_only`` (record only), ``warn``, or ``block``. For a
request, the engine finds **every** applicable policy across the request's scopes
(workspace / project / environment / user / team / virtual key), plus the virtual
key's own inline limits, computes committed + in-flight spend for each window, and
applies the **most restrictive** valid one. To stop concurrent requests all
spending the same remaining allowance, evaluation runs under the per-workspace
lock and places a per-scope reservation for the request's estimated exposure;
the charge that lands later replaces the hold. Every decision is recorded.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional

from . import orm
from .db import session_scope
from .rates import to_money_str
from ..orchestration.models import new_id

SCOPE_TYPES = ("workspace", "project", "environment", "user", "team", "virtual_key")
LIMIT_TYPES = ("per_run", "daily", "monthly", "total")
MODES = ("observe_only", "warn", "block")
_ZERO = Decimal("0")


class LimitError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class PolicyView:
    id: str
    workspace_id: str
    enabled: bool
    scope_type: str
    scope_id: str
    limit_type: str
    amount: str
    currency: str
    warning_threshold: Optional[str]
    enforcement_mode: str
    reset_period: str
    effective_at: Optional[str]
    expires_at: Optional[str]


@dataclass(frozen=True)
class _Applicable:
    """A real or synthetic (key-inline) policy to evaluate."""
    policy_id: Optional[str]
    policy_ref: Optional[str]
    scope_type: str
    scope_id: str
    limit_type: str
    amount: Decimal
    warning_threshold: Optional[Decimal]
    enforcement_mode: str


@dataclass(frozen=True)
class EvalResult:
    result: str                      # ok | warn | block
    block_scope: Optional[str] = None
    block_limit_id: Optional[str] = None
    warnings: Optional[List[str]] = None


@dataclass(frozen=True)
class LimitContext:
    workspace_id: str
    run_id: str
    project_id: Optional[str] = None
    environment_id: Optional[str] = None
    user_id: Optional[str] = None
    team_id: Optional[str] = None
    virtual_key_id: Optional[str] = None
    key_limits: Optional[Dict[str, Optional[str]]] = None  # soft/hard/per_run/daily/monthly


def _dec(v, field: str) -> Decimal:
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise LimitError(f"{field} is not a valid amount") from exc
    if d.is_nan() or d.is_infinite() or d < 0:
        raise LimitError(f"{field} must be a non-negative amount")
    return d


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _policy_view(p: orm.LimitPolicy) -> PolicyView:
    return PolicyView(
        id=p.id, workspace_id=p.workspace_id, enabled=p.enabled, scope_type=p.scope_type,
        scope_id=p.scope_id, limit_type=p.limit_type, amount=p.amount, currency=p.currency,
        warning_threshold=p.warning_threshold, enforcement_mode=p.enforcement_mode,
        reset_period=p.reset_period,
        effective_at=p.effective_at.isoformat() if p.effective_at else None,
        expires_at=p.expires_at.isoformat() if p.expires_at else None,
    )


_SCOPE_COL = {
    "workspace": "workspace_id", "project": "project_id", "environment": "environment",
    "user": "user_id", "team": "team_id", "virtual_key": "virtual_key_id",
}


def _window(limit_type: str, now: datetime, run_id: str):
    """(window_start, window_key) for a limit type."""
    if limit_type == "per_run":
        return None, f"run:{run_id}"
    if limit_type == "daily":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, f"day:{start.date().isoformat()}"
    if limit_type == "monthly":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return start, f"month:{start.strftime('%Y-%m')}"
    return None, "total"


class LimitStore:
    """CRUD for limit policies (workspace-scoped)."""

    def create(self, *, workspace_id, scope_type, scope_id, limit_type, amount,
               enforcement_mode="block", warning_threshold=None, reset_period=None,
               currency="USD", effective_at=None, expires_at=None) -> PolicyView:
        if scope_type not in SCOPE_TYPES:
            raise LimitError(f"scope_type must be one of {SCOPE_TYPES}")
        if limit_type not in LIMIT_TYPES:
            raise LimitError(f"limit_type must be one of {LIMIT_TYPES}")
        if enforcement_mode not in MODES:
            raise LimitError(f"enforcement_mode must be one of {MODES}")
        amt = _dec(amount, "amount")
        thr = None
        if warning_threshold not in (None, ""):
            thr = _dec(warning_threshold, "warning_threshold")
            if thr > 1:
                raise LimitError("warning_threshold is a fraction between 0 and 1")
        with session_scope() as s:
            p = orm.LimitPolicy(
                id=new_id("limit"), workspace_id=workspace_id, enabled=True,
                scope_type=scope_type, scope_id=scope_id, limit_type=limit_type,
                amount=to_money_str(amt), currency=currency,
                warning_threshold=(str(thr) if thr is not None else None),
                enforcement_mode=enforcement_mode, reset_period=(reset_period or limit_type),
                effective_at=_aware(effective_at), expires_at=_aware(expires_at),
            )
            s.add(p)
            s.flush()
            return _policy_view(p)

    def list_for_workspace(self, workspace_id: str, *, limit: int = 200) -> List[PolicyView]:
        with session_scope() as s:
            rows = (
                s.query(orm.LimitPolicy)
                .filter(orm.LimitPolicy.workspace_id == workspace_id)
                .order_by(orm.LimitPolicy.created_at.desc())
                .limit(limit)
                .all()
            )
            return [_policy_view(r) for r in rows]

    def update(self, workspace_id: str, policy_id: str, **fields) -> PolicyView:
        allowed = {"enabled", "amount", "warning_threshold", "enforcement_mode",
                   "expires_at", "effective_at"}
        with session_scope() as s:
            p = s.get(orm.LimitPolicy, policy_id)
            if p is None or p.workspace_id != workspace_id:
                raise LimitError("limit policy not found", status=404)
            for k, v in fields.items():
                if v is None or k not in allowed:
                    continue
                if k == "amount":
                    p.amount = to_money_str(_dec(v, "amount"))
                elif k == "warning_threshold":
                    p.warning_threshold = str(_dec(v, "warning_threshold"))
                elif k == "enforcement_mode":
                    if v not in MODES:
                        raise LimitError(f"enforcement_mode must be one of {MODES}")
                    p.enforcement_mode = v
                elif k in ("expires_at", "effective_at"):
                    setattr(p, k, _aware(v))
                elif k == "enabled":
                    p.enabled = bool(v)
            s.flush()
            return _policy_view(p)

    def disable(self, workspace_id: str, policy_id: str) -> PolicyView:
        return self.update(workspace_id, policy_id, enabled=False)


class PolicyEngine:
    """Evaluate + reserve + reconcile spending policies."""

    def _applicable(self, s, ctx: LimitContext) -> List[_Applicable]:
        out: List[_Applicable] = []
        # Real policies whose scope matches a value present on this request.
        scope_values = {
            "workspace": ctx.workspace_id, "project": ctx.project_id,
            "environment": ctx.environment_id, "user": ctx.user_id,
            "team": ctx.team_id, "virtual_key": ctx.virtual_key_id,
        }
        rows = (
            s.query(orm.LimitPolicy)
            .filter(orm.LimitPolicy.workspace_id == ctx.workspace_id,
                    orm.LimitPolicy.enabled.is_(True))
            .all()
        )
        now = datetime.now(timezone.utc)
        for p in rows:
            sv = scope_values.get(p.scope_type)
            if not sv or sv != p.scope_id:
                continue
            if _aware(p.effective_at) and _aware(p.effective_at) > now:
                continue
            if _aware(p.expires_at) and _aware(p.expires_at) <= now:
                continue
            out.append(_Applicable(
                policy_id=p.id, policy_ref=None, scope_type=p.scope_type, scope_id=p.scope_id,
                limit_type=p.limit_type, amount=Decimal(p.amount or "0"),
                warning_threshold=(Decimal(p.warning_threshold) if p.warning_threshold else None),
                enforcement_mode=p.enforcement_mode,
            ))
        # Synthetic policies from the virtual key's own inline limits.
        kl = ctx.key_limits or {}
        if ctx.virtual_key_id:
            for field, ltype, mode in (
                ("hard_limit", "total", "block"), ("soft_limit", "total", "warn"),
                ("per_run_limit", "per_run", "block"), ("daily_limit", "daily", "block"),
                ("monthly_limit", "monthly", "block"),
            ):
                v = kl.get(field)
                if v in (None, ""):
                    continue
                out.append(_Applicable(
                    policy_id=None, policy_ref=f"key:{field}", scope_type="virtual_key",
                    scope_id=ctx.virtual_key_id, limit_type=ltype, amount=Decimal(str(v)),
                    warning_threshold=None, enforcement_mode=mode,
                ))
        return out

    def _committed_spend(self, s, scope_type, scope_id, limit_type, window_start, run_id) -> Decimal:
        col = getattr(orm.UsageRecord, _SCOPE_COL[scope_type])
        q = (
            s.query(orm.UsageCharge.retail_cost)
            .join(orm.UsageRecord, orm.UsageCharge.usage_record_id == orm.UsageRecord.id)
            .filter(col == scope_id)
        )
        if limit_type == "per_run":
            q = q.filter(orm.UsageRecord.run_id == run_id)
        elif window_start is not None:
            q = q.filter(orm.UsageCharge.created_at >= window_start)
        return sum((Decimal(r[0] or "0") for r in q.all()), _ZERO)

    def _active_holds(self, s, scope_type, scope_id, window_key) -> Decimal:
        rows = (
            s.query(orm.LimitReservation.amount)
            .filter(orm.LimitReservation.scope_type == scope_type,
                    orm.LimitReservation.scope_id == scope_id,
                    orm.LimitReservation.window_key == window_key,
                    orm.LimitReservation.status == "active")
            .all()
        )
        return sum((Decimal(r[0] or "0") for r in rows), _ZERO)

    def evaluate(self, settings, ctx: LimitContext, estimated_cost, request_id: str) -> EvalResult:
        """Deterministic, concurrency-safe pre-request evaluation.

        Blocks (most-restrictive-wins) when a ``block`` policy would be exceeded;
        otherwise places per-scope holds so concurrent requests can't overspend.
        """
        estimate = Decimal(str(estimated_cost))
        now = datetime.now(timezone.utc)
        with session_scope() as s:
            # Serialise all evaluation for this workspace on its billing anchor.
            anchor = s.get(orm.WorkspaceBilling, ctx.workspace_id)
            if anchor is None:
                anchor = orm.WorkspaceBilling(workspace_id=ctx.workspace_id)
                s.add(anchor)
                s.flush()
            s.query(orm.WorkspaceBilling).filter(
                orm.WorkspaceBilling.workspace_id == ctx.workspace_id
            ).with_for_update().all()

            applicable = self._applicable(s, ctx)
            overall = "ok"
            block_scope = block_limit = None
            warnings: List[str] = []
            # Keyed by (scope_type, scope_id, window_key): several policies can share
            # one scope+window (e.g. a key's soft *and* hard limit are both
            # virtual_key/total), and they place a single hold for the request's
            # estimated exposure — not one per policy (which would both double-count
            # the in-flight exposure and violate the reservation's uniqueness).
            to_hold: Dict[tuple, None] = {}
            decisions = []

            for a in applicable:
                window_start, window_key = _window(a.limit_type, now, ctx.run_id)
                committed = self._committed_spend(s, a.scope_type, a.scope_id, a.limit_type, window_start, ctx.run_id)
                held = self._active_holds(s, a.scope_type, a.scope_id, window_key)
                projected = committed + held + estimate
                exceeded = projected > a.amount
                warn_at = (a.amount * a.warning_threshold) if a.warning_threshold is not None else None

                if exceeded and a.enforcement_mode == "block":
                    result = "block"
                    overall = "block"
                    block_scope, block_limit = a.scope_type, (a.policy_id or a.policy_ref)
                elif exceeded and a.enforcement_mode == "warn":
                    result = "warn"
                    overall = "warn" if overall != "block" else overall
                    warnings.append(f"{a.scope_type} {a.limit_type} limit exceeded")
                elif exceeded:  # observe_only
                    result = "observe"
                elif warn_at is not None and projected >= warn_at and a.enforcement_mode in ("warn", "block"):
                    result = "warn"
                    overall = "warn" if overall != "block" else overall
                    warnings.append(f"{a.scope_type} {a.limit_type} nearing limit")
                else:
                    result = "ok"

                decisions.append((a, committed, result))
                if result != "block" and a.enforcement_mode in ("warn", "block"):
                    to_hold[(a.scope_type, a.scope_id, window_key)] = None

            # Persist every decision (audit).
            for a, committed, result in decisions:
                s.add(orm.LimitDecision(
                    id=new_id("ldec"), request_id=request_id, workspace_id=ctx.workspace_id,
                    policy_id=a.policy_id, policy_ref=a.policy_ref, scope_type=a.scope_type,
                    scope_id=a.scope_id, limit_type=a.limit_type, amount=to_money_str(a.amount),
                    previous_usage=to_money_str(committed), reserved_amount=to_money_str(estimate),
                    enforcement_mode=a.enforcement_mode, result=result,
                ))

            if overall == "block":
                s.flush()  # keep the audit trail even on a block
                return EvalResult(result="block", block_scope=block_scope, block_limit_id=block_limit)

            # Place holds so concurrent requests see this request's exposure — one
            # per distinct (scope, window), deduplicated above.
            for scope_type, scope_id, window_key in to_hold:
                s.add(orm.LimitReservation(
                    id=new_id("lresv"), reservation_key=request_id, workspace_id=ctx.workspace_id,
                    scope_type=scope_type, scope_id=scope_id, window_key=window_key,
                    amount=to_money_str(estimate), status="active",
                ))
            s.flush()
            return EvalResult(result=overall, warnings=warnings or None)

    def _finish_holds(self, request_id: str, status: str, actual_cost=None) -> None:
        with session_scope() as s:
            rows = (
                s.query(orm.LimitReservation)
                .filter(orm.LimitReservation.reservation_key == request_id,
                        orm.LimitReservation.status == "active")
                .all()
            )
            for r in rows:
                r.status = status
            if actual_cost is not None:
                for d in (
                    s.query(orm.LimitDecision)
                    .filter(orm.LimitDecision.request_id == request_id)
                    .all()
                ):
                    d.actual_usage = to_money_str(Decimal(str(actual_cost)))
            s.flush()

    def reconcile(self, request_id: str, actual_cost) -> None:
        """The real charge has landed → release the in-flight holds (it now counts
        in committed spend) and record actual usage on the decisions."""
        self._finish_holds(request_id, "settled", actual_cost)

    def release(self, request_id: str) -> None:
        """Request failed/aborted → drop the holds."""
        self._finish_holds(request_id, "released")
