"""Automatic welcome credits: idempotent per-campaign, safety-valved, non-fatal.

Exercises the service directly (grant conditions, idempotency, platform ceiling,
misconfiguration) and end-to-end through the real GitHub claim path so a claim
never fails because a credit fails.
"""

from __future__ import annotations

import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from _authkit import AUDIENCE, ISSUER, fresh_sqlite_env, make_keypair, mint  # noqa: E402


def _reset_settings():
    from gnsis.service import settings as sm
    sm._settings = None


def _enable_welcome(
    *,
    amount: str = "5.00",
    per_run: str = "0.50",
    campaign: str = "beta-2026-07",
    daily_ceiling: str = "",
    max_grant: str = "50.00",
    run_max_cost: str = "3.00",
):
    os.environ["GNSIS_WELCOME_CREDIT_ENABLED"] = "true"
    os.environ["GNSIS_WELCOME_CREDIT_USD"] = amount
    os.environ["GNSIS_WELCOME_CREDIT_PER_RUN_USD"] = per_run
    os.environ["GNSIS_WELCOME_CREDIT_CAMPAIGN"] = campaign
    os.environ["GNSIS_PLATFORM_DAILY_PROVIDER_LIMIT_USD"] = daily_ceiling
    os.environ["GNSIS_BETA_CREDIT_MAX_USD"] = max_grant
    # Pin the run cost cap so a preceding test that mutated it can't drift the
    # per-run-cap sanity check into a false "SLA exceeds cap" skip.
    os.environ["GNSIS_RUN_MAX_COST_USD"] = run_max_cost
    _reset_settings()


def _disable_welcome():
    for k in (
        "GNSIS_WELCOME_CREDIT_ENABLED",
        "GNSIS_WELCOME_CREDIT_USD",
        "GNSIS_WELCOME_CREDIT_PER_RUN_USD",
        "GNSIS_WELCOME_CREDIT_CAMPAIGN",
        "GNSIS_PLATFORM_DAILY_PROVIDER_LIMIT_USD",
    ):
        os.environ.pop(k, None)
    _reset_settings()


def _prepare():
    fresh_sqlite_env()
    _reset_settings()
    from gnsis.service.db import init_db
    init_db()


def _balance(workspace_id: str) -> Decimal:
    from gnsis.service.billing import BillingStore
    return BillingStore().balance(workspace_id)


