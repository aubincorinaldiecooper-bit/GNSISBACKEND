# Stripe Dashboard configuration — required manual steps

> **These are settings you must configure in the Stripe Dashboard and your tax/
> legal setup. Backend support existing in the code does NOT mean any of the
> below is done.** Nothing here is completed by merging a PR. In particular,
> enabling Stripe Tax in code (`GNSIS_STRIPE_TAX_ENABLED=true`) does not register
> you to collect tax anywhere — that is a legal/registration step only you can do.

## 1. API keys & webhook (required for refills)

1. **API keys** → copy the **Secret key** (`sk_live_…` / `sk_test_…`) into
   `STRIPE_SECRET_KEY` on the API service.
2. **Developers → Webhooks → Add endpoint** →
   `{GNSIS_FRONTEND_URL_or_API_URL}/billing/stripe/webhook` on the **API** service.
   Subscribe to exactly:
   - `checkout.session.completed`
   - `charge.refunded`
   - `charge.dispute.created`
   Copy the endpoint's **Signing secret** (`whsec_…`) into `STRIPE_WEBHOOK_SECRET`.
   Do **not** add subscription events. `payment_intent.succeeded` is handled
   safely by the code (payment-level dedup) but is not required for the Checkout
   refill path — leave it off until PR B (auto-refill) needs it.

## 2. Customer Portal (required for "Manage billing")

**Settings → Billing → Customer portal**:
- Turn **on** the portal.
- Enable: update **payment methods**, update **billing information**, and view
  **invoice history**.
- Since this is subscription-free, leave subscription cancel/update features off.
- (Optional) create a named configuration and put its id in
  `GNSIS_STRIPE_PORTAL_CONFIGURATION_ID`; otherwise the account default is used.
- Set your business name / branding / support info — the portal shows these.

## 3. Invoices & receipts

- Checkout is created with `invoice_creation.enabled = true`, so each successful
  refill produces a finalized, paid invoice.
- **Settings → Billing → Invoices**: set your invoice/receipt branding, footer,
  and (if desired) automatic **email receipts** to customers.

## 4. Stripe Tax (OPTIONAL — only if you will actually collect tax)

Leave `GNSIS_STRIPE_TAX_ENABLED` unset until **all** of the following are true.
Turning it on before you are registered can produce incorrect or non-compliant
tax collection.

1. **Settings → Tax**: activate **Stripe Tax**.
2. Set your **business origin address**.
3. Add every **tax registration** (jurisdiction) where you are registered to
   collect. Stripe only collects where you have added a registration.
4. Choose the correct **product tax code** for AI/SaaS compute in your
   jurisdictions. This code is **not** hard-coded in GNSIS — set it as the default
   tax code / on the product in Stripe, or extend the Checkout line item. Confirm
   the code with your tax advisor; do not assume the default is correct.
5. Only then set `GNSIS_STRIPE_TAX_ENABLED=true` on the API service.

> The code path enables `automatic_tax` on Checkout when the flag is set; it makes
> no claim that your registrations, origin, or tax code are correct or complete.

## 5. What GNSIS never stores

Full card numbers, CVCs, billing addresses, and tax calculations live only in
Stripe. GNSIS stores the Customer id and reads display-safe card metadata
(brand / last4 / expiry) on demand for the billing summary.
