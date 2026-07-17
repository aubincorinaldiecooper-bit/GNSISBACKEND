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
    return {
        "accepted": True,
        "duplicate": not created,
        "usage_id": record.id,
        "litellm_request_id": record.litellm_request_id,
    }
