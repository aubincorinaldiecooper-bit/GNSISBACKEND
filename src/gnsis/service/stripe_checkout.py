"""Stripe Checkout for prepaid top-ups.

``POST /v1/billing/checkout`` creates a Stripe-hosted Checkout Session for the
caller's workspace and returns its URL. The session carries
``metadata.workspace_id``, so the verified ``checkout.session.completed``
webhook (``stripe_webhook.py``) credits exactly that workspace. Like the
webhook, this talks to Stripe's REST API with the standard library.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from typing import Callable, Optional

from fastapi import HTTPException

_STRIPE_CHECKOUT_URL = "https://api.stripe.com/v1/checkout/sessions"

Poster = Callable[[str, dict, str], dict]


def _stripe_post(url: str, form: dict, secret_key: str) -> dict:
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Authorization": f"Bearer {secret_key}", "User-Agent": "gnsis-api/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read().decode("utf-8"))["error"]["message"]
        except Exception:  # noqa: BLE001
            message = f"HTTP {exc.code}"
        raise HTTPException(status_code=502, detail=f"stripe: {message}")


def create_checkout_session(
    settings, workspace_id: str, amount_usd, post: Optional[Poster] = None,
) -> dict:
    if not (settings.stripe_secret_key and settings.stripe_webhook_secret):
        raise HTTPException(status_code=503, detail="payments are not configured")
    try:
        amount = Decimal(str(amount_usd)).quantize(Decimal("0.01"))
    except InvalidOperation:
        raise HTTPException(status_code=422, detail="amount_usd must be a number")
    lo, hi = Decimal(settings.topup_min_usd), Decimal(settings.topup_max_usd)
    if not (lo <= amount <= hi):
        raise HTTPException(status_code=422, detail=f"amount_usd must be between {lo} and {hi}")
    base = (settings.frontend_url or "https://gnsis.studio").rstrip("/")
    form = {
        "mode": "payment",
        "client_reference_id": workspace_id,
        "metadata[workspace_id]": workspace_id,
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": (settings.default_currency or "USD").lower(),
        "line_items[0][price_data][unit_amount]": str(int(amount * 100)),
        "line_items[0][price_data][product_data][name]": "GNSIS balance top-up",
        "success_url": f"{base}/dashboard?topup=success",
        "cancel_url": f"{base}/dashboard?topup=cancelled",
    }
    session = (post or _stripe_post)(_STRIPE_CHECKOUT_URL, form, settings.stripe_secret_key)
    url = session.get("url")
    if not url:
        raise HTTPException(status_code=502, detail="stripe: no checkout URL returned")
    return {"id": session.get("id"), "url": url}
