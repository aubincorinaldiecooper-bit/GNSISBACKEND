"""G3 — versioned model pricing + provider-vs-Genesis cost separation.

Covers: historical version selection, token-cost calculation (incl. cached /
reasoning defaults), populating the Genesis cost + pricing version on a usage
row, resolving an unknown provider cost once priced, flagging a meaningful
discrepancy without overwriting either value, leaving unpriced+unknown rows for
reconciliation, and preserving historical pricing when the rate card changes.
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from _authkit import fresh_sqlite_env  # noqa: E402


def _configure():
    fresh_sqlite_env()
    os.environ["GNSIS_MARKUP_RATE"] = "0.05"
    os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test"
    from gnsis.service import settings as sm

    sm._settings = None
    from gnsis.service.db import init_db

    init_db()


def _usage(rid, *, provider="anthropic", model="m", cost=None, cost_source="provider_reported",
           reconciliation_state="resolved", inp=100, out=50, ws="ws-1"):
    from gnsis.service.usage import MeasuredUsage, UsageStore

    u = MeasuredUsage(
        litellm_request_id=rid, workspace_id=ws, user_id="u", provider=provider, model=model,
        input_tokens=inp, output_tokens=out, cached_tokens=0, reasoning_tokens=0,
        duration_ms=1, request_status="success", upstream_cost=(cost or "0"), currency="USD",
        cost_source=cost_source, reconciliation_state=reconciliation_state,
    )
    rec, _ = UsageStore().record(u)
    return rec


class PricingStoreTests(unittest.TestCase):
    def setUp(self):
        _configure()
        from gnsis.service.pricing import PricingStore

        self.ps = PricingStore()

    def test_historical_version_selection(self):
        v1 = self.ps.add_price(provider="anthropic", model="m", input_price="0.00001",
                               output_price="0.00003", effective_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
                               source="v1")
        v2 = self.ps.add_price(provider="anthropic", model="m", input_price="0.00002",
                               output_price="0.00006", effective_start=datetime(2026, 7, 1, tzinfo=timezone.utc),
                               source="v2")
        self.assertEqual(self.ps.resolve("anthropic", "m", datetime(2026, 3, 1, tzinfo=timezone.utc)).id, v1.id)
        self.assertEqual(self.ps.resolve("anthropic", "m", datetime(2026, 8, 1, tzinfo=timezone.utc)).id, v2.id)
        # Only the latest is "current".
        current = self.ps.list_current("anthropic")
        self.assertEqual([c.id for c in current], [v2.id])

    def test_resolve_none_when_unpriced(self):
        self.assertIsNone(self.ps.resolve("openai", "gpt", datetime.now(timezone.utc)))

    def test_calculate_cost_with_defaults(self):
        from gnsis.service.pricing import calculate_cost

        p = self.ps.add_price(provider="anthropic", model="m", input_price="0.00001",
                              output_price="0.00003")
        # cached defaults to input price, reasoning defaults to output price.
        cost = calculate_cost(p, input_tokens=100, output_tokens=50, cached_tokens=10, reasoning_tokens=5)
        expected = Decimal("100")*Decimal("0.00001") + Decimal("50")*Decimal("0.00003") \
            + Decimal("10")*Decimal("0.00001") + Decimal("5")*Decimal("0.00003")
        self.assertEqual(Decimal(cost), expected)


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        _configure()
        from gnsis.service.pricing import PricingStore

        self.ps = PricingStore()
        self.ps.add_price(provider="anthropic", model="m", input_price="0.00002", output_price="0.00006")

    def settings(self):
        from gnsis.service.settings import get_settings

        return get_settings()

    def _reget(self, rec_id):
        from gnsis.service.usage import UsageStore

        return UsageStore().get(rec_id)

    def test_unknown_cost_priced_resolves_and_bills_on_genesis(self):
        from gnsis.service.billing import BillingStore
        from gnsis.service.pricing import price_usage_record

        rec = _usage("r1", cost=None, cost_source="unknown", reconciliation_state="needs_reconciliation")
        price_usage_record(self.settings(), rec.id)
        u = self._reget(rec.id)
        self.assertEqual(u.reconciliation_state, "resolved")
        self.assertEqual(Decimal(u.genesis_calculated_cost), Decimal("0.005"))  # 100*2e-5 + 50*6e-5
        self.assertIsNotNone(u.pricing_version_id)
        BillingStore().top_up("ws-1", "25", idempotency_key="seed")
        charge, charged = BillingStore().charge_usage(self.settings(), rec.id)
        self.assertTrue(charged)
        self.assertEqual(Decimal(charge.retail_cost), Decimal("0.005") * Decimal("1.05"))

    def test_discrepancy_flagged_but_billed_on_provider(self):
        from gnsis.service.billing import BillingStore
        from gnsis.service.pricing import price_usage_record

        # Provider says $1.00; pricing computes ~$0.005 → >5% gap.
        rec = _usage("r2", cost="1.00", cost_source="provider_reported")
        price_usage_record(self.settings(), rec.id)
        u = self._reget(rec.id)
        self.assertEqual(u.reconciliation_reason, "cost_discrepancy")
        self.assertEqual(u.reconciliation_state, "resolved")  # still billable
        self.assertEqual(Decimal(u.genesis_calculated_cost), Decimal("0.005"))
        BillingStore().top_up("ws-1", "25", idempotency_key="seed")
        charge, _ = BillingStore().charge_usage(self.settings(), rec.id)
        # Billed on the provider figure ($1.00), not the calculated one.
        self.assertEqual(Decimal(charge.retail_cost), Decimal("1.05"))

    def test_matching_costs_not_flagged(self):
        from gnsis.service.pricing import price_usage_record

        rec = _usage("r3", cost="0.005", cost_source="provider_reported")
        price_usage_record(self.settings(), rec.id)
        u = self._reget(rec.id)
        self.assertIsNone(u.reconciliation_reason)
        self.assertEqual(u.reconciliation_state, "resolved")

    def test_unpriced_unknown_cost_needs_reconciliation(self):
        from gnsis.service.billing import BillingStore
        from gnsis.service.pricing import price_usage_record

        rec = _usage("r4", provider="openai", model="gpt", cost=None, cost_source="unknown",
                     reconciliation_state="needs_reconciliation")
        price_usage_record(self.settings(), rec.id)
        u = self._reget(rec.id)
        self.assertEqual(u.reconciliation_state, "needs_reconciliation")
        self.assertEqual(u.reconciliation_reason, "unknown_pricing")
        # Never silently charged.
        charge, charged = BillingStore().charge_usage(self.settings(), rec.id)
        self.assertIsNone(charge)
        self.assertFalse(charged)

    def test_historical_pricing_preserved_on_rate_change(self):
        from gnsis.service.pricing import price_usage_record

        rec = _usage("r5", cost=None, cost_source="unknown", reconciliation_state="needs_reconciliation")
        price_usage_record(self.settings(), rec.id)
        original = self._reget(rec.id)
        # Publish a new (more expensive) price; the old event must not be repriced.
        self.ps.add_price(provider="anthropic", model="m", input_price="0.001", output_price="0.001")
        price_usage_record(self.settings(), rec.id)  # re-run: uses version effective at created_at
        after = self._reget(rec.id)
        self.assertEqual(after.pricing_version_id, original.pricing_version_id)
        self.assertEqual(after.genesis_calculated_cost, original.genesis_calculated_cost)


if __name__ == "__main__":
    unittest.main()
