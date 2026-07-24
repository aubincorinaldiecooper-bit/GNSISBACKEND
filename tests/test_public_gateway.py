"""G4 — public OpenAI-compatible gateway (POST /v1/chat/completions).

Drives the full request flow through the FastAPI app with a monkeypatched
provider seam (no network): virtual-key auth, model allowlist, non-streaming +
streaming metering, provider-vs-Genesis cost separation, balance enforcement,
provider-failure handling, structured errors, and Genesis-request-id propagation.
"""

from __future__ import annotations

import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from _authkit import fresh_sqlite_env  # noqa: E402

MODEL = "anthropic/claude-opus-4.8"


def _fake_forward(usage=None, cost=None):
    def _f(settings, provider, payload):
        u = usage if usage is not None else {"prompt_tokens": 100, "completion_tokens": 50}
        if cost is not None:
            u = dict(u, cost=cost)
        return {"id": "chatcmpl-x", "model": payload["model"],
                "choices": [{"message": {"role": "assistant", "content": "hi"}}], "usage": u}
    return _f


class GatewayTestBase(unittest.TestCase):
    def setUp(self):
        fresh_sqlite_env()
        os.environ["GNSIS_MARKUP_RATE"] = "0.05"
        os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test"  # billing_enabled
        from gnsis.service import settings as sm

        sm._settings = None
        from gnsis.service.db import init_db

        init_db()
        from gnsis.service.billing import BillingStore
        from gnsis.service.pricing import PricingStore
        from gnsis.service.settings import get_settings
        from gnsis.service.virtual_keys import VirtualKeyStore

        self.settings = get_settings()
        PricingStore().add_price(provider="anthropic", model=MODEL, input_price="0.00002", output_price="0.00006")
        self.view, self.secret = VirtualKeyStore().create(
            self.settings, workspace_id="ws-1", name="app", allowed_models=[MODEL]
        )
        BillingStore().top_up("ws-1", "25.00", idempotency_key="seed")

        import gnsis.service.public_gateway as pg

        self.pg = pg
        self._orig_forward = pg._forward
        self._orig_stream = pg._forward_stream
        pg._forward = _fake_forward()

        from fastapi.testclient import TestClient

        from gnsis.service import api

        self.client = TestClient(api.app)

    def tearDown(self):
        self.pg._forward = self._orig_forward
        self.pg._forward_stream = self._orig_stream

    def call(self, body, key=None):
        headers = {"Authorization": f"Bearer {key or self.secret}"}
        return self.client.post("/v1/chat/completions", json=body, headers=headers)

    def usage(self):
        from gnsis.service.usage import UsageStore

        return UsageStore().list_for_workspace("ws-1")

    def balance(self):
        from gnsis.service.billing import BillingStore

        return BillingStore().balance("ws-1")


