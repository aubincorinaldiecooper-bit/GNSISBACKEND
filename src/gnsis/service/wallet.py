"""Wallet billing summary + Customer Portal — the Stripe-facing read/entry layer.

GNSIS stays the source of truth for balances, reservations, and month-to-date
spend (all derived from its own ledger + charges). Stripe stays the source of
truth for the saved payment method and hosted invoices/receipts; this module only
reads *safe* card metadata (brand/last4/expiry) and opens a Customer Portal
session. Money is returned as exact decimal strings.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from . import orm, stripe_client, stripe_customers
from .billing import BillingStore
from .db import session_scope
from .rates import to_money_str

_ZERO = Decimal("0")


def _month_start(now: Optional[datetime] = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _spent_this_month(workspace_id: str) -> Decimal:
    start = _month_start()
    with session_scope() as s:
        rows = (
            s.query(orm.UsageCharge.retail_cost)
            .filter(
                orm.UsageCharge.workspace_id == workspace_id,
                orm.UsageCharge.created_at >= start,
            )
            .all()
        )
    return sum((Decimal(r[0] or "0") for r in rows), _ZERO)


def billing_summary(settings, workspace_id: str, *, email: Optional[str] = None) -> dict:
    """Compose the pay-as-you-go billing summary for a workspace.

    GNSIS-owned figures (balance/reserved/available/month spend) are authoritative;
    the card block is best-effort from Stripe and never blocks the summary.
    """
    billing = BillingStore()
    balance = billing.balance(workspace_id)
    available = billing.available(workspace_id)
    reserved = balance - available

    customer_id = stripe_customers.get_customer_id(workspace_id)
    default_card = None
    if customer_id and settings.stripe_secret_key:
        try:
            default_card = stripe_customers.default_card_metadata(settings, customer_id)
        except stripe_client.StripeError:
            default_card = None  # never fail the whole summary on a Stripe hiccup

    return {
        "currency": settings.default_currency or "USD",
        "balance": to_money_str(balance),
        "available": to_money_str(available),
        "reserved": to_money_str(reserved),
        "spent_this_month": to_money_str(_spent_this_month(workspace_id)),
        "has_customer": bool(customer_id),
        "default_card": default_card,
        "refill_enabled": settings.refill_enabled,
        "portal_available": bool(settings.stripe_secret_key and settings.frontend_url),
        "tax_enabled": settings.stripe_tax_enabled,
    }


def create_portal_url(settings, workspace_id: str, *, email: Optional[str] = None, return_url: str) -> str:
    """Open a Stripe Customer Portal session for this workspace's Customer.

    Creates the Customer on demand if the workspace has never paid, so "manage
    billing" works before the first refill.
    """
    if not settings.stripe_secret_key:
        from .billing import BillingError

        raise BillingError("billing portal is not configured", status=503)
    customer_id = stripe_customers.get_or_create_customer(settings, workspace_id, email=email)
    session = stripe_client.create_portal_session(
        settings, customer_id=customer_id, return_url=return_url
    )
    url = session.get("url")
    if not url:
        from .billing import BillingError

        raise BillingError("Stripe did not return a portal URL", status=502)
    return url
