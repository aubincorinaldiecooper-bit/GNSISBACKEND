"""The restricted, OpenAI-compatible model gateway.

The agent container never holds a model-provider key. It calls this endpoint with
only its short-lived run token; the backend (and only the backend) attaches the
real OpenRouter credential and forwards a *sanitised* request to the single
allowed upstream path. Everything is bounded: the model must be on the
server-controlled allowlist, per-request output is capped, and the run's call
count / token totals / dollar spend are enforced atomically in the store. Usage
is recorded on every call and attached to the run receipt; a call that pushes the
run over budget revokes the token so no further calls succeed. It is deliberately
not a general HTTP proxy — no client-chosen upstream, no path but chat-completions.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional, Tuple

from .models import ExecutionRunRecord
from .store import ExecutionStore

# Only these fields are forwarded upstream; anything else (e.g. a smuggled
# ``base_url``/``api_base``) is dropped so the gateway cannot be redirected.
_FORWARD_FIELDS = (
    "model",
    "messages",
    "max_tokens",
    "temperature",
    "top_p",
    "stop",
    "tools",
    "tool_choice",
    "response_format",
)

# Conservative per-token USD rates by model prefix (input, output).
_RATES = {
    "anthropic/claude-opus": (1.5e-5, 7.5e-5),
    "anthropic/claude-sonnet": (3.0e-6, 1.5e-5),
    "anthropic/claude-haiku": (8.0e-7, 4.0e-6),
}
_DEFAULT_RATE = (1.5e-5, 7.5e-5)


class GatewayError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status
        self.message = message


def _estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    rate_in, rate_out = _DEFAULT_RATE
    for prefix, rate in _RATES.items():
        if model.startswith(prefix):
            rate_in, rate_out = rate
            break
    return input_tokens * rate_in + output_tokens * rate_out


def _default_upstream(settings, payload: Dict[str, Any]) -> Dict[str, Any]:
    base = (
        (settings.openrouter_api_key and __import__("os").environ.get("OPENROUTER_BASE_URL"))
        or "https://openrouter.ai/api/v1"
    ).rstrip("/")
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"{base}/chat/completions", data=body, method="POST")
    req.add_header("Authorization", f"Bearer {settings.openrouter_api_key}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "gnsis-gateway")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise GatewayError(f"upstream error {exc.code}: {detail[:300]}", status=502) from exc
    except urllib.error.URLError as exc:
        raise GatewayError(f"upstream unreachable: {exc.reason}", status=502) from exc


def handle_chat_completion(
    settings,
    store: ExecutionStore,
    run: ExecutionRunRecord,
    body: Dict[str, Any],
    *,
    upstream: Optional[Callable[[Any, Dict[str, Any]], Dict[str, Any]]] = None,
) -> Tuple[int, Dict[str, Any]]:
    """Validate, budget-check, forward, and account one chat-completion call."""
    if not settings.openrouter_api_key:
        raise GatewayError("model gateway not configured", status=503)
    if not isinstance(body, dict):
        raise GatewayError("invalid request body")

    model = body.get("model")
    if not model or model not in settings.run_allowed_models:
        raise GatewayError(f"model not allowed: {model}", status=403)

    max_tokens = body.get("max_tokens")
    if max_tokens is not None:
        try:
            max_tokens = int(max_tokens)
        except (TypeError, ValueError):
            raise GatewayError("max_tokens must be an integer")
        if max_tokens > settings.run_max_output_tokens:
            raise GatewayError(
                f"max_tokens {max_tokens} exceeds per-run output limit", status=403
            )

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise GatewayError("messages is required")

    # Reserve a call slot within budget (atomic).
    ok, reason = store.reserve_model_call(run.id)
    if not ok:
        raise GatewayError(f"model budget: {reason}", status=402)

    # Build a sanitised upstream payload — only known fields, forced model.
    payload = {k: body[k] for k in _FORWARD_FIELDS if k in body}
    payload["model"] = model

    caller = upstream or _default_upstream
    data = caller(settings, payload)

    usage = (data or {}).get("usage") or {}
    input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    cost = usage.get("cost")
    cost_usd = float(cost) if cost is not None else _estimate_cost(model, input_tokens, output_tokens)

    within, totals = store.record_model_usage(
        run.id,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
    )
    if not within:
        # This call pushed the run over budget: no further calls may be made.
        store.revoke_token(run.id)

    return 200, data
