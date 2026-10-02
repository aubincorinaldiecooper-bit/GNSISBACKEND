from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from gnsis_runtime.visual.grants import GrantVerifier


ISSUER = "test-control-plane"


def _keypair() -> tuple[str, str]:
    private = Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


def _claims(**overrides):
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": "gnsis-visual",
        "sub": "key-1",
        "jti": "grant-1",
        "iat": now,
        "exp": now + 300,
        "ws": "workspace-1",
        "prj": "project-1",
        "env": "environment-1",
        "scp": ["visual:host"],
        "lim": {
            "max_concurrent_sessions": 4,
            "max_decisions_per_session": 2000,
            "max_frames_per_session": 100000,
        },
    }
    claims.update(overrides)
    return claims


def _jwt_bytes(header: dict, payload: dict, secret: bytes = b"") -> str:
    def encode(value: dict) -> bytes:
        return base64.urlsafe_b64encode(
            json.dumps(value, separators=(",", ":")).encode()
        ).rstrip(b"=")

    encoded_header = encode(header)
    encoded_payload = encode(payload)
    signing_input = encoded_header + b"." + encoded_payload
    signature = hmac.new(secret, signing_input, hashlib.sha256).digest()
    return b".".join(
        (
            encoded_header,
            encoded_payload,
            base64.urlsafe_b64encode(signature).rstrip(b"="),
        )
    ).decode()


def test_grant_verifier_accepts_valid_ed25519_grants() -> None:
    private_pem, public_pem = _keypair()
    token = jwt.encode(_claims(), private_pem, algorithm="EdDSA")

    grant = GrantVerifier(public_pem, issuer=ISSUER).verify(token)

    assert grant is not None
    assert grant.grant_id == "grant-1"
    assert grant.workspace_id == "workspace-1"
    assert grant.key_id == "key-1"
    assert grant.max_frames_per_session == 100000


def test_grant_verifier_rejects_expiry_issuer_audience_scope_and_limits() -> None:
    private_pem, public_pem = _keypair()
    verifier = GrantVerifier(public_pem, issuer=ISSUER)
    cases = (
        {"exp": int(time.time()) - 60},
        {"iss": "wrong"},
        {"aud": "wrong"},
        {"scp": []},
        {"lim": {"max_concurrent_sessions": 0}},
    )

    for override in cases:
        token = jwt.encode(_claims(**override), private_pem, algorithm="EdDSA")
        assert verifier.verify(token) is None


def test_grant_verifier_rejects_none_and_hs256_public_key_confusion() -> None:
    _, public_pem = _keypair()
    verifier = GrantVerifier(public_pem, issuer=ISSUER)
    none_token = _jwt_bytes({"alg": "none", "typ": "JWT"}, _claims())
    confused_token = _jwt_bytes(
        {"alg": "HS256", "typ": "JWT"},
        _claims(),
        public_pem.encode(),
    )

    assert verifier.verify(none_token) is None
    assert verifier.verify(confused_token) is None
