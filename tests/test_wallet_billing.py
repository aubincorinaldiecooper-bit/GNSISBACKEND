"""PR A — wallet foundation: Stripe Customer, Checkout reuse, invoice, tax,
portal, safe card metadata, and payment-level + event-level idempotency.

All Stripe I/O is faked at the central transport (``stripe_client._http_request``)
so nothing touches the network. GNSIS stays the source of truth for balances;
Stripe is the source of truth for the customer, card, invoices, and portal.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from _authkit import AUDIENCE, ISSUER, fresh_sqlite_env, make_keypair, mint  # noqa: E402


class FakeStripe:
    """Minimal Stripe REST stand-in; records calls and returns canned objects."""

    def __init__(self):
        self.calls = []
        self.n_customers = 0

    def __call__(self, method, url, headers, body=None, timeout=30):
        self.calls.append({
            "method": method, "url": url,
            "idempotency_key": headers.get("Idempotency-Key"),
            "body": body.decode() if body else "",
        })
        if url.endswith("/v1/customers"):
            self.n_customers += 1
            return 200, json.dumps({"id": f"cus_{self.n_customers}", "invoice_settings": {}})
        if "/v1/customers/" in url:
            return 200, json.dumps({
                "id": url.rsplit("/", 1)[-1],
                "invoice_settings": {"default_payment_method": "pm_1"},
            })
        if "/v1/payment_methods/" in url:
            return 200, json.dumps({
                "id": "pm_1",
                "card": {
                    "brand": "visa", "last4": "4242", "exp_month": 9, "exp_year": 2030,
                    # Sensitive fields that must NEVER be surfaced:
                    "fingerprint": "TOP_SECRET", "iin": "424242",
                },
            })
        if url.endswith("/v1/checkout/sessions"):
            return 200, json.dumps({"id": "cs_1", "url": "https://checkout.stripe.test/c/cs_1"})
        if url.endswith("/v1/billing_portal/sessions"):
            return 200, json.dumps({"id": "ps_1", "url": "https://portal.stripe.test/p/ps_1"})
        return 200, "{}"


def _configure(tax=False):
    fresh_sqlite_env()
    os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test"
    os.environ["STRIPE_SECRET_KEY"] = "sk_test_x"
    os.environ["GNSIS_FRONTEND_URL"] = "https://app.gnsis.test"
    os.environ["STRIPE_API_BASE"] = "https://api.stripe.test"
    os.environ["GNSIS_MARKUP_RATE"] = "0.05"
    if tax:
        os.environ["GNSIS_STRIPE_TAX_ENABLED"] = "true"
    else:
        os.environ.pop("GNSIS_STRIPE_TAX_ENABLED", None)
    from gnsis.service import settings as sm

    sm._settings = None
    from gnsis.service.db import init_db

    init_db()


class CustomerAndCheckoutTests(unittest.TestCase):
    def setUp(self):
        _configure()
        import gnsis.service.stripe_client as scl

        self.fake = FakeStripe()
        self._orig = scl._http_request
        scl._http_request = self.fake

    def tearDown(self):
        import gnsis.service.stripe_client as scl

        scl._http_request = self._orig

    def _settings(self):
        from gnsis.service.settings import get_settings

        return get_settings()

    def test_customer_created_once_reused_and_isolated(self):
        from gnsis.service import stripe_customers as sc

        a1 = sc.get_or_create_customer(self._settings(), "ws-A", email="a@x.io")
        a2 = sc.get_or_create_customer(self._settings(), "ws-A")
        b1 = sc.get_or_create_customer(self._settings(), "ws-B")
        self.assertEqual(a1, a2)              # reused for the same workspace
        self.assertNotEqual(a1, b1)           # isolated across workspaces
        self.assertEqual(self.fake.n_customers, 2)  # exactly one create per workspace

    def test_create_uses_stripe_idempotency_key(self):
        from gnsis.service import stripe_customers as sc

        sc.get_or_create_customer(self._settings(), "ws-A")
        create = [c for c in self.fake.calls if c["url"].endswith("/v1/customers")][0]
        self.assertEqual(create["idempotency_key"], "gnsis-customer:ws-A")

    def test_checkout_reuses_customer_and_enables_invoice(self):
        from gnsis.service import stripe_customers as sc
        from gnsis.service.stripe_checkout import create_refill_session

        sc.get_or_create_customer(self._settings(), "ws-A")
        before = self.fake.n_customers
        sess = create_refill_session(self._settings(), workspace_id="ws-A", amount_usd="25")
        self.assertEqual(sess["url"], "https://checkout.stripe.test/c/cs_1")
        self.assertEqual(self.fake.n_customers, before)  # no new customer
        checkout = [c for c in self.fake.calls if c["url"].endswith("/v1/checkout/sessions")][0]
        self.assertIn("customer=cus_1", checkout["body"])
        self.assertIn("invoice_creation%5Benabled%5D=true", checkout["body"])

    def test_tax_disabled_by_default(self):
        from gnsis.service.stripe_checkout import create_refill_session

        create_refill_session(self._settings(), workspace_id="ws-A", amount_usd="25")
        checkout = [c for c in self.fake.calls if c["url"].endswith("/v1/checkout/sessions")][0]
        self.assertNotIn("automatic_tax", checkout["body"])


class TaxEnabledTests(unittest.TestCase):
    def setUp(self):
        _configure(tax=True)
        import gnsis.service.stripe_client as scl

        self.fake = FakeStripe()
        self._orig = scl._http_request
        scl._http_request = self.fake

    def tearDown(self):
        import gnsis.service.stripe_client as scl

        scl._http_request = self._orig

    def test_automatic_tax_enabled_when_configured(self):
        from gnsis.service.settings import get_settings
        from gnsis.service.stripe_checkout import create_refill_session

        self.assertTrue(get_settings().stripe_tax_enabled)
        create_refill_session(get_settings(), workspace_id="ws-A", amount_usd="25")
        checkout = [c for c in self.fake.calls if c["url"].endswith("/v1/checkout/sessions")][0]
        self.assertIn("automatic_tax%5Benabled%5D=true", checkout["body"])


class WebhookIdempotencyTests(unittest.TestCase):
    def setUp(self):
        _configure()

    def _session_event(self, event_id, pi_id="pi_1", ws="ws-1", amount=2500):
        return {"id": event_id, "type": "checkout.session.completed", "data": {"object": {
            "id": "cs_1", "payment_intent": pi_id, "payment_status": "paid",
            "status": "complete", "amount_total": amount, "currency": "usd",
            "metadata": {"workspace_id": ws},
        }}}

    def _pi_event(self, event_id, pi_id="pi_1", ws="ws-1", amount=2500):
        return {"id": event_id, "type": "payment_intent.succeeded", "data": {"object": {
            "id": pi_id, "status": "succeeded", "amount_received": amount,
            "currency": "usd", "metadata": {"workspace_id": ws},
        }}}

    def test_payment_level_idempotency_across_event_types(self):
        from gnsis.service.billing import BillingStore
        from gnsis.service.stripe_webhook import handle_event

        # Two DISTINCT events (checkout completion + PI success) for the SAME
        # payment (pi_1) must credit the balance exactly once.
        handle_event(self._session_event("evt_checkout"))
        handle_event(self._pi_event("evt_pi"))
        self.assertEqual(BillingStore().balance("ws-1"), Decimal("25.00"))

    def test_event_level_idempotency_on_replay(self):
        from gnsis.service.billing import BillingStore
        from gnsis.service.stripe_webhook import handle_event

        ev = self._session_event("evt_checkout")
        r1 = handle_event(ev)
        r2 = handle_event(ev)  # exact redelivery
        self.assertTrue(r1["created"])
        self.assertFalse(r2["created"])
        self.assertEqual(BillingStore().balance("ws-1"), Decimal("25.00"))

    def test_dispute_creates_negative_entry(self):
        from gnsis.service.billing import BillingStore
        from gnsis.service.stripe_webhook import handle_event

        handle_event(self._session_event("evt_checkout"))  # +25
        handle_event({"id": "evt_disp", "type": "charge.dispute.created", "data": {"object": {
            "id": "ch_1", "amount": 2500, "currency": "usd", "metadata": {"workspace_id": "ws-1"},
        }}})
        self.assertEqual(BillingStore().balance("ws-1"), Decimal("0.00"))


class WalletEndpointTests(unittest.TestCase):
    def setUp(self):
        _configure()
        os.environ["BETTER_AUTH_JWKS_URL"] = "https://auth.test/jwks"
        os.environ["BETTER_AUTH_ISSUER"] = ISSUER
        os.environ["BETTER_AUTH_AUDIENCE"] = AUDIENCE
        from gnsis.service import settings as sm

        sm._settings = None
        import gnsis.service.stripe_client as scl

        self.fake = FakeStripe()
        self._orig = scl._http_request
        scl._http_request = self.fake

        from fastapi.testclient import TestClient

        from gnsis.service import api
        from gnsis.service.auth import JwksCache, JwtVerifier

        self.api = api
        self.priv, self.jwks = make_keypair("k1")
        verifier = JwtVerifier(JwksCache(fetcher=lambda: self.jwks), issuer=ISSUER, audience=AUDIENCE)
        api.app.dependency_overrides[api.get_verifier] = lambda: verifier
        self.client = TestClient(api.app)

    def tearDown(self):
        import gnsis.service.stripe_client as scl

        scl._http_request = self._orig
        self.api.app.dependency_overrides.clear()

    def auth(self, sub, **kw):
        return {"Authorization": f"Bearer {mint(self.priv, 'k1', sub, **kw)}"}

    def _seed_customer_and_balance(self, sub):
        from gnsis.service import stripe_customers as sc
        from gnsis.service import workspaces as ws
        from gnsis.service.billing import BillingStore
        from gnsis.service.settings import get_settings

        wid = ws.get_or_create_workspace(sub).id
        BillingStore().top_up(wid, "25.00", idempotency_key="seed")
        sc.get_or_create_customer(get_settings(), wid, email="u@x.io")
        return wid

    def test_summary_requires_auth(self):
        self.assertEqual(self.client.get("/v1/billing/summary").status_code, 401)

    def test_summary_exposes_only_safe_card_fields(self):
        self._seed_customer_and_balance("user-1")
        r = self.client.get("/v1/billing/summary", headers=self.auth("user-1"))
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(Decimal(body["balance"]), Decimal("25.00"))
        self.assertEqual(Decimal(body["available"]), Decimal("25.00"))
        self.assertEqual(Decimal(body["reserved"]), Decimal("0"))
        card = body["default_card"]
        self.assertEqual(set(card.keys()), {"brand", "last4", "exp_month", "exp_year"})
        self.assertEqual(card["last4"], "4242")
        # No sensitive fields leaked from the payment method.
        self.assertNotIn("fingerprint", card)
        self.assertNotIn("iin", card)
        self.assertTrue(body["has_customer"])
        self.assertTrue(body["portal_available"])

    def test_portal_isolated_per_workspace(self):
        self._seed_customer_and_balance("user-1")
        r = self.client.post("/v1/billing/portal", headers=self.auth("user-1"))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["url"], "https://portal.stripe.test/p/ps_1")
        # The portal session was opened against a customer (this workspace's).
        portal = [c for c in self.fake.calls if c["url"].endswith("/v1/billing_portal/sessions")][-1]
        self.assertIn("customer=cus_", portal["body"])


if __name__ == "__main__":
    unittest.main()
