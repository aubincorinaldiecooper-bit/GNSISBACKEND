"""PR-G1 — usage-ledger integrity + idempotency.

The append-only usage ledger must never silently assign $0 when the provider
cost is unknown, must dedup by explicit idempotency key (so a provider retry or
webhook redelivery never creates duplicate billable usage), must preserve the
provider request id + error category, and must surface rows needing
reconciliation. All exercised through the documented callback contract + the
stores directly (no network).
"""

from __future__ import annotations

import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from _authkit import fresh_sqlite_env  # noqa: E402


def _configure():
    fresh_sqlite_env()
    os.environ["GNSIS_MARKUP_RATE"] = "0.05"
    os.environ["GNSIS_RATE_CARD_VERSION"] = "beta-2026-07"
    os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test"  # billing_enabled
    from gnsis.service import settings as sm

    sm._settings = None
    from gnsis.service.db import init_db

    init_db()


def _cb(rid, *, cost=None, status="success", idem=None, provider_request_id=None,
        error_category=None, retry_of=None, ws="ws-1"):
    md = {"workspace_id": ws, "user_id": "u-1"}
    if idem:
        md["idempotency_key"] = idem
    body = {
        "litellm_request_id": rid, "metadata": md, "provider": "anthropic",
        "model": "anthropic/claude-opus-4.8", "input_tokens": 100, "output_tokens": 50,
        "request_status": status, "currency": "USD",
    }
    if cost is not None:
        body["upstream_cost"] = cost
    if provider_request_id:
        body["provider_request_id"] = provider_request_id
    if error_category:
        body["error_category"] = error_category
    if retry_of:
        body["retry_of"] = retry_of
    return body


def _record(body):
    from gnsis.service.usage import UsageStore, parse_callback

    return UsageStore().record(parse_callback(body))


class LedgerIntegrityTests(unittest.TestCase):
    def setUp(self):
        _configure()

    def _settings(self):
        from gnsis.service.settings import get_settings

        return get_settings()

    def _balance(self, ws="ws-1"):
        from gnsis.service.billing import BillingStore

        return BillingStore().balance(ws)

    def test_unknown_cost_on_success_flags_reconciliation_and_never_charges_zero(self):
        from gnsis.service.billing import BillingStore
        from gnsis.service.usage import UsageStore

        rec, created = _record(_cb("u1", cost=None, status="success"))
        self.assertTrue(created)
        self.assertEqual(rec.cost_source, "unknown")
        self.assertTrue(rec.needs_reconciliation)

        charge, charged = BillingStore().charge_usage(self._settings(), rec.id)
        self.assertIsNone(charge)          # no invented charge
        self.assertFalse(charged)
        self.assertEqual(self._balance(), Decimal("0"))  # never silently debited
        self.assertEqual(UsageStore().count_needs_reconciliation("ws-1"), 1)

    def test_known_cost_resolves_and_charges(self):
        from gnsis.service.billing import BillingStore

        BillingStore().top_up("ws-1", "25.00", idempotency_key="seed")
        rec, _ = _record(_cb("u2", cost="1.00"))
        self.assertEqual(rec.cost_source, "provider_reported")
        self.assertFalse(rec.needs_reconciliation)
        charge, charged = BillingStore().charge_usage(self._settings(), rec.id)
        self.assertTrue(charged)
        self.assertEqual(Decimal(charge.retail_cost), Decimal("1.05"))
        self.assertEqual(self._balance(), Decimal("23.95"))

    def test_failed_request_without_cost_is_resolved_not_flagged(self):
        rec, _ = _record(_cb("u3", cost=None, status="error"))
        # A failed request legitimately carries no charge — not a reconciliation case.
        self.assertEqual(rec.reconciliation_state, "resolved")
        from gnsis.service.usage import UsageStore

        self.assertEqual(UsageStore().count_needs_reconciliation("ws-1"), 0)

    def test_explicit_idempotency_key_dedups(self):
        r1, c1 = _record(_cb("prov-a", cost="1.00", idem="op-1"))
        r2, c2 = _record(_cb("prov-b", cost="1.00", idem="op-1"))  # different call id, same op
        self.assertTrue(c1)
        self.assertFalse(c2)
        self.assertEqual(r1.id, r2.id)

    def test_provider_retry_same_idempotency_key_never_double_bills(self):
        from gnsis.service.billing import BillingStore

        BillingStore().top_up("ws-1", "25.00", idempotency_key="seed")
        r1, _ = _record(_cb("prov-req-1", cost="1.00", idem="call-1"))
        BillingStore().charge_usage(self._settings(), r1.id)
        # The provider request is retried and re-reported with the same op key.
        r2, created2 = _record(_cb("prov-req-1-retry", cost="1.00", idem="call-1"))
        self.assertFalse(created2)
        self.assertEqual(r2.id, r1.id)
        self.assertEqual(self._balance(), Decimal("23.95"))  # billed exactly once

    def test_provider_request_id_and_error_category_captured(self):
        rec, _ = _record(_cb("u6", cost="1.00", provider_request_id="prov-123",
                             error_category="rate_limited"))
        self.assertEqual(rec.provider_request_id, "prov-123")
        self.assertEqual(rec.error_category, "rate_limited")

    def test_retry_of_lineage_recorded(self):
        rec, _ = _record(_cb("u8", cost="1.00", retry_of="u7"))
        self.assertEqual(rec.retry_of, "u7")

    def test_needs_reconciliation_listing(self):
        from gnsis.service.usage import UsageStore

        _record(_cb("bad", cost=None, status="success"))
        _record(_cb("good", cost="1.00"))
        flagged = UsageStore().list_needs_reconciliation("ws-1")
        self.assertEqual([r.litellm_request_id for r in flagged], ["bad"])


if __name__ == "__main__":
    unittest.main()
