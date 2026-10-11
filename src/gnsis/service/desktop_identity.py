"""Standalone desktop device authentication and person-level ownership."""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta

import jwt
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from sqlalchemy.exc import IntegrityError

from ..clock import utcnow
from . import orm
from .db import session_scope

_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


class DesktopIdentityError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class Enrollment:
    person_id: str
    device_id: str
    public_id: str
    recovery_key: str | None


def _decode_b64(value: str, *, name: str) -> bytes:
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise DesktopIdentityError(f"{name} is not valid base64") from exc
    if not raw:
        raise DesktopIdentityError(f"{name} is empty")
    return raw


def derive_public_id(public_key: bytes) -> str:
    digest = hashlib.sha256(public_key).digest()
    bits = 0
    value = 0
    out = ""
    for byte in digest:
        value = (value << 8) | byte
        bits += 8
        while bits >= 5 and len(out) < 12:
            out += _ALPHABET[(value >> (bits - 5)) & 31]
            bits -= 5
        if len(out) >= 12:
            break
    return f"gnsis:{out[:4]}-{out[4:8]}-{out[8:12]}"


def create_challenge(public_id: str, public_key_b64: str) -> dict:
    public_key = _decode_b64(public_key_b64, name="public_key")
    if len(public_key) != 65 or public_key[0] != 4:
        raise DesktopIdentityError("public_key must be an uncompressed P-256 key")
    if derive_public_id(public_key) != public_id:
        raise DesktopIdentityError("public_id does not match public_key")
    challenge = secrets.token_bytes(32)
    challenge_id = f"dch_{secrets.token_hex(16)}"
    expires_at = utcnow() + timedelta(minutes=5)
    with session_scope() as session:
        session.add(
            orm.DesktopAuthChallenge(
                id=challenge_id,
                public_id=public_id,
                public_key_b64=public_key_b64,
                challenge_b64=base64.b64encode(challenge).decode(),
                purpose="authenticate",
                expires_at=expires_at,
            )
        )
    return {
        "challenge_id": challenge_id,
        "challenge": base64.b64encode(challenge).decode(),
        "expires_at": expires_at.isoformat(),
    }


def _verify_signature(
    public_key_raw: bytes, challenge: bytes, signature: bytes
) -> None:
    if len(signature) != 64:
        raise DesktopIdentityError("signature must be a 64-byte P-256 signature")
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    try:
        public_key = ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), public_key_raw
        )
        public_key.verify(
            encode_dss_signature(r, s),
            challenge,
            ec.ECDSA(hashes.SHA256()),
        )
    except (InvalidSignature, ValueError) as exc:
        raise DesktopIdentityError("invalid device signature", status=401) from exc


