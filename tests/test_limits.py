"""G5 — concurrency-safe configurable spending limits.

Engine: observe/warn/block modes, most-restrictive precedence, the reservation
that stops concurrent requests overspending, and post-request reconcile. Gateway:
a block policy denies with a structured error, warn/generous policies allow.
"""

from __future__ import annotations

import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from _authkit import AUDIENCE, ISSUER, fresh_sqlite_env, make_keypair, mint  # noqa: E402

MODEL = "anthropic/claude-opus-4.8"


def _configure():
    fresh_sqlite_env()
    os.environ["GNSIS_MARKUP_RATE"] = "0.05"
    os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test"
    from gnsis.service import settings as sm

    sm._settings = None
    from gnsis.service.db import init_db

    init_db()


class LimitEngineTests(unittest.TestCase):
    def setUp(self):
        _configure()
        from gnsis.service.limits import LimitStore, PolicyEngine

        self.store = LimitStore()
        self.engine = PolicyEngine()

    def settings(self):
        from gnsis.service.settings import get_settings

        return get_settings()

    def ctx(self, **over):
        from gnsis.service.limits import LimitContext

        base = dict(workspace_id="ws-1", run_id="run-1", virtual_key_id="vk-1")
        base.update(over)
        return LimitContext(**base)

    def test_no_policy_ok(self):
        self.assertEqual(self.engine.evaluate(self.settings(), self.ctx(), "0.05", "r0").result, "ok")

    def test_block_when_exceeded(self):
        self.store.create(workspace_id="ws-1", scope_type="workspace", scope_id="ws-1",
                          limit_type="daily", amount="0.01", enforcement_mode="block")
        r = self.engine.evaluate(self.settings(), self.ctx(), "0.05", "r1")
        self.assertEqual(r.result, "block")
        self.assertEqual(r.block_scope, "workspace")

    def test_observe_only_allows_but_records(self):
        self.store.create(workspace_id="ws-1", scope_type="workspace", scope_id="ws-1",
                          limit_type="daily", amount="0.01", enforcement_mode="observe_only")
        r = self.engine.evaluate(self.settings(), self.ctx(), "0.05", "r2")
        self.assertEqual(r.result, "ok")
        from gnsis.service import orm
        from gnsis.service.db import session_scope

        with session_scope() as s:
            d = s.query(orm.LimitDecision).filter(orm.LimitDecision.request_id == "r2").one()
            self.assertEqual(d.result, "observe")
            # observe_only never reserves.
            self.assertEqual(s.query(orm.LimitReservation).count(), 0)

    def test_warn_mode_allows(self):
        self.store.create(workspace_id="ws-1", scope_type="workspace", scope_id="ws-1",
                          limit_type="daily", amount="0.01", enforcement_mode="warn")
        r = self.engine.evaluate(self.settings(), self.ctx(), "0.05", "r3")
        self.assertEqual(r.result, "warn")

    def test_most_restrictive_wins(self):
        self.store.create(workspace_id="ws-1", scope_type="project", scope_id="proj-1",
                          limit_type="monthly", amount="100", enforcement_mode="block")
        self.store.create(workspace_id="ws-1", scope_type="workspace", scope_id="ws-1",
                          limit_type="daily", amount="0.01", enforcement_mode="block")
        r = self.engine.evaluate(self.settings(), self.ctx(project_id="proj-1"), "0.05", "r4")
        self.assertEqual(r.result, "block")  # the tight ws limit wins over the generous project one

    def test_reservation_prevents_concurrent_overspend(self):
        # limit 0.08, estimate 0.05: the first request holds 0.05; a second
        # in-flight request would project 0.10 > 0.08 and is blocked.
        self.store.create(workspace_id="ws-1", scope_type="workspace", scope_id="ws-1",
                          limit_type="daily", amount="0.08", enforcement_mode="block")
        r1 = self.engine.evaluate(self.settings(), self.ctx(), "0.05", "rA")
        r2 = self.engine.evaluate(self.settings(), self.ctx(run_id="run-2"), "0.05", "rB")
        self.assertEqual(r1.result, "ok")
        self.assertEqual(r2.result, "block")
        # Once the first reconciles (its hold released), room frees up again.
        self.engine.reconcile("rA", "0.005")
        r3 = self.engine.evaluate(self.settings(), self.ctx(run_id="run-3"), "0.05", "rC")
        self.assertEqual(r3.result, "ok")


