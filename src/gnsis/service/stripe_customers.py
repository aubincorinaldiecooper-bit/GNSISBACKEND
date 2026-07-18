"""Persistent per-workspace Stripe Customer + safe card metadata.

Exactly one Stripe Customer exists per workspace, stored as
``workspace_billing.stripe_customer_id`` (unique). Get-or-create is
concurrency-safe: it serialises on the same ``WorkspaceBilling`` row lock used
for balance reservations, and additionally passes a per-workspace Stripe
idempotency key so even a duplicate create call returns the same Customer.

Only *safe* card metadata (brand, last four, expiry) is ever read back and
surfaced — GNSIS never stores or exposes full card numbers, CVCs, or addresses;
those live in Stripe.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy.exc import IntegrityError

from . import orm, stripe_client
from .billing import BillingError
from .db import session_scope


def get_customer_id(workspace_id: str) -> Optional[str]:
    """Read the stored Customer id without calling Stripe (may be ``None``)."""
    with session_scope() as s:
        anchor = s.get(orm.WorkspaceBilling, workspace_id)
        return anchor.stripe_customer_id if anchor else None


def get_or_create_customer(
    settings, workspace_id: str, *, email: Optional[str] = None, name: Optional[str] = None
) -> str:
    """Return the workspace's Stripe Customer id, creating it once if needed."""
    if not settings.stripe_secret_key:
        raise BillingError("Stripe is not configured", status=503)

    # Fast path: already created.
    existing = get_customer_id(workspace_id)
    if existing:
        return existing

    # Slow path: lock the billing anchor so concurrent callers serialise.
    with session_scope() as s:
        anchor = s.get(orm.WorkspaceBilling, workspace_id)
        if anchor is None:
            anchor = orm.WorkspaceBilling(workspace_id=workspace_id)
            s.add(anchor)
            try:
                s.flush()
            except IntegrityError:
                s.rollback()
                anchor = s.get(orm.WorkspaceBilling, workspace_id)
        # Serialise: on Postgres this blocks a concurrent creator until commit.
        s.query(orm.WorkspaceBilling).filter(
            orm.WorkspaceBilling.workspace_id == workspace_id
        ).with_for_update().all()
        s.refresh(anchor)
        if anchor.stripe_customer_id:
            return anchor.stripe_customer_id

        # A per-workspace idempotency key means a retried create returns the same
        # Customer instead of minting a duplicate.
        customer = stripe_client.create_customer(
            settings, workspace_id=workspace_id, email=email, name=name,
            idempotency_key=f"gnsis-customer:{workspace_id}",
        )
        customer_id = customer.get("id")
        if not customer_id:
            raise BillingError("Stripe did not return a customer id", status=502)
        anchor.stripe_customer_id = customer_id
        try:
            s.flush()
        except IntegrityError:
            # Another writer set it first — converge on the stored value.
            s.rollback()
            anchor = s.get(orm.WorkspaceBilling, workspace_id)
            if anchor and anchor.stripe_customer_id:
                return anchor.stripe_customer_id
            raise
        return customer_id


def _safe_card(pm: dict) -> Optional[dict]:
    card = (pm or {}).get("card") or {}
    if not card:
        return None
    # Only non-sensitive display fields — never the full PAN, CVC, or address.
    return {
        "brand": card.get("brand"),
        "last4": card.get("last4"),
        "exp_month": card.get("exp_month"),
        "exp_year": card.get("exp_year"),
    }


def default_card_metadata(settings, customer_id: str) -> Optional[dict]:
    """Safe display metadata for the customer's default card, or ``None``."""
    if not customer_id:
        return None
    customer = stripe_client.retrieve_customer(settings, customer_id)
    invoice_settings = customer.get("invoice_settings") or {}
    pm_id = invoice_settings.get("default_payment_method")
    # Fall back to a legacy default source if no invoice-settings default is set.
    if isinstance(pm_id, dict):
        pm_id = pm_id.get("id")
    if not pm_id:
        return None
    pm = stripe_client.retrieve_payment_method(settings, pm_id)
    return _safe_card(pm)
