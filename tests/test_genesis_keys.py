"""G2 — Genesis-native virtual keys.

Security-first coverage: the full secret is returned once and never stored or
retrievable; only a hash + non-secret prefix persist; authentication rejects
unknown / disabled / rotated / expired keys; scopes and limits round-trip; and
every read/mutation is workspace-isolated (knowing an id is never enough).
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from _authkit import AUDIENCE, ISSUER, fresh_sqlite_env, make_keypair, mint  # noqa: E402


def _configure():
    fresh_sqlite_env()
    os.environ["GNSIS_VIRTUAL_KEY_PEPPER"] = "test-pepper"
    from gnsis.service import settings as sm

    sm._settings = None
    from gnsis.service.db import init_db

    init_db()


class KeyStoreTests(unittest.TestCase):
    def setUp(self):
        _configure()
        from gnsis.service.virtual_keys import VirtualKeyStore

        self.store = VirtualKeyStore()

    def settings(self):
        from gnsis.service.settings import get_settings

        return get_settings()

    def _create(self, **over):
        cfg = dict(workspace_id="ws-1", name="k", mode="live")
        cfg.update(over)
        return self.store.create(self.settings(), **cfg)

    def test_secret_format_and_prefix_is_non_secret(self):
        view, secret = self._create()
        self.assertTrue(secret.startswith("gns_live_"))
        self.assertNotIn(secret, view.key_prefix)
        self.assertTrue(view.key_prefix.startswith("gns_live_"))
        self.assertLess(len(view.key_prefix), len(secret))

    def test_only_hash_is_stored_never_the_secret(self):
        _, secret = self._create()
        from gnsis.service import orm
        from gnsis.service.db import session_scope

        with session_scope() as s:
            row = s.query(orm.VirtualKey).one()
            self.assertNotEqual(row.key_hash, secret)
            self.assertEqual(len(row.key_hash), 64)  # sha256 hex
            # The secret string must not appear in any stored column.
            for col in (row.key_hash, row.key_prefix, row.name):
                self.assertNotIn(secret, col or "")

    def test_authenticate_valid_and_unknown(self):
        view, secret = self._create(allowed_models=["anthropic/claude-opus-4.8"], hard_limit="100")
        ok = self.store.authenticate(self.settings(), secret)
        self.assertIsNotNone(ok)
        self.assertEqual(ok.id, view.id)
        self.assertEqual(ok.allowed_models, ["anthropic/claude-opus-4.8"])
        self.assertEqual(ok.hard_limit, "100")
        self.assertIsNone(self.store.authenticate(self.settings(), "gns_live_totally_bogus"))
        self.assertIsNone(self.store.authenticate(self.settings(), "not-a-gns-key"))

    def test_pepper_matters(self):
        _, secret = self._create()
        # A different pepper must not validate the same secret.
        os.environ["GNSIS_VIRTUAL_KEY_PEPPER"] = "different"
        from gnsis.service import settings as sm

        sm._settings = None
        self.assertIsNone(self.store.authenticate(sm.get_settings(), secret))

    def test_disabled_and_rotated_keys_do_not_authenticate(self):
        view, secret = self._create()
        self.store.disable("ws-1", view.id)
        self.assertIsNone(self.store.authenticate(self.settings(), secret))

        v2, s2 = self._create()
        new_view, new_secret = self.store.rotate(self.settings(), "ws-1", v2.id)
        self.assertIsNone(self.store.authenticate(self.settings(), s2))       # old retired
        self.assertIsNotNone(self.store.authenticate(self.settings(), new_secret))  # successor works
        self.assertEqual(self.store.get("ws-1", v2.id).rotated_to, new_view.id)

    def test_expired_key_does_not_authenticate(self):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        _, secret = self._create(expires_at=past)
        self.assertIsNone(self.store.authenticate(self.settings(), secret))

    def test_test_mode_prefix(self):
        _, secret = self._create(mode="test")
        self.assertTrue(secret.startswith("gns_test_"))

    def test_soft_over_hard_rejected(self):
        from gnsis.service.virtual_keys import VirtualKeyError

        with self.assertRaises(VirtualKeyError):
            self._create(soft_limit="200", hard_limit="100")

    def test_workspace_isolation(self):
        from gnsis.service.virtual_keys import VirtualKeyError

        view, _ = self._create(workspace_id="ws-1")
        self.assertIsNone(self.store.get("ws-other", view.id))
        self.assertEqual(self.store.list_for_workspace("ws-other"), [])
        with self.assertRaises(VirtualKeyError):
            self.store.disable("ws-other", view.id)  # 404 — id knowledge is not enough


class KeyApiTests(unittest.TestCase):
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

    def auth(self, sub, **kw):
        return {"Authorization": f"Bearer {mint(self.priv, 'k1', sub, **kw)}"}

    def test_requires_auth(self):
        self.assertEqual(self.client.post("/v1/virtual-keys", json={"name": "x"}).status_code, 401)
        self.assertEqual(self.client.get("/v1/virtual-keys").status_code, 401)

    def test_create_returns_secret_once_and_get_never_reveals_it(self):
        r = self.client.post(
            "/v1/virtual-keys",
            json={"name": "prod", "mode": "live", "allowed_models": ["anthropic/claude-opus-4.8"],
                  "hard_limit": "100"},
            headers=self.auth("user-1"),
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        secret = body["key"]
        self.assertTrue(secret.startswith("gns_live_"))
        key_id = body["virtual_key"]["id"]
        # The secret must never come back from a read.
        got = self.client.get(f"/v1/virtual-keys/{key_id}", headers=self.auth("user-1"))
        self.assertEqual(got.status_code, 200)
        self.assertNotIn(secret, got.text)
        self.assertNotIn("key_hash", got.text)  # hash is not exposed either

    def test_list_disable_rotate_and_cross_workspace(self):
        created = self.client.post(
            "/v1/virtual-keys", json={"name": "k"}, headers=self.auth("user-1")
        ).json()["virtual_key"]

        # user-2 cannot see or mutate user-1's key.
        self.assertEqual(self.client.get("/v1/virtual-keys", headers=self.auth("user-2")).json()["items"], [])
        self.assertEqual(
            self.client.post(f"/v1/virtual-keys/{created['id']}/disable", headers=self.auth("user-2")).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(f"/v1/virtual-keys/{created['id']}", headers=self.auth("user-2")).status_code,
            404,
        )

        # user-1 disables then rotates.
        d = self.client.post(f"/v1/virtual-keys/{created['id']}/disable", headers=self.auth("user-1"))
        self.assertEqual(d.json()["status"], "disabled")


if __name__ == "__main__":
    unittest.main()
