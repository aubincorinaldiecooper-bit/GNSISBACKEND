"""Stripe prepaid-refill webhook — verified, idempotent, replay-safe.

Stripe is the payment source: a verified webhook turns a succeeded payment into
exactly one ``top_up`` ledger entry. Signatures are verified with the webhook
secret (stdlib HMAC — no Stripe SDK dependency). Each Stripe event id is stored
uniquely, so a replayed event never credits twice. Failed/cancelled payments add
nothing; refunds and chargebacks create explicit negative ledger entries. Stripe
is not the request-level usage source of truth — that stays with LiteLLM/GNSIS.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request

from .billing import BillingStore
from .settings import get_settings

router = APIRouter()

_DEFAULT_TOLERANCE = 300  # seconds


class StripeSignatureError(Exception):
    pass


def verify_signature(payload: bytes, sig_header: str, secret: str, tolerance: int = _DEFAULT_TOLERANCE) -> int:
    """Verify a Stripe ``Stripe-Signature`` header. Returns the signed timestamp.

    Reimplements Stripe's scheme: ``v1 = HMAC-SHA256(secret, f"{t}.{payload}")``.
    """
    if not sig_header:
        raise StripeSignatureError("missing signature header")
    parts = {}
    for item in sig_header.split(","):
        k, _, v = item.partition("=")
        parts.setdefault(k.strip(), []).append(v.strip())
    timestamps = parts.get("t")
    v1s = parts.get("v1")
    if not timestamps or not v1s:
        raise StripeSignatureError("malformed signature header")
    t = timestamps[0]
    signed_payload = f"{t}.".encode() + payload
    expected = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, v) for v in v1s):
        raise StripeSignatureError("signature mismatch")
    try:
        ts = int(t)
    except ValueError:
        raise StripeSignatureError("bad timestamp")
    if tolerance and abs(time.time() - ts) > tolerance:
        raise StripeSignatureError("timestamp outside tolerance")
    return ts


def _workspace_id(obj: dict) -> Optional[str]:
    md = obj.get("metadata") or {}
    return md.get("workspace_id") or md.get("gnsis_workspace_id")


def _dollars(minor_units) -> Decimal:
    try:
        return (Decimal(int(minor_units)) / Decimal(100))
    except (TypeError, ValueError):
        return Decimal("0")


def handle_event(event: dict, store: Optional[BillingStore] = None) -> dict:
    """Apply a verified Stripe event to the ledger. Idempotent on the event id."""
    store = store or BillingStore()
    event_id = event.get("id")
    if not event_id:
        raise HTTPException(status_code=400, detail="event id required")
    etype = event.get("type", "")
    obj = ((event.get("data") or {}).get("object")) or {}
    workspace_id = _workspace_id(obj)
    currency = (obj.get("currency") or "usd").upper()

    if etype in ("checkout.session.completed", "payment_intent.succeeded"):
        # Only a genuinely paid session/intent credits balance.
        paid = obj.get("payment_status") in (None, "paid") and obj.get("status") in (
            None, "succeeded", "complete",
        )
        amount = obj.get("amount_received")
        if amount is None:
            amount = obj.get("amount_total")
        if not workspace_id or not paid or not amount:
            return {"handled": False, "reason": "not a completed, attributable payment"}
        _, created = store.top_up(
            workspace_id, _dollars(amount),
            idempotency_key=f"stripe:{event_id}", stripe_event_id=event_id,
            stripe_payment_reference=obj.get("id"), currency=currency,
        )
        return {"handled": True, "type": etype, "created": created}

    if etype in ("charge.refunded", "refund.created", "charge.dispute.created"):
        refunded = obj.get("amount_refunded") or obj.get("amount")
        if not workspace_id or not refunded:
            return {"handled": False, "reason": "refund not attributable"}
        _, created = store.refund(
            workspace_id, _dollars(refunded),
            idempotency_key=f"stripe:{event_id}", stripe_event_id=event_id,
            stripe_payment_reference=obj.get("id"), currency=currency,
        )
        return {"handled": True, "type": etype, "created": created}

    # payment_intent.payment_failed, checkout.session.expired, etc.: never credit.
    return {"handled": False, "reason": f"ignored event type: {etype}"}


@router.post("/billing/stripe/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(default="")):
    settings = get_settings()
    if not settings.stripe_webhook_secret:
        raise HTTPException(status_code=503, detail="stripe webhooks are not configured")
    payload = await request.body()
    if len(payload) > settings.executor_callback_max_bytes:
        raise HTTPException(status_code=413, detail="payload too large")
    try:
        verify_signature(payload, stripe_signature, settings.stripe_webhook_secret)
    except StripeSignatureError as exc:
        raise HTTPException(status_code=400, detail=f"invalid signature: {exc}")
    try:
        event = json.loads(payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="invalid JSON body")
    return handle_event(event)
