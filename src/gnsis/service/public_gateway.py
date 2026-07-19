"""Public OpenAI-compatible Genesis gateway.

An OpenAI-compatible client points its ``base_url`` at Genesis and presents a
Genesis virtual key (``gns_…``). For each request Genesis:

  1. mints a Genesis request id            10. captures token usage + provider meta
  2. authenticates the virtual key (G2)     11. computes provider cost (versioned pricing, G3)
  3. resolves attribution from key scopes   12. computes the Genesis service fee separately
  4. resolves/creates a run id              13. records one immutable usage event (G1)
  5. checks provider/model are permitted    14. reconciles the balance hold
  6. checks balance before forwarding       15. (run receipt — derivable; enriched in G6)
  7. resolves the upstream credential       16. returns an OpenAI-compatible response
  8. forwards, preserving behavior          17. returns the id in ``X-Genesis-Request-Id``
  9. preserves streaming when supported

Prompts and responses are **not** stored. The provider-forwarding functions are a
module-level seam (``_forward`` / ``_forward_stream``) so tests never hit a
provider. The structure — a per-endpoint handler over shared steps — lets
``/v1/responses`` / ``/v1/embeddings`` / ``/v1/audio/transcriptions`` be added
later without redesigning the flow.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, Iterator, Optional, Tuple

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .settings import get_settings
from .usage import MeasuredUsage, UsageStore

router = APIRouter()

# Only these fields are forwarded upstream; anything else (a smuggled base_url,
# api_base, …) is dropped so the gateway cannot be redirected.
_FORWARD_FIELDS = (
    "model", "messages", "max_tokens", "max_completion_tokens", "temperature",
    "top_p", "stop", "tools", "tool_choice", "response_format", "seed", "n",
    "presence_penalty", "frequency_penalty", "logprobs", "top_logprobs", "user",
)


class GatewayError(Exception):
    """A structured gateway error rendered as ``{"error": {...}}``."""

    def __init__(self, code: str, message: str, status: int = 400, details: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}


def _provider_of(model: str) -> str:
    return model.split("/", 1)[0] if "/" in model else (model.split("-", 1)[0] or "unknown")


def _sanitize(body: Dict[str, Any], model: str) -> Dict[str, Any]:
    payload = {k: body[k] for k in _FORWARD_FIELDS if k in body}
    payload["model"] = model
    return payload


# -- provider forwarding seam (monkeypatched in tests) ----------------------

def _forward(settings, provider: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Default non-streaming forward to the configured upstream (OpenRouter-style,
    which routes by ``provider/model``). Provider-specific credential vaults can
    slot in behind this function later."""
    key = settings.openrouter_api_key
    if not key:
        raise GatewayError("gateway_not_configured", "no upstream provider is configured", status=503)
    import os

    base = (os.environ.get("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1").rstrip("/")
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"{base}/chat/completions", data=data, method="POST")
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise GatewayError("provider_error", f"upstream error {exc.code}: {detail[:300]}", status=502) from exc
    except urllib.error.URLError as exc:
        raise GatewayError("provider_unreachable", f"upstream unreachable: {exc.reason}", status=502) from exc


def _forward_stream(settings, provider: str, payload: Dict[str, Any]) -> Iterator[bytes]:
    """Default streaming forward — yields raw SSE chunks from the upstream."""
    key = settings.openrouter_api_key
    if not key:
        raise GatewayError("gateway_not_configured", "no upstream provider is configured", status=503)
    import os

    base = (os.environ.get("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1").rstrip("/")
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"{base}/chat/completions", data=data, method="POST")
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    resp = urllib.request.urlopen(req, timeout=300)  # noqa: S310
    for line in resp:
        yield line


# -- key resolution + permission --------------------------------------------

def resolve_key(settings, authorization: Optional[str]):
    from .virtual_keys import VirtualKeyStore

    if not authorization:
        raise GatewayError("missing_credential", "a Genesis virtual key is required", status=401)
    parts = authorization.split(" ", 1)
    presented = parts[1].strip() if len(parts) == 2 and parts[0].lower() == "bearer" else authorization.strip()
    key = VirtualKeyStore().authenticate(settings, presented)
    if key is None:
        raise GatewayError("invalid_key", "the virtual key is invalid, disabled, or expired", status=401)
    return key


def check_permitted(key, provider: str, model: str) -> None:
    if key.allowed_providers and provider not in key.allowed_providers:
        raise GatewayError("provider_not_allowed", f"provider not allowed for this key: {provider}",
                           status=403, details={"provider": provider})
    if key.allowed_models and model not in key.allowed_models:
        raise GatewayError("model_not_allowed", f"model not allowed for this key: {model}",
                           status=403, details={"model": model})


def _resolve_run_id(body: Dict[str, Any], header_run_id: Optional[str]) -> str:
    md = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    return header_run_id or md.get("run_id") or f"run_{uuid.uuid4().hex}"


