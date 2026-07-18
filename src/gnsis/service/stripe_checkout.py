"""Create a Stripe Checkout Session for a prepaid balance refill.

The *initiation* half of the refill loop; the *crediting* half is the verified
webhook (``stripe_webhook``). The redirect never credits a balance — only the
signed ``checkout.session.completed`` webhook does, payment-level idempotently.

Each refill reuses the workspace's persistent Stripe Customer, emits a paid
invoice + receipt, and (when Stripe Tax is enabled in config) lets Stripe compute
tax. All Stripe I/O goes through :mod:`stripe_client`, so tests never touch the
network.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Dict, Optional

from . import stripe_client, stripe_customers


class RefillError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _minor_units(amount_usd: str, settings) -> int:
    """Validate the refill amount and convert dollars to integer cents."""
    try:
        amt = Decimal(str(amount_usd))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise RefillError(f"invalid amount: {amount_usd!r}") from exc
    if amt.is_nan() or amt.is_infinite():
        raise RefillError(f"invalid amount: {amount_usd!r}")
    lo = Decimal(str(settings.refill_min_usd))
    hi = Decimal(str(settings.refill_max_usd))
    if amt < lo or amt > hi:
        raise RefillError(f"amount must be between {lo} and {hi} {settings.default_currency}")
    cents = (amt * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(cents)


def create_refill_session(
    settings,
    *,
    workspace_id: str,
    amount_usd: str,
    user_email: Optional[str] = None,
) -> Dict[str, object]:
    """Open a hosted Stripe Checkout page for a one-time refill.

    Returns ``{"url", "session_id", "amount_usd", "currency"}``. Raises
    :class:`RefillError` (503) if refills are not configured.
    """
    if not settings.refill_enabled:
        raise RefillError("refills are not configured", status=503)
    if not workspace_id:
        raise RefillError("workspace is required", status=400)

    unit_amount = _minor_units(amount_usd, settings)
    currency = (settings.default_currency or "USD").lower()
    return_base = settings.frontend_url.rstrip("/")

    try:
        customer_id = stripe_customers.get_or_create_customer(
            settings, workspace_id, email=user_email
        )
        session = stripe_client.create_checkout_session(
            settings,
            customer_id=customer_id,
            workspace_id=workspace_id,
            amount_cents=unit_amount,
            currency=currency,
            success_url=f"{return_base}/billing?refill=success&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{return_base}/billing?refill=cancelled",
            tax_enabled=settings.stripe_tax_enabled,
        )
    except stripe_client.StripeError as exc:
        raise RefillError(f"Stripe rejected the refill: {exc.message}", status=502) from exc

    session_url = session.get("url")
    if not session_url:
        raise RefillError("Stripe did not return a checkout URL", status=502)
    return {
        "url": session_url,
        "session_id": session.get("id"),
        "amount_usd": str(amount_usd),
        "currency": settings.default_currency or "USD",
    }
