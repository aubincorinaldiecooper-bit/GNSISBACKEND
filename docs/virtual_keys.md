# Customer virtual keys + budgets (PR 3.1)

Lets a customer mint a key to call the model proxy **directly** (outside a native
GNSIS run) with a per-key spend budget. GNSIS issues the key through LiteLLM's
admin API, stamps attribution metadata so the usage flows back through the same
metering (PR 1) + billing (PR 2) path, and stores only LiteLLM's hashed **token**
and a display prefix — **never the secret**, which is shown to the caller exactly
once at creation.

```
POST /v1/dashboard/keys → LiteLLM /key/generate (master key)
   → returns secret ONCE + stores {token, prefix, budget}  (virtual_keys)
customer uses key → LiteLLM enforces the per-key budget
   → usage callback (metadata.workspace_id / application_name)
   → usage_records (PR1) → usage_charges + balance debit (PR2) → dashboard (PR3)
```

## Endpoints (user JWT; workspace-scoped)

| Method / path | Behaviour |
|---|---|
| `GET /v1/dashboard/keys` | List this workspace's keys (active + revoked) + an `enabled` flag. Works even when issuance is unconfigured (returns what's stored). |
| `POST /v1/dashboard/keys` | Body `{"key_alias","max_budget_usd"?,"budget_duration"?,"models"?}` → mints a key. Response `{"key": <secret, shown once>, "virtual_key": {...}, "warning": …}`. `503` if not configured; `400` for an invalid/over-cap budget. |
| `DELETE /v1/dashboard/keys/{id}` | Revoke in LiteLLM, then mark revoked. Idempotent; `404` if the key isn't in your workspace. |

## Budgets

`max_budget_usd` defaults to `GNSIS_VIRTUAL_KEY_DEFAULT_BUDGET_USD` and is **capped**
at `GNSIS_VIRTUAL_KEY_MAX_BUDGET_USD`. It is validated as a positive decimal and
stored as an exact decimal string; only the value sent to LiteLLM is a JSON number
(their wire format). Enforcement of the cap on live spend is LiteLLM's — the key
stops working once its budget is exhausted. `budget_duration` (e.g. `30d`,
`monthly`) is passed through to LiteLLM unchanged.

## Security

- The LiteLLM **master key** (`GNSIS_LITELLM_MASTER_KEY`) is used only server-side
  to call the admin API. It never reaches a browser, an agent, or a run token.
- The customer's key **secret** is returned once and never persisted; the row keeps
  LiteLLM's hashed `token` (used to revoke/inspect) and a `sk-…abcd` display prefix.
- All reads/writes are workspace-isolated: a key can only be listed or revoked by
  the workspace that owns it.

## LiteLLM admin contract (documented, not the SDK)

`litellm_admin.py` speaks a small slice of the proxy admin API with the stdlib:

- `POST {LITELLM_URL}/key/generate` — `{key_alias, max_budget, budget_duration, models, metadata}` → `{key, token, key_name}`.
- `POST {LITELLM_URL}/key/delete` — `{"keys": [token]}`.
- `GET  {LITELLM_URL}/key/info?key={token}` — live spend/budget (optional).

## Configuration (API service)

| Var | Default | Purpose |
|---|---|---|
| `GNSIS_LITELLM_URL` | — | LiteLLM proxy base URL (shared with metering) |
| `GNSIS_LITELLM_MASTER_KEY` | — | Admin credential; **enables virtual keys** |
| `GNSIS_VIRTUAL_KEY_DEFAULT_BUDGET_USD` | `10` | Applied when the customer names no budget |
| `GNSIS_VIRTUAL_KEY_MAX_BUDGET_USD` | `50` | Per-key ceiling a customer may set |

`virtual_keys_enabled` is true only when both `GNSIS_LITELLM_URL` and
`GNSIS_LITELLM_MASTER_KEY` are set.

## Manual steps (you)

1. Set `GNSIS_LITELLM_MASTER_KEY` (and confirm `GNSIS_LITELLM_URL`) on the **API** service.
2. Ensure your LiteLLM proxy forwards `metadata.workspace_id` / `application_name`
   on its usage callback (already required by PR 1) so virtual-key usage is billed.
3. `gnsis-migrate` (adds the `virtual_keys` table — additive, idempotent).
