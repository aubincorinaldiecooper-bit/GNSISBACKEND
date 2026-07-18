"""Central low-level Stripe REST client (stdlib only — no Stripe SDK).

Every Stripe API call in GNSIS goes through :func:`_request` here, so the wire
format (deep form-encoding, ``Idempotency-Key`` header, typed error parsing) lives
in exactly one place and could be swapped for the official SDK behind this seam
without touching callers. Stripe is the source of truth for customers, saved
payment methods, Checkout payments, invoices, tax, and the Customer Portal; GNSIS
never stores full card numbers, CVCs, or tax calculations.

``_http_request`` is a module-level indirection so tests never touch the network.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple


class StripeError(Exception):
    def __init__(self, message: str, status: int = 502, code: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def _flatten(params: Dict[str, Any], prefix: str = "") -> List[Tuple[str, str]]:
    """Encode a nested dict/list into Stripe's ``a[b][0][c]`` form-field pairs."""
    out: List[Tuple[str, str]] = []
    for key, value in params.items():
        field = f"{prefix}[{key}]" if prefix else key
        if value is None:
            continue
        if isinstance(value, bool):
            out.append((field, "true" if value else "false"))
        elif isinstance(value, dict):
            out.extend(_flatten(value, field))
        elif isinstance(value, (list, tuple)):
            for i, item in enumerate(value):
                ifield = f"{field}[{i}]"
                if isinstance(item, dict):
                    out.extend(_flatten(item, ifield))
                else:
                    out.append((ifield, str(item)))
        else:
            out.append((field, str(value)))
    return out


def _http_request(
    method: str, url: str, headers: Dict[str, str], body: Optional[bytes] = None, timeout: int = 30
) -> Tuple[int, str]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        raise StripeError(f"could not reach Stripe: {exc.reason}", status=502) from exc


def _request(
    settings,
    method: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    *,
    idempotency_key: Optional[str] = None,
) -> dict:
    if not settings.stripe_secret_key:
        raise StripeError("Stripe is not configured", status=503)
    base = settings.stripe_api_base.rstrip("/")
    headers = {
        "Authorization": f"Bearer {settings.stripe_secret_key}",
        "User-Agent": "gnsis-billing",
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key

    encoded = urllib.parse.urlencode(_flatten(params or {}))
    body: Optional[bytes] = None
    url = f"{base}{path}"
    if method == "GET":
        if encoded:
            url = f"{url}?{encoded}"
    else:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        body = encoded.encode("utf-8")

    status, text = _http_request(method, url, headers, body)
    try:
        data = json.loads(text) if text else {}
    except json.JSONDecodeError:
        data = {}
    if status >= 400:
        err = data.get("error") if isinstance(data, dict) else None
        message = (err or {}).get("message") if isinstance(err, dict) else None
        code = (err or {}).get("code") if isinstance(err, dict) else None
        raise StripeError(message or f"Stripe error {status}", status=502, code=code)
    return data


# -- typed helpers ----------------------------------------------------------

def create_customer(
    settings, *, workspace_id: str, email: Optional[str] = None, name: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> dict:
    params: Dict[str, Any] = {"metadata": {"workspace_id": workspace_id}}
    if email:
        params["email"] = email
    if name:
        params["name"] = name
    return _request(settings, "POST", "/v1/customers", params, idempotency_key=idempotency_key)


def retrieve_customer(settings, customer_id: str) -> dict:
    return _request(settings, "GET", f"/v1/customers/{customer_id}")


def retrieve_payment_method(settings, payment_method_id: str) -> dict:
    return _request(settings, "GET", f"/v1/payment_methods/{payment_method_id}")


def create_checkout_session(
    settings,
    *,
    customer_id: str,
    workspace_id: str,
    amount_cents: int,
    currency: str,
    success_url: str,
    cancel_url: str,
    product_name: str = "GNSIS balance refill",
    tax_enabled: bool = False,
    idempotency_key: Optional[str] = None,
) -> dict:
    """A one-time Checkout payment that reuses the workspace Customer, creates a
    paid invoice, and (optionally) computes tax via Stripe Tax."""
    params: Dict[str, Any] = {
        "mode": "payment",
        "customer": customer_id,
        "client_reference_id": workspace_id,
        "success_url": success_url,
        "cancel_url": cancel_url,
        "metadata": {"workspace_id": workspace_id},
        "payment_intent_data": {"metadata": {"workspace_id": workspace_id}},
        # Emit a finalized, paid invoice + receipt for every refill.
        "invoice_creation": {"enabled": True},
        "line_items": [
            {
                "quantity": 1,
                "price_data": {
                    "currency": currency,
                    "unit_amount": amount_cents,
                    "product_data": {"name": product_name},
                    # Let Stripe apply tax behaviour per your dashboard config.
                    "tax_behavior": "exclusive",
                },
            }
        ],
    }
    if tax_enabled:
        params["automatic_tax"] = {"enabled": True}
    return _request(
        settings, "POST", "/v1/checkout/sessions", params, idempotency_key=idempotency_key
    )


def create_portal_session(settings, *, customer_id: str, return_url: str) -> dict:
    params: Dict[str, Any] = {"customer": customer_id, "return_url": return_url}
    if settings.stripe_portal_configuration_id:
        params["configuration"] = settings.stripe_portal_configuration_id
    return _request(settings, "POST", "/v1/billing_portal/sessions", params)
