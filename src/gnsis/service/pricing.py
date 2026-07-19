"""Versioned model pricing + provider-vs-Genesis cost separation.

Prices live in a time-versioned table instead of being hardcoded. Each usage
event references the pricing version that was effective when it happened, so a
later price change never rewrites historical cost. The provider-reported cost and
the Genesis-calculated cost are kept as **separate** values; a meaningful
discrepancy is flagged (reason ``cost_discrepancy``) without overwriting either.

Cost is billed on the best available basis: the provider-reported cost when
known, otherwise the Genesis-calculated cost. A row with **neither** is left
``needs_reconciliation`` and is never charged a silent $0.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import List, Optional

from . import orm
from .db import session_scope
from .rates import to_money_str
from ..orchestration.models import new_id

# Relative tolerance before a provider-vs-calculated gap is flagged.
_DISCREPANCY_TOLERANCE = Decimal("0.05")  # 5%
_ZERO = Decimal("0")
# Request statuses that denote a completed (billable) request.
_SUCCESS_STATES = frozenset({"success", "succeeded", "ok", "completed"})


class PricingError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class PricingView:
    id: str
    provider: str
    model: str
    input_price: str
    output_price: str
    cached_input_price: Optional[str]
    reasoning_price: Optional[str]
    currency: str
    effective_start: str
    effective_end: Optional[str]
    source: Optional[str]


def _dec(value, field: str) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise PricingError(f"{field} is not a valid price") from exc
    if d.is_nan() or d.is_infinite() or d < 0:
        raise PricingError(f"{field} must be a non-negative number")
    return d


def _view(row: orm.ModelPricing) -> PricingView:
    return PricingView(
        id=row.id, provider=row.provider, model=row.model,
        input_price=row.input_price, output_price=row.output_price,
        cached_input_price=row.cached_input_price, reasoning_price=row.reasoning_price,
        currency=row.currency,
        effective_start=row.effective_start.isoformat() if row.effective_start else "",
        effective_end=row.effective_end.isoformat() if row.effective_end else None,
        source=row.source,
    )


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


class PricingStore:
    def add_price(
        self, *, provider: str, model: str, input_price, output_price,
        cached_input_price=None, reasoning_price=None, currency: str = "USD",
        effective_start: Optional[datetime] = None, source: Optional[str] = None,
    ) -> PricingView:
        """Insert a new price version, closing the previously-open one for this
        (provider, model) at the new start so windows never overlap."""
        if not provider or not model:
            raise PricingError("provider and model are required")
        _dec(input_price, "input_price"); _dec(output_price, "output_price")
        if cached_input_price is not None:
            _dec(cached_input_price, "cached_input_price")
        if reasoning_price is not None:
            _dec(reasoning_price, "reasoning_price")
        start = _aware(effective_start) or datetime.now(timezone.utc)
        with session_scope() as s:
            open_rows = (
                s.query(orm.ModelPricing)
                .filter(
                    orm.ModelPricing.provider == provider,
                    orm.ModelPricing.model == model,
                    orm.ModelPricing.effective_end.is_(None),
                )
                .all()
            )
            for row in open_rows:
                row.effective_end = start
            new = orm.ModelPricing(
                id=new_id("price"), provider=provider, model=model,
                input_price=str(input_price), output_price=str(output_price),
                cached_input_price=str(cached_input_price) if cached_input_price is not None else None,
                reasoning_price=str(reasoning_price) if reasoning_price is not None else None,
                currency=currency, effective_start=start, source=source,
            )
            s.add(new)
            s.flush()
            return _view(new)

    def resolve(self, provider: str, model: str, at: Optional[datetime] = None) -> Optional[PricingView]:
        """The price version effective at ``at`` (default now), or None."""
        at = _aware(at) or datetime.now(timezone.utc)
        with session_scope() as s:
            rows = (
                s.query(orm.ModelPricing)
                .filter(orm.ModelPricing.provider == provider, orm.ModelPricing.model == model)
                .all()
            )
        candidates = []
        for r in rows:
            start = _aware(r.effective_start)
            end = _aware(r.effective_end)
            if start is not None and start <= at and (end is None or at < end):
                candidates.append((start, r))
        if not candidates:
            return None
        candidates.sort(key=lambda t: t[0])
        return _view(candidates[-1][1])

    def get(self, pricing_id: str) -> Optional[PricingView]:
        with session_scope() as s:
            row = s.get(orm.ModelPricing, pricing_id)
            return _view(row) if row else None

    def list_current(self, provider: Optional[str] = None) -> List[PricingView]:
        with session_scope() as s:
            q = s.query(orm.ModelPricing).filter(orm.ModelPricing.effective_end.is_(None))
            if provider:
                q = q.filter(orm.ModelPricing.provider == provider)
            return [_view(r) for r in q.order_by(orm.ModelPricing.provider, orm.ModelPricing.model).all()]


def calculate_cost(
    pricing: PricingView, *, input_tokens: int, output_tokens: int,
    cached_tokens: int = 0, reasoning_tokens: int = 0,
) -> str:
    """Genesis-calculated provider cost from token counts × the version's prices.

    Per OpenAI/LiteLLM usage semantics ``cached_tokens`` is the cached subset
    *within* ``input_tokens`` and ``reasoning_tokens`` the reasoning subset
    *within* ``output_tokens`` — not separate token counts. So each subset is
    priced at its special rate and only the **remainder** at the base rate; the
    subset is never added on top of the aggregate, which would double-charge
    those tokens (and spuriously inflate the provider-vs-Genesis discrepancy).
    """
    inp = Decimal(pricing.input_price or "0")
    out = Decimal(pricing.output_price or "0")
    cached = Decimal(pricing.cached_input_price) if pricing.cached_input_price else inp
    reasoning = Decimal(pricing.reasoning_price) if pricing.reasoning_price else out
    # Clamp each subset to its aggregate so the two parts always sum back to the
    # aggregate, even on malformed provider detail counts (never a negative
    # remainder, never more cached tokens than prompt tokens).
    in_total = max(input_tokens, 0)
    out_total = max(output_tokens, 0)
    cached_billed = min(max(cached_tokens, 0), in_total)
    reasoning_billed = min(max(reasoning_tokens, 0), out_total)
    total = (
        Decimal(in_total - cached_billed) * inp
        + Decimal(cached_billed) * cached
        + Decimal(out_total - reasoning_billed) * out
        + Decimal(reasoning_billed) * reasoning
    )
    return to_money_str(total)


def _is_discrepant(provider_cost: Decimal, genesis_cost: Decimal) -> bool:
    biggest = max(provider_cost, genesis_cost)
    if biggest <= _ZERO:
        return False
    return (abs(provider_cost - genesis_cost) / biggest) > _DISCREPANCY_TOLERANCE


def price_usage_record(settings, usage_record_id: str) -> None:
    """Compute + store the Genesis cost for a usage row and reconcile its state.

    - priced + provider cost known → store genesis cost; flag ``cost_discrepancy``
      (informational, still billable on the provider figure) if they diverge.
    - priced + provider cost unknown + usage measured → store genesis cost;
      **resolve** (billable on the genesis figure).
    - priced + provider cost unknown + **no usage reported** on a *successful*
      request → ``needs_reconciliation`` (missing_usage). A $0 cost computed from
      zero tokens is the absence of a measurement, not a real basis, so it is
      never settled as free (e.g. a stream that ended without its usage chunk).
    - unpriced + provider cost unknown → ``needs_reconciliation`` (unknown_pricing).
    - unpriced + provider cost known → stays billable on the provider figure.
    """
    with session_scope() as s:
        u = s.get(orm.UsageRecord, usage_record_id)
        if u is None:
            return
        store = PricingStore()
        pricing = store.resolve(u.provider, u.model, at=_aware(u.created_at))
        provider_known = getattr(u, "cost_source", "provider_reported") == "provider_reported"

        if pricing is not None:
            # A successful request whose provider cost is unknown *and* that
            # reported no tokens at all has no real cost basis — pricing zero
            # tokens yields $0, which would silently settle a billable request as
            # free. Keep it flagged so the missing usage is reconciled, not buried.
            # (A *failed* request legitimately has no usage → falls through to $0.)
            succeeded = (u.request_status or "") in _SUCCESS_STATES
            measured_tokens = (
                (u.input_tokens or 0) + (u.output_tokens or 0)
                + (u.cached_tokens or 0) + (u.reasoning_tokens or 0)
            )
            if not provider_known and succeeded and measured_tokens == 0:
                u.reconciliation_state = "needs_reconciliation"
                u.reconciliation_reason = "missing_usage"
                s.flush()
                return

            genesis = calculate_cost(
                pricing, input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                cached_tokens=u.cached_tokens, reasoning_tokens=u.reasoning_tokens,
            )
            u.genesis_calculated_cost = genesis
            u.pricing_version_id = pricing.id
            if provider_known:
                if _is_discrepant(Decimal(u.upstream_cost or "0"), Decimal(genesis)):
                    u.reconciliation_reason = "cost_discrepancy"
                else:
                    u.reconciliation_reason = None
                # Provider figure stays authoritative → billable.
                u.reconciliation_state = "resolved"
            else:
                # We now have a cost basis (the calculated cost) → billable.
                u.reconciliation_state = "resolved"
                u.reconciliation_reason = None
        else:
            if not provider_known:
                u.reconciliation_state = "needs_reconciliation"
                u.reconciliation_reason = "unknown_pricing"
            # else: provider cost known, no pricing row — remains billable as-is.
        s.flush()
