# Usage-based wallet billing (PR A)

Subscription-free, pay-as-you-go. **GNSIS is the source of truth** for compute
usage, available/reserved balances, usage charges, service markup, refill credits,
virtual-key budgets, and run attribution. **Stripe is the source of truth** for the
billing customer, saved payment methods, billing address, Checkout payments,
automatic tax, invoices, receipts, refunds, disputes, and the Customer Portal.

GNSIS never stores full card numbers, CVCs, billing addresses, or tax
calculations — only a workspace's Stripe Customer id and *display-safe* card
metadata (brand / last4 / expiry) read back on demand.

```
workspace ──1:1──► Stripe Customer (workspace_billing.stripe_customer_id, unique)
refill  → Checkout(customer, invoice_creation, [automatic_tax]) → hosted invoice+receipt
paid    → checkout.session.completed webhook → top_up (payment-level idempotent)
manage  → POST /v1/billing/portal → Stripe Customer Portal (cards, invoices, receipts)
```

## Persistent Customer (`stripe_customers.py`)

`get_or_create_customer(settings, workspace_id)` returns the workspace's one
Stripe Customer, creating it once. Concurrency-safe two ways: it serialises on the
`WorkspaceBilling` row lock (`with_for_update`, same anchor as balance
reservations), and passes a per-workspace Stripe **idempotency key**
(`gnsis-customer:{workspace_id}`) so even a duplicate create returns the same
Customer. The id is stored in `workspace_billing.stripe_customer_id` (unique).

## Refill Checkout (`stripe_checkout.py` → `stripe_client.py`)

Each refill reuses that Customer, sets `invoice_creation[enabled]=true` (a
finalized, paid invoice + receipt per refill), and — only when Stripe Tax is
configured — `automatic_tax[enabled]=true`. All Stripe I/O flows through the
single `stripe_client._request` seam (deep form-encoding, `Idempotency-Key`
header, typed errors).

## Payment-level idempotency (the correctness fix)

`balance_transactions` now has a unique `payment_reference` (the underlying
PaymentIntent id) **in addition to** the unique `idempotency_key` and
`stripe_event_id`. A Checkout completion and its PaymentIntent success are two
*different* Stripe events but resolve to the **same** PaymentIntent id, so they
can never credit the same payment twice — while same-event redelivery is still
caught by the event-level guards. Three independent guards, first writer wins.

## Billing summary + Portal (`wallet.py`)

- `GET /v1/billing/summary` → GNSIS-owned `balance` / `available` / `reserved` /
  `spent_this_month` (exact decimal strings) + `has_customer`, `default_card`
  (safe fields only, best-effort from Stripe), `refill_enabled`,
  `portal_available`, `tax_enabled`.
- `POST /v1/billing/portal` → a Stripe Customer Portal session URL (payment
  methods, billing info, invoices, receipts). Creates the Customer on demand so
  it works before the first refill. Workspace-isolated.

## Webhooks handled

`checkout.session.completed` (credit), `payment_intent.succeeded` (credit — safe
now that dedup is payment-level), `charge.refunded` / `refund.created` /
`charge.dispute.created` (negative entries). **No subscription events.** For the
current Checkout refill path, subscribe to `checkout.session.completed` + refunds
+ disputes (see the Dashboard report below).

## Configuration (API service)

| Var | Default | Purpose |
|---|---|---|
| `STRIPE_SECRET_KEY` | — | `sk_…`; enables Customer/Checkout/Portal calls |
| `STRIPE_WEBHOOK_SECRET` | — | `whsec_…`; verifies webhooks + enables balance enforcement |
| `GNSIS_FRONTEND_URL` | — | Checkout + Portal return URLs (also CORS) |
| `GNSIS_STRIPE_TAX_ENABLED` | `false` | Turn on `automatic_tax` — only after you activate Stripe Tax |
| `GNSIS_STRIPE_PORTAL_CONFIGURATION_ID` | — | Optional named Portal config; else the account default |
| `GNSIS_REFILL_MIN_USD` / `_MAX_USD` | `5` / `500` | Refill bounds |

Auto-refill is **not** in this PR and is not exposed as functional; it lands in PR B.

## Migration

`gnsis-migrate` adds `workspace_billing.stripe_customer_id` and
`balance_transactions.payment_reference` (additive, idempotent, re-runnable).
