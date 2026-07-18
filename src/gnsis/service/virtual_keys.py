"""Issue, list, and revoke customer virtual keys (workspace-scoped).

A virtual key lets a customer call the model proxy directly (outside a native
GNSIS run). GNSIS mints it through LiteLLM's admin API, stamps attribution
metadata (``workspace_id`` / ``user_id`` / ``application_name``) so the usage
callback (PR 1) records and charges (PR 2) that usage to the right workspace, and
stores only LiteLLM's hashed token + a display prefix — **never the secret**,
which is shown to the caller exactly once at creation. Per-key spend limits are
enforced by LiteLLM's own budget on the key.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import List, Optional, Tuple

from . import litellm_admin, orm
from .db import session_scope
from .rates import to_money_str
from ..orchestration.models import new_id


class VirtualKeyError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class VirtualKeyView:
    id: str
    workspace_id: str
    user_id: str
    key_alias: str
    application_name: Optional[str]
    key_prefix: str
    max_budget: Optional[str]
    budget_duration: Optional[str]
    models: List[str]
    status: str
    created_at: str
    revoked_at: Optional[str]


def _view(row: orm.VirtualKey) -> VirtualKeyView:
    return VirtualKeyView(
        id=row.id,
        workspace_id=row.workspace_id,
        user_id=row.user_id,
        key_alias=row.key_alias,
        application_name=row.application_name,
        key_prefix=row.key_prefix,
        max_budget=row.max_budget,
        budget_duration=row.budget_duration,
        models=[m for m in (row.models or "").split(",") if m],
        status=row.status,
        created_at=row.created_at.isoformat() if row.created_at else "",
        revoked_at=row.revoked_at.isoformat() if row.revoked_at else None,
    )


def _display_prefix(secret: str) -> str:
    if not secret:
        return ""
    return f"{secret[:6]}…{secret[-4:]}" if len(secret) > 12 else secret


def _validate_budget(settings, max_budget: Optional[str]) -> str:
    """Resolve + cap the per-key budget; returns an exact decimal string."""
    raw = max_budget if max_budget not in (None, "") else settings.virtual_key_default_budget_usd
    try:
        amt = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise VirtualKeyError(f"invalid budget: {raw!r}") from exc
    if amt.is_nan() or amt.is_infinite() or amt <= 0:
        raise VirtualKeyError("budget must be a positive amount")
    cap = Decimal(str(settings.virtual_key_max_budget_usd))
    if amt > cap:
        raise VirtualKeyError(f"budget exceeds the per-key maximum of {cap}")
    return to_money_str(amt)


class VirtualKeyStore:
    """Workspace-isolated issuance/listing/revocation over ``virtual_keys``."""

    def create(
        self,
        settings,
        *,
        workspace_id: str,
        user_id: str,
        key_alias: str,
        max_budget: Optional[str] = None,
        budget_duration: Optional[str] = None,
        models: Optional[List[str]] = None,
    ) -> Tuple[VirtualKeyView, str]:
        """Mint a key. Returns ``(view, secret)`` — the secret is shown ONCE."""
        if not settings.virtual_keys_enabled:
            raise VirtualKeyError("virtual keys are not configured", status=503)
        alias = (key_alias or "").strip()
        if not alias:
            raise VirtualKeyError("a key name is required")
        budget = _validate_budget(settings, max_budget)
        model_list = [m.strip() for m in (models or []) if m and m.strip()]

        # Attribution so external (virtual-key) usage meters + charges correctly.
        metadata = {
            "workspace_id": workspace_id,
            "user_id": user_id,
            "application_name": alias,
        }
        try:
            data = litellm_admin.generate_key(
                settings,
                key_alias=alias,
                max_budget=budget,
                budget_duration=budget_duration,
                models=model_list or None,
                metadata=metadata,
            )
        except litellm_admin.VirtualKeyError as exc:
            raise VirtualKeyError(exc.message, status=exc.status) from exc

        secret = str(data.get("key") or "")
        token = str(data.get("token") or data.get("key_name") or "")
        if not token:
            raise VirtualKeyError("LiteLLM did not return a key token", status=502)

        with session_scope() as s:
            row = orm.VirtualKey(
                id=new_id("vkey"),
                workspace_id=workspace_id,
                user_id=user_id,
                key_alias=alias,
                application_name=alias,
                litellm_token=token,
                key_prefix=_display_prefix(secret),
                max_budget=budget,
                budget_duration=budget_duration,
                models=",".join(model_list),
                status="active",
            )
            s.add(row)
            s.flush()
            view = _view(row)
        return view, secret

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

    def revoke(self, settings, workspace_id: str, key_id: str) -> VirtualKeyView:
        """Revoke a key in LiteLLM, then mark it revoked. Idempotent per key."""
        with session_scope() as s:
            row = s.get(orm.VirtualKey, key_id)
            if row is None or row.workspace_id != workspace_id:
                raise VirtualKeyError("key not found", status=404)
            if row.status == "revoked":
                return _view(row)
            token = row.litellm_token

        try:
            litellm_admin.delete_key(settings, token)
        except litellm_admin.VirtualKeyError as exc:
            raise VirtualKeyError(exc.message, status=exc.status) from exc

        with session_scope() as s:
            row = s.get(orm.VirtualKey, key_id)
            if row is None or row.workspace_id != workspace_id:
                raise VirtualKeyError("key not found", status=404)
            if row.status != "revoked":
                row.status = "revoked"
                row.revoked_at = datetime.now(timezone.utc)
                s.flush()
            return _view(row)
