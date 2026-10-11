from __future__ import annotations

import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from gnsis_runtime.live_grants import LiveGrantError, verify_live_grant
from gnsis_runtime.online_duplex import _can_resume


def _token(private_key, **overrides):
    now = int(time.time())
    claims = {
        "iss": "gnsis-control-plane",
        "aud": "gnsis-desktop-live",
        "sub": "person_abc123",
        "device": "device_abc123",
        "iat": now,
        "exp": now + 300,
        "scp": ["live:connect", "memory:owner"],
    }
    claims.update(overrides)
    return jwt.encode(claims, private_key, algorithm="EdDSA")


def test_verified_grant_derives_person_namespace():
    private_key = Ed25519PrivateKey.generate()
    grant = verify_live_grant(
        _token(private_key),
        public_key=private_key.public_key(),
        issuer="gnsis-control-plane",
    )
    assert grant.memory_namespace == "person:person_abc123"
    assert grant.device_id == "device_abc123"


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "wrong"},
        {"iss": "wrong"},
        {"exp": 1},
        {"scp": ["live:connect"]},
        {"sub": "client-selected-owner"},
    ],
)
def test_rejects_invalid_ownership_grants(overrides):
    private_key = Ed25519PrivateKey.generate()
    with pytest.raises(LiveGrantError):
        verify_live_grant(
            _token(private_key, **overrides),
            public_key=private_key.public_key(),
            issuer="gnsis-control-plane",
        )


def test_rejects_tampered_grant():
    private_key = Ed25519PrivateKey.generate()
    token = _token(private_key)
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    with pytest.raises(LiveGrantError):
        verify_live_grant(
            tampered,
            public_key=private_key.public_key(),
            issuer="gnsis-control-plane",
        )


def test_namespace_is_person_owned_not_device_or_session_owned():
    private_key = Ed25519PrivateKey.generate()
    first = verify_live_grant(
        _token(private_key, device="device_first"),
        public_key=private_key.public_key(),
        issuer="gnsis-control-plane",
    )
    second_device = verify_live_grant(
        _token(private_key, device="device_second"),
        public_key=private_key.public_key(),
        issuer="gnsis-control-plane",
    )
    other_person = verify_live_grant(
        _token(
            private_key,
            sub="person_other",
            device="device_other",
        ),
        public_key=private_key.public_key(),
        issuer="gnsis-control-plane",
    )
    assert first.memory_namespace == second_device.memory_namespace
    assert first.memory_namespace != other_person.memory_namespace


def test_resume_token_cannot_cross_memory_owners():
    parked = SimpleNamespace(
        attached=False,
        stopped=False,
        resume_token="resume-secret",
        owner_namespace="person:person_one",
    )
    assert _can_resume(
        parked,
        resume_token="resume-secret",
        owner_namespace="person:person_one",
    )
    assert not _can_resume(
        parked,
        resume_token="resume-secret",
        owner_namespace="person:person_two",
    )
