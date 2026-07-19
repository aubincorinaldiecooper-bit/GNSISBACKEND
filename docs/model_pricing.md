# Versioned model pricing + cost separation (G3)

Provider prices live in a **time-versioned table**, not hardcoded. Each usage
event references the pricing version effective when it happened, so a price change
never rewrites historical cost. The **provider-reported** cost and the
**Genesis-calculated** cost are kept as separate values; a meaningful gap is
flagged, never silently overwritten.

## `model_pricing`

One row per (provider, model) time window. `id` **is** the pricing version id.
Fields: per-token `input_price` / `output_price` / `cached_input_price?` /
`reasoning_price?` (exact decimal strings), `currency`, `effective_start`,
`effective_end` (NULL = current), `source`. Publishing a new price closes the
previous open window at the new start, so windows never overlap.

## Cost calculation

`cached_tokens` is the cached subset **within** `prompt_tokens` and
`reasoning_tokens` the reasoning subset **within** `completion_tokens` (OpenAI /
LiteLLM usage semantics), so `calculate_cost` prices each subset once at its
special rate and the *remainder* at the base rate:
`(input−cached)·input_price + cached·cached_input_price + (output−reasoning)·output_price + reasoning·reasoning_price`.
The subset is never added on top of the aggregate (that would double-charge
those tokens and spuriously inflate the provider-vs-Genesis discrepancy).
`cached_input_price` defaults to `input_price` and `reasoning_price` to
`output_price` when unset; each subset is clamped to its aggregate so malformed
detail counts can't go negative or over-count. The result is an exact decimal
string.

## Reconciliation on each usage event (`price_usage_record`)

Runs when a usage row is first recorded (before charging):

| provider cost | priced? | outcome |
|---|---|---|
| known | yes | store Genesis cost + version; **bill on the provider figure**; flag `cost_discrepancy` if they differ > 5% (both values kept) |
| unknown | yes | store Genesis cost + version; **resolve**; bill on the Genesis figure |
| unknown | no | `needs_reconciliation`, reason `unknown_pricing` — **never a silent $0** |
| known | no | stays billable on the provider figure (Genesis cost null) |

Billing (`charge_usage`) uses the provider-reported cost when known, otherwise the
Genesis-calculated cost, and still skips any row left `needs_reconciliation`.

## Historical preservation

`price_usage_record` resolves the version effective at the row's `created_at`, so
re-running it after a rate change does **not** reprice old events — the stored
`pricing_version_id` and `genesis_calculated_cost` are stable.

## API

| Method / path | Auth | Behaviour |
|---|---|---|
| `GET /v1/pricing?provider=` | user session | Current rate card |
| `POST /v1/pricing` | internal admin key | Publish a new version (closes the prior open window) |

## Migration

`gnsis-migrate` adds the `model_pricing` table + `usage_records.pricing_version_id`
and `usage_records.reconciliation_reason` (additive, idempotent).

## Program position

Populates the `genesis_calculated_cost` field G1 added. The public gateway (G4)
will call this inline; the limits engine (G5) reads per-key limits. No hardcoded
prices remain in the metering/billing path (the executor gateway's legacy
`_RATES` estimate is superseded once G4 lands).
