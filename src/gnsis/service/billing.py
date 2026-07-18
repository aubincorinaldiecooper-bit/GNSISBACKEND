"""Billing — turn measured usage into an immutable charge + a prepaid balance move.

Everything monetary is :class:`decimal.Decimal`, stored as exact decimal strings.
The balance is *derived* from the ledger (sum of signed amounts), never a mutable
field. Charges are immutable historical facts: each stores the exact rate it
applied, so changing the current markup never alters an old statement. Idempotency
is enforced structurally — unique ``usage_record_id`` per charge, unique
``idempotency_key``/``stripe_event_id`` per ledger row, unique reservation key —
so duplicate callbacks/webhooks and races cannot double-charge or double-credit.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import List, Optional, Tuple

from sqlalchemy.exc import IntegrityError

from . import orm
from .db import session_scope
from . import rates
from ..orchestration.models import new_id

# Ledger transaction types.
TOP_UP = "top_up"
USAGE_DEBIT = "usage_debit"
CREDIT = "credit"
REFUND = "refund"
ADJUSTMENT = "adjustment"

_ZERO = Decimal("0")


class BillingError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class TransactionView:
    id: str
    workspace_id: str
    transaction_type: str
    signed_amount: str
    usage_charge_id: Optional[str]
    stripe_event_id: Optional[str]
    stripe_payment_reference: Optional[str]
    idempotency_key: str
    currency: str
    created_at: str = ""

    @property
    def amount(self) -> Decimal:
        return Decimal(self.signed_amount or "0")


@dataclass(frozen=True)
class ChargeView:
    id: str
    usage_record_id: str
    workspace_id: str
    upstream_cost: str
    markup_rate: str
    service_fee: str
    retail_cost: str
    currency: str
    rate_card_version: str
    billing_status: str
    run_id: Optional[str] = None
    repository_id: Optional[str] = None
    application_name: Optional[str] = None
    created_at: str = ""


def _txn_view(row: orm.BalanceTransaction) -> TransactionView:
    return TransactionView(
        id=row.id, workspace_id=row.workspace_id, transaction_type=row.transaction_type,
        signed_amount=row.signed_amount, usage_charge_id=row.usage_charge_id,
        stripe_event_id=row.stripe_event_id, stripe_payment_reference=row.stripe_payment_reference,
        idempotency_key=row.idempotency_key, currency=row.currency,
        created_at=row.created_at.isoformat() if row.created_at else "",
    )


def _charge_view(row: orm.UsageCharge) -> ChargeView:
    return ChargeView(
        id=row.id, usage_record_id=row.usage_record_id, workspace_id=row.workspace_id,
        upstream_cost=row.upstream_cost, markup_rate=row.markup_rate, service_fee=row.service_fee,
        retail_cost=row.retail_cost, currency=row.currency, rate_card_version=row.rate_card_version,
        billing_status=row.billing_status, run_id=row.run_id, repository_id=row.repository_id,
        application_name=row.application_name,
        created_at=row.created_at.isoformat() if row.created_at else "",
    )


class BillingStore:
    """Durable ledger, charges, and reservations. Workspace-isolated."""

    # -- balance derivation ----------------------------------------------
    def balance(self, workspace_id: str) -> Decimal:
        with session_scope() as s:
            rows = (
                s.query(orm.BalanceTransaction.signed_amount)
                .filter(orm.BalanceTransaction.workspace_id == workspace_id)
                .all()
            )
            return sum((Decimal(r[0] or "0") for r in rows), _ZERO)

    def _active_holds(self, s, workspace_id: str) -> Decimal:
        rows = (
            s.query(orm.BalanceReservation.amount)
            .filter(
                orm.BalanceReservation.workspace_id == workspace_id,
                orm.BalanceReservation.status == "active",
            )
            .all()
        )
        return sum((Decimal(r[0] or "0") for r in rows), _ZERO)

    def available(self, workspace_id: str) -> Decimal:
        """Balance minus active reservation holds."""
        with session_scope() as s:
            bal = sum(
                (
                    Decimal(r[0] or "0")
                    for r in s.query(orm.BalanceTransaction.signed_amount)
                    .filter(orm.BalanceTransaction.workspace_id == workspace_id)
                    .all()
                ),
                _ZERO,
            )
            return bal - self._active_holds(s, workspace_id)

    def transactions(self, workspace_id: str, *, limit: int = 200) -> List[TransactionView]:
        with session_scope() as s:
            rows = (
                s.query(orm.BalanceTransaction)
                .filter(orm.BalanceTransaction.workspace_id == workspace_id)
                .order_by(orm.BalanceTransaction.created_at.desc())
                .limit(limit)
                .all()
            )
            return [_txn_view(r) for r in rows]

    # -- ledger writes (idempotent) --------------------------------------
    def _find_existing_txn(self, s, idempotency_key, stripe_event_id, payment_reference):
        """Return an existing ledger row matching any idempotency dimension.

        Three independent guards converge to the first winner: the caller's
        ``idempotency_key`` (structural), the Stripe *event* id (redelivery/retry
        of the same event), and the *payment* reference (two different events for
        the same underlying payment). Any hit means "already credited".
        """
        row = (
            s.query(orm.BalanceTransaction)
            .filter(orm.BalanceTransaction.idempotency_key == idempotency_key)
            .one_or_none()
        )
        if row is None and stripe_event_id:
            row = (
                s.query(orm.BalanceTransaction)
                .filter(orm.BalanceTransaction.stripe_event_id == stripe_event_id)
                .one_or_none()
            )
        if row is None and payment_reference:
            row = (
                s.query(orm.BalanceTransaction)
                .filter(orm.BalanceTransaction.payment_reference == payment_reference)
                .one_or_none()
            )
        return row

    def _add_transaction(
        self,
        *,
        workspace_id: str,
        transaction_type: str,
        signed_amount: Decimal,
        idempotency_key: str,
        stripe_event_id: Optional[str] = None,
        stripe_payment_reference: Optional[str] = None,
        payment_reference: Optional[str] = None,
        usage_charge_id: Optional[str] = None,
        currency: str = "USD",
    ) -> Tuple[TransactionView, bool]:
        with session_scope() as s:
            existing = self._find_existing_txn(s, idempotency_key, stripe_event_id, payment_reference)
            if existing is not None:
                return _txn_view(existing), False
            row = orm.BalanceTransaction(
                id=new_id("txn"),
                workspace_id=workspace_id,
                transaction_type=transaction_type,
                signed_amount=rates.to_money_str(signed_amount),
                usage_charge_id=usage_charge_id,
                stripe_event_id=stripe_event_id,
                stripe_payment_reference=stripe_payment_reference,
                payment_reference=payment_reference,
                idempotency_key=idempotency_key,
                currency=currency,
            )
            s.add(row)
            try:
                s.flush()
            except IntegrityError:
                # Lost a race on any unique guard — converge to the winner.
                s.rollback()
                existing = self._find_existing_txn(s, idempotency_key, stripe_event_id, payment_reference)
                return _txn_view(existing), False
            return _txn_view(row), True

    def top_up(
        self, workspace_id: str, amount, *, idempotency_key: str,
        stripe_event_id: Optional[str] = None, stripe_payment_reference: Optional[str] = None,
        payment_reference: Optional[str] = None, currency: str = "USD",
    ) -> Tuple[TransactionView, bool]:
        amt = Decimal(str(amount))
        if amt <= 0:
            raise BillingError("top-up amount must be positive")
        return self._add_transaction(
            workspace_id=workspace_id, transaction_type=TOP_UP, signed_amount=amt,
            idempotency_key=idempotency_key, stripe_event_id=stripe_event_id,
            stripe_payment_reference=stripe_payment_reference,
            payment_reference=payment_reference, currency=currency,
        )

    def credit(self, workspace_id: str, amount, *, idempotency_key: str, currency: str = "USD"):
        return self._add_transaction(
            workspace_id=workspace_id, transaction_type=CREDIT,
            signed_amount=Decimal(str(amount)), idempotency_key=idempotency_key, currency=currency,
        )

    def refund(self, workspace_id: str, amount, *, idempotency_key: str,
               stripe_event_id: Optional[str] = None, stripe_payment_reference: Optional[str] = None,
               currency: str = "USD"):
        # A refund/chargeback is an explicit negative ledger entry (history is never edited).
        amt = abs(Decimal(str(amount)))
        return self._add_transaction(
            workspace_id=workspace_id, transaction_type=REFUND, signed_amount=-amt,
            idempotency_key=idempotency_key, stripe_event_id=stripe_event_id,
            stripe_payment_reference=stripe_payment_reference, currency=currency,
        )

    def adjustment(self, workspace_id: str, signed_amount, *, idempotency_key: str, currency: str = "USD"):
        return self._add_transaction(
            workspace_id=workspace_id, transaction_type=ADJUSTMENT,
            signed_amount=Decimal(str(signed_amount)), idempotency_key=idempotency_key, currency=currency,
        )

    # -- reservations (concurrency-safe) ---------------------------------
    def _lock_workspace(self, s, workspace_id: str) -> None:
        anchor = s.get(orm.WorkspaceBilling, workspace_id)
        if anchor is None:
            s.add(orm.WorkspaceBilling(workspace_id=workspace_id))
            try:
                s.flush()
            except IntegrityError:
                s.rollback()
        # Serialise concurrent reservations for this workspace.
        s.query(orm.WorkspaceBilling).filter(
            orm.WorkspaceBilling.workspace_id == workspace_id
        ).with_for_update().all()

    def reserve(self, workspace_id: str, amount, reservation_key: str) -> bool:
        """Place a hold if available balance covers ``amount``. Idempotent by key."""
        amt = Decimal(str(amount))
        with session_scope() as s:
            self._lock_workspace(s, workspace_id)
            existing = (
                s.query(orm.BalanceReservation)
                .filter(orm.BalanceReservation.reservation_key == reservation_key)
                .one_or_none()
            )
            if existing is not None:
                return existing.status in ("active", "settled")
            bal = sum(
                (
                    Decimal(r[0] or "0")
                    for r in s.query(orm.BalanceTransaction.signed_amount)
                    .filter(orm.BalanceTransaction.workspace_id == workspace_id)
                    .all()
                ),
                _ZERO,
            )
            available = bal - self._active_holds(s, workspace_id)
            if available < amt:
                return False
            s.add(orm.BalanceReservation(
                id=new_id("resv"), workspace_id=workspace_id,
                reservation_key=reservation_key, amount=rates.to_money_str(amt), status="active",
            ))
            try:
                s.flush()
            except IntegrityError:
                s.rollback()
                return True  # concurrent identical reserve won
            return True

    def release(self, reservation_key: str) -> None:
        with session_scope() as s:
            row = (
                s.query(orm.BalanceReservation)
                .filter(orm.BalanceReservation.reservation_key == reservation_key)
                .one_or_none()
            )
            if row is not None and row.status == "active":
                row.status = "released"
                s.flush()

    def _settle_reservation(self, s, reservation_key: str) -> None:
        row = (
            s.query(orm.BalanceReservation)
            .filter(orm.BalanceReservation.reservation_key == reservation_key)
            .one_or_none()
        )
        if row is not None and row.status == "active":
            row.status = "settled"

    # -- charging (atomic charge + one debit; idempotent) ----------------
    def charge_usage(self, settings, usage_record_id: str) -> Tuple[Optional[ChargeView], bool]:
        """Create the immutable charge + exactly one usage debit for a usage row.

        Idempotent on ``usage_record_id``: a second call (replayed callback)
        returns the existing charge without applying markup or debiting again.
        Charge and debit are committed together; a lost race on either unique
        constraint converges to the first winner.
        """
        with session_scope() as s:
            usage = s.get(orm.UsageRecord, usage_record_id)
            if usage is None:
                raise BillingError(f"usage record not found: {usage_record_id}", status=404)

            existing = (
                s.query(orm.UsageCharge)
                .filter(orm.UsageCharge.usage_record_id == usage_record_id)
                .one_or_none()
            )
            if existing is not None:
                if usage.trace_event_id:
                    self._settle_reservation(s, usage.trace_event_id)
                return _charge_view(existing), False

            q = rates.quote(
                settings, upstream_cost=usage.upstream_cost,
                workspace_id=usage.workspace_id, provider=usage.provider, model=usage.model,
                currency=usage.currency,
            )
            strings = q.as_strings()

            # Settle the pre-request hold (if any) into the actual debit.
            if usage.trace_event_id:
                self._settle_reservation(s, usage.trace_event_id)

            if q.retail_cost <= 0:
                # Nothing billable (e.g. a failed request at zero cost).
                return None, True

            charge = orm.UsageCharge(
                id=new_id("charge"),
                usage_record_id=usage_record_id,
                litellm_request_id=usage.litellm_request_id,
                workspace_id=usage.workspace_id,
                user_id=usage.user_id,
                run_id=usage.run_id,
                trace_event_id=usage.trace_event_id,
                repository_id=usage.repository_id,
                application_name=usage.application_name,
                upstream_cost=strings["upstream_cost"],
                markup_rate=strings["markup_rate"],
                service_fee=strings["service_fee"],
                retail_cost=strings["retail_cost"],
                currency=strings["currency"],
                rate_card_version=strings["rate_card_version"],
                billing_status="charged",
            )
            s.add(charge)
            try:
                s.flush()
            except IntegrityError:
                # Concurrent charge won — return it, do not debit again.
                s.rollback()
                existing = (
                    s.query(orm.UsageCharge)
                    .filter(orm.UsageCharge.usage_record_id == usage_record_id)
                    .one()
                )
                return _charge_view(existing), False

            # Exactly one debit per charge, keyed idempotently to the usage record.
            debit = orm.BalanceTransaction(
                id=new_id("txn"),
                workspace_id=usage.workspace_id,
                transaction_type=USAGE_DEBIT,
                signed_amount=rates.to_money_str(-q.retail_cost),
                usage_charge_id=charge.id,
                idempotency_key=f"debit:{usage_record_id}",
                currency=strings["currency"],
            )
            s.add(debit)
            try:
                s.flush()
            except IntegrityError:
                # The debit already exists (shouldn't, given the charge is new) —
                # converge without double-debiting.
                s.rollback()
                existing = (
                    s.query(orm.UsageCharge)
                    .filter(orm.UsageCharge.usage_record_id == usage_record_id)
                    .one()
                )
                return _charge_view(existing), False
            return _charge_view(charge), True

    def get_charge_for_usage(self, usage_record_id: str) -> Optional[ChargeView]:
        with session_scope() as s:
            row = (
                s.query(orm.UsageCharge)
                .filter(orm.UsageCharge.usage_record_id == usage_record_id)
                .one_or_none()
            )
            return _charge_view(row) if row else None

    def release_stale_reservations(self, older_than_seconds: int = 1800) -> int:
        """Release active holds older than a cutoff (a lost callback safety net)."""
        from datetime import datetime, timezone, timedelta

        cutoff = datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)
        released = 0
        with session_scope() as s:
            rows = (
                s.query(orm.BalanceReservation)
                .filter(
                    orm.BalanceReservation.status == "active",
                    orm.BalanceReservation.created_at < cutoff,
                )
                .all()
            )
            for row in rows:
                row.status = "released"
                released += 1
            s.flush()
        return released
