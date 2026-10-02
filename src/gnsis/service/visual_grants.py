"""Short-lived visual runtime grants minted from scoped Genesis virtual keys."""

from __future__ import annotations

from datetime import datetime, timezone

import jwt
from jwt.exceptions import PyJWTError

from ..orchestration.models import new_id
from .public_api import ErrorCode, PublicApiError
from .virtual_keys import VirtualKeyView


def issue_grant(settings, key: VirtualKeyView) -> dict:
    """Mint an offline-verifiable grant for one virtual key and workspace."""
    private_key = settings.visual_grant_private_key
    issuer = settings.visual_grant_issuer
    if not private_key or not issuer.strip():
        raise PublicApiError(
            ErrorCode.VISUAL_GRANTS_UNAVAILABLE,
            "visual grants are not configured",
            status=503,
        )
    quota = settings.visual_daily_decision_quota
    if quota is not None:
        from .visual_usage import VisualUsageStore

        if VisualUsageStore().decisions_today(key.id) >= quota:
            raise PublicApiError(
                ErrorCode.QUOTA_EXCEEDED,
                "daily visual decision quota exceeded",
                status=429,
            )
    now = datetime.now(timezone.utc)
    iat = int(now.timestamp())
    exp = iat + settings.visual_grant_ttl_s
    limits = {
        "max_concurrent_sessions": settings.visual_max_concurrent_sessions,
        "max_decisions_per_session": settings.visual_max_decisions_per_session,
        "max_frames_per_session": settings.visual_max_frames_per_session,
    }
    claims = {
        "iss": issuer,
        "aud": "gnsis-visual",
        "sub": key.id,
        "jti": new_id("vgr"),
        "iat": iat,
        "exp": exp,
        "ws": key.workspace_id,
        "prj": key.project_id,
        "env": key.environment_id,
        "scp": ["visual:host"],
        "lim": limits,
    }
    try:
        grant = jwt.encode(claims, private_key, algorithm="EdDSA")
    except (PyJWTError, TypeError, ValueError) as exc:
        raise PublicApiError(
            ErrorCode.VISUAL_GRANTS_UNAVAILABLE,
            "visual grant signing is not configured correctly",
            status=503,
        ) from exc
    expires_at = datetime.fromtimestamp(exp, tz=timezone.utc)
    return {
        "grant": grant,
        "expires_at": expires_at.isoformat(),
        "limits": limits,
    }
