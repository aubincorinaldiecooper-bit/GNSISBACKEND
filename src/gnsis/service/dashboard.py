"""Read model for the customer utility dashboard.

Aggregates the facts recorded by metering (PR 1, ``usage_records``) and billing
(PR 2, ``usage_charges`` / ``balance_transactions``) into the shapes the
dashboard screens need — an overview, a usage ledger, the balance ledger, and
per-run spend. It is strictly read-only and workspace-isolated: every query is
filtered by the caller's ``workspace_id``. Money stays :class:`decimal.Decimal`
end to end and is serialised as an exact decimal string, never binary float.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Optional

from . import orm
from .billing import BillingStore
from .db import session_scope
from .rates import to_money_str

_ZERO = Decimal("0")


@dataclass(frozen=True)
class Overview:
    currency: str
    balance: str
    available: str
    on_hold: str
    spent_30d: str
    spent_total: str
    usage_count: int
    run_count: int
    charge_count: int
    last_activity_at: Optional[str]


@dataclass(frozen=True)
class UsageLedgerItem:
    id: str
    created_at: str
    provider: str
    model: str
    engine: Optional[str]
    phase: Optional[str]
    run_id: Optional[str]
    repository_id: Optional[str]
    request_status: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    currency: str
    upstream_cost: str
    # From the immutable charge (absent for failed / zero-cost calls).
    retail_cost: Optional[str]
    markup_rate: Optional[str]
    service_fee: Optional[str]
    billing_status: Optional[str]


@dataclass(frozen=True)
class LedgerEntry:
    id: str
    created_at: str
    transaction_type: str
    signed_amount: str
    currency: str
    reference: Optional[str]


def _sum_money(values) -> Decimal:
    return sum((Decimal(v or "0") for v in values), _ZERO)


class DashboardStore:
    """Workspace-isolated, read-only aggregation over metering + billing."""

    def __init__(self) -> None:
        self._billing = BillingStore()

    # -- overview --------------------------------------------------------
    def overview(self, workspace_id: str, *, currency: str = "USD") -> Overview:
        balance = self._billing.balance(workspace_id)
        available = self._billing.available(workspace_id)
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        with session_scope() as s:
            retail_all = [
                r[0]
                for r in s.query(orm.UsageCharge.retail_cost)
                .filter(orm.UsageCharge.workspace_id == workspace_id)
                .all()
            ]
            retail_30d = [
                r[0]
                for r in s.query(orm.UsageCharge.retail_cost)
                .filter(
                    orm.UsageCharge.workspace_id == workspace_id,
                    orm.UsageCharge.created_at >= cutoff,
                )
                .all()
            ]
            usage_count = (
                s.query(orm.UsageRecord)
                .filter(orm.UsageRecord.workspace_id == workspace_id)
                .count()
            )
            charge_count = len(retail_all)
            last_usage = (
                s.query(orm.UsageRecord.created_at)
                .filter(orm.UsageRecord.workspace_id == workspace_id)
                .order_by(orm.UsageRecord.created_at.desc())
                .first()
            )
            run_ids = {
                r[0]
                for r in s.query(orm.UsageRecord.run_id)
                .filter(
                    orm.UsageRecord.workspace_id == workspace_id,
                    orm.UsageRecord.run_id.isnot(None),
                )
                .all()
            }
        last_activity = (
            last_usage[0].isoformat() if last_usage and last_usage[0] else None
        )
        return Overview(
            currency=currency,
            balance=to_money_str(balance),
            available=to_money_str(available),
            on_hold=to_money_str(balance - available),
            spent_30d=to_money_str(_sum_money(retail_30d)),
            spent_total=to_money_str(_sum_money(retail_all)),
            usage_count=usage_count,
            run_count=len(run_ids),
            charge_count=charge_count,
            last_activity_at=last_activity,
        )

    # -- usage ledger ----------------------------------------------------
    def usage_ledger(
        self, workspace_id: str, *, limit: int = 50, offset: int = 0
    ) -> List[UsageLedgerItem]:
        with session_scope() as s:
            rows = (
                s.query(orm.UsageRecord, orm.UsageCharge)
                .outerjoin(
                    orm.UsageCharge,
                    orm.UsageCharge.usage_record_id == orm.UsageRecord.id,
                )
                .filter(orm.UsageRecord.workspace_id == workspace_id)
                .order_by(orm.UsageRecord.created_at.desc())
                .limit(limit)
                .offset(offset)
                .all()
            )
            items: List[UsageLedgerItem] = []
            for rec, charge in rows:
                items.append(
                    UsageLedgerItem(
                        id=rec.id,
                        created_at=rec.created_at.isoformat() if rec.created_at else "",
                        provider=rec.provider,
                        model=rec.model,
                        engine=rec.engine,
                        phase=rec.phase,
                        run_id=rec.run_id,
                        repository_id=rec.repository_id,
                        request_status=rec.request_status,
                        input_tokens=rec.input_tokens,
                        output_tokens=rec.output_tokens,
                        total_tokens=rec.input_tokens + rec.output_tokens,
                        currency=rec.currency,
                        upstream_cost=rec.upstream_cost,
                        retail_cost=charge.retail_cost if charge else None,
                        markup_rate=charge.markup_rate if charge else None,
                        service_fee=charge.service_fee if charge else None,
                        billing_status=charge.billing_status if charge else None,
                    )
                )
            return items

    def usage_count(self, workspace_id: str) -> int:
        with session_scope() as s:
            return (
                s.query(orm.UsageRecord)
                .filter(orm.UsageRecord.workspace_id == workspace_id)
                .count()
            )

    # -- billing ledger --------------------------------------------------
    def transactions(
        self, workspace_id: str, *, limit: int = 50, offset: int = 0
    ) -> List[LedgerEntry]:
        with session_scope() as s:
            rows = (
                s.query(orm.BalanceTransaction)
                .filter(orm.BalanceTransaction.workspace_id == workspace_id)
                .order_by(orm.BalanceTransaction.created_at.desc())
                .limit(limit)
                .offset(offset)
                .all()
            )
            return [
                LedgerEntry(
                    id=row.id,
                    created_at=row.created_at.isoformat() if row.created_at else "",
                    transaction_type=row.transaction_type,
                    signed_amount=row.signed_amount,
                    currency=row.currency,
                    reference=row.stripe_payment_reference
                    or row.stripe_event_id
                    or row.usage_charge_id,
                )
                for row in rows
            ]

    def transaction_count(self, workspace_id: str) -> int:
        with session_scope() as s:
            return (
                s.query(orm.BalanceTransaction)
                .filter(orm.BalanceTransaction.workspace_id == workspace_id)
                .count()
            )

    # -- per-run spend ---------------------------------------------------
    def run_spend(self, workspace_id: str, run_ids: List[str]) -> Dict[str, str]:
        """Total retail spend per run (job) id, for the requested runs only."""
        wanted = [r for r in run_ids if r]
        if not wanted:
            return {}
        totals: Dict[str, Decimal] = {r: _ZERO for r in wanted}
        with session_scope() as s:
            rows = (
                s.query(orm.UsageCharge.run_id, orm.UsageCharge.retail_cost)
                .filter(
                    orm.UsageCharge.workspace_id == workspace_id,
                    orm.UsageCharge.run_id.in_(wanted),
                )
                .all()
            )
        for run_id, retail in rows:
            if run_id in totals:
                totals[run_id] += Decimal(retail or "0")
        return {r: to_money_str(v) for r, v in totals.items()}
