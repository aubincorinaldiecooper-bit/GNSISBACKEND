"""Random, hashed, single-purpose secrets for the execution path.

Two kinds of secret cross the trust boundary to the executor VM, and neither is
ever stored or logged in plaintext:

* the **dispatch nonce** — generated at dispatch, carried in the workflow input,
  proven back (with the OIDC identity) at exchange, then consumed once;
* the **executor/run token** — minted at OIDC exchange, returned to the VM once,
  and only ever compared by hash thereafter.

Only the SHA-256 hash is persisted; the plaintext lives just long enough to hand
to GitHub (nonce) or return in the exchange response (token).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

_NONCE_BYTES = 32
_TOKEN_BYTES = 32
_TOKEN_PREFIX = "gnsis_rt_"  # run token


def new_nonce() -> str:
    """A fresh, unguessable dispatch nonce (plaintext)."""
    return secrets.token_urlsafe(_NONCE_BYTES)


def new_run_token() -> str:
    """A fresh, unguessable executor/run token (plaintext)."""
    return _TOKEN_PREFIX + secrets.token_urlsafe(_TOKEN_BYTES)


def hash_secret(value: str) -> str:
    """Stable SHA-256 hex of a secret — the only form persisted or compared."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def secrets_equal(a: str, b: str) -> bool:
    """Constant-time comparison for hex hashes."""
    return hmac.compare_digest(a, b)
