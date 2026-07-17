# LiteLLM metering + trace correlation (PR 1)

GNSIS meters model compute by routing native requests through **LiteLLM** (a
separate service) and correlating LiteLLM's measured usage back to the exact
GNSIS records via metadata GNSIS attaches to each request. This PR measures and
attributes only — markup, charges, and balance are PR 2.

```
execution_model_call (existing; gateway attaches a correlation event_id)
  → GNSIS gateway → LiteLLM → provider
  → LiteLLM usage callback → POST /internal/usage/litellm/callback
  → one usage_records row, idempotent on litellm_request_id
```

## Configuration (API service only)

The model gateway runs in the **API** service, so set these there. When unset,
the gateway keeps its current direct OpenRouter path (nothing changes).

| Var | Purpose |
|---|---|
| `GNSIS_LITELLM_URL` | LiteLLM proxy base URL (OpenAI-compatible), e.g. `https://gnsis-litellm.up.railway.app` |
| `GNSIS_LITELLM_API_KEY` | The key GNSIS uses to call LiteLLM |
| `GNSIS_LITELLM_CALLBACK_SECRET` | Shared secret LiteLLM must send on the usage callback |

## Request metadata contract (GNSIS → LiteLLM)

For every native run request, the gateway attaches a `metadata` object built
**only** from explicit ids (never timestamps/tokens/model/ordering):

```json
{
  "workspace_id": "ws_…",
  "user_id": "<better-auth-subject>",
  "run_id": "job_…",
  "repository_id": "repo_…",
  "model_call_event_id": "<uuid>",
  "trace_event_id": "<uuid>",
  "engine": "gnsis",
  "phase": "implementation"
}
```

For **non-native** usage, issue a LiteLLM virtual key whose metadata carries:
`workspace_id`, `user_id`, `team_id` (nullable), `application_name`,
`environment` (nullable). LiteLLM includes it on the callback; GNSIS records it
with `run_id`/`trace_event_id` null.

## Callback contract (LiteLLM → GNSIS)

`POST {GNSIS_PUBLIC_API_URL}/internal/usage/litellm/callback`
Header: `Authorization: Bearer <GNSIS_LITELLM_CALLBACK_SECRET>`

Body (GNSIS-shaped — **not** LiteLLM's internal schema):

```json
{
  "litellm_request_id": "<litellm call id, unique>",
  "provider": "anthropic",
  "model": "anthropic/claude-opus-4.8",
  "input_tokens": 100,
  "output_tokens": 50,
  "cached_tokens": 0,
  "reasoning_tokens": 0,
  "duration_ms": 1840,
  "request_status": "success",          // or "failure"
  "upstream_cost": "0.00012345",         // exact decimal string
  "currency": "USD",
  "retry_of": null,                      // litellm_request_id of the original, if a retry
  "metadata": { …the request metadata above… }
}
```

Response: `200 {"accepted": true, "duplicate": <bool>, "usage_id": "…", "litellm_request_id": "…"}`.
Replaying the same `litellm_request_id` returns `duplicate: true` and never
creates a second row (idempotent). Failed requests and retries are persisted and
remain visible/distinguishable.

## LiteLLM adapter (map LiteLLM → the contract)

Deploy this as a LiteLLM custom logger so its success/failure hook posts the
contract above. It reads the request metadata LiteLLM echoes and LiteLLM's own
measured usage/cost/latency — it does not couple GNSIS to LiteLLM's DB.

```python
# gnsis_callback.py  (referenced from LiteLLM config: litellm_settings.callbacks)
import os, urllib.request, json
from litellm.integrations.custom_logger import CustomLogger

class GnsisUsage(CustomLogger):
    def _send(self, kwargs, response_obj, start, end, status):
        std = kwargs.get("standard_logging_object") or {}
        md = (std.get("metadata") or {}).get("requester_metadata") or kwargs.get("litellm_params", {}).get("metadata") or {}
        usage = (std.get("response") or {}).get("usage") or {}
        payload = {
            "litellm_request_id": std.get("id") or kwargs.get("litellm_call_id"),
            "provider": std.get("custom_llm_provider") or kwargs.get("custom_llm_provider") or "",
            "model": std.get("model") or kwargs.get("model") or "",
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "cached_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
            "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0),
            "duration_ms": int((end - start).total_seconds() * 1000) if start and end else 0,
            "request_status": status,
            "upstream_cost": str(std.get("response_cost") or 0),
            "currency": "USD",
            "metadata": md,
        }
        req = urllib.request.Request(
            os.environ["GNSIS_USAGE_CALLBACK_URL"], method="POST",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + os.environ["GNSIS_LITELLM_CALLBACK_SECRET"]},
        )
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass  # GNSIS reconciliation-friendly; a lost callback is safely retryable

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._send(kwargs, response_obj, start_time, end_time, "success")
    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        self._send(kwargs, response_obj, start_time, end_time, "failure")

gnsis_usage = GnsisUsage()
```

## Railway deployment (LiteLLM as a separate service — you)

1. New Railway service `GNSISLITELLM` from the LiteLLM proxy image
   (`ghcr.io/berriai/litellm:main-stable`).
2. Provide a `litellm_config.yaml` with your provider keys (OpenRouter/Anthropic)
   and `litellm_settings: { callbacks: ["gnsis_callback.gnsis_usage"] }`, plus a
   master key.
3. Env on `GNSISLITELLM`: your provider keys, `GNSIS_USAGE_CALLBACK_URL=
   {GNSIS_PUBLIC_API_URL}/internal/usage/litellm/callback`, and
   `GNSIS_LITELLM_CALLBACK_SECRET=<shared secret>`.
4. Env on **GNSISBACKEND (API)**: `GNSIS_LITELLM_URL`, `GNSIS_LITELLM_API_KEY`,
   `GNSIS_LITELLM_CALLBACK_SECRET` (same secret).
5. Migration: `gnsis-migrate` (adds `usage_records` + the `event_id` column).

The GNSIS FastAPI process never embeds the LiteLLM proxy.
