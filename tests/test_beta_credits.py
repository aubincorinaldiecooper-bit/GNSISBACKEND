"""Operator beta credits: idempotent, capped, reversible manual top-ups.

Covers the service layer directly (grant / reverse / summary against the
balance ledger) and the internal-key-guarded admin API (auth gate + the money
path end to end). Money is compared by decimal value, not string form, since the
ledger stores normalised decimal strings.
"""

from __future__ import annotations

import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from _authkit import fresh_sqlite_env  # noqa: E402


def _prepare(api_key: str = "test-internal-key"):
    os.environ["GNSIS_API_KEY"] = api_key
    os.environ["GNSIS_BETA_CREDIT_MAX_USD"] = "50.00"
    fresh_sqlite_env()
    from gnsis.service import settings as sm

    sm._settings = None
    from gnsis.service.db import init_db

    init_db()


class GrantServiceTests(unittest.TestCase):
    def setUp(self):
        _prepare()

    def _balance(self, ws) -> Decimal:
        from gnsis.service.billing import BillingStore

        return BillingStore().balance(ws)

    def test_grant_credits_balance_once(self):
        from gnsis.service.beta_credits import grant_credit

        g = grant_credit(workspace_id="ws-1", amount="10.00", reason="beta invite",
                         operator="ops@gnsis", idempotency_key="k1", max_amount="50.00")
        self.assertEqual(g["status"], "granted")
        self.assertFalse(g["duplicate"])
        self.assertEqual(self._balance("ws-1"), Decimal("10"))

    def test_grant_is_idempotent(self):
        from gnsis.service.beta_credits import grant_credit

        a = grant_credit(workspace_id="ws-1", amount="10.00", reason="r",
                         operator="ops", idempotency_key="dup", max_amount="50.00")
        b = grant_credit(workspace_id="ws-1", amount="10.00", reason="r",
                         operator="ops", idempotency_key="dup", max_amount="50.00")
        self.assertEqual(a["id"], b["id"])
        self.assertTrue(b["duplicate"])
        self.assertEqual(self._balance("ws-1"), Decimal("10"))  # not double-credited

    def test_over_cap_rejected(self):
        from gnsis.service.beta_credits import BetaCreditError, grant_credit

        with self.assertRaises(BetaCreditError):
            grant_credit(workspace_id="ws-1", amount="500.00", reason="r",
                         operator="ops", idempotency_key="k", max_amount="50.00")
        self.assertEqual(self._balance("ws-1"), Decimal("0"))

    def test_invalid_inputs_rejected(self):
        from gnsis.service.beta_credits import BetaCreditError, grant_credit

        for kw in (
            {"amount": "0"}, {"amount": "-5"}, {"amount": "abc"},
            {"reason": ""}, {"operator": ""}, {"idempotency_key": ""},
        ):
            params = dict(workspace_id="ws-1", amount="5", reason="r", operator="ops",
                          idempotency_key="k", max_amount="50.00")
            params.update(kw)
            with self.assertRaises(BetaCreditError):
                grant_credit(**params)

    def test_reverse_is_compensating_and_idempotent(self):
        from gnsis.service.beta_credits import grant_credit, reverse_grant

        g = grant_credit(workspace_id="ws-1", amount="10.00", reason="r",
                         operator="ops", idempotency_key="k1", max_amount="50.00")
        r1 = reverse_grant(grant_id=g["id"], operator="ops2", reason="mistake")
        self.assertEqual(r1["status"], "reversed")
        self.assertEqual(r1["reversed_by"], "ops2")
        self.assertEqual(self._balance("ws-1"), Decimal("0"))  # credit undone
        # Reversing again is a no-op (no second negative transaction).
        r2 = reverse_grant(grant_id=g["id"], operator="ops2", reason="again")
        self.assertTrue(r2["duplicate"])
        self.assertEqual(self._balance("ws-1"), Decimal("0"))

    def test_reverse_unknown_grant(self):
        from gnsis.service.beta_credits import BetaCreditError, reverse_grant

        with self.assertRaises(BetaCreditError):
            reverse_grant(grant_id="nope", operator="ops", reason="")

    def test_summary_lists_grants_and_balance(self):
        from gnsis.service.beta_credits import grant_credit, workspace_summary

        grant_credit(workspace_id="ws-1", amount="10.00", reason="r",
                     operator="ops", idempotency_key="k1", max_amount="50.00")
        s = workspace_summary("ws-1")
        self.assertEqual(Decimal(s["balance"]), Decimal("10"))
        self.assertEqual(len(s["grants"]), 1)

    def test_tenant_isolation(self):
        from gnsis.service.beta_credits import grant_credit

        grant_credit(workspace_id="ws-1", amount="10.00", reason="r",
                     operator="ops", idempotency_key="k1", max_amount="50.00")
        self.assertEqual(self._balance("ws-1"), Decimal("10"))
        self.assertEqual(self._balance("ws-2"), Decimal("0"))


class AdminApiTests(unittest.TestCase):
    def setUp(self):
        _prepare(api_key="secret-key")
        from fastapi.testclient import TestClient
        from gnsis.service import api

        self.client = TestClient(api.app)
        self.auth = {"Authorization": "Bearer secret-key"}

    def test_requires_internal_key(self):
        r = self.client.post("/v1/admin/credits", json={
            "workspace_id": "ws-1", "amount": "5", "reason": "r",
            "operator": "ops", "idempotency_key": "k"})
        self.assertEqual(r.status_code, 401)

    def test_grant_reverse_and_inspect(self):
        body = {"workspace_id": "ws-1", "amount": "10.00", "reason": "beta",
                "operator": "ops@gnsis", "idempotency_key": "k1"}
        r = self.client.post("/v1/admin/credits", json=body, headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(Decimal(data["workspace"]["balance"]), Decimal("10"))
        grant_id = data["grant"]["id"]

        # Duplicate request → same grant, balance unchanged.
        r2 = self.client.post("/v1/admin/credits", json=body, headers=self.auth)
        self.assertEqual(r2.json()["grant"]["id"], grant_id)
        self.assertEqual(Decimal(r2.json()["workspace"]["balance"]), Decimal("10"))

        # Over cap → 422.
        r3 = self.client.post("/v1/admin/credits", json={**body, "amount": "9999", "idempotency_key": "k2"}, headers=self.auth)
        self.assertEqual(r3.status_code, 422)

        # Inspect.
        r4 = self.client.get("/v1/admin/credits", params={"workspace_id": "ws-1"}, headers=self.auth)
        self.assertEqual(Decimal(r4.json()["balance"]), Decimal("10"))
        self.assertEqual(len(r4.json()["grants"]), 1)

        # Reverse → balance back to zero.
        r5 = self.client.post(f"/v1/admin/credits/{grant_id}/reverse",
                              json={"operator": "ops2", "reason": "mistake"}, headers=self.auth)
        self.assertEqual(r5.status_code, 200, r5.text)
        self.assertEqual(Decimal(r5.json()["workspace"]["balance"]), Decimal("0"))

    def test_reverse_unknown_grant_404(self):
        r = self.client.post("/v1/admin/credits/nope/reverse",
                             json={"operator": "ops", "reason": ""}, headers=self.auth)
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