def _extract_usage(data: Dict[str, Any]) -> Dict[str, Any]:
    usage = (data or {}).get("usage") or {}
    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    return {
        "input_tokens": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
        "cached_tokens": int(prompt_details.get("cached_tokens") or usage.get("cached_tokens") or 0),
        "reasoning_tokens": int(completion_details.get("reasoning_tokens") or usage.get("reasoning_tokens") or 0),
        "cost": usage.get("cost"),
        "has_usage": bool(usage),
    }


def _meter(settings, key, *, provider: str, requested_model: str, data: Dict[str, Any],
           run_id: str, request_id: str, duration_ms: int, status: str = "success") -> None:
    """Steps 10-14: record the immutable usage event, price it, and charge.

    Uses the Genesis request id as both the unique record id and the reservation
    key (via ``trace_event_id``), so charging settles the pre-request hold and a
    provider retry reusing the id never double-bills.
    """
    u = _extract_usage(data)
    cost = u["cost"]
    cost_present = cost is not None
    resolved_model = (data or {}).get("model") or requested_model
    succeeded = status == "success"
    measured = MeasuredUsage(
        litellm_request_id=request_id, idempotency_key=request_id,
        provider_request_id=(data or {}).get("id"),
        workspace_id=key.workspace_id, user_id=key.user_id or "",
        team_id=key.team_id, project_id=key.project_id, virtual_key_id=key.id,
        run_id=run_id, trace_event_id=request_id, environment=key.environment_id,
        provider=provider, model=resolved_model,
        input_tokens=u["input_tokens"], output_tokens=u["output_tokens"],
        cached_tokens=u["cached_tokens"], reasoning_tokens=u["reasoning_tokens"],
        duration_ms=duration_ms, request_status=status,
        upstream_cost=str(cost) if cost_present else "0",
        currency=settings.default_currency or "USD",
        cost_source="provider_reported" if cost_present else "unknown",
        reconciliation_state=("resolved" if cost_present or not succeeded else "needs_reconciliation"),
    )
    rec, created = UsageStore().record(measured)
    if created:
        try:
            from .pricing import price_usage_record

            price_usage_record(settings, rec.id)
        except Exception:  # noqa: BLE001 — pricing must never break a completed response
            pass
    retail = None
    if settings.billing_enabled:
        from .billing import BillingError, BillingStore

        try:
            charge, _ = BillingStore().charge_usage(settings, rec.id)
            retail = charge.retail_cost if charge else None
        except BillingError:
            pass
    return retail


def _release_hold(settings, key, request_id: str) -> None:
    if settings.billing_enabled and getattr(key, "workspace_id", None):
        from .billing import BillingStore

        BillingStore().release(request_id)


def _reserve_or_402(settings, key, request_id: str) -> None:
    if settings.billing_enabled and key.workspace_id:
        from .billing import BillingStore

        ok = BillingStore().reserve(key.workspace_id, settings.balance_reserve_estimate_usd, request_id)
        if not ok:
            raise GatewayError(
                "insufficient_balance", "This request would exceed the available balance.",
                status=402, details={"scope": "workspace"},
            )


def _limit_context(key, run_id: str):
    from .limits import LimitContext

    return LimitContext(
        workspace_id=key.workspace_id, run_id=run_id,
        project_id=key.project_id, environment_id=key.environment_id,
        user_id=key.user_id or None, team_id=key.team_id, virtual_key_id=key.id,
        key_limits={
            "soft_limit": key.soft_limit, "hard_limit": key.hard_limit,
            "per_run_limit": key.per_run_limit, "daily_limit": key.daily_limit,
            "monthly_limit": key.monthly_limit,
        },
    )


def _enforce_limits_or_deny(settings, key, run_id: str, request_id: str) -> None:
    """Evaluate configurable spending policies; block (releasing the balance hold)
    if a ``block`` policy would be exceeded. Warn/observe modes allow the request."""
    if not getattr(key, "workspace_id", None):
        return
    from .limits import PolicyEngine

    ctx = _limit_context(key, run_id)
    result = PolicyEngine().evaluate(settings, ctx, settings.balance_reserve_estimate_usd, request_id)
    if result.result == "block":
        _release_hold(settings, key, request_id)
        raise GatewayError(
            "spending_limit_exceeded",
            "This request would exceed a configured spending limit.",
            status=402, details={"scope": result.block_scope, "limit_id": result.block_limit_id},
        )


def _reconcile_limits(request_id: str, actual_cost=None) -> None:
    from .limits import PolicyEngine

    try:
        PolicyEngine().reconcile(request_id, actual_cost)
    except Exception:  # noqa: BLE001
        pass


def _release_limits(request_id: str) -> None:
    from .limits import PolicyEngine

    try:
        PolicyEngine().release(request_id)
    except Exception:  # noqa: BLE001
        pass