class LimitGatewayTests(unittest.TestCase):
    def setUp(self):
        _configure()
        from gnsis.service.billing import BillingStore
        from gnsis.service.pricing import PricingStore
        from gnsis.service.settings import get_settings
        from gnsis.service.virtual_keys import VirtualKeyStore

        self.settings = get_settings()
        PricingStore().add_price(provider="anthropic", model=MODEL, input_price="0.00002", output_price="0.00006")
        self.view, self.secret = VirtualKeyStore().create(self.settings, workspace_id="ws-1", name="app", allowed_models=[MODEL])
        BillingStore().top_up("ws-1", "25.00", idempotency_key="seed")

        import gnsis.service.public_gateway as pg

        self.pg = pg
        self._orig = pg._forward
        pg._forward = lambda settings, provider, payload: {
            "id": "c", "model": payload["model"],
            "choices": [{"message": {"role": "assistant", "content": "hi"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        }
        from fastapi.testclient import TestClient
        from gnsis.service import api

        self.client = TestClient(api.app)

    def tearDown(self):
        self.pg._forward = self._orig

    def _call(self):
        return self.client.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {self.secret}"},
        )

    def test_block_limit_denies_with_structured_error(self):
        from gnsis.service.limits import LimitStore

        LimitStore().create(workspace_id="ws-1", scope_type="workspace", scope_id="ws-1",
                            limit_type="daily", amount="0.01", enforcement_mode="block")
        r = self._call()
        self.assertEqual(r.status_code, 402)
        self.assertEqual(r.json()["error"]["code"], "spending_limit_exceeded")
        self.assertEqual(r.json()["error"]["details"]["scope"], "workspace")

    def test_generous_limit_allows(self):
        from gnsis.service.limits import LimitStore

        LimitStore().create(workspace_id="ws-1", scope_type="workspace", scope_id="ws-1",
                            limit_type="monthly", amount="100", enforcement_mode="block")
        self.assertEqual(self._call().status_code, 200)

    def test_key_hard_limit_enforced(self):
        # A tiny per-key hard limit blocks via the key's inline policy.
        from gnsis.service.virtual_keys import VirtualKeyStore

        _v, secret = VirtualKeyStore().create(self.settings, workspace_id="ws-1", name="tight",
                                              allowed_models=[MODEL], hard_limit="0.001")
        r = self.client.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {secret}"},
        )
        self.assertEqual(r.status_code, 402)
        self.assertEqual(r.json()["error"]["details"]["scope"], "virtual_key")


class LimitApiTests(unittest.TestCase):
    def setUp(self):
        _configure()
        os.environ["BETTER_AUTH_JWKS_URL"] = "https://auth.test/jwks"
        os.environ["BETTER_AUTH_ISSUER"] = ISSUER
        os.environ["BETTER_AUTH_AUDIENCE"] = AUDIENCE
        from gnsis.service import settings as sm

        sm._settings = None
        from fastapi.testclient import TestClient
        from gnsis.service import api
        from gnsis.service.auth import JwksCache, JwtVerifier

        self.api = api
        self.priv, self.jwks = make_keypair("k1")
        verifier = JwtVerifier(JwksCache(fetcher=lambda: self.jwks), issuer=ISSUER, audience=AUDIENCE)
        api.app.dependency_overrides[api.get_verifier] = lambda: verifier
        self.client = TestClient(api.app)

    def tearDown(self):
        self.api.app.dependency_overrides.clear()

    def auth(self, sub):
        return {"Authorization": f"Bearer {mint(self.priv, 'k1', sub)}"}

    def test_crud_and_balances(self):
        created = self.client.post("/v1/limits", json={
            "scope_type": "workspace", "scope_id": "ignored", "limit_type": "monthly",
            "amount": "50", "enforcement_mode": "warn",
        }, headers=self.auth("user-1"))
        self.assertEqual(created.status_code, 200, created.text)
        lid = created.json()["id"]

        listed = self.client.get("/v1/limits", headers=self.auth("user-1")).json()
        self.assertEqual(len(listed["items"]), 1)

        patched = self.client.patch(f"/v1/limits/{lid}", json={"enabled": False},
                                    headers=self.auth("user-1"))
        self.assertFalse(patched.json()["enabled"])

        # Another workspace cannot see or patch it.
        self.assertEqual(self.client.get("/v1/limits", headers=self.auth("user-2")).json()["items"], [])
        self.assertEqual(
            self.client.patch(f"/v1/limits/{lid}", json={"enabled": True}, headers=self.auth("user-2")).status_code,
            404,
        )

        bal = self.client.get("/v1/balances", headers=self.auth("user-1"))
        self.assertEqual(bal.status_code, 200)
        self.assertIn("available", bal.json())


if __name__ == "__main__":
    unittest.main()
