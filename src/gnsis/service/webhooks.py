"""GitHub App webhook handling.

Webhooks *maintain* already-claimed installation records — they are never the
sole proof that an installation belongs to a user (that's the auth-service
ownership check in the claim flow). The signature is verified against
``GITHUB_WEBHOOK_SECRET`` before any payload field is trusted, and each delivery
is processed at most once (idempotent by ``X-GitHub-Delivery``).

An event for an installation that hasn't been claimed yet is acknowledged and
ignored — there's no workspace to attach it to, and a webhook must never create
that link (only the verified claim flow may).
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any, Dict

from . import workspaces


class WebhookError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def verify_signature(secret: str, body: bytes, signature_header: str) -> None:
    """Verify the ``X-Hub-Signature-256`` header, or raise WebhookError(401)."""
    if not signature_header:
        raise WebhookError("missing signature", status=401)
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature_header):
        raise WebhookError("invalid signature", status=401)


def handle_event(event: str, delivery_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Apply a *signature-verified* webhook event. Idempotent by delivery id.

    The caller must have verified the signature already. Returns a small,
    log-safe summary (no tokens, no secrets).
    """
    if delivery_id and workspaces.delivery_already_processed(delivery_id, event):
        return {"status": "duplicate", "event": event}

    installation = payload.get("installation") or {}
    installation_id = installation.get("id")
    action = payload.get("action")

    if event == "installation":
        return _handle_installation(action, installation_id)
    if event == "installation_repositories":
        return _handle_installation_repositories(installation_id, action, payload)
    return {"status": "ignored", "event": event, "action": action}


def _handle_installation(action: str, installation_id: Any) -> Dict[str, Any]:
    if installation_id is None:
        return {"status": "ignored", "reason": "no installation id"}
    inst_id = int(installation_id)
    if action == "created":
        # Nothing to do until the user claims it through the verified flow.
        return {"status": "ack", "action": action}
    if action == "deleted":
        workspaces.set_installation_status(inst_id, "deleted")
        return {"status": "applied", "action": action}
    if action == "suspend":
        workspaces.set_installation_status(inst_id, "suspended", suspended=True)
        return {"status": "applied", "action": action}
    if action == "unsuspend":
        workspaces.set_installation_status(inst_id, "active", suspended=False)
        return {"status": "applied", "action": action}
    return {"status": "ignored", "action": action}


def _handle_installation_repositories(
    installation_id: Any, action: str, payload: Dict[str, Any]
) -> Dict[str, Any]:
    if installation_id is None:
        return {"status": "ignored", "reason": "no installation id"}
    inst_id = int(installation_id)
    added = payload.get("repositories_added") or []
    removed = payload.get("repositories_removed") or []

    if removed:
        ids = [r.get("id") for r in removed if r.get("id") is not None]
        workspaces.remove_repositories_by_github_id(inst_id, ids)
    # For "added", we don't have full repo metadata (default_branch etc.) in the
    # webhook, and we can't mint a token here safely without the App creds wired
    # through — the next explicit sync (or claim) picks them up. We record the
    # event idempotently and leave a re-sync to the authenticated flow.
    return {
        "status": "applied",
        "action": action,
        "added": len(added),
        "removed": len(removed),
    }
