# Public OpenAI-compatible gateway (G4)

Point any OpenAI-compatible client at Genesis and present a Genesis virtual key.
Genesis authenticates, attributes, checks balance, forwards to the provider,
meters inline with versioned pricing, records one immutable usage event, and
returns an OpenAI-compatible response plus a Genesis request id.

```
client (base_url = Genesis, api_key = gns_…)  →  POST /v1/chat/completions
  request id ▸ authenticate key ▸ attribute (ws/project/env/user/team) ▸
  check provider+model allowlist ▸ reserve balance ▸ forward ▸ capture usage ▸
  provider cost (or versioned pricing) ▸ service fee (separate) ▸ usage event ▸
  settle hold ▸ response + X-Genesis-Request-Id
```

## Request flow (the 17 steps)

Implemented in `public_gateway.py`: request id, key auth (G2), attribution from
key scopes, run-id resolve/create, provider+model allowlist, **balance check
before forwarding** (concurrency-safe reservation hold), forward, streaming
pass-through, usage capture, **provider cost** (provider-reported when given,
else versioned pricing — G3), **Genesis service fee computed separately**,
immutable **usage event** (G1), balance reconciliation (the hold settles into the
real debit), and the `X-Genesis-Request-Id` response header. Prompts and
responses are **not** stored.

## Auth

A Genesis virtual key (`gns_live_/gns_test_`) in `Authorization: Bearer …` — a
**separate** auth method from the dashboard session and the internal admin key.
Unknown / disabled / rotated / expired keys all return `401` identically.

## Cost separation

`provider_cost` (reported or Genesis-calculated) and the `service_fee` are stored
separately on the usage event / charge. A row with unknown provider cost **and**
no pricing is flagged `needs_reconciliation` — never billed a silent $0.

## Streaming

`"stream": true` is passed through untouched (SSE). Genesis injects
`stream_options.include_usage` and meters from the final usage chunk once the
stream ends. If the provider sends no usage chunk, the event is recorded
`needs_reconciliation` (reason `missing_usage`) rather than guessed: the
pre-request hold is released and **no charge is created** — a $0 cost computed
from zero tokens is never settled as free.

## Structured errors

```json
{"error": {"code": "insufficient_balance",
           "message": "This request would exceed the available balance.",
           "request_id": "req_…", "details": {"scope": "workspace"}}}
```

Codes include `missing_credential`, `invalid_key`, `model_not_allowed`,
`provider_not_allowed`, `insufficient_balance`, `provider_error`,
`gateway_not_configured`. Every response (success or error) carries
`X-Genesis-Request-Id`.

## Extensibility

The flow is a per-endpoint handler over shared steps, so `POST /v1/responses`,
`/v1/embeddings`, and `/v1/audio/transcriptions` can be added later without
redesigning it. **Only `/v1/chat/completions` is implemented today** — no
compatibility is claimed for endpoints that are not.

## Config

Uses `OPENROUTER_API_KEY` as the default upstream (it routes by `provider/model`);
`OPENROUTER_BASE_URL` overrides the endpoint. The forwarding functions
(`_forward` / `_forward_stream`) are a seam — provider-specific credential
resolution can slot in behind them without touching the flow. Billing enforcement
requires the balance/Stripe config (see `docs/wallet_billing.md`).

## Deferred (next PRs)

- Per-scope **configurable spending limits** (per-key/project/etc.) — G5. G4 does
  the concurrency-safe **balance** check only; the per-key limit fields on the key
  are stored (G2) but not yet enforced.
- First-class **run receipts** — G6 (usage already carries `run_id`).
- Cursor pagination + full REST resource surface + OpenAPI — G7.