class WelcomeCreditServiceTests(unittest.TestCase):
    """Direct-service coverage; no HTTP, no GitHub."""

    def setUp(self):
        _prepare()
        _enable_welcome()

    def tearDown(self):
        _disable_welcome()

    def test_disabled_flag_returns_none_and_grants_nothing(self):
        _disable_welcome()
        from gnsis.service.welcome_credits import try_grant
        self.assertIsNone(try_grant("ws-1"))
        self.assertEqual(_balance("ws-1"), Decimal("0"))

    def test_first_grant_credits_the_configured_amount(self):
        from gnsis.service.welcome_credits import try_grant
        result = try_grant("ws-1")
        self.assertIsNotNone(result)
        self.assertFalse(result["duplicate"])
        self.assertEqual(_balance("ws-1"), Decimal("5"))
        self.assertEqual(result["operator"], "welcome")
        self.assertIn("beta-2026-07", result["reason"])

    def test_second_call_same_workspace_is_idempotent(self):
        from gnsis.service.welcome_credits import try_grant
        a = try_grant("ws-1")
        b = try_grant("ws-1")
        self.assertEqual(a["id"], b["id"])
        self.assertTrue(b["duplicate"])
        # Balance is credited exactly once — the second call did not add another $5.
        self.assertEqual(_balance("ws-1"), Decimal("5"))

    def test_different_workspaces_each_get_one_grant(self):
        from gnsis.service.welcome_credits import try_grant
        try_grant("ws-1")
        try_grant("ws-2")
        self.assertEqual(_balance("ws-1"), Decimal("5"))
        self.assertEqual(_balance("ws-2"), Decimal("5"))

    def test_new_campaign_makes_the_same_workspace_eligible_again(self):
        from gnsis.service.welcome_credits import try_grant

        # First campaign.
        try_grant("ws-1")
        self.assertEqual(_balance("ws-1"), Decimal("5"))

        # Operator flips campaigns — the (workspace, campaign) key changes so
        # a fresh grant is legitimate.
        _enable_welcome(campaign="birthday-2027", amount="3.00")
        try_grant("ws-1")
        self.assertEqual(_balance("ws-1"), Decimal("8"))

    def test_platform_daily_ceiling_silently_skips_further_grants(self):
        # Grant amount is $5; ceiling is $8 → only one workspace can be credited.
        _enable_welcome(daily_ceiling="8.00")
        from gnsis.service.welcome_credits import try_grant

        self.assertIsNotNone(try_grant("ws-1"))
        self.assertEqual(_balance("ws-1"), Decimal("5"))

        # Second grant would bring the day's total to $10 > $8, so it's skipped.
        self.assertIsNone(try_grant("ws-2"))
        self.assertEqual(_balance("ws-2"), Decimal("0"))

    def test_platform_daily_ceiling_admits_grants_within_budget(self):
        # Ceiling $15 fits three $5 grants.
        _enable_welcome(daily_ceiling="15.00")
        from gnsis.service.welcome_credits import try_grant
        for i in range(3):
            self.assertIsNotNone(try_grant(f"ws-{i}"))
        # A fourth grant tips the day over the ceiling.
        self.assertIsNone(try_grant("ws-3"))

    def test_amount_over_shared_beta_cap_is_rejected(self):
        # The shared beta_credit_max_usd cap ($10) is smaller than the $50
        # welcome amount — the grant is refused so we can't accidentally spend
        # more than the audit machinery is willing to record.
        _enable_welcome(amount="50.00", max_grant="10.00")
        from gnsis.service.welcome_credits import try_grant
        self.assertIsNone(try_grant("ws-1"))
        self.assertEqual(_balance("ws-1"), Decimal("0"))

    def test_per_run_cap_over_run_max_cost_is_rejected(self):
        # welcome_credit_per_run_usd advertised as $10 but run_max_cost_usd is
        # $3 — the SLA would be a lie, so the grant is skipped.
        _enable_welcome(per_run="10.00", run_max_cost="3.00")
        from gnsis.service.welcome_credits import try_grant
        self.assertIsNone(try_grant("ws-1"))

    def test_empty_workspace_id_is_skipped_not_errored(self):
        from gnsis.service.welcome_credits import try_grant
        self.assertIsNone(try_grant(""))
        self.assertIsNone(try_grant("   "))

    def test_summary_includes_the_welcome_grant(self):
        from gnsis.service.welcome_credits import try_grant
        from gnsis.service.beta_credits import workspace_summary
        try_grant("ws-1")
        s = workspace_summary("ws-1")
        self.assertEqual(Decimal(s["balance"]), Decimal("5"))
        self.assertEqual(len(s["grants"]), 1)
        self.assertEqual(s["grants"][0]["operator"], "welcome")

    def test_idempotency_key_shape_survives_workspace_and_campaign(self):
        # This is the invariant the whole "one grant per (workspace, campaign)"
        # guarantee depends on — encode it as a test so a rename in the service
        # module is caught immediately.
        from gnsis.service.welcome_credits import _idempotency_key
        self.assertEqual(
            _idempotency_key("workspace-abc", "beta-2026-07"),
            "welcome:workspace-abc:beta-2026-07",
        )


# -----------------------------------------------------------------------------
# End-to-end coverage: welcome credit is triggered by the real claim path and
# never fails the claim.
# -----------------------------------------------------------------------------


class FakeAuthClient:
    def __init__(self, allowed):
        self.allowed = allowed

    def verify_installation(self, auth_subject, installation_id):
        from gnsis.service.auth_client import (
            InstallationVerificationError,
            VerifiedInstallation,
        )
        if installation_id not in self.allowed:
            raise InstallationVerificationError("not accessible", status=403)
        return VerifiedInstallation(
            installation_id=installation_id,
            account_id=1000 + installation_id,
            account_login=f"acct-{installation_id}",
            account_type="User",
        )


class FakeGitHubApp:
    def __init__(self, repos_by_installation):
        self.repos_by_installation = repos_by_installation

    def token_for_installation(self, installation_id):
        return f"ghs_faketoken_{installation_id}"


def _repo(repo_id, full_name, default_branch="main", private=False):
    owner, name = full_name.split("/")
    return {
        "id": repo_id,
        "full_name": full_name,
        "name": name,
        "owner": {"login": owner},
        "default_branch": default_branch,
        "private": private,
        "archived": False,
    }


