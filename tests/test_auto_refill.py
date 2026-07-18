"""PR B — production auto-refill: state machine, caps, off-session charges,
exactly-once crediting, cooldown, and auto-pause.

Stripe is faked at the central transport; no network. Focused on the money-safety
invariants: consent-gating, one refill per threshold crossing (single-active
invariant), deterministic idempotency, no credit until Stripe confirms, cap
enforcement, failure classification, cooldown, and repeated-failure pause.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from _authkit import fresh_sqlite_env  # noqa: E402


def _configure():
    fresh_sqlite_env()
    os.environ["STRIPE_SECRET_KEY"] = "sk_test_x"
    os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_x"
    os.environ["GNSIS_FRONTEND_URL"] = "https://app.gnsis.test"
    os.environ["STRIPE_API_BASE"] = "https://api.stripe.test"
    os.environ["GNSIS_MARKUP_RATE"] = "0.05"
    from gnsis.service import settings as sm

    sm._settings = None
    from gnsis.service.db import init_db

    init_db()


class FakePI:
    """Configurable PaymentIntent transport: success / declined / auth-required /
    processing, driven by ``mode``."""

    def __init__(self, mode="succeeded"):
        self.mode = mode
        self.n = 0

    def __call__(self, method, url, headers, body=None, timeout=30):
        if not url.endswith("/v1/payment_intents"):
            return 200, "{}"
        self.n += 1
        pi = f"pi_{self.n}"
        if self.mode == "succeeded":
            return 200, json.dumps({"id": pi, "status": "succeeded"})
        if self.mode == "processing":
            return 200, json.dumps({"id": pi, "status": "processing"})
        if self.mode == "declined":
            return 402, json.dumps({"error": {
                "code": "card_declined", "decline_code": "generic_decline",
                "message": "Your card was declined.",
                "payment_intent": {"id": pi, "status": "requires_payment_method"},
            }})
        if self.mode == "auth_required":
            return 402, json.dumps({"error": {
                "code": "authentication_required",
                "message": "Authentication required.",
                "payment_intent": {"id": pi, "status": "requires_action"},
            }})
        return 200, json.dumps({"id": pi, "status": "succeeded"})


class AutoRefillTestBase(unittest.TestCase):
    def setUp(self):
        _configure()
        import gnsis.service.stripe_client as scl

        self.fake = FakePI()
        self._orig = scl._http_request
        scl._http_request = self.fake
        # Seed a Customer on the billing anchor so off-session charges have a target.
        from gnsis.service import orm
        from gnsis.service.db import session_scope

        with session_scope() as s:
            s.add(orm.WorkspaceBilling(workspace_id="ws-1", stripe_customer_id="cus_1"))

    def tearDown(self):
        import gnsis.service.stripe_client as scl

        scl._http_request = self._orig

    def settings(self):
        from gnsis.service.settings import get_settings

        return get_settings()

    def enable(self, **over):
        from gnsis.service.auto_refill import save_config

        cfg = dict(
            enabled=True, threshold="5", refill_amount="20", max_refill_amount="50",
            max_refills_per_day=3, daily_cap="100", monthly_cap="500",
            payment_method_id="pm_1", consent=True,
        )
        cfg.update(over)
        return save_config(self.settings(), "ws-1", **cfg)

    def balance(self):
        from gnsis.service.billing import BillingStore

        return BillingStore().balance("ws-1")


class ConfigTests(AutoRefillTestBase):
    def test_enable_requires_consent(self):
        from gnsis.service.auto_refill import AutoRefillError

        with self.assertRaises(AutoRefillError):
            self.enable(consent=False)

    def test_enable_requires_payment_method(self):
        from gnsis.service.auto_refill import AutoRefillError

        with self.assertRaises(AutoRefillError):
            self.enable(payment_method_id=None)

    def test_validation_bounds(self):
        from gnsis.service.auto_refill import AutoRefillError

        with self.assertRaises(AutoRefillError):
            self.enable(refill_amount="60", max_refill_amount="50")  # refill > max
        with self.assertRaises(AutoRefillError):
            self.enable(daily_cap="10", refill_amount="20")           # daily < refill
        with self.assertRaises(AutoRefillError):
            self.enable(monthly_cap="10", daily_cap="100")            # monthly < daily

    def test_consent_is_timestamped_and_active_flag(self):
        cfg = self.enable()
        self.assertTrue(cfg.active)
        self.assertTrue(cfg.consent)
        self.assertIsNotNone(cfg.consent_at)


class TriggerTests(AutoRefillTestBase):
    def test_not_triggered_when_inactive(self):
        from gnsis.service.auto_refill import evaluate_and_maybe_refill

        self.enable(enabled=False, consent=False)
        self.assertIsNone(evaluate_and_maybe_refill(self.settings(), "ws-1"))

    def test_success_credits_exactly_once(self):
        from gnsis.service.auto_refill import SUCCEEDED, evaluate_and_maybe_refill

        self.enable()
        a = evaluate_and_maybe_refill(self.settings(), "ws-1")
        self.assertEqual(a.status, SUCCEEDED)
        self.assertEqual(self.balance(), Decimal("20.00"))

    def test_no_double_credit_when_webhook_arrives(self):
        from gnsis.service.auto_refill import evaluate_and_maybe_refill
        from gnsis.service.stripe_webhook import handle_event

        self.enable()
        a = evaluate_and_maybe_refill(self.settings(), "ws-1")
        pi_id = a.stripe_payment_intent_id
        # The webhook for the SAME PaymentIntent must not credit again.
        handle_event({"id": "evt_1", "type": "payment_intent.succeeded", "data": {"object": {
            "id": pi_id, "status": "succeeded", "amount_received": 2000,
            "currency": "usd", "metadata": {"workspace_id": "ws-1"},
        }}})
        self.assertEqual(self.balance(), Decimal("20.00"))  # still one credit

    def test_skips_when_balance_above_threshold(self):
        from gnsis.service.auto_refill import evaluate_and_maybe_refill
        from gnsis.service.billing import BillingStore

        self.enable(threshold="5")
        BillingStore().top_up("ws-1", "50", idempotency_key="seed")  # above threshold
        self.assertIsNone(evaluate_and_maybe_refill(self.settings(), "ws-1"))

    def test_sweep_refills_eligible_workspaces_only(self):
        from gnsis.service.auto_refill import save_config, sweep

        self.enable()  # ws-1 active, balance 0 < threshold 5
        # A disabled workspace is never swept.
        from gnsis.service import orm
        from gnsis.service.db import session_scope

        with session_scope() as s:
            s.add(orm.WorkspaceBilling(workspace_id="ws-2", stripe_customer_id="cus_2"))
        save_config(
            self.settings(), "ws-2", enabled=False, threshold="5", refill_amount="20",
            max_refill_amount="50", max_refills_per_day=3, daily_cap="100",
            monthly_cap="500", payment_method_id="pm_2", consent=True,
        )
        started = sweep(self.settings())
        self.assertEqual(started, 1)               # only ws-1
        self.assertEqual(self.balance(), Decimal("20.00"))
        from gnsis.service.billing import BillingStore

        self.assertEqual(BillingStore().balance("ws-2"), Decimal("0"))

    def test_single_active_attempt_blocks_duplicate(self):
        # A processing attempt (PI still settling) must block a second trigger,
        # so simultaneous usage events can't fire two refills.
        from gnsis.service.auto_refill import evaluate_and_maybe_refill

        self.fake.mode = "processing"
        self.enable()
        first = evaluate_and_maybe_refill(self.settings(), "ws-1")
        self.assertEqual(first.status, "processing")
        self.assertIsNone(evaluate_and_maybe_refill(self.settings(), "ws-1"))
        self.assertEqual(self.balance(), Decimal("0"))  # nothing credited yet


class FailureTests(AutoRefillTestBase):
    def test_decline_no_credit_sets_cooldown_and_streak(self):
        from gnsis.service.auto_refill import FAILED, evaluate_and_maybe_refill, get_config

        self.fake.mode = "declined"
        self.enable()
        a = evaluate_and_maybe_refill(self.settings(), "ws-1")
        self.assertEqual(a.status, FAILED)
        self.assertEqual(a.failure_code, "generic_decline")
        self.assertEqual(self.balance(), Decimal("0"))
        cfg = get_config("ws-1")
        self.assertEqual(cfg.consecutive_failures, 1)
        self.assertIsNotNone(cfg.cooldown_until)

    def test_authentication_required_is_requires_action(self):
        from gnsis.service.auto_refill import REQUIRES_ACTION, evaluate_and_maybe_refill

        self.fake.mode = "auth_required"
        self.enable()
        a = evaluate_and_maybe_refill(self.settings(), "ws-1")
        self.assertEqual(a.status, REQUIRES_ACTION)
        self.assertEqual(self.balance(), Decimal("0"))

    def test_repeated_failures_auto_pause(self):
        from gnsis.service.auto_refill import evaluate_and_maybe_refill, get_config
        from gnsis.service.db import session_scope
        from gnsis.service import orm

        self.fake.mode = "declined"
        self.enable()
        for _ in range(3):
            # Clear the cooldown between attempts so we reach the pause threshold.
            with session_scope() as s:
                c = s.get(orm.AutoRefillConfig, "ws-1")
                c.cooldown_until = None
            evaluate_and_maybe_refill(self.settings(), "ws-1")
        cfg = get_config("ws-1")
        self.assertTrue(cfg.paused)
        self.assertFalse(cfg.active)
        # Paused → no further attempts.
        self.assertIsNone(evaluate_and_maybe_refill(self.settings(), "ws-1"))


class CapTests(AutoRefillTestBase):
    def test_max_refills_per_day(self):
        from gnsis.service.auto_refill import evaluate_and_maybe_refill

        # High threshold so balance stays below it across refills.
        self.enable(threshold="1000", refill_amount="20", daily_cap="1000",
                    monthly_cap="2000", max_refills_per_day=2)
        evaluate_and_maybe_refill(self.settings(), "ws-1")
        evaluate_and_maybe_refill(self.settings(), "ws-1")
        third = evaluate_and_maybe_refill(self.settings(), "ws-1")  # over the daily count
        self.assertIsNone(third)
        self.assertEqual(self.balance(), Decimal("40.00"))  # exactly two credited

    def test_daily_dollar_cap(self):
        from gnsis.service.auto_refill import evaluate_and_maybe_refill

        self.enable(threshold="1000", refill_amount="20", daily_cap="30", max_refills_per_day=10)
        evaluate_and_maybe_refill(self.settings(), "ws-1")           # 20 <= 30
        second = evaluate_and_maybe_refill(self.settings(), "ws-1")  # 40 > 30 → blocked
        self.assertIsNone(second)
        self.assertEqual(self.balance(), Decimal("20.00"))

    def test_cooldown_blocks_next_attempt(self):
        from gnsis.service.auto_refill import evaluate_and_maybe_refill

        self.fake.mode = "declined"
        self.enable(threshold="1000")
        evaluate_and_maybe_refill(self.settings(), "ws-1")           # fails → cooldown
        self.fake.mode = "succeeded"
        blocked = evaluate_and_maybe_refill(self.settings(), "ws-1")  # still in cooldown
        self.assertIsNone(blocked)
        self.assertEqual(self.balance(), Decimal("0"))


if __name__ == "__main__":
    unittest.main()
