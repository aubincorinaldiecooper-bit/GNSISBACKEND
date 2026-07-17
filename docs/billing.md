# Markup, prepaid balance, and Stripe refills (PR 2)

Converts a measured usage record (PR 1) into an **immutable retail charge** and a
**prepaid balance move**, and lets customers refill via Stripe. Balance is
derived from a ledger, not a mutable number. Everything monetary is
`decimal.Decimal`, stored as exact decimal strings.

```
usage_records (PR1) → charge_usage() → usage_charges (immutable, applied rate)
                                     → balance_transactions (one usage_debit)
Stripe payment → verified webhook → balance_transactions (one top_up)
balance(workspace) = Σ signed_amount        available = balance − active holds
```

## Rate service (`rates.py`)

`service_fee = upstream_cost × markup_rate`, `retail_cost = upstream_cost +
service_fee`. The markup is config-driven and **versioned** — never hardcoded.
`quote()` returns the full decision (upstream, markup rate, service fee, retail,
rate-card version), which is stored **verbatim** on each charge. Changing the
current markup never alters an existing charge; a re-read returns the original
applied rate. Corrections are explicit `credit` / `refund` / `adjustment` ledger
entries — history is never edited.

## Immutable charge + atomic debit (`billing.py`)

`charge_usage(settings, usage_record_id)` is idempotent on `usage_record_id`
(unique). It creates one `usage_charges` row + exactly one `usage_debit`
transaction in a single commit; a replayed LiteLLM callback returns the existing
charge and never applies markup or debits twice. Ledger idempotency is
structural: unique `idempotency_key` (and unique `stripe_event_id` for
Stripe rows).

Example: top-up +$25.00; a $1.00 upstream request at 5% → charge (upstream 1.00,
fee 0.05, retail 1.05) + debit −1.05 → balance **$23.95**.

## Insufficient-balance enforcement (pre-request hold)

A LiteLLM callback lands *after* compute, so the gateway places a **hold** before
the upstream request: it reserves `GNSIS_BALANCE_RESERVE_ESTIMATE_USD` against
`available = balance − active holds` (serialised per workspace via a lock
anchor, so concurrent requests cannot overspend). Zero/insufficient available
balance → the request is rejected `402` and never reaches upstream. The usage
callback **settles** the hold into the real debit (actual measured cost); an
upstream failure **releases** it; `release_stale_reservations()` frees holds
whose callback was lost (run it from the beat service). This path requires
LiteLLM (its callback does the settlement). Virtual-key (non-native) budgets are
enforced by LiteLLM key budgets (issued in PR 3).

## Stripe (`stripe_webhook.py`)

`POST /billing/stripe/webhook` — signature verified with the webhook secret
(stdlib HMAC; no Stripe SDK). Your payment-creation API must set
`metadata.workspace_id` on the PaymentIntent/Checkout Session. Then:

- `payment_intent.succeeded` / `checkout.session.completed` (paid) → one
  idempotent `top_up` (amount = minor units ÷ 100).
- `charge.refunded` / `charge.dispute.created` → explicit negative `refund` entry.
- `payment_intent.payment_failed` / `checkout.session.expired` → **no** credit.

Replaying any event (same `id`) never credits twice. Out of scope (per spec): no
subscriptions, tax, invoice PDFs, multi-currency, or annual contracts.

## Configuration (API service)

| Var | Default | Purpose |
|---|---|---|
| `GNSIS_MARKUP_RATE` | `0.05` | Current markup (decimal) |
| `GNSIS_RATE_CARD_VERSION` | `beta-2026-07` | Stamped on each charge |
| `GNSIS_DEFAULT_CURRENCY` | `USD` | — |
| `STRIPE_WEBHOOK_SECRET` | — | `whsec_…`; **enables billing enforcement** |
| `GNSIS_BALANCE_RESERVE_ESTIMATE_USD` | `0.05` | Pre-request hold size |

## Manual steps (you)

1. Set the vars above on the **API** service.
2. In Stripe, add a webhook endpoint → `{GNSIS_PUBLIC_API_URL}/billing/stripe/webhook`,
   events `payment_intent.succeeded` (or `checkout.session.completed`) + `charge.refunded`;
   copy its signing secret into `STRIPE_WEBHOOK_SECRET`.
3. Ensure your payment-creation sets `metadata.workspace_id`.
4. `gnsis-migrate` (adds `usage_charges`, `balance_transactions`,
   `balance_reservations`, `workspace_billing`).
5. (Optional) add `billing.release_stale_reservations()` to the beat schedule.