class AuthAndPermissionTests(GatewayTestBase):
    def test_missing_key_401(self):
        r = self.client.post("/v1/chat/completions", json={"model": MODEL, "messages": []})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["error"]["code"], "missing_credential")

    def test_invalid_key_401(self):
        r = self.call({"model": MODEL, "messages": []}, key="gns_live_bogus")
        self.assertEqual(r.status_code, 401)
        self.assertIn("X-Genesis-Request-Id", r.headers)

    def test_disabled_key_401(self):
        from gnsis.service.virtual_keys import VirtualKeyStore

        VirtualKeyStore().disable("ws-1", self.view.id)
        r = self.call({"model": MODEL, "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(r.status_code, 401)

    def test_disallowed_model_403(self):
        r = self.call({"model": "openai/gpt-4", "messages": []})
        self.assertEqual(r.status_code, 403)
        body = r.json()
        self.assertEqual(body["error"]["code"], "model_not_allowed")
        self.assertEqual(body["error"]["details"]["model"], "openai/gpt-4")
        self.assertTrue(body["error"]["request_id"].startswith("req_"))


class MeteringTests(GatewayTestBase):
    def test_happy_path_meters_and_separates_cost(self):
        r = self.call({"model": MODEL, "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("X-Genesis-Request-Id", r.headers)
        self.assertEqual(r.json()["id"], "chatcmpl-x")  # response passed through

        u = self.usage()[0]
        self.assertEqual(u.virtual_key_id, self.view.id)
        self.assertTrue(u.run_id.startswith("run_"))
        self.assertEqual(u.input_tokens + u.output_tokens, 150)
        # Provider returned no cost → Genesis-calculated from versioned pricing.
        self.assertEqual(u.cost_source, "unknown")
        self.assertEqual(Decimal(u.genesis_calculated_cost), Decimal("0.005"))
        self.assertEqual(u.reconciliation_state, "resolved")
        # Charged on the Genesis basis (0.005 * 1.05).
        self.assertEqual(self.balance(), Decimal("25.00") - Decimal("0.005") * Decimal("1.05"))

    def test_provider_reported_cost_preserved(self):
        self.pg._forward = _fake_forward(cost="0.02")
        r = self.call({"model": MODEL, "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(r.status_code, 200)
        u = self.usage()[0]
        self.assertEqual(u.cost_source, "provider_reported")
        self.assertEqual(Decimal(u.upstream_cost), Decimal("0.02"))
        # Billed on the provider figure.
        self.assertEqual(self.balance(), Decimal("25.00") - Decimal("0.02") * Decimal("1.05"))

    def test_run_id_from_header(self):
        r = self.client.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {self.secret}", "X-Genesis-Run-Id": "run_custom"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.usage()[0].run_id, "run_custom")


class BalanceAndFailureTests(GatewayTestBase):
    def test_insufficient_balance_402(self):
        # Drain the balance below the reserve estimate.
        from gnsis.service.billing import BillingStore

        BillingStore().adjustment("ws-1", "-25.00", idempotency_key="drain")
        r = self.call({"model": MODEL, "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(r.status_code, 402)
        self.assertEqual(r.json()["error"]["code"], "insufficient_balance")

    def test_provider_failure_502_records_failed_no_charge(self):
        def _boom(settings, provider, payload):
            raise RuntimeError("upstream exploded")

        self.pg._forward = _boom
        r = self.call({"model": MODEL, "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.json()["error"]["code"], "provider_error")
        u = self.usage()[0]
        self.assertEqual(u.request_status, "error")
        self.assertEqual(self.balance(), Decimal("25.00"))  # hold released, nothing charged


class StreamingTests(GatewayTestBase):
    def test_streaming_passes_through_and_meters(self):
        def _fake_stream(settings, provider, payload):
            yield b'data: {"id":"c","choices":[{"delta":{"content":"hel"}}]}\n\n'
            yield b'data: {"id":"c","choices":[{"delta":{"content":"lo"}}]}\n\n'
            yield b'data: {"id":"c","choices":[],"usage":{"prompt_tokens":100,"completion_tokens":50}}\n\n'
            yield b"data: [DONE]\n\n"

        self.pg._forward_stream = _fake_stream
        r = self.client.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "stream": True},
            headers={"Authorization": f"Bearer {self.secret}"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertIn("hello".encode() if False else b"hel", r.content)
        self.assertIn("X-Genesis-Request-Id", r.headers)
        # Usage was captured from the final chunk and metered after the stream.
        u = self.usage()
        self.assertEqual(len(u), 1)
        self.assertEqual(u[0].input_tokens + u[0].output_tokens, 150)
        self.assertEqual(Decimal(u[0].genesis_calculated_cost), Decimal("0.005"))

    def test_streaming_without_usage_chunk_flags_reconciliation_not_free(self):
        # Provider streams content but never sends a final usage chunk. The request
        # must NOT be silently recorded as a free $0 charge — it's flagged for
        # reconciliation and the pre-request hold is released (nothing charged).
        def _no_usage_stream(settings, provider, payload):
            yield b'data: {"id":"c","choices":[{"delta":{"content":"hel"}}]}\n\n'
            yield b'data: {"id":"c","choices":[{"delta":{"content":"lo"}}]}\n\n'
            yield b"data: [DONE]\n\n"

        self.pg._forward_stream = _no_usage_stream
        r = self.client.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "stream": True},
            headers={"Authorization": f"Bearer {self.secret}"},
        )
        self.assertEqual(r.status_code, 200)
        u = self.usage()
        self.assertEqual(len(u), 1)
        self.assertEqual(u[0].reconciliation_state, "needs_reconciliation")
        self.assertEqual(u[0].reconciliation_reason, "missing_usage")
        # No silent $0 charge, and the pre-request hold is released (balance and
        # available both back to the full top-up).
        from gnsis.service.billing import BillingStore

        self.assertIsNone(BillingStore().get_charge_for_usage(u[0].id))
        self.assertEqual(self.balance(), Decimal("25.00"))
        self.assertEqual(BillingStore().available("ws-1"), Decimal("25.00"))


if __name__ == "__main__":
    unittest.main()
