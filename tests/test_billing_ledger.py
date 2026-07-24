"""PR 2 — markup, balance ledger, and Stripe refills.

Focused coverage of the acceptance criteria: decimal markup, rate-card
persistence, historical immutability, duplicate protection, atomic charge+debit,
balance derivation, Stripe signature + idempotency, top-up, failed-not-credited,
refund/adjustment, insufficient-balance rejection, reservation settlement,
concurrent-balance protection, and workspace isolation.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sys
import time
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from _authkit import fresh_sqlite_env  # noqa: E402

WHSEC = "whsec_test"


def _configure(markup="0.05"):
    fresh_sqlite_env()
    os.environ["GNSIS_MARKUP_RATE"] = markup
    os.environ["GNSIS_RATE_CARD_VERSION"] = "beta-2026-07"
    os.environ["STRIPE_WEBHOOK_SECRET"] = WHSEC
    from gnsis.service import settings as settings_mod

    settings_mod._settings = None
    from gnsis.service.db import init_db

    init_db()


def _usage(rid, workspace="ws-1", upstream="1.00", trace_event_id=None, status="success"):
    from gnsis.service.usage import MeasuredUsage, UsageStore

    u = MeasuredUsage(
        litellm_request_id=rid, workspace_id=workspace, user_id="user-1",
        provider="anthropic", model="anthropic/claude-opus-4.8",
        input_tokens=100, output_tokens=50, cached_tokens=0, reasoning_tokens=0,
        duration_ms=10, request_status=status, upstream_cost=upstream, currency="USD",
        trace_event_id=trace_event_id,
    )
    rec, _ = UsageStore().record(u)
    return rec


class RateAndChargeTests(unittest.TestCase):
    def setUp(self):
        _configure()

    def test_decimal_markup_arithmetic(self):
        from gnsis.service import rates
        from gnsis.service.settings import get_settings

        q = rates.quote(get_settings(), upstream_cost="1.00")
        self.assertEqual(q.upstream_cost, Decimal("1.00"))
        self.assertEqual(q.markup_rate, Decimal("0.05"))
        self.assertEqual(q.service_fee, Decimal("0.05"))
        self.assertEqual(q.retail_cost, Decimal("1.05"))
        self.assertEqual(q.rate_card_version, "beta-2026-07")

    def test_charge_persists_applied_rate_and_debits(self):
        from gnsis.service.billing import BillingStore
        from gnsis.service.settings import get_settings

        store = BillingStore()
        store.top_up("ws-1", "25.00", idempotency_key="seed")
        rec = _usage("u1", upstream="1.00")
        charge, created = store.charge_usage(get_settings(), rec.id)
        self.assertTrue(created)
        self.assertEqual(Decimal(charge.upstream_cost), Decimal("1"))
        self.assertEqual(Decimal(charge.service_fee), Decimal("0.05"))
        self.assertEqual(Decimal(charge.retail_cost), Decimal("1.05"))
        self.assertEqual(charge.rate_card_version, "beta-2026-07")
        self.assertEqual(store.balance("ws-1"), Decimal("23.95"))

    def test_duplicate_charge_does_not_double_apply(self):
        from gnsis.service.billing import BillingStore
        from gnsis.service.settings import get_settings

        store = BillingStore()
        store.top_up("ws-1", "25.00", idempotency_key="seed")
        rec = _usage("u1")
        c1, created1 = store.charge_usage(get_settings(), rec.id)
        c2, created2 = store.charge_usage(get_settings(), rec.id)
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(c1.id, c2.id)
        self.assertEqual(store.balance("ws-1"), Decimal("23.95"))  # debited once only

    def test_atomic_one_debit_per_charge(self):
        from gnsis.service.billing import BillingStore, USAGE_DEBIT
        from gnsis.service.settings import get_settings

        store = BillingStore()
        store.top_up("ws-1", "10.00", idempotency_key="seed")
        rec = _usage("u1")
        store.charge_usage(get_settings(), rec.id)
        debits = [t for t in store.transactions("ws-1") if t.transaction_type == USAGE_DEBIT]
        self.assertEqual(len(debits), 1)
        self.assertEqual(debits[0].amount, Decimal("-1.05"))

    def test_historical_charge_immutable_when_rate_changes(self):
        from gnsis.service.billing import BillingStore
        from gnsis.service.settings import get_settings

        store = BillingStore()
        store.top_up("ws-1", "25.00", idempotency_key="seed")
        old_rec = _usage("old", upstream="1.00")
        old = store.charge_usage(get_settings(), old_rec.id)[0]

        # Change the active markup to 8%.
        get_settings().markup_rate = "0.08"
        new_rec = _usage("new", upstream="1.00")
        new = store.charge_usage(get_settings(), new_rec.id)[0]

        # Old charge keeps its applied 5%; new charge records 8%.
        self.assertEqual(Decimal(old.markup_rate), Decimal("0.05"))
        self.assertEqual(Decimal(old.retail_cost), Decimal("1.05"))
        self.assertEqual(Decimal(new.markup_rate), Decimal("0.08"))
        self.assertEqual(Decimal(new.retail_cost), Decimal("1.08"))
        # Re-reading the old charge shows the original rate, never recomputed.
        reread = store.get_charge_for_usage(old_rec.id)
        self.assertEqual(Decimal(reread.markup_rate), Decimal("0.05"))
        self.assertEqual(Decimal(reread.retail_cost), Decimal("1.05"))


class BalanceAndReservationTests(unittest.TestCase):
    def setUp(self):
        _configure()

    def test_balance_derivation_and_refund_adjustment(self):
        from gnsis.service.billing import BillingStore

        store = BillingStore()
        store.top_up("ws-1", "25.00", idempotency_key="a")
        store.refund("ws-1", "2.00", idempotency_key="b")
        store.adjustment("ws-1", "1.00", idempotency_key="c")
        self.assertEqual(store.balance("ws-1"), Decimal("24.00"))  # 25 - 2 + 1

    def test_insufficient_balance_reservation_rejected(self):
        from gnsis.service.billing import BillingStore

        store = BillingStore()
        self.assertFalse(store.reserve("ws-empty", "0.05", "k1"))  # zero balance
        store.top_up("ws-1", "0.03", idempotency_key="a")
        self.assertFalse(store.reserve("ws-1", "0.05", "k2"))  # under-funded

    def test_reservation_holds_prevent_concurrent_overspend(self):
        from gnsis.service.billing import BillingStore

        store = BillingStore()
        store.top_up("ws-1", "1.00", idempotency_key="a")
        self.assertTrue(store.reserve("ws-1", "0.60", "r1"))
        # Second request cannot be reserved — only 0.40 remains available.
        self.assertFalse(store.reserve("ws-1", "0.60", "r2"))
        self.assertEqual(store.available("ws-1"), Decimal("0.40"))

    def test_reservation_settles_into_actual_debit(self):
        from gnsis.service.billing import BillingStore
        from gnsis.service.settings import get_settings

        store = BillingStore()
        store.top_up("ws-1", "25.00", idempotency_key="a")
        self.assertTrue(store.reserve("ws-1", "0.05", "ev1"))
        rec = _usage("u1", upstream="1.00", trace_event_id="ev1")
        store.charge_usage(get_settings(), rec.id)
        # Hold settled; balance reflects the real 1.05 debit and no lingering hold.
        self.assertEqual(store.balance("ws-1"), Decimal("23.95"))
        self.assertEqual(store.available("ws-1"), Decimal("23.95"))

    def test_workspace_isolation(self):
        from gnsis.service.billing import BillingStore

        store = BillingStore()
        store.top_up("ws-A", "10.00", idempotency_key="a")
        store.top_up("ws-B", "5.00", idempotency_key="b")
        self.assertEqual(store.balance("ws-A"), Decimal("10.00"))
        self.assertEqual(store.balance("ws-B"), Decimal("5.00"))
        self.assertEqual({t.workspace_id for t in store.transactions("ws-A")}, {"ws-A"})


class StripeWebhookTests(unittest.TestCase):
    def setUp(self):
        _configure()

    def _event(self, event_id="evt_1", etype="payment_intent.succeeded", workspace="ws-1", amount=2500, extra=None):
        obj = {"id": "pi_1", "amount_received": amount, "status": "succeeded",
                "currency": "usd", "metadata": {"workspace_id": workspace}}
        if extra:
            obj.update(extra)
        return {"id": event_id, "type": etype, "data": {"object": obj}}

    def _sign(self, payload: bytes):
        t = str(int(time.time()))
        sig = hmac.new(WHSEC.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
        return f"t={t},v1={sig}"

    def test_signature_validation(self):
        from gnsis.service.stripe_webhook import StripeSignatureError, verify_signature

        payload = b'{"hello":"world"}'
        good = self._sign(payload)
        self.assertTrue(verify_signature(payload, good, WHSEC))
        with self.assertRaises(StripeSignatureError):
            verify_signature(payload, "t=1,v1=deadbeef", WHSEC)

    def test_successful_topup_and_idempotency(self):
        from gnsis.service.billing import BillingStore
        from gnsis.service.stripe_webhook import handle_event

        ev = self._event(event_id="evt_top")
        r1 = handle_event(ev)
        r2 = handle_event(ev)  # replay
        self.assertTrue(r1["created"])
        self.assertFalse(r2["created"])
        self.assertEqual(BillingStore().balance("ws-1"), Decimal("25.00"))  # one +25 only

    def test_failed_payment_not_credited(self):
        from gnsis.service.billing import BillingStore
        from gnsis.service.stripe_webhook import handle_event

        handle_event(self._event(event_id="evt_fail", etype="payment_intent.payment_failed"))
        self.assertEqual(BillingStore().balance("ws-1"), Decimal("0"))

    def test_refund_creates_negative_entry(self):
        from gnsis.service.billing import BillingStore
        from gnsis.service.stripe_webhook import handle_event

        store = BillingStore()
        handle_event(self._event(event_id="evt_top2"))  # +25
        handle_event(self._event(event_id="evt_refund", etype="charge.refunded",
                                 extra={"amount_refunded": 500}))
        self.assertEqual(store.balance("ws-1"), Decimal("20.00"))  # 25 - 5

    def test_webhook_endpoint_rejects_bad_signature(self):
        from fastapi.testclient import TestClient
        from gnsis.service.api import app

        client = TestClient(app)
        r = client.post("/billing/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "t=1,v1=bad"})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
