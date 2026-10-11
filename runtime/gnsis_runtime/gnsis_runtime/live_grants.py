"""Verification for short-lived desktop grants minted by GNSISBACKEND."""

from __future__ import annotations

from dataclasses import dataclass

import jwt


class LiveGrantError(ValueError):
    pass


@dataclass(frozen=True)
class LiveGrant:
    person_id: str
    device_id: str

    @property
    def memory_namespace(self) -> str:
        return f"person:{self.person_id}"


def verify_live_grant(token: str, *, public_key: str, issuer: str) -> LiveGrant:
    if not token:
        raise LiveGrantError("desktop grant is required")
    try:
        claims = jwt.decode(
            token,
            public_key,
            algorithms=["EdDSA"],
            issuer=issuer,
            audience="gnsis-desktop-live",
            options={"require": ["iss", "aud", "sub", "device", "iat", "exp", "scp"]},
        )
    except jwt.PyJWTError as exc:
        raise LiveGrantError("desktop grant is invalid or expired") from exc
    scopes = claims.get("scp")
    if not isinstance(scopes, list) or not {
        "live:connect",
        "memory:owner",
    }.issubset(scopes):
        raise LiveGrantError("desktop grant is missing required scope")
    person_id = claims.get("sub")
    device_id = claims.get("device")
    if (
        not isinstance(person_id, str)
        or not person_id.startswith("person_")
        or not isinstance(device_id, str)
        or not device_id.startswith("device_")
    ):
        raise LiveGrantError("desktop grant ownership claims are invalid")
    return LiveGrant(person_id=person_id, device_id=device_id)
