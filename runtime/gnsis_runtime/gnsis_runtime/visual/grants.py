from __future__ import annotations

from dataclasses import dataclass

import jwt
from jwt import InvalidTokenError


@dataclass(frozen=True, slots=True)
class VisualGrant:
    grant_id: str
    workspace_id: str
    key_id: str
    project_id: str | None
    environment_id: str | None
    expires_at: int
    max_concurrent_sessions: int
    max_decisions_per_session: int
    max_frames_per_session: int


class GrantVerifier:
    def __init__(
        self,
        public_key_pem: str,
        *,
        issuer: str,
        audience: str = "gnsis-visual",
        leeway_s: int = 30,
    ) -> None:
        self.public_key_pem = public_key_pem
        self.issuer = issuer
        self.audience = audience
        self.leeway_s = leeway_s

    def verify(self, token: str, *, allow_expired: bool = False) -> VisualGrant | None:
        try:
            claims = jwt.decode(
                token,
                self.public_key_pem,
                algorithms=["EdDSA"],
                audience=self.audience,
                issuer=self.issuer,
                leeway=self.leeway_s,
                options={
                    "require": ["exp", "iat", "jti", "sub", "aud", "iss"],
                    "verify_exp": not allow_expired,
                },
            )
            if not isinstance(claims, dict):
                return None
            grant_id = self._string_claim(claims, "jti")
            key_id = self._string_claim(claims, "sub")
            workspace_id = self._string_claim(claims, "ws")
            project_id = self._optional_string_claim(claims, "prj")
            environment_id = self._optional_string_claim(claims, "env")
            expires_at = self._positive_int_claim(claims, "exp")
            scopes = claims.get("scp")
            limits = claims.get("lim")
            if (
                not isinstance(scopes, list)
                or "visual:host" not in scopes
                or not isinstance(limits, dict)
            ):
                return None
            max_concurrent_sessions = self._positive_int(
                limits, "max_concurrent_sessions"
            )
            max_decisions_per_session = self._positive_int(
                limits, "max_decisions_per_session"
            )
            max_frames_per_session = self._positive_int(
                limits, "max_frames_per_session"
            )
            if any(not isinstance(scope, str) for scope in scopes):
                return None
            return VisualGrant(
                grant_id=grant_id,
                workspace_id=workspace_id,
                key_id=key_id,
                project_id=project_id,
                environment_id=environment_id,
                expires_at=expires_at,
                max_concurrent_sessions=max_concurrent_sessions,
                max_decisions_per_session=max_decisions_per_session,
                max_frames_per_session=max_frames_per_session,
            )
        except (InvalidTokenError, TypeError, ValueError, KeyError):
            return None

    @staticmethod
    def _string_claim(claims: dict, name: str) -> str:
        value = claims.get(name)
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} must be a non-empty string")
        return value

    @staticmethod
    def _optional_string_claim(claims: dict, name: str) -> str | None:
        value = claims.get(name)
        if value is not None and (not isinstance(value, str) or not value):
            raise ValueError(f"{name} must be a string or null")
        return value

    @staticmethod
    def _positive_int_claim(claims: dict, name: str) -> int:
        return GrantVerifier._positive_int(claims, name)

    @staticmethod
    def _positive_int(values: dict, name: str) -> int:
        value = values.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
        return value
