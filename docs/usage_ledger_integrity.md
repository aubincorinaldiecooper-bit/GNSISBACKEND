# Usage-ledger integrity + idempotency (PR-G1)

First hardening step of the Genesis gateway program. Protects the append-only
usage ledger's correctness before the public gateway, versioned pricing, and
concurrency-safe limits build on top of it. No behavior change for existing
callers — every field is additive with safe defaults.

## New fields on `usage_records`

| Field | Meaning |
|---|---|
| `idempotency_key` (unique, nullable) | Caller-supplied logical-operation key. A provider retry or webhook redelivery that reuses it is deduped — **never a second billable row**. Distinct from `litellm_request_id` (the provider/callback dedup key). |
| `provider_request_id` | The provider's own request id, for provider-side reconciliation and telling a retry from a new call. |
| `upstream_cost` | Provider-**reported** cost, kept verbatim. |
| `genesis_calculated_cost` | Genesis's own cost from versioned pricing (populated in the pricing PR). Stored **separately** so neither overwrites the other and discrepancies can be flagged. |
| `cost_source` | `provider_reported` or `unknown`. |
| `reconciliation_state` | `resolved` or `needs_reconciliation`. |
| `error_category` | Classified failure bucket (e.g. `rate_limited`, `provider_timeout`). |

## The no-silent-$0 rule

A **successful** request whose cost is unknown is recorded with
`cost_source="unknown"` and `reconciliation_state="needs_reconciliation"` — it is
**not** treated as $0 and **not** charged. `BillingStore.charge_usage` skips such
a row (returns "not charged", releasing any pre-request hold) and leaves it
flagged, so missing cost surfaces instead of quietly under-billing. A **failed**
request legitimately carries no charge and stays `resolved`.

Surface the flagged rows with `UsageStore.list_needs_reconciliation(workspace_id)`
/ `count_needs_reconciliation(workspace_id)`.

## Idempotency vocabulary (distinguished, not conflated)

- **Provider retry of the same request** → same `idempotency_key` ⇒ deduped, billed once.
- **Second model call in the same run** → new `idempotency_key`/`litellm_request_id` ⇒ a distinct billable row (same `run_id`).
- **Webhook redelivery** → same event/id ⇒ deduped.
- **Repeated user action** → the caller supplies distinct keys for distinct intents.

## Migration

`gnsis-migrate` adds the six columns above (additive, idempotent, re-runnable).
No new environment variables.

## Deferred to later gateway PRs

`genesis_calculated_cost` is populated by the **versioned pricing** PR; the
public `POST /v1/chat/completions` gateway that sets these fields inline (rather
than via the LiteLLM callback) is a later PR. This PR makes the ledger *able* to
record them correctly first.
