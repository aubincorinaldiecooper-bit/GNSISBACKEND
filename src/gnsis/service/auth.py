"""Better Auth JWT verification — how the backend authenticates end users.

The Better Auth service (a separate Node process) owns login and sessions and
mints short-lived JWTs signed with keys published at its JWKS endpoint. This
module verifies those JWTs: signature against the JWKS, plus issuer, audience,
and expiration. A valid token yields an :class:`AuthedUser`; nothing here trusts
identity fields from a request body.

The JWKS is cached by key id and refreshed once when a token presents an unknown
``kid`` (i.e. after a key rotation), so verification keeps working across
rotations without hammering the JWKS endpoint on every request.

Design notes:
* The JWKS *fetcher* is injectable so this is unit-testable with no network —
  tests supply a JWKS built from a throwaway key and mint tokens against it.
* We never accept an unsigned token (``alg: none``): the allowed algorithm set
  is fixed and asymmetric-only.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

import jwt
from jwt import InvalidTokenError, PyJWK

# Better Auth's JWT plugin defaults to EdDSA and can be configured for ES256 /
# RS256. We accept those asymmetric algorithms and nothing else — never "none".
ALLOWED_ALGORITHMS: List[str] = ["EdDSA", "ES256", "RS256"]

JwksFetcher = Callable[[], dict]


@dataclass(frozen=True)
class AuthedUser:
    """The authenticated principal resolved from a verified Better Auth JWT."""

    subject: str
    email: Optional[str] = None
    name: Optional[str] = None
    avatar_url: Optional[str] = None
    github_login: Optional[str] = None


class AuthError(Exception):
    """Raised when a token cannot be verified. Carries an HTTP-ish status."""

    def __init__(self, message: str, status: int = 401) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class JwksCache:
    """Fetches and caches JWKS keys by ``kid``, refreshing on an unknown key."""

    def __init__(
        self,
        url: Optional[str] = None,
        fetcher: Optional[JwksFetcher] = None,
        min_refresh_interval: float = 10.0,
    ) -> None:
        if not (url or fetcher):
            raise ValueError("JwksCache needs a url or a fetcher")
        self._url = url
        self._fetcher = fetcher or self._http_fetch
        self._min_refresh_interval = min_refresh_interval
        self._keys: Dict[str, PyJWK] = {}
        self._last_refresh = 0.0
        self._lock = threading.Lock()

    def _http_fetch(self) -> dict:
        assert self._url is not None
        with urllib.request.urlopen(self._url, timeout=10) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))

    def _refresh(self) -> None:
        raw = self._fetcher()
        keys: Dict[str, PyJWK] = {}
        for entry in raw.get("keys", []):
            try:
                key = PyJWK.from_dict(entry)
            except Exception:  # noqa: BLE001 - skip malformed keys, keep the rest
                continue
            kid = entry.get("kid") or key.key_id
            if kid:
                keys[kid] = key
        self._keys = keys
        self._last_refresh = time.monotonic()

    def get_key(self, kid: str) -> PyJWK:
        """Return the signing key for ``kid``, refreshing once if unknown."""
        key = self._keys.get(kid)
        if key is not None:
            return key
        with self._lock:
            key = self._keys.get(kid)
            if key is not None:
                return key
            # Rate-limit refreshes so a flood of bad-kid tokens can't hammer JWKS.
            if time.monotonic() - self._last_refresh >= self._min_refresh_interval:
                self._refresh()
            key = self._keys.get(kid)
        if key is None:
            raise AuthError("unknown signing key", status=401)
        return key


class JwtVerifier:
    """Verifies Better Auth JWTs against a :class:`JwksCache`."""

    def __init__(
        self,
        jwks: JwksCache,
        issuer: str,
        audience: str,
    ) -> None:
        self._jwks = jwks
        self._issuer = issuer
        self._audience = audience

    def verify(self, token: str) -> AuthedUser:
        try:
            header = jwt.get_unverified_header(token)
        except InvalidTokenError as exc:
            raise AuthError(f"malformed token: {exc}", status=401) from exc

        alg = header.get("alg")
        if alg not in ALLOWED_ALGORITHMS:
            raise AuthError("unacceptable token algorithm", status=401)
        kid = header.get("kid")
        if not kid:
            raise AuthError("token missing key id", status=401)

        signing_key = self._jwks.get_key(kid)
        try:
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=ALLOWED_ALGORITHMS,
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "sub"]},
            )
        except InvalidTokenError as exc:
            raise AuthError(f"invalid token: {exc}", status=401) from exc

        subject = claims.get("sub")
        if not subject:
            raise AuthError("token missing subject", status=401)

        return AuthedUser(
            subject=str(subject),
            email=claims.get("email"),
            name=claims.get("name"),
            avatar_url=claims.get("image") or claims.get("avatar_url"),
            github_login=claims.get("github_login") or claims.get("githubLogin"),
        )


def bearer_token(authorization: Optional[str]) -> str:
    """Extract the token from an ``Authorization: Bearer <token>`` header."""
    if not authorization:
        raise AuthError("missing Authorization header", status=401)
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise AuthError("malformed Authorization header", status=401)
    return parts[1].strip()
