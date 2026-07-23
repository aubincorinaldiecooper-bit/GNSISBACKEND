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

Server-controlled tools
=======================

For a **primary** agent call, the gateway *appends* two server-controlled tools
before forwarding to OpenRouter:

* ``openrouter:web_search`` — the primary can search the web on demand.
* ``openrouter:advisor`` — the primary can consult an Advisor model (pinned to
  the run's ``advisor_model``, fixed by the server so the primary cannot swap
  it). The Advisor definition includes its OWN nested ``openrouter:web_search``.

Both tools are OpenRouter-native — the search API key stays with OpenRouter and
the executor sandbox never sees any external search endpoint. **Client-supplied
``openrouter:*`` tools are rejected** with a structured error so a compromised
prompt / repository / sandbox cannot smuggle in an alternate Advisor model or
weaken the search configuration.

For a **condenser** call (context compaction, tagged by the client), the gateway
appends *nothing*: condensation is a plain model call, so its usage is trackable
separately from primary-agent work. The tool-rejection is still enforced, so the
condenser call can't smuggle in an Advisor either.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
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


def build_litellm_metadata(
    settings, run: ExecutionRunRecord, body: Dict[str, Any], event_id: str
) -> Dict[str, Any]:
    """Deterministic attribution metadata for a native run's model request.

    Every field is an explicit id from the authenticated run (or the agent's own
    request metadata for engine/phase) — never a timestamp, token total, model
    name, or ordering. LiteLLM echoes this back on its usage callback, which is
    how the measurement is tied to the exact GNSIS run, trace event, workspace,
    user, and repository.
    """
    from .. import workspaces as ws

    agent_md = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    metadata = {
        "workspace_id": run.workspace_id,
        "user_id": ws.get_owner_subject(run.workspace_id) if run.workspace_id else None,
        "run_id": run.job_id,
        "repository_id": run.repository_id,
        "model_call_event_id": event_id,
        "trace_event_id": agent_md.get("trace_event_id") or event_id,
        "engine": agent_md.get("engine") or "gnsis",
        "phase": agent_md.get("phase"),
    }
    return {k: v for k, v in metadata.items() if v is not None}


