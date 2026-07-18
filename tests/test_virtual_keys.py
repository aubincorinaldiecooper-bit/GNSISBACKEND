"""PR 3.1 — customer virtual keys (LiteLLM-issued, budget-capped).

Exercises the workspace-scoped key endpoints via the FastAPI TestClient with a
monkeypatched LiteLLM admin transport (no network): issuance returns the secret
exactly once and stores only the token + display prefix, budgets are validated
and capped, listing/revocation are workspace-isolated, revoke calls LiteLLM and
is idempotent, attribution metadata is stamped on the LiteLLM request, and the
endpoints report 503 when virtual keys aren't configured.
"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from _authkit import AUDIENCE, ISSUER, fresh_sqlite_env, make_keypair, mint  # noqa: E402


class FakeLiteLLM:
    """Records requests and returns canned LiteLLM admin responses."""

    def __init__(self):
        self.calls = []
        self.n = 0

    def __call__(self, method, url, headers, body=None, timeout=30):
        parsed = json.loads(body.decode()) if body else None
        self.calls.append({"method": method, "url": url, "body": parsed})
        if url.endswith("/key/generate"):
            self.n += 1
            return 200, json.dumps(
                {"key": f"sk-secret{self.n:04d}value", "token": f"tok_{self.n}", "key_name": "sk-...alue"}
            )
        if url.endswith("/key/delete"):
            return 200, json.dumps({"deleted_keys": (parsed or {}).get("keys", [])})
        return 200, json.dumps({})


class VirtualKeyTestBase(unittest.TestCase):
    enabled = True

    def setUp(self):
        fresh_sqlite_env()
        os.environ["BETTER_AUTH_JWKS_URL"] = "https://auth.test/jwks"
        os.environ["BETTER_AUTH_ISSUER"] = ISSUER
        os.environ["BETTER_AUTH_AUDIENCE"] = AUDIENCE
        os.environ["GNSIS_LITELLM_URL"] = "https://litellm.test"
        if self.enabled:
            os.environ["GNSIS_LITELLM_MASTER_KEY"] = "sk-master"
        else:
            os.environ.pop("GNSIS_LITELLM_MASTER_KEY", None)
        os.environ["GNSIS_VIRTUAL_KEY_MAX_BUDGET_USD"] = "50"
        os.environ["GNSIS_VIRTUAL_KEY_DEFAULT_BUDGET_USD"] = "10"
        from gnsis.service import settings as settings_mod

        settings_mod._settings = None
        from gnsis.service.db import init_db

        init_db()

        import gnsis.service.litellm_admin as admin

        self.fake = FakeLiteLLM()
        self._orig = admin._http_request
        admin._http_request = self.fake

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
        import gnsis.service.litellm_admin as admin

        admin._http_request = self._orig
        self.api.app.dependency_overrides.clear()

    def auth(self, sub, **kw):
        return {"Authorization": f"Bearer {mint(self.priv, 'k1', sub, **kw)}"}


class VirtualKeyTests(VirtualKeyTestBase):
    def test_requires_auth(self):
        self.assertEqual(self.client.get("/v1/dashboard/keys").status_code, 401)
        self.assertEqual(self.client.post("/v1/dashboard/keys", json={"key_alias": "x"}).status_code, 401)

    def test_create_returns_secret_once_and_stores_no_secret(self):
        r = self.client.post(
            "/v1/dashboard/keys",
            json={"key_alias": "prod app", "max_budget_usd": "15"},
            headers=self.auth("user-1", email="u@x.io"),
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["key"].startswith("sk-secret"))  # the one-time secret
        vk = body["virtual_key"]
        self.assertEqual(vk["key_alias"], "prod app")
        self.assertEqual(vk["max_budget"], "15")
        self.assertEqual(vk["status"], "active")
        self.assertIn("…", vk["key_prefix"])  # display prefix, not the secret
        self.assertNotIn("secret", vk["key_prefix"])

        # The stored row keeps the LiteLLM token + prefix, never the secret value.
        from gnsis.service import orm
        from gnsis.service.db import session_scope

        with session_scope() as s:
            row = s.query(orm.VirtualKey).one()
            self.assertEqual(row.litellm_token, "tok_1")
            self.assertNotIn(body["key"], (row.key_prefix or ""))

    def test_attribution_metadata_sent_to_litellm(self):
        self.client.post(
            "/v1/dashboard/keys",
            json={"key_alias": "app-a"},
            headers=self.auth("user-1"),
        )
        gen = [c for c in self.fake.calls if c["url"].endswith("/key/generate")][0]
        md = gen["body"]["metadata"]
        self.assertEqual(md["application_name"], "app-a")
        self.assertEqual(md["user_id"], "user-1")
        self.assertTrue(md["workspace_id"])
        # Default budget applied (10) and forwarded as a JSON number.
        self.assertEqual(gen["body"]["max_budget"], 10.0)

    def test_budget_over_max_rejected(self):
        r = self.client.post(
            "/v1/dashboard/keys",
            json={"key_alias": "big", "max_budget_usd": "500"},
            headers=self.auth("user-1"),
        )
        self.assertEqual(r.status_code, 400)
        r2 = self.client.post(
            "/v1/dashboard/keys",
            json={"key_alias": "bad", "max_budget_usd": "-1"},
            headers=self.auth("user-1"),
        )
        self.assertEqual(r2.status_code, 400)

    def test_list_and_revoke_workspace_isolated(self):
        # user-1 creates a key.
        created = self.client.post(
            "/v1/dashboard/keys", json={"key_alias": "k1"}, headers=self.auth("user-1")
        ).json()["virtual_key"]

        # user-2 sees none of user-1's keys.
        other = self.client.get("/v1/dashboard/keys", headers=self.auth("user-2")).json()
        self.assertEqual(other["items"], [])
        self.assertTrue(other["enabled"])

        # user-2 cannot revoke user-1's key.
        forbidden = self.client.delete(
            f"/v1/dashboard/keys/{created['id']}", headers=self.auth("user-2")
        )
        self.assertEqual(forbidden.status_code, 404)

        # user-1 revokes it → LiteLLM delete called, status flips, idempotent.
        r = self.client.delete(f"/v1/dashboard/keys/{created['id']}", headers=self.auth("user-1"))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "revoked")
        self.assertTrue(any(c["url"].endswith("/key/delete") for c in self.fake.calls))
        again = self.client.delete(f"/v1/dashboard/keys/{created['id']}", headers=self.auth("user-1"))
        self.assertEqual(again.json()["status"], "revoked")

        mine = self.client.get("/v1/dashboard/keys", headers=self.auth("user-1")).json()
        self.assertEqual(len(mine["items"]), 1)


class VirtualKeyDisabledTests(VirtualKeyTestBase):
    enabled = False

    def test_create_requires_configuration(self):
        r = self.client.post(
            "/v1/dashboard/keys", json={"key_alias": "x"}, headers=self.auth("user-1")
        )
        self.assertEqual(r.status_code, 503)

    def test_list_reports_disabled_but_ok(self):
        r = self.client.get("/v1/dashboard/keys", headers=self.auth("user-1"))
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["enabled"])


if __name__ == "__main__":
    unittest.main()