def complete_challenge(
    challenge_id: str,
    signature_b64: str,
    *,
    recovery_key: str | None = None,
    approved_person_id: str | None = None,
) -> Enrollment:
    signature = _decode_b64(signature_b64, name="signature")
    now = utcnow()
    with session_scope() as session:
        challenge = session.get(orm.DesktopAuthChallenge, challenge_id)
        if challenge is None:
            raise DesktopIdentityError("challenge not found", status=404)
        if challenge.consumed_at is not None:
            raise DesktopIdentityError("challenge has already been used", status=409)
        expires_at = challenge.expires_at
        comparable_now = (
            now if expires_at.tzinfo is not None else now.replace(tzinfo=None)
        )
        if expires_at <= comparable_now:
            raise DesktopIdentityError("challenge has expired", status=401)
        public_key = _decode_b64(challenge.public_key_b64, name="public_key")
        challenge_bytes = _decode_b64(challenge.challenge_b64, name="challenge")
        _verify_signature(public_key, challenge_bytes, signature)
        challenge.consumed_at = now

        device = (
            session.query(orm.DesktopDevice)
            .filter(orm.DesktopDevice.public_id == challenge.public_id)
            .one_or_none()
        )
        if device is not None:
            if (
                not secrets.compare_digest(
                    device.public_key_b64, challenge.public_key_b64
                )
                or device.status != "active"
            ):
                raise DesktopIdentityError("device is not active", status=403)
            device.last_authenticated_at = now
            return Enrollment(
                person_id=device.person_id,
                device_id=device.id,
                public_id=device.public_id,
                recovery_key=None,
            )

        recovery_hash = (
            hashlib.sha256(recovery_key.encode()).hexdigest() if recovery_key else None
        )
        existing_person = (
            session.query(orm.DesktopPerson)
            .filter(orm.DesktopPerson.recovery_key_hash == recovery_hash)
            .one_or_none()
            if recovery_hash
            else None
        )
        if recovery_key and existing_person is None:
            raise DesktopIdentityError("recovery key is invalid", status=401)
        if approved_person_id:
            approved_person = session.get(orm.DesktopPerson, approved_person_id)
            if approved_person is None:
                raise DesktopIdentityError("approving person was not found", status=401)
            if existing_person is not None and existing_person.id != approved_person.id:
                raise DesktopIdentityError("device approvals do not match", status=401)
            existing_person = approved_person

        person_id = (
            existing_person.id
            if existing_person is not None
            else f"person_{hashlib.sha256(public_key).hexdigest()[:24]}"
        )
        device_id = f"device_{hashlib.sha256(public_key).hexdigest()[:24]}"
        new_recovery_key = None
        if existing_person is None:
            new_recovery_key = secrets.token_urlsafe(32)
            person = orm.DesktopPerson(
                id=person_id,
                legacy_public_id=challenge.public_id,
                recovery_key_hash=hashlib.sha256(new_recovery_key.encode()).hexdigest(),
            )
            session.add(person)
        device = orm.DesktopDevice(
            id=device_id,
            person_id=person_id,
            public_id=challenge.public_id,
            public_key_b64=challenge.public_key_b64,
            credential_kind="legacy_webcrypto",
            status="active",
            last_authenticated_at=now,
        )
        session.add(device)
        try:
            session.flush()
        except IntegrityError as exc:
            raise DesktopIdentityError(
                "device identity is already registered", status=409
            ) from exc
        return Enrollment(
            person_id=person_id,
            device_id=device_id,
            public_id=challenge.public_id,
            recovery_key=new_recovery_key,
        )


def issue_grant(settings, enrollment: Enrollment) -> dict:
    private_key = settings.desktop_grant_private_key
    issuer = settings.desktop_grant_issuer.strip()
    if not private_key or not issuer:
        raise DesktopIdentityError("desktop grants are not configured", status=503)
    now = int(utcnow().timestamp())
    expires_at = now + settings.desktop_grant_ttl_s
    token = jwt.encode(
        {
            "iss": issuer,
            "aud": "gnsis-desktop-live",
            "sub": enrollment.person_id,
            "device": enrollment.device_id,
            "jti": f"dgr_{secrets.token_hex(16)}",
            "iat": now,
            "exp": expires_at,
            "scp": ["live:connect", "memory:owner"],
        },
        private_key,
        algorithm="EdDSA",
    )
    return {"grant": token, "expires_at": expires_at}


def verify_approval_grant(settings, token: str) -> str:
    private_key = settings.desktop_grant_private_key
    issuer = settings.desktop_grant_issuer.strip()
    if not private_key or not issuer:
        raise DesktopIdentityError("desktop grants are not configured", status=503)
    try:
        claims = jwt.decode(
            token,
            private_key,
            algorithms=["EdDSA"],
            issuer=issuer,
            audience="gnsis-desktop-live",
            options={"require": ["sub", "device", "exp", "scp"]},
        )
    except jwt.PyJWTError as exc:
        raise DesktopIdentityError(
            "device approval is invalid or expired", status=401
        ) from exc
    scopes = claims.get("scp")
    person_id = claims.get("sub")
    device_id = claims.get("device")
    if (
        not isinstance(scopes, list)
        or "memory:owner" not in scopes
        or not isinstance(person_id, str)
        or not isinstance(device_id, str)
    ):
        raise DesktopIdentityError("device approval is invalid", status=401)
    with session_scope() as session:
        device = session.get(orm.DesktopDevice, device_id)
        if device is None or device.person_id != person_id or device.status != "active":
            raise DesktopIdentityError("approving device is not active", status=401)
    return person_id
