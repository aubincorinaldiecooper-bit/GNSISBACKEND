"""Shared helpers for auth/tenancy tests: throwaway keys, JWTs, and a wired app.

Lets the backend's JWT + workspace machinery be exercised with no live auth
service and no Postgres — an ES256 keypair stands in for Better Auth's signing
key, a JWKS dict is served via the injectable fetcher, and SQLite stands in for
Postgres (the ORM/JSON paths are identical).
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import jwt  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from jwt.algorithms import ECAlgorithm  # noqa: E402

ISSUER = "https://auth.example.test"
AUDIENCE = "gnsis-api"


def make_keypair(kid: str = "test-key-1"):
    priv = ec.generate_private_key(ec.SECP256R1())
    jwk = json.loads(ECAlgorithm.to_jwk(priv.public_key()))
    jwk.update({"kid": kid, "alg": "ES256", "use": "sig"})
    return priv, {"keys": [jwk]}


def mint(
    priv,
    kid: str,
    sub: str,
    *,
    issuer: str = ISSUER,
    audience: str = AUDIENCE,
    email=None,
    name=None,
    image=None,
    github_login=None,
    exp_delta: int = 900,
    alg: str = "ES256",
) -> str:
    now = int(time.time())
    claims = {"sub": sub, "iss": issuer, "aud": audience, "iat": now, "exp": now + exp_delta}
    if email:
        claims["email"] = email
    if name:
        claims["name"] = name
    if image:
        claims["image"] = image
    if github_login:
        claims["github_login"] = github_login
    return jwt.encode(claims, priv, algorithm=alg, headers={"kid": kid})


def fresh_sqlite_env() -> str:
    """Point the service DB at a unique temp SQLite file and reset engine state."""
    path = os.path.join("/tmp", f"gnsis-test-{uuid.uuid4().hex}.db")
    os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{path}"
    os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
    # Reset the settings + engine singletons so the new DATABASE_URL takes hold.
    from gnsis.service import db, settings as settings_mod

    settings_mod._settings = None
    db._engine = None
    db._SessionLocal = None
    return path
