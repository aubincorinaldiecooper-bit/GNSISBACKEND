"""Genesis-native virtual keys — scoped inference credentials issued by Genesis.

Format ``gns_live_<random>`` / ``gns_test_<random>``. Genesis generates the
secret with a CSPRNG, returns it **exactly once**, and stores only a SHA-256
(optionally peppered) hash plus a non-secret prefix for display/logging. The full
secret is never stored and cannot be retrieved after creation. Validation hashes
the presented key and looks it up in constant-ish time; disabled / rotated /
expired keys never authenticate. Destructive delete is avoided — keys are
disabled or rotated so historical usage stays attributable.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import List, Optional

from sqlalchemy.exc import IntegrityError

from . import orm
from .db import session_scope
from ..orchestration.models import new_id

_MODES = ("live", "test")
_LIMIT_FIELDS = ("soft_limit", "hard_limit", "per_run_limit", "daily_limit", "monthly_limit")


class VirtualKeyError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class VirtualKeyView:
    id: str
    key_prefix: str
    mode: str
    name: str
    status: str
    workspace_id: str
    project_id: Optional[str]
    environment_id: Optional[str]
    user_id: Optional[str]
    team_id: Optional[str]
    allowed_providers: List[str]
    allowed_models: List[str]
    soft_limit: Optional[str]
    hard_limit: Optional[str]
    per_run_limit: Optional[str]
    daily_limit: Optional[str]
    monthly_limit: Optional[str]
    expires_at: Optional[str]
    rotated_to: Optional[str]
    metadata: Optional[dict]
    last_used_at: Optional[str]
    created_at: str
    disabled_at: Optional[str]

    @property
    def active(self) -> bool:
        return self.status == "active"


def _csv(value: Optional[str]) -> List[str]:
    return [v for v in (value or "").split(",") if v]


def _view(row: orm.VirtualKey) -> VirtualKeyView:
    return VirtualKeyView(
        id=row.id, key_prefix=row.key_prefix, mode=row.mode, name=row.name, status=row.status,
        workspace_id=row.workspace_id, project_id=row.project_id,
        environment_id=row.environment_id, user_id=row.user_id, team_id=row.team_id,
        allowed_providers=_csv(row.allowed_providers), allowed_models=_csv(row.allowed_models),
        soft_limit=row.soft_limit, hard_limit=row.hard_limit, per_run_limit=row.per_run_limit,
        daily_limit=row.daily_limit, monthly_limit=row.monthly_limit,
        expires_at=row.expires_at.isoformat() if row.expires_at else None,
        rotated_to=row.rotated_to, metadata=row.key_metadata,
        last_used_at=row.last_used_at.isoformat() if row.last_used_at else None,
        created_at=row.created_at.isoformat() if row.created_at else "",
        disabled_at=row.disabled_at.isoformat() if row.disabled_at else None,
    )


def hash_key(settings, secret: str) -> str:
    """Deterministic SHA-256 of the secret, mixed with the configured pepper."""
    pepper = getattr(settings, "virtual_key_pepper", "") or ""
    return hashlib.sha256((pepper + secret).encode("utf-8")).hexdigest()


def _generate_secret(mode: str) -> str:
    return f"gns_{mode}_{secrets.token_urlsafe(32)}"


def _display_prefix(secret: str) -> str:
    # Non-secret: the "gns_<mode>_" scheme + a few chars, enough to identify a key
    # in a list/log without revealing anything usable.
    head = secret.split("_")
    tag = "_".join(head[:2])  # gns_live / gns_test
    body = head[2] if len(head) > 2 else ""
    return f"{tag}_{body[:6]}…"


def _norm_limit(value, field: str) -> Optional[str]:
    if value in (None, ""):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise VirtualKeyError(f"{field} is not a valid amount") from exc
    if d.is_nan() or d.is_infinite() or d < 0:
        raise VirtualKeyError(f"{field} must be a non-negative amount")
    return format(d.normalize(), "f")


def _norm_csv(value) -> Optional[str]:
    if not value:
        return None
    if isinstance(value, str):
        items = [v.strip() for v in value.split(",")]
    else:
        items = [str(v).strip() for v in value]
    items = [v for v in items if v]
    return ",".join(items) or None


def _parse_expiry(value) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise VirtualKeyError("expires_at must be an ISO-8601 timestamp") from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class VirtualKeyStore:
    """Issue / list / authenticate / disable / rotate Genesis-native keys."""

    def create(
        self,
        settings,
        *,
        workspace_id: str,
        name: str = "",
        mode: str = "live",
        project_id: Optional[str] = None,
        environment_id: Optional[str] = None,
        user_id: Optional[str] = None,
        team_id: Optional[str] = None,
        allowed_providers=None,
        allowed_models=None,
        soft_limit=None,
        hard_limit=None,
        per_run_limit=None,
        daily_limit=None,
        monthly_limit=None,
        expires_at=None,
        metadata: Optional[dict] = None,
    ) -> tuple[VirtualKeyView, str]:
        """Mint a key. Returns ``(view, secret)`` — the secret is shown ONCE."""
        if mode not in _MODES:
            raise VirtualKeyError("mode must be 'live' or 'test'")
        if not workspace_id:
            raise VirtualKeyError("workspace is required")
        limits = {
            f: _norm_limit(v, f) for f, v in (
                ("soft_limit", soft_limit), ("hard_limit", hard_limit),
                ("per_run_limit", per_run_limit), ("daily_limit", daily_limit),
                ("monthly_limit", monthly_limit),
            )
        }
        if limits["soft_limit"] and limits["hard_limit"] and Decimal(limits["soft_limit"]) > Decimal(limits["hard_limit"]):
            raise VirtualKeyError("soft_limit cannot exceed hard_limit")
        expiry = _parse_expiry(expires_at)

        secret = _generate_secret(mode)
        key_hash = hash_key(settings, secret)
        with session_scope() as s:
            row = orm.VirtualKey(
                id=new_id("vk"),
                key_hash=key_hash,
                key_prefix=_display_prefix(secret),
                mode=mode,
                name=(name or "").strip(),
                status="active",
                workspace_id=workspace_id,
                project_id=project_id, environment_id=environment_id,
                user_id=user_id, team_id=team_id,
                allowed_providers=_norm_csv(allowed_providers),
                allowed_models=_norm_csv(allowed_models),
                expires_at=expiry,
                key_metadata=metadata or None,
                **limits,
            )
            s.add(row)
            try:
                s.flush()
            except IntegrityError as exc:  # astronomically unlikely hash collision
                s.rollback()
                raise VirtualKeyError("could not issue key; retry", status=500) from exc
            view = _view(row)
        return view, secret

    def authenticate(self, settings, presented_secret: str) -> Optional[VirtualKeyView]:
        """Validate a presented key. Returns the key view or ``None``.

        None means "reject" for every failure mode (unknown, disabled, rotated,
        expired) — the caller must not distinguish them to an unauthenticated peer.
        """
        if not presented_secret or not presented_secret.startswith("gns_"):
            return None
        key_hash = hash_key(settings, presented_secret)
        now = datetime.now(timezone.utc)
        with session_scope() as s:
            row = (
                s.query(orm.VirtualKey)
                .filter(orm.VirtualKey.key_hash == key_hash)
                .one_or_none()
            )
            if row is None or row.status != "active":
                return None
            if row.expires_at is not None:
                exp = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
                if exp <= now:
                    return None
            row.last_used_at = now
            s.flush()
            return _view(row)

    def list_for_workspace(self, workspace_id: str, *, limit: int = 100) -> List[VirtualKeyView]:
        with session_scope() as s:
            rows = (
                s.query(orm.VirtualKey)
                .filter(orm.VirtualKey.workspace_id == workspace_id)
                .order_by(orm.VirtualKey.created_at.desc())
                .limit(limit)
                .all()
            )
            return [_view(r) for r in rows]

    def get(self, workspace_id: str, key_id: str) -> Optional[VirtualKeyView]:
        with session_scope() as s:
            row = s.get(orm.VirtualKey, key_id)
            if row is None or row.workspace_id != workspace_id:
                return None
            return _view(row)

    def disable(self, workspace_id: str, key_id: str) -> VirtualKeyView:
        with session_scope() as s:
            row = s.get(orm.VirtualKey, key_id)
            if row is None or row.workspace_id != workspace_id:
                raise VirtualKeyError("key not found", status=404)
            if row.status == "active":
                row.status = "disabled"
                row.disabled_at = datetime.now(timezone.utc)
                s.flush()
            return _view(row)

    def rotate(self, settings, workspace_id: str, key_id: str) -> tuple[VirtualKeyView, str]:
        """Issue a successor with the same scopes and retire the old key.

        Returns ``(new_view, new_secret)``. The old key is marked ``rotated`` and
        points at its successor, so it stops authenticating but stays on record.
        """
        old = self.get(workspace_id, key_id)
        if old is None:
            raise VirtualKeyError("key not found", status=404)
        new_view, secret = self.create(
            settings,
            workspace_id=workspace_id, name=old.name, mode=old.mode,
            project_id=old.project_id, environment_id=old.environment_id,
            user_id=old.user_id, team_id=old.team_id,
            allowed_providers=old.allowed_providers, allowed_models=old.allowed_models,
            soft_limit=old.soft_limit, hard_limit=old.hard_limit,
            per_run_limit=old.per_run_limit, daily_limit=old.daily_limit,
            monthly_limit=old.monthly_limit, expires_at=old.expires_at,
            metadata=old.metadata,
        )
        with session_scope() as s:
            row = s.get(orm.VirtualKey, key_id)
            if row is not None and row.status != "rotated":
                row.status = "rotated"
                row.rotated_to = new_view.id
                row.disabled_at = datetime.now(timezone.utc)
                s.flush()
        return new_view, secret