def _litellm_upstream(settings, payload: Dict[str, Any], metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Forward to the LiteLLM proxy (separate service) with attribution metadata."""
    base = settings.litellm_url.rstrip("/")
    forwarded = dict(payload)
    forwarded["metadata"] = metadata
    data = json.dumps(forwarded).encode("utf-8")
    req = urllib.request.Request(f"{base}/chat/completions", data=data, method="POST")
    req.add_header("Authorization", f"Bearer {settings.litellm_api_key}")
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


# --- Server-controlled tool injection --------------------------------------
#
# The primary agent's coding tools are ordinary function tools ("function":
# {...}). The server appends TWO more, marked with an OpenRouter-native ``type``
# prefix (``openrouter:``) that OpenRouter interprets natively. The prefix is
# also the security marker: **any client-supplied tool whose type starts with
# ``openrouter:`` is refused**, so a compromised prompt/repo/sandbox can never
# smuggle in an alternate Advisor model or weaker Web Search configuration.

#: Advertised call purposes. Primary agent calls receive Web Search + Advisor.
#: Condenser calls (context compaction) do NOT — they are plain model calls so
#: their usage is trackable separately.
CALL_PURPOSE_PRIMARY = "primary"
CALL_PURPOSE_CONDENSER = "condenser"
_VALID_CALL_PURPOSES = frozenset({CALL_PURPOSE_PRIMARY, CALL_PURPOSE_CONDENSER})

#: The exact web-search config both the primary and the Advisor use. Values
#: match the sprint's approved product decision — ``search_context_size`` is
#: deliberately NOT set so OpenRouter's adaptive default applies.
_WEB_SEARCH_TOOL: Dict[str, Any] = {
    "type": "openrouter:web_search",
    "engine": "auto",
    "max_results": 5,
    "max_total_results": 10,
}

#: The Advisor's system prompt (concise; the Advisor is a code-review peer, not
#: the primary's replacement).
_ADVISOR_INSTRUCTIONS = (
    "You are a concise senior software architect and code reviewer. When the "
    "primary agent consults you, provide a focused, actionable opinion: name "
    "the specific risk or design consideration, and give one clear "
    "recommendation. Do not restate the primary's plan back to it. If you are "
    "uncertain, say so plainly."
)


def _resolve_call_purpose(body: Dict[str, Any]) -> str:
    """Read the client-declared call purpose; default to primary.

    The purpose is set by the executor sandbox (never inferred from prompt
    content) and travels in the request's ``metadata`` so a compromised primary
    cannot claim to be a condenser after the fact to skip the tools — the
    gateway metering treats condenser calls separately regardless.
    """
    md = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    raw = str(md.get("call_purpose") or CALL_PURPOSE_PRIMARY).strip().lower()
    return raw if raw in _VALID_CALL_PURPOSES else CALL_PURPOSE_PRIMARY


def _reject_client_openrouter_tools(tools: Any) -> None:
    """Refuse any client-supplied tool whose type begins with ``openrouter:``.

    Only the server may declare openrouter-native tools; the prefix is
    reserved so the repository / user prompt / sandbox cannot smuggle in an
    alternate Advisor model, a weakened Web Search config, or a masqueraded
    server tool.
    """
    if not isinstance(tools, list):
        return
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        ttype = str(tool.get("type") or "").strip().lower()
        if ttype.startswith("openrouter:"):
            raise GatewayError(
                f"client cannot declare server-controlled tool type: {ttype}",
                status=400,
            )


def _pick_advisor_model(run: ExecutionRunRecord, settings) -> Optional[str]:
    """The Advisor model, pinned on the run (never read from the request body).

    Falls back to the primary_model (which was itself validated) if a legacy
    run has no Advisor recorded, so historical runs remain replayable.
    Returns None only if the run has neither pinned — in which case the
    Advisor tool is silently omitted for that call.
    """
    advisor = getattr(run, "advisor_model", None)
    if advisor and advisor in (settings.run_allowed_models or []):
        return advisor
    primary = getattr(run, "primary_model", None)
    if primary and primary in (settings.run_allowed_models or []):
        return primary
    return None


def _advisor_tool(advisor_model: str) -> Dict[str, Any]:
    """The openrouter:advisor tool definition — model is FIXED by the server."""
    return {
        "type": "openrouter:advisor",
        "name": "advisor",
        "description": (
            "Consult a senior software architect for a focused second opinion. "
            "Provide only the relevant context in the prompt — the Advisor does "
            "not see the primary transcript."
        ),
        # The Advisor's model is baked into the tool definition here; the
        # primary cannot replace it in a tool call because OpenRouter binds the
        # tool config server-side to what the gateway sent, not to what the
        # primary later references.
        "model": advisor_model,
        "forward_transcript": False,
        "max_tool_calls": 4,
        "max_completion_tokens": 4096,
        "instructions": _ADVISOR_INSTRUCTIONS,
        # The Advisor has its own nested Web Search so it can research current
        # APIs and dependencies independently — its search activity is metered
        # separately (recorded from OpenRouter usage details).
        "tools": [dict(_WEB_SEARCH_TOOL)],
    }


def _inject_server_tools(payload: Dict[str, Any], run: ExecutionRunRecord, settings) -> None:
    """Append the server-controlled tools to a primary-agent call.

    The client's OpenHands function tools are preserved verbatim (they carry
    the coding actions: terminal, file editor, task tracker). We only *append*
    — never mutate a client tool — so the client's tool set stays intact and
    the appended entries are visibly server-owned.
    """
    tools = list(payload.get("tools") or [])
    tools.append(dict(_WEB_SEARCH_TOOL))
    advisor = _pick_advisor_model(run, settings)
    if advisor:
        tools.append(_advisor_tool(advisor))
    payload["tools"] = tools


def handle_chat_completion(
    settings,
    store: ExecutionStore,
    run: ExecutionRunRecord,
    body: Dict[str, Any],
    *,
    upstream: Optional[Callable[[Any, Dict[str, Any]], Dict[str, Any]]] = None,
) -> Tuple[int, Dict[str, Any]]:
    """Validate, budget-check, forward, and account one chat-completion call."""
    if not (settings.litellm_enabled or settings.openrouter_api_key):
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

    # Correlation key attached to the LiteLLM request so its usage callback can be
    # tied back to this exact model call (also stored on the model-call row).
    event_id = uuid.uuid4().hex

    # Pre-request balance control: place an estimated hold so concurrent requests
    # cannot overspend before the actual cost is known. Requires LiteLLM (its
    # usage callback settles the hold into the real debit or releases it).
    billing_hold = bool(
        settings.billing_enabled and settings.litellm_enabled and run.workspace_id
    )
    if billing_hold:
        from ..billing import BillingStore

        if not BillingStore().reserve(run.workspace_id, settings.balance_reserve_estimate_usd, event_id):
            raise GatewayError("insufficient balance", status=402)

    # Reserve a model-call slot within budget (atomic).
    ok, reason = store.reserve_model_call(run.id)
    if not ok:
        if billing_hold:
            from ..billing import BillingStore

            BillingStore().release(event_id)
        raise GatewayError(f"model budget: {reason}", status=402)

    # Reject any client-supplied openrouter:* tool BEFORE building the payload,
    # so a smuggled server-tool masquerade never reaches OpenRouter — even
    # for a condenser call where we would otherwise pass the tools through.
    _reject_client_openrouter_tools(body.get("tools"))

    # Build a sanitised upstream payload — only known fields, forced model.
    payload = {k: body[k] for k in _FORWARD_FIELDS if k in body}
    payload["model"] = model

    # Append the server-controlled tools (Web Search + Advisor) for primary
    # agent calls only. Condenser calls stay plain so their usage is trackable
    # separately from primary-agent work — a condenser must not consult the
    # Advisor or hit the web.
    call_purpose = _resolve_call_purpose(body)
    if call_purpose == CALL_PURPOSE_PRIMARY:
        _inject_server_tools(payload, run, settings)

    try:
        if upstream is not None:
            data = upstream(settings, payload)
        elif settings.litellm_enabled:
            metadata = build_litellm_metadata(settings, run, body, event_id)
            data = _litellm_upstream(settings, payload, metadata)
        else:
            data = _default_upstream(settings, payload)
    except Exception:
        if billing_hold:
            from ..billing import BillingStore

            BillingStore().release(event_id)
        raise

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
        event_id=event_id,
    )
    if not within:
        # This call pushed the run over budget: no further calls may be made.
        store.revoke_token(run.id)

    return 200, data
