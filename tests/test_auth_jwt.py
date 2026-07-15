import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from _authkit import AUDIENCE, ISSUER, make_keypair, mint  # noqa: E402

from gnsis.service.auth import AuthError, JwksCache, JwtVerifier, bearer_token  # noqa: E402


class BearerTests(unittest.TestCase):
    def test_extracts_token(self):
        self.assertEqual(bearer_token("Bearer abc.def.ghi"), "abc.def.ghi")

    def test_missing_header_rejected(self):
        with self.assertRaises(AuthError):
            bearer_token(None)

    def test_malformed_header_rejected(self):
        for bad in ["abc", "Bearer", "Basic xyz", "Bearer "]:
            with self.assertRaises(AuthError):
                bearer_token(bad)


class JwtVerifierTests(unittest.TestCase):
    def setUp(self):
        self.priv, self.jwks = make_keypair("k1")
        self.verifier = JwtVerifier(
            JwksCache(fetcher=lambda: self.jwks), issuer=ISSUER, audience=AUDIENCE
        )

    def test_valid_token_accepted(self):
        tok = mint(self.priv, "k1", "user-1", email="u@x.io", github_login="octo")
        user = self.verifier.verify(tok)
        self.assertEqual(user.subject, "user-1")
        self.assertEqual(user.email, "u@x.io")
        self.assertEqual(user.github_login, "octo")

    def test_expired_token_rejected(self):
        tok = mint(self.priv, "k1", "user-1", exp_delta=-10)
        with self.assertRaises(AuthError):
            self.verifier.verify(tok)

    def test_wrong_issuer_rejected(self):
        tok = mint(self.priv, "k1", "user-1", issuer="https://evil.test")
        with self.assertRaises(AuthError):
            self.verifier.verify(tok)

    def test_wrong_audience_rejected(self):
        tok = mint(self.priv, "k1", "user-1", audience="some-other-api")
        with self.assertRaises(AuthError):
            self.verifier.verify(tok)

    def test_unknown_kid_rejected(self):
        # Sign with a key whose kid is not in the served JWKS.
        other_priv, _ = make_keypair("k2")
        tok = mint(other_priv, "k2", "user-1")
        with self.assertRaises(AuthError):
            self.verifier.verify(tok)

    def test_tampered_signature_rejected(self):
        tok = mint(self.priv, "k1", "user-1")
        tampered = tok[:-4] + ("aaaa" if not tok.endswith("aaaa") else "bbbb")
        with self.assertRaises(AuthError):
            self.verifier.verify(tampered)

    def test_unsigned_alg_none_rejected(self):
        import jwt as _jwt

        tok = _jwt.encode(
            {"sub": "user-1", "iss": ISSUER, "aud": AUDIENCE, "exp": 9999999999},
            key="",
            algorithm="none",
        )
        with self.assertRaises(AuthError):
            self.verifier.verify(tok)

    def test_missing_subject_rejected(self):
        import time as _t

        import jwt as _jwt

        now = int(_t.time())
        tok = _jwt.encode(
            {"iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": now + 900},
            self.priv,
            algorithm="ES256",
            headers={"kid": "k1"},
        )
        with self.assertRaises(AuthError):
            self.verifier.verify(tok)


class JwksRefreshTests(unittest.TestCase):
    def test_refreshes_once_on_unknown_kid(self):
        priv1, jwks1 = make_keypair("k1")
        priv2, jwks2 = make_keypair("k2")
        state = {"jwks": jwks1, "fetches": 0}

        def fetcher():
            state["fetches"] += 1
            return state["jwks"]

        cache = JwksCache(fetcher=fetcher, min_refresh_interval=0)
        verifier = JwtVerifier(cache, issuer=ISSUER, audience=AUDIENCE)

        # First token with k1 works and populates the cache.
        verifier.verify(mint(priv1, "k1", "u1"))
        fetches_after_first = state["fetches"]

        # Key rotates: now the server serves k2; a k2 token triggers a refresh.
        state["jwks"] = jwks2
        user = verifier.verify(mint(priv2, "k2", "u2"))
        self.assertEqual(user.subject, "u2")
        self.assertGreater(state["fetches"], fetches_after_first)


if __name__ == "__main__":
    unittest.main()
