# Utility dashboard APIs + Stripe refill (PR 3)

Customer-facing, **read-only** views over the facts recorded by metering (PR 1,
`usage_records`) and billing (PR 2, `usage_charges` / `balance_transactions`),
plus the **initiation** half of a Stripe refill. No new tables, no second trace
system — the dashboard only reads what PRs 1–2 already write. Every endpoint is
scoped to the caller's workspace (resolved from the Better Auth JWT, never from
the request body), so no data crosses a workspace boundary. All money is returned
as an **exact decimal string**.

```
usage_records + usage_charges  ─┐
balance_transactions           ─┼─►  DashboardStore (read-only)  ─►  /v1/dashboard/*
jobs (execution runs)          ─┘
Stripe Checkout  ◄── POST /v1/billing/refill ──  (webhook credits the balance, PR 2)
```

## Endpoints (all require a user JWT; all workspace-scoped)

| Method / path | Returns |
|---|---|
| `GET /v1/dashboard/overview` | `balance`, `available`, `on_hold`, `spent_30d`, `spent_total`, `usage_count`, `charge_count`, `run_count`, `last_activity_at`, `currency`, `billing_enabled`, `refill_enabled` |
| `GET /v1/dashboard/usage?limit=&offset=` | Usage ledger: each `usage_record` left-joined to its immutable charge — provider, model, engine, phase, run_id, tokens, `upstream_cost`, and `retail_cost` / `markup_rate` / `service_fee` / `billing_status` (null for failed / zero-cost calls). Paginated with `total`. |
| `GET /v1/dashboard/transactions?limit=&offset=` | Balance ledger: `transaction_type` (`top_up` / `usage_debit` / `refund` / `credit` / `adjustment`), `signed_amount`, `currency`, `reference`, `created_at`. Paginated with `total`. |
| `GET /v1/dashboard/runs?limit=` | The caller's runs (jobs) with per-run retail `spend` aggregated from `usage_charges.run_id`. |
| `POST /v1/billing/refill` | Body `{"amount_usd": "25"}` → creates a Stripe Checkout Session and returns its `url` (+ `session_id`). `503` if refills aren't configured; `400` for an out-of-range amount. |

`limit` is clamped to `1..200`; `offset` floors at `0`.

## Refill flow (`stripe_checkout.py`)

`POST /v1/billing/refill` opens a hosted Stripe Checkout page for a one-time
top-up. The Checkout Session is created with a plain form-encoded POST via the
stdlib — **no Stripe SDK**. It stamps `metadata.workspace_id` on **both** the
session and the resulting PaymentIntent, so whichever event the operator
subscribes to (`checkout.session.completed` or `payment_intent.succeeded`) carries
the attribution the PR 2 webhook reads. The browser redirect **never** credits a
balance — only the signed webhook does, idempotently. Success/cancel links return
to `{GNSIS_FRONTEND_URL}/billing`.

## Configuration (API service)

| Var | Default | Purpose |
|---|---|---|
| `STRIPE_SECRET_KEY` | — | `sk_live_…` / `sk_test_…`; **enables refills** (with a frontend URL) |
| `GNSIS_FRONTEND_URL` | — | Checkout success/cancel return base (already used for CORS) |
| `GNSIS_REFILL_MIN_USD` | `5` | Minimum refill amount |
| `GNSIS_REFILL_MAX_USD` | `500` | Maximum refill amount |
| `STRIPE_API_BASE` | `https://api.stripe.com` | Override only for testing |

`refill_enabled` is true only when **both** `STRIPE_SECRET_KEY` and
`GNSIS_FRONTEND_URL` are set. Balance enforcement itself (PR 2) still keys off
`STRIPE_WEBHOOK_SECRET`; refill initiation and webhook crediting are independent
so you can enable them in either order.

## Manual steps (you)

1. Set `STRIPE_SECRET_KEY` (and confirm `GNSIS_FRONTEND_URL`) on the **API** service.
2. Keep the PR 2 webhook (`STRIPE_WEBHOOK_SECRET`) configured so completed
   payments actually credit the balance.
3. Optionally tune `GNSIS_REFILL_MIN_USD` / `GNSIS_REFILL_MAX_USD`.

No migration is required — PR 3 adds no tables or columns.
