"""PR 3 — customer utility dashboard read APIs + Stripe refill initiation.

Exercises the workspace-scoped dashboard endpoints end to end via the FastAPI
TestClient (SQLite stands in for Postgres; the JWT verifier is injected). Seeds
real metering (PR 1) + billing (PR 2) rows for a workspace and asserts the
overview, usage ledger, billing ledger, and per-run spend reflect them, that a
second workspace sees none of it, and that the refill endpoint is correctly
gated and never touches the network.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from _authkit import AUDIENCE, ISSUER, fresh_sqlite_env, make_keypair, mint  # noqa: E402


def _seed_usage(workspace_id, rid, *, upstream="1.00", run_id=None, trace_event_id=None,
                status="success", model="anthropic/claude-opus-4.8"):
    from gnsis.service.usage import MeasuredUsage, UsageStore

    u = MeasuredUsage(
        litellm_request_id=rid, workspace_id=workspace_id, user_id="owner",
        provider="anthropic", model=model,
        input_tokens=100, output_tokens=50, cached_tokens=0, reasoning_tokens=0,
        duration_ms=12, request_status=status, upstream_cost=upstream, currency="USD",
        run_id=run_id, trace_event_id=trace_event_id,
    )
    rec, _ = UsageStore().record(u)
    return rec


class DashboardTestBase(unittest.TestCase):
    def setUp(self):
        fresh_sqlite_env()
        os.environ["BETTER_AUTH_JWKS_URL"] = "https://auth.test/jwks"
        os.environ["BETTER_AUTH_ISSUER"] = ISSUER
        os.environ["BETTER_AUTH_AUDIENCE"] = AUDIENCE
        os.environ["GNSIS_MARKUP_RATE"] = "0.05"
        os.environ["GNSIS_RATE_CARD_VERSION"] = "beta-2026-07"
        os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test"
        # Refill stays disabled unless a test opts in.
        os.environ.pop("STRIPE_SECRET_KEY", None)
        os.environ.pop("GNSIS_FRONTEND_URL", None)
        self._reset_settings()

        from gnsis.service.db import init_db

        init_db()

        from fastapi.testclient import TestClient

        from gnsis.service import api
        from gnsis.service.auth import JwksCache, JwtVerifier

        self.api = api
        self.priv, self.jwks = make_keypair("k1")
        verifier = JwtVerifier(
            JwksCache(fetcher=lambda: self.jwks), issuer=ISSUER, audience=AUDIENCE
        )
        api.app.dependency_overrides[api.get_verifier] = lambda: verifier
        self.client = TestClient(api.app)

    def tearDown(self):
        self.api.app.dependency_overrides.clear()

    def _reset_settings(self):
        from gnsis.service import settings as settings_mod

        settings_mod._settings = None

    def auth(self, sub, **kw):
        return {"Authorization": f"Bearer {mint(self.priv, 'k1', sub, **kw)}"}

    def workspace_id(self, sub):
        from gnsis.service import workspaces as ws

        return ws.get_or_create_workspace(sub).id


class OverviewAndLedgerTests(DashboardTestBase):
    def _seed_workspace(self, sub="user-1"):
        from gnsis.service.billing import BillingStore
        from gnsis.service.settings import get_settings

        wid = self.workspace_id(sub)
        store = BillingStore()
        store.top_up(wid, "25.00", idempotency_key="seed")
        r1 = _seed_usage(wid, "u1", upstream="1.00", run_id="job-A", trace_event_id="ev1")
        r2 = _seed_usage(wid, "u2", upstream="2.00", run_id="job-A", trace_event_id="ev2")
        store.charge_usage(get_settings(), r1.id)
        store.charge_usage(get_settings(), r2.id)
        return wid

    def test_requires_auth(self):
        self.assertEqual(self.client.get("/v1/dashboard/overview").status_code, 401)
        self.assertEqual(self.client.get("/v1/dashboard/usage").status_code, 401)
        self.assertEqual(self.client.get("/v1/dashboard/transactions").status_code, 401)

    def test_overview_reflects_balance_and_spend(self):
        self._seed_workspace("user-1")
        r = self.client.get("/v1/dashboard/overview", headers=self.auth("user-1"))
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        # 25 top-up minus 1.05 + 2.10 retail = 21.85.
        self.assertEqual(Decimal(body["balance"]), Decimal("21.85"))
        self.assertEqual(Decimal(body["available"]), Decimal("21.85"))
        self.assertEqual(Decimal(body["spent_total"]), Decimal("3.15"))
        self.assertEqual(Decimal(body["spent_30d"]), Decimal("3.15"))
        self.assertEqual(body["usage_count"], 2)
        self.assertEqual(body["charge_count"], 2)
        self.assertEqual(body["run_count"], 1)  # both usage rows share job-A
        self.assertTrue(body["billing_enabled"])
        self.assertFalse(body["refill_enabled"])
        self.assertEqual(body["currency"], "USD")

    def test_usage_ledger_joins_retail_charge(self):
        self._seed_workspace("user-1")
        r = self.client.get("/v1/dashboard/usage", headers=self.auth("user-1"))
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["total"], 2)
        self.assertEqual(len(body["items"]), 2)
        item = body["items"][0]
        self.assertIn(item["model"], {"anthropic/claude-opus-4.8"})
        self.assertEqual(item["total_tokens"], 150)
        # Retail cost is present and equals upstream + 5% markup.
        self.assertEqual(
            Decimal(item["retail_cost"]),
            Decimal(item["upstream_cost"]) * Decimal("1.05"),
        )
        self.assertEqual(item["billing_status"], "charged")

    def test_transactions_ledger_lists_topup_and_debits(self):
        self._seed_workspace("user-1")
        r = self.client.get("/v1/dashboard/transactions", headers=self.auth("user-1"))
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        types = [t["transaction_type"] for t in body["items"]]
        self.assertIn("top_up", types)
        self.assertEqual(types.count("usage_debit"), 2)
        self.assertEqual(body["total"], 3)

    def test_runs_empty_without_jobs(self):
        self._seed_workspace("user-1")
        r = self.client.get("/v1/dashboard/runs", headers=self.auth("user-1"))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["runs"], [])

    def test_workspace_isolation(self):
        self._seed_workspace("user-1")
        # A different user's workspace sees an empty dashboard.
        r = self.client.get("/v1/dashboard/overview", headers=self.auth("user-2"))
        body = r.json()
        self.assertEqual(Decimal(body["balance"]), Decimal("0"))
        self.assertEqual(body["usage_count"], 0)
        u = self.client.get("/v1/dashboard/usage", headers=self.auth("user-2")).json()
        self.assertEqual(u["items"], [])

    def test_run_spend_aggregates_per_run(self):
        from gnsis.service.dashboard import DashboardStore

        wid = self._seed_workspace("user-1")
        spend = DashboardStore().run_spend(wid, ["job-A", "job-missing"])
        self.assertEqual(Decimal(spend["job-A"]), Decimal("3.15"))
        self.assertEqual(Decimal(spend["job-missing"]), Decimal("0"))


class RefillTests(DashboardTestBase):
    def _enable_refill(self):
        os.environ["STRIPE_SECRET_KEY"] = "sk_test_123"
        os.environ["GNSIS_FRONTEND_URL"] = "https://app.gnsis.test"
        os.environ["STRIPE_API_BASE"] = "https://api.stripe.test"
        self._reset_settings()

    def test_refill_disabled_returns_503(self):
        r = self.client.post(
            "/v1/billing/refill", json={"amount_usd": "25"}, headers=self.auth("user-1")
        )
        self.assertEqual(r.status_code, 503)

    def test_refill_requires_auth(self):
        self._enable_refill()
        r = self.client.post("/v1/billing/refill", json={"amount_usd": "25"})
        self.assertEqual(r.status_code, 401)

    def test_refill_creates_checkout_session(self):
        self._enable_refill()
        import gnsis.service.stripe_checkout as sc

        captured = {}

        def fake_post(url, data, headers, timeout=30):
            captured["url"] = url
            captured["data"] = data.decode()
            return 200, json.dumps({"id": "cs_test_1", "url": "https://checkout.stripe.test/c/cs_test_1"})

        orig = sc._http_post
        sc._http_post = fake_post
        try:
            r = self.client.post(
                "/v1/billing/refill", json={"amount_usd": "25"}, headers=self.auth("user-1")
            )
        finally:
            sc._http_post = orig
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["url"], "https://checkout.stripe.test/c/cs_test_1")
        self.assertEqual(body["session_id"], "cs_test_1")
        # The Stripe call carried workspace attribution and the right amount (cents).
        self.assertIn("metadata%5Bworkspace_id%5D", captured["data"])
        self.assertIn("%5Bunit_amount%5D=2500", captured["data"])  # $25.00 -> 2500 cents
        self.assertTrue(captured["url"].endswith("/v1/checkout/sessions"))

    def test_refill_rejects_out_of_range_amount(self):
        self._enable_refill()
        r = self.client.post(
            "/v1/billing/refill", json={"amount_usd": "1000"}, headers=self.auth("user-1")
        )
        self.assertEqual(r.status_code, 400)
        r2 = self.client.post(
            "/v1/billing/refill", json={"amount_usd": "0"}, headers=self.auth("user-1")
        )
        self.assertEqual(r2.status_code, 400)


if __name__ == "__main__":
    unittest.main()
