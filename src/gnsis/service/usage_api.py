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
from .visual_usage import VisualUsageStore

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


_VISUAL_IDS = ("event_id", "workspace_id", "virtual_key_id", "grant_id", "session_id")
_VISUAL_COUNTS = (
    "report_seq",
    "frames_accepted",
    "frame_bytes",
    "decisions",
    "decisions_act",
    "decisions_abstain",
    "attempts_recorded",
    "inference_ms",
    "session_ms",
)


def _authenticate_visual_callback(authorization: Optional[str]) -> None:
    secret = get_settings().visual_usage_secret
    if not secret:
        raise HTTPException(status_code=503, detail="visual usage callback is not configured")
    if not authorization:
        raise HTTPException(status_code=401, detail="missing Authorization")
    parts = authorization.split(" ", 1)
    presented = parts[1].strip() if len(parts) == 2 and parts[0].lower() == "bearer" else ""
    if not presented or not hmac.compare_digest(presented, secret):
        raise HTTPException(status_code=401, detail="invalid callback credential")


def _parse_visual_usage_report(report: object) -> dict:
    if not isinstance(report, dict):
        raise HTTPException(status_code=400, detail="each report must be an object")
    for name in _VISUAL_IDS:
        value = report.get(name)
        if not isinstance(value, str) or not value:
            raise HTTPException(status_code=400, detail=f"{name} is required")
    for name in _VISUAL_COUNTS:
        value = report.get(name)
        if type(value) is not int or value < 0:
            raise HTTPException(
                status_code=400, detail=f"{name} must be a non-negative integer"
            )
    for name in ("project_id", "environment_id"):
        value = report.get(name)
        if value is not None and (not isinstance(value, str) or not value):
            raise HTTPException(status_code=400, detail=f"{name} must be a string or null")
    if type(report.get("closed")) is not bool:
        raise HTTPException(status_code=400, detail="closed must be a boolean")
    if report["event_id"] != f"{report['session_id']}:{report['report_seq']}":
        raise HTTPException(status_code=400, detail="event_id does not match session_id and report_seq")
    return {
        **report,
        "project_id": report.get("project_id"),
        "environment_id": report.get("environment_id"),
    }


@router.post("/internal/usage/visual")
async def visual_usage_callback(
    request: Request, authorization: Optional[str] = Header(default=None)
):
    settings = get_settings()
    _authenticate_visual_callback(authorization)

    raw = await request.body()
    if len(raw) > settings.executor_callback_max_bytes:
        raise HTTPException(status_code=413, detail="callback body too large")
    try:
        body = json.loads(raw.decode("utf-8")) if raw else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="invalid JSON body")
    if not isinstance(body, dict) or not isinstance(body.get("reports"), list):
        raise HTTPException(status_code=400, detail="reports must be an array")
    if len(body["reports"]) > 500:
        raise HTTPException(status_code=400, detail="at most 500 reports are allowed")

    accepted = 0
    duplicates = 0
    for report in body["reports"]:
        record, created = VisualUsageStore().record(_parse_visual_usage_report(report))
        if created:
            accepted += 1
        else:
            duplicates += 1
    return {"accepted": accepted, "duplicates": duplicates}
