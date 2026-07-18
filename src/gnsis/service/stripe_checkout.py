"""Create a Stripe Checkout Session for a prepaid balance refill.

This is the *initiation* half of the refill loop; the *crediting* half is the
verified webhook in :mod:`stripe_checkout`'s sibling ``stripe_webhook`` (PR 2).
The redirect never credits a balance — only the signed ``payment_intent.succeeded``
/ ``checkout.session.completed`` webhook does, idempotently. Here we simply open a
hosted Stripe page for a chosen dollar amount and stamp ``metadata.workspace_id``
(on both the session and the resulting PaymentIntent) so the webhook can attribute
the top-up to the right workspace.

No Stripe SDK: the Checkout Session is created with a plain form-encoded POST via
the stdlib. ``_http_post`` is a module-level indirection so tests never touch the
network.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Dict, Optional, Tuple


class RefillError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _http_post(url: str, data: bytes, headers: Dict[str, str], timeout: int = 30) -> Tuple[int, str]:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:  # network/DNS/TLS failure
        raise RefillError(f"could not reach Stripe: {exc.reason}", status=502) from exc


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

    # Stripe expects deep form-encoded params. Setting workspace_id on both the
    # session metadata and payment_intent_data.metadata means whichever event the
    # operator subscribes to (checkout.session.completed or payment_intent.succeeded)
    # carries the attribution the PR 2 webhook reads.
    params = [
        ("mode", "payment"),
        ("success_url", f"{return_base}/billing?refill=success&session_id={{CHECKOUT_SESSION_ID}}"),
        ("cancel_url", f"{return_base}/billing?refill=cancelled"),
        ("client_reference_id", workspace_id),
        ("metadata[workspace_id]", workspace_id),
        ("payment_intent_data[metadata][workspace_id]", workspace_id),
        ("line_items[0][quantity]", "1"),
        ("line_items[0][price_data][currency]", currency),
        ("line_items[0][price_data][unit_amount]", str(unit_amount)),
        ("line_items[0][price_data][product_data][name]", "GNSIS balance refill"),
    ]
    if user_email:
        params.append(("customer_email", user_email))

    body = urllib.parse.urlencode(params).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {settings.stripe_secret_key}",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "gnsis-billing",
    }
    url = f"{settings.stripe_api_base.rstrip('/')}/v1/checkout/sessions"

    status, text = _http_post(url, body, headers)
    try:
        payload = json.loads(text) if text else {}
    except json.JSONDecodeError:
        payload = {}
    if status >= 400:
        detail = (payload.get("error") or {}).get("message") if isinstance(payload, dict) else None
        raise RefillError(f"Stripe rejected the refill: {detail or text[:200]}", status=502)

    session_url = payload.get("url")
    if not session_url:
        raise RefillError("Stripe did not return a checkout URL", status=502)
    return {
        "url": session_url,
        "session_id": payload.get("id"),
        "amount_usd": str(amount_usd),
        "currency": settings.default_currency or "USD",
    }