class ClaimEndToEndTests(unittest.TestCase):
    """The claim endpoint triggers the grant, but it never blocks the claim."""

    def setUp(self):
        _prepare()
        os.environ["BETTER_AUTH_JWKS_URL"] = "https://auth.test/jwks"
        os.environ["BETTER_AUTH_ISSUER"] = ISSUER
        os.environ["BETTER_AUTH_AUDIENCE"] = AUDIENCE
        os.environ["GNSIS_AUTH_INTERNAL_URL"] = "https://auth.test"
        os.environ["GNSIS_AUTH_INTERNAL_SECRET"] = "internal-secret"
        os.environ["GITHUB_APP_ID"] = "12345"
        os.environ["GITHUB_APP_PRIVATE_KEY"] = "key"
        os.environ["GITHUB_APP_SLUG"] = "genesis"
        _enable_welcome()

        from fastapi.testclient import TestClient
        from gnsis.service import api
        from gnsis.service.auth import JwksCache, JwtVerifier

        self.priv, self.jwks = make_keypair("k1")
        self.api = api
        verifier = JwtVerifier(
            JwksCache(fetcher=lambda: self.jwks), issuer=ISSUER, audience=AUDIENCE
        )
        self.fake_auth = FakeAuthClient(allowed={555, 777})
        self.fake_gh = FakeGitHubApp({
            555: [_repo(10, "octo/alpha"), _repo(11, "octo/beta", private=True)],
            777: [_repo(20, "octo/gamma")],
        })

        import gnsis.service.installations as inst_mod
        self._orig_list = inst_mod.list_installation_repositories
        inst_mod.list_installation_repositories = (
            lambda token: self.fake_gh.repos_by_installation.get(
                int(token.split("_")[-1]), []
            )
        )

        api.app.dependency_overrides[api.get_verifier] = lambda: verifier
        api.app.dependency_overrides[api.get_auth_client] = lambda: self.fake_auth
        api.app.dependency_overrides[api.get_github_app] = lambda: self.fake_gh
        self.client = TestClient(api.app)

    def tearDown(self):
        self.api.app.dependency_overrides.clear()
        import gnsis.service.installations as inst_mod
        inst_mod.list_installation_repositories = self._orig_list
        _disable_welcome()

    def _auth(self, sub):
        return {"Authorization": f"Bearer {mint(self.priv, 'k1', sub)}"}

    def _workspace_id(self, sub):
        me = self.client.get("/v1/me", headers=self._auth(sub)).json()
        return me["workspace"]["id"]

    def test_first_claim_grants_the_welcome_credit(self):
        # Bring the workspace into existence and remember its id.
        ws = self._workspace_id("user-1")
        self.assertEqual(_balance(ws), Decimal("0"))

        r = self.client.post(
            "/v1/github/installations/claim",
            json={"installation_id": 555},
            headers=self._auth("user-1"),
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(_balance(ws), Decimal("5"))

    def test_second_claim_same_workspace_never_double_grants(self):
        ws = self._workspace_id("user-1")
        for _ in range(3):
            r = self.client.post(
                "/v1/github/installations/claim",
                json={"installation_id": 555},
                headers=self._auth("user-1"),
            )
            self.assertEqual(r.status_code, 200, r.text)
        # Still exactly one $5 grant.
        self.assertEqual(_balance(ws), Decimal("5"))

    def test_adding_a_second_installation_does_not_grant_again(self):
        ws = self._workspace_id("user-1")
        # First installation.
        self.client.post(
            "/v1/github/installations/claim",
            json={"installation_id": 555},
            headers=self._auth("user-1"),
        )
        # Same user, different installation on the same workspace.
        self.client.post(
            "/v1/github/installations/claim",
            json={"installation_id": 777},
            headers=self._auth("user-1"),
        )
        self.assertEqual(_balance(ws), Decimal("5"))

    def test_claim_still_succeeds_when_credit_is_skipped(self):
        # Flip welcome credit off after the workspace exists — the claim should
        # still succeed with no funding side effects.
        _disable_welcome()
        ws = self._workspace_id("user-1")
        r = self.client.post(
            "/v1/github/installations/claim",
            json={"installation_id": 555},
            headers=self._auth("user-1"),
        )
        self.assertEqual(r.status_code, 200, r.text)
        # No credit granted; workspace is still connected.
        self.assertEqual(_balance(ws), Decimal("0"))
        me = self.client.get("/v1/me", headers=self._auth("user-1")).json()
        self.assertTrue(me["github"]["connected"])

    def test_claim_still_succeeds_when_grant_raises(self):
        # Force the credit call to raise; the claim endpoint must swallow it.
        import gnsis.service.welcome_credits as wc

        def boom(_ws, settings=None):  # noqa: ARG001
            raise RuntimeError("simulated ledger outage")

        original = wc.try_grant
        wc.try_grant = boom
        try:
            r = self.client.post(
                "/v1/github/installations/claim",
                json={"installation_id": 555},
                headers=self._auth("user-1"),
            )
            self.assertEqual(r.status_code, 200, r.text)
        finally:
            wc.try_grant = original


if __name__ == "__main__":
    unittest.main()
