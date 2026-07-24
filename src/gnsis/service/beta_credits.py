"""Operator-issued beta credits — the minimal manual top-up path for the beta.

Before Stripe self-service, an operator grants a fixed credit to a workspace.
The money is a normal positive row in the prepaid balance ledger
(``balance_transactions``), so the existing balance / available / reservation /
spending-limit machinery applies unchanged — when the granted balance is spent,
new paid activity is blocked by the same enforcement that already exists.

Every grant also writes a ``beta_credit_grants`` audit row (operator, reason,
amount, idempotency key) in the *same* transaction as the ledger credit, so the
two can never diverge. Guarantees:

* **Idempotent** — a re-sent grant (same ``idempotency_key``) is a no-op that
  returns the original; it never double-credits.
* **Capped** — a grant above ``settings.beta_credit_max_usd`` is rejected.
* **Reversible** — a mistaken grant is undone by a *compensating* negative
  ledger transaction, never by editing or deleting the original rows. Reversal
  is itself idempotent.

Access is gated by the internal API key at the route layer; ``operator`` is an
attested identity recorded for the audit trail, not an authenticated principal.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy.exc import IntegrityError

from . import orm, rates
from .db import session_scope
from ..orchestration.models import new_id

BETA_GRANT = "beta_grant"
BETA_GRANT_REVERSAL = "beta_grant_reversal"


class BetaCreditError(Exception):
    """A grant/reversal was rejected for a caller-fixable reason."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _grant_view(row: orm.BetaCreditGrant, *, duplicate: bool = False) -> dict:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "amount": row.amount,
        "currency": row.currency,
        "reason": row.reason,
        "operator": row.operator,
        "status": row.status,
        "transaction_id": row.transaction_id,
        "reversal_transaction_id": row.reversal_transaction_id,
        "reversed_by": row.reversed_by,
        "reversed_reason": row.reversed_reason,
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "reversed_at": row.reversed_at.isoformat() if row.reversed_at else None,
        "duplicate": duplicate,
    }


def _positive_amount(amount, max_amount) -> Decimal:
    try:
        amt = Decimal(str(amount))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise BetaCreditError("amount is not a valid number") from exc
    if amt <= 0:
        raise BetaCreditError("amount must be greater than 0")
    try:
        cap = Decimal(str(max_amount))
    except (InvalidOperation, ValueError, TypeError):
        cap = Decimal("50.00")
    if amt > cap:
        raise BetaCreditError(f"amount {amt} exceeds the maximum beta grant of {cap}")
    return amt


def grant_credit(
    *,
    workspace_id: str,
    amount,
    reason: str,
    operator: str,
    idempotency_key: str,
    max_amount,
    currency: str = "USD",
) -> dict:
    """Grant a beta credit. Idempotent on ``idempotency_key``."""
    workspace_id = (workspace_id or "").strip()
    reason = (reason or "").strip()
    operator = (operator or "").strip()
    idempotency_key = (idempotency_key or "").strip()
    if not workspace_id:
        raise BetaCreditError("workspace_id is required")
    if not reason:
        raise BetaCreditError("reason is required")
    if not operator:
        raise BetaCreditError("operator is required")
    if not idempotency_key:
        raise BetaCreditError("idempotency_key is required")
    amt = _positive_amount(amount, max_amount)

    with session_scope() as s:
        existing = (
            s.query(orm.BetaCreditGrant)
            .filter(orm.BetaCreditGrant.idempotency_key == idempotency_key)
            .one_or_none()
        )
        if existing is not None:
            return _grant_view(existing, duplicate=True)

        txn_id = new_id("txn")
        grant = orm.BetaCreditGrant(
            id=new_id("grant"),
            workspace_id=workspace_id,
            amount=rates.to_money_str(amt),
            currency=currency,
            reason=reason,
            operator=operator,
            idempotency_key=idempotency_key,
            status="granted",
            transaction_id=txn_id,
        )
        txn = orm.BalanceTransaction(
            id=txn_id,
            workspace_id=workspace_id,
            transaction_type=BETA_GRANT,
            signed_amount=rates.to_money_str(amt),
            # Namespace the ledger idempotency key so it can't collide with a
            # Stripe/adjustment key that happens to share the same string.
            idempotency_key=f"{BETA_GRANT}:{idempotency_key}",
            currency=currency,
        )
        s.add(grant)
        s.add(txn)
        try:
            s.flush()
        except IntegrityError:
            s.rollback()
            existing = (
                s.query(orm.BetaCreditGrant)
                .filter(orm.BetaCreditGrant.idempotency_key == idempotency_key)
                .one()
            )
            return _grant_view(existing, duplicate=True)
        return _grant_view(grant, duplicate=False)


def reverse_grant(*, grant_id: str, operator: str, reason: str) -> dict:
    """Reverse a grant with a compensating negative transaction. Idempotent."""
    operator = (operator or "").strip()
    reason = (reason or "").strip()
    if not operator:
        raise BetaCreditError("operator is required")

    with session_scope() as s:
        grant = s.get(orm.BetaCreditGrant, (grant_id or "").strip())
        if grant is None:
            raise BetaCreditError("grant not found")
        if grant.status == "reversed":
            return _grant_view(grant, duplicate=True)

        rev_id = new_id("txn")
        rev = orm.BalanceTransaction(
            id=rev_id,
            workspace_id=grant.workspace_id,
            transaction_type=BETA_GRANT_REVERSAL,
            signed_amount=rates.to_money_str(-Decimal(grant.amount)),
            idempotency_key=f"{BETA_GRANT_REVERSAL}:{grant.id}",
            currency=grant.currency,
        )
        grant.status = "reversed"
        grant.reversal_transaction_id = rev_id
        grant.reversed_by = operator
        grant.reversed_reason = reason
        grant.reversed_at = _utcnow()
        s.add(rev)
        try:
            s.flush()
        except IntegrityError:
            s.rollback()
            refreshed = s.get(orm.BetaCreditGrant, grant.id)
            return _grant_view(refreshed, duplicate=True)
        return _grant_view(grant, duplicate=False)


def workspace_summary(workspace_id: str, *, limit: int = 100) -> dict:
    """Balance + granted history for an operator to inspect one workspace."""
    from .billing import BillingStore

    billing = BillingStore()
    with session_scope() as s:
        grants = (
            s.query(orm.BetaCreditGrant)
            .filter(orm.BetaCreditGrant.workspace_id == workspace_id)
            .order_by(orm.BetaCreditGrant.created_at.desc())
            .limit(limit)
            .all()
        )
        grant_views = [_grant_view(g) for g in grants]
    return {
        "workspace_id": workspace_id,
        "balance": str(billing.balance(workspace_id)),
        "available": str(billing.available(workspace_id)),
        "grants": grant_views,
    }
