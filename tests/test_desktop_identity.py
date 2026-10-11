"""Desktop device proof, migration, recovery-key and grant tests."""

from __future__ import annotations

import base64
import os
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import jwt  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)
from cryptography.hazmat.primitives.asymmetric.utils import (  # noqa: E402
    decode_dss_signature,
)

from _authkit import fresh_sqlite_env  # noqa: E402


class DesktopIdentityTests(unittest.TestCase):
    def setUp(self):
        fresh_sqlite_env()
        from gnsis.service.db import init_db

        init_db()
        self.device_private = ec.generate_private_key(ec.SECP256R1())
        public_raw = self.device_private.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
        self.public_key = base64.b64encode(public_raw).decode()
        from gnsis.service.desktop_identity import derive_public_id

        self.public_id = derive_public_id(public_raw)

    def _sign(self, challenge_b64: str) -> str:
        signature_der = self.device_private.sign(
            base64.b64decode(challenge_b64), ec.ECDSA(hashes.SHA256())
        )
        r, s = decode_dss_signature(signature_der)
        signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        return base64.b64encode(signature).decode()

    def test_first_proof_creates_person_and_returns_recovery_key(self):
        from gnsis.service.desktop_identity import (
            complete_challenge,
            create_challenge,
        )

        challenge = create_challenge(self.public_id, self.public_key)
        enrollment = complete_challenge(
            challenge["challenge_id"], self._sign(challenge["challenge"])
        )
        self.assertEqual(enrollment.public_id, self.public_id)
        self.assertTrue(enrollment.person_id.startswith("person_"))
        self.assertTrue(enrollment.device_id.startswith("device_"))
        self.assertIsNotNone(enrollment.recovery_key)

        next_challenge = create_challenge(self.public_id, self.public_key)
        authenticated = complete_challenge(
            next_challenge["challenge_id"],
            self._sign(next_challenge["challenge"]),
        )
        self.assertEqual(authenticated.person_id, enrollment.person_id)
        self.assertIsNone(authenticated.recovery_key)

    def test_challenge_is_one_time_and_rejects_invalid_signature(self):
        from gnsis.service.desktop_identity import (
            DesktopIdentityError,
            complete_challenge,
            create_challenge,
        )

        challenge = create_challenge(self.public_id, self.public_key)
        with self.assertRaises(DesktopIdentityError):
            complete_challenge(
                challenge["challenge_id"], base64.b64encode(bytes(64)).decode()
            )
        valid_signature = self._sign(challenge["challenge"])
        complete_challenge(challenge["challenge_id"], valid_signature)
        with self.assertRaises(DesktopIdentityError) as reused:
            complete_challenge(challenge["challenge_id"], valid_signature)
        self.assertEqual(reused.exception.status, 409)

    def test_challenge_issuance_is_bounded(self):
        from gnsis.service.desktop_identity import (
            DesktopIdentityError,
            create_challenge,
            derive_public_id,
        )

        create_challenge(self.public_id, self.public_key)
        other = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
        with patch("gnsis.service.desktop_identity._MAX_OPEN_CHALLENGES", 1):
            with self.assertRaises(DesktopIdentityError) as limited:
                create_challenge(
                    derive_public_id(other),
                    base64.b64encode(other).decode(),
                )
        self.assertEqual(limited.exception.status, 429)

    def test_grant_contains_person_and_device_ownership(self):
        from gnsis.service.desktop_identity import (
            complete_challenge,
            create_challenge,
            issue_grant,
        )

        challenge = create_challenge(self.public_id, self.public_key)
        enrollment = complete_challenge(
            challenge["challenge_id"], self._sign(challenge["challenge"])
        )
        private_key = Ed25519PrivateKey.generate()
        private_pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        settings = types.SimpleNamespace(
            desktop_grant_private_key=private_pem,
            desktop_grant_issuer="gnsis-control-plane",
            desktop_grant_ttl_s=300,
        )
        encoded = issue_grant(settings, enrollment)["grant"]
        claims = jwt.decode(
            encoded,
            private_key.public_key(),
            algorithms=["EdDSA"],
            issuer="gnsis-control-plane",
            audience="gnsis-desktop-live",
        )
        self.assertEqual(claims["sub"], enrollment.person_id)
        self.assertEqual(claims["device"], enrollment.device_id)
        self.assertEqual(
            claims["scp"],
            ["live:connect", "memory:owner", "account:preferences"],
        )

    def test_recovery_key_registers_second_device_under_same_person(self):
        from gnsis.service.desktop_identity import (
            complete_challenge,
            create_challenge,
            derive_public_id,
        )

        first_challenge = create_challenge(self.public_id, self.public_key)
        first = complete_challenge(
            first_challenge["challenge_id"],
            self._sign(first_challenge["challenge"]),
        )
        second_private = ec.generate_private_key(ec.SECP256R1())
        second_raw = second_private.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
        second_public = base64.b64encode(second_raw).decode()
        second_challenge = create_challenge(derive_public_id(second_raw), second_public)
        signature_der = second_private.sign(
            base64.b64decode(second_challenge["challenge"]),
            ec.ECDSA(hashes.SHA256()),
        )
        r, s = decode_dss_signature(signature_der)
        signature = base64.b64encode(
            r.to_bytes(32, "big") + s.to_bytes(32, "big")
        ).decode()
        second = complete_challenge(
            second_challenge["challenge_id"],
            signature,
            recovery_key=first.recovery_key,
        )
        self.assertEqual(second.person_id, first.person_id)
        self.assertNotEqual(second.device_id, first.device_id)
        self.assertIsNone(second.recovery_key)

    def test_existing_device_grant_approves_protected_credential(self):
        from gnsis.service.desktop_identity import (
            complete_challenge,
            create_challenge,
            derive_public_id,
            issue_grant,
            verify_approval_grant,
        )

        first_challenge = create_challenge(self.public_id, self.public_key)
        first = complete_challenge(
            first_challenge["challenge_id"],
            self._sign(first_challenge["challenge"]),
        )
        signing_key = Ed25519PrivateKey.generate()
        settings = types.SimpleNamespace(
            desktop_grant_private_key=signing_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ).decode(),
            desktop_grant_issuer="gnsis-control-plane",
            desktop_grant_ttl_s=300,
        )
        approval = issue_grant(settings, first)["grant"]
        approved_person_id = verify_approval_grant(settings, approval)

        protected_private = ec.generate_private_key(ec.SECP256R1())
        protected_raw = protected_private.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
        protected_challenge = create_challenge(
            derive_public_id(protected_raw),
            base64.b64encode(protected_raw).decode(),
        )
        signature_der = protected_private.sign(
            base64.b64decode(protected_challenge["challenge"]),
            ec.ECDSA(hashes.SHA256()),
        )
        r, s = decode_dss_signature(signature_der)
        protected = complete_challenge(
            protected_challenge["challenge_id"],
            base64.b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big")).decode(),
            approved_person_id=approved_person_id,
        )

        self.assertEqual(protected.person_id, first.person_id)
        self.assertNotEqual(protected.device_id, first.device_id)
        self.assertIsNone(protected.recovery_key)


if __name__ == "__main__":
    unittest.main()
