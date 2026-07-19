"""Internal LiteLLM usage callback.

LiteLLM (a separate service) reports each completed/failed model request here.
The endpoint authenticates with a shared secret, validates the documented
callback contract, and idempotently records one measured usage row keyed on
``litellm_request_id``. It measures and attributes only — no markup, charge, or
balance change happens here (those are PR 2). A replayed callback returns a
successful, non-duplicating response.
"""

from __future__ import annotations

import hmac
import json
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request

from .settings import get_settings
from .usage import UsageStore, UsageValidationError, parse_callback

router = APIRouter()


def _authenticate_callback(authorization: Optional[str]) -> None:
    settings = get_settings()
    secret = settings.litellm_callback_secret
    if not secret:
        raise HTTPException(status_code=503, detail="usage callback is not configured")
    if not authorization:
        raise HTTPException(status_code=401, detail="missing Authorization")
    parts = authorization.split(" ", 1)
    presented = parts[1].strip() if len(parts) == 2 and parts[0].lower() == "bearer" else ""
    if not presented or not hmac.compare_digest(presented, secret):
        raise HTTPException(status_code=401, detail="invalid callback credential")


@router.post("/internal/usage/litellm/callback")
async def litellm_usage_callback(
    request: Request, authorization: Optional[str] = Header(default=None)
):
    settings = get_settings()
    _authenticate_callback(authorization)

    raw = await request.body()
    if len(raw) > settings.executor_callback_max_bytes:
        raise HTTPException(status_code=413, detail="callback body too large")
    try:
        body = json.loads(raw.decode("utf-8")) if raw else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="invalid JSON body")

    try:
        measured = parse_callback(body)
    except UsageValidationError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message)

    record, created = UsageStore().record(measured)

    # Price the measurement from the versioned pricing table: compute the
    # Genesis cost, stamp the pricing version, and reconcile state (resolve an
    # unknown provider cost when priced, flag a provider-vs-calculated
    # discrepancy). Best-effort + only for a freshly-created row; a replay skips
    # it. Runs BEFORE charging so the charge sees the reconciled cost basis.
    if created:
        try:
            from .pricing import price_usage_record

            price_usage_record(settings, record.id)
        except Exception:  # noqa: BLE001 — pricing must never fail metering
            pass

    # When billing is configured, convert the measurement into an immutable
    # charge + one balance debit (and settle any pre-request hold). Idempotent —
    # a replayed callback neither re-records nor re-charges.
    charged = False
    if settings.billing_enabled:
        from .billing import BillingError, BillingStore

        try:
            _, charged = BillingStore().charge_usage(settings, record.id)
        except BillingError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.message)

    return {
        "accepted": True,
        "duplicate": not created,
        "usage_id": record.id,
        "litellm_request_id": record.litellm_request_id,
        "charged": charged,
    }