def handle_chat_completion(
    settings, key, body: Dict[str, Any], request_id: str, *,
    header_run_id: Optional[str] = None,
    forward: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Tuple[int, Dict[str, Any], str]:
    """Non-streaming flow. Returns ``(status, response_dict, run_id)``."""
    if not isinstance(body, dict) or not body.get("model"):
        raise GatewayError("invalid_request", "model is required", status=400)
    model = body["model"]
    provider = _provider_of(model)
    check_permitted(key, provider, model)
    run_id = _resolve_run_id(body, header_run_id)
    _reserve_or_402(settings, key, request_id)
    _enforce_limits_or_deny(settings, key, run_id, request_id)

    fwd = forward or _forward
    started = datetime.now(timezone.utc)
    try:
        data = fwd(settings, provider, _sanitize(body, model))
    except GatewayError:
        _release_hold(settings, key, request_id)
        _release_limits(request_id)
        raise
    except Exception as exc:  # noqa: BLE001
        _release_hold(settings, key, request_id)
        _release_limits(request_id)
        # The provider failed after the hold; record the failed attempt (no charge)
        # and release. Billing correctness beats pretending it was metered clean.
        _meter(settings, key, provider=provider, requested_model=model, data={}, run_id=run_id,
               request_id=request_id, duration_ms=0, status="error")
        raise GatewayError("provider_error", f"upstream provider error: {exc}", status=502) from exc

    duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    retail = _meter(settings, key, provider=provider, requested_model=model, data=data, run_id=run_id,
                    request_id=request_id, duration_ms=duration_ms)
    _reconcile_limits(request_id, retail)
    return 200, data, run_id


def stream_chat_completion(
    settings, key, body: Dict[str, Any], request_id: str, *,
    header_run_id: Optional[str] = None,
    forward_stream: Optional[Callable[..., Iterable[bytes]]] = None,
) -> Iterator[bytes]:
    """Streaming flow: pass SSE chunks through untouched, capture usage from the
    final chunk (best-effort), then meter once the stream ends."""
    model = body["model"]
    provider = _provider_of(model)
    check_permitted(key, provider, model)
    run_id = _resolve_run_id(body, header_run_id)
    _reserve_or_402(settings, key, request_id)
    _enforce_limits_or_deny(settings, key, run_id, request_id)

    payload = _sanitize(body, model)
    payload["stream"] = True
    # Ask the provider to include a final usage chunk so we can meter accurately.
    payload["stream_options"] = {"include_usage": True}

    fwd = forward_stream or _forward_stream
    captured: Dict[str, Any] = {}
    started = datetime.now(timezone.utc)

    def _gen() -> Iterator[bytes]:
        try:
            for chunk in fwd(settings, provider, payload):
                raw = chunk if isinstance(chunk, (bytes, bytearray)) else str(chunk).encode("utf-8")
                text = raw.decode("utf-8", "replace").strip()
                if text.startswith("data:"):
                    payload_text = text[len("data:"):].strip()
                    if payload_text and payload_text != "[DONE]":
                        try:
                            obj = json.loads(payload_text)
                            if obj.get("usage"):
                                captured["data"] = obj
                        except json.JSONDecodeError:
                            pass
                yield raw
        except Exception:  # noqa: BLE001
            _release_hold(settings, key, request_id)
            _release_limits(request_id)
            _meter(settings, key, provider=provider, requested_model=model, data={}, run_id=run_id,
                   request_id=request_id, duration_ms=0, status="error")
            raise
        duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        retail = _meter(settings, key, provider=provider, requested_model=model,
                        data=captured.get("data") or {}, run_id=run_id, request_id=request_id,
                        duration_ms=duration_ms)
        _reconcile_limits(request_id, retail)

    return _gen()


def _error_response(exc: GatewayError, request_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={"error": {"code": exc.code, "message": exc.message,
                           "request_id": request_id, "details": exc.details}},
        headers={"X-Genesis-Request-Id": request_id},
    )


@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_genesis_run_id: Optional[str] = Header(default=None),
):
    settings = get_settings()
    request_id = f"req_{uuid.uuid4().hex}"
    try:
        raw = await request.body()
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise GatewayError("invalid_request", "request body must be valid JSON", status=400)
        key = resolve_key(settings, authorization)

        if body.get("stream"):
            stream = stream_chat_completion(settings, key, body, request_id, header_run_id=x_genesis_run_id)
            return StreamingResponse(
                stream, media_type="text/event-stream",
                headers={"X-Genesis-Request-Id": request_id},
            )

        status, data, _run_id = handle_chat_completion(
            settings, key, body, request_id, header_run_id=x_genesis_run_id
        )
        return JSONResponse(status_code=status, content=data,
                            headers={"X-Genesis-Request-Id": request_id})
    except GatewayError as exc:
        return _error_response(exc, request_id)
