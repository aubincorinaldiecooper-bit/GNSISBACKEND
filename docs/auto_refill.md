# Production auto-refill (PR B)

Off-session prepaid top-ups with hard guardrails. When a workspace's **available**
balance falls below its configured threshold, GNSIS charges the saved card
off-session and — only once Stripe confirms — credits the wallet. No balance is
ever credited before Stripe confirms a successful payment.

```
beat sweep (60s) ─► evaluate_and_maybe_refill(workspace)   [per-workspace lock]
  balance < threshold? not paused/cooldown? under caps? no active attempt?
    └─► create attempt (processing) ─► off-session PaymentIntent (confirm, off_session)
          succeeded  → top_up (payment-level idempotent) → attempt succeeded
          auth req.  → attempt requires_action (needs on-session) → cooldown
          declined   → attempt failed → cooldown → auto-pause after N failures
webhook payment_intent.succeeded/.payment_failed ─► reconcile the attempt
```

## Trigger — off the request path

A Celery **beat sweep** (`gnsis.auto_refill_sweep`, every 60s) evaluates eligible
workspaces. Auto-refill is deliberately **not** triggered from the metering
callback: a broker/worker outage must never block metering, and a periodic sweep
plus the per-workspace lock is a simpler, safer trigger than a request-path
enqueue. Latency to a refill is at most one sweep interval; the balance
reservation/hold system prevents overspend in the meantime.

## Concurrency & exactly-once

- **Per-workspace row lock** (the `WorkspaceBilling` anchor, `with_for_update`)
  wraps the "should we refill + create the attempt" decision.
- **Single-active-attempt invariant**: a workspace with a `pending`/`processing`/
  `requires_action` attempt starts no new one. Simultaneous threshold crossings
  and duplicate worker deliveries therefore collapse to at most one in-flight
  refill.
- **Deterministic Stripe idempotency key** per attempt (`gnsis-autorefill:{id}`),
  so retrying the same attempt never creates a second PaymentIntent.
- **Payment-level ledger idempotency** (PR A): crediting keys on the PaymentIntent
  id, so the synchronous success path and the webhook can never double-credit.

## State machine (`auto_refill_attempts`, immutable audit)

`pending → processing → succeeded | requires_action | failed | cancelled`. Each
row records the trigger balance, threshold, refill amount, currency, PaymentIntent
id, failure code/message, and timestamps — an immutable audit trail.

## Failure handling

Stripe errors are classified: `authentication_required` → `requires_action`
(recoverable on-session); `card_declined` / `expired_card` / `insufficient_funds`
→ `failed`. Any non-success sets a **cooldown** (`AUTO_REFILL` waits ~1h) and
increments a consecutive-failure counter; after **3** consecutive failures the
config is **auto-paused** (`paused=true`) and no further attempts run until the
user re-enables. Re-enabling clears the pause, streak, and cooldown.

## Consent & caps (enforced server-side)

Enabling requires **explicit, timestamped consent** (`consent` + `consent_at`) to
off-session charging **and** a saved default payment method. Per-workspace limits,
all validated and enforced before any charge:

- `threshold` — trigger level;
- `refill_amount` — amount per top-up (≤ `max_refill_amount` ≤ account max);
- `max_refills_per_day` — count cap;
- `daily_cap` — total auto-refill $/day;
- `monthly_cap` — optional total $/month.

## API (workspace-scoped)

- `GET /v1/billing/auto-refill` → `{config, attempts}`. `config.active` is true
  only when `enabled && consent && payment_method && !paused` — the UI must not
  imply auto-refill is on otherwise.
- `PUT /v1/billing/auto-refill` → save the policy. The workspace's **default
  Stripe payment method** is resolved automatically for off-session charges;
  enabling without consent or without a saved card is rejected (`400`).

## Webhooks

Add `payment_intent.succeeded` and `payment_intent.payment_failed` (both safe
now that crediting is payment-level idempotent). Success credits + reconciles the
attempt; failure never credits and advances the failure streak / cooldown /
pause. Still no subscription events.

## Migration

`gnsis-migrate` adds `auto_refill_config` + `auto_refill_attempts` (new tables;
additive, idempotent).
