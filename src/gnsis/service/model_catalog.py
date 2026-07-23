"""The user-facing model catalog + the authoritative allowlist check.

Users pick a *model* (not an engine/harness/provider). The catalog is derived
entirely from the server-controlled allowlist ``settings.run_allowed_models`` —
the operator decides which OpenRouter models are offered, and the frontend must
never carry an independent list that can drift. Optional per-model display
metadata comes from ``settings.model_metadata``; a model without an entry falls
back safely to its own id.

``resolve_allowed_model`` is the single authority the rest of the backend uses
to accept or reject a requested model. A prompt, repository file, frontend
request, or executor can never widen the allowlist.
"""

from __future__ import annotations

from typing import List, Optional


def default_model(settings) -> Optional[str]:
    """The model used when the user doesn't choose one (first in the allowlist)."""
    allowed = settings.run_allowed_models or []
    return allowed[0] if allowed else None


def is_allowed(settings, model_id: str) -> bool:
    return bool(model_id) and model_id in (settings.run_allowed_models or [])


def resolve_allowed_model(settings, requested: Optional[str]) -> Optional[str]:
    """Return the model to use, or ``None`` if ``requested`` is not allowed.

    * ``requested`` empty/None → the configured default (may be ``None`` if the
      allowlist is empty).
    * ``requested`` in the allowlist → that exact id.
    * ``requested`` not allowed → ``None`` (caller rejects). Never falls back to
      the default for an *explicit* unsupported choice — that would silently
      run a different model than the user asked for.
    """
    if not requested:
        return default_model(settings)
    return requested if is_allowed(settings, requested) else None


def _provider_of(model_id: str) -> str:
    return model_id.split("/", 1)[0] if "/" in model_id else ""


def model_catalog(settings) -> List[dict]:
    """The ordered catalog of offerable models, with display metadata.

    Never lists a model outside ``run_allowed_models``. The first allowed model
    is flagged ``default``.
    """
    allowed = list(settings.run_allowed_models or [])
    metadata = settings.model_metadata or {}
    default = allowed[0] if allowed else None
    catalog: List[dict] = []
    for model_id in allowed:
        meta = metadata.get(model_id) if isinstance(metadata.get(model_id), dict) else {}
        entry = {
            "id": model_id,
            "label": str(meta.get("label") or model_id),
            "provider": str(meta.get("provider") or _provider_of(model_id)),
            "default": model_id == default,
        }
        # Optional, non-blocking metadata — only included when the operator set it.
        for key in ("description", "speed_tier", "cost_tier", "context_window"):
            if meta.get(key) is not None:
                entry[key] = meta[key]
        catalog.append(entry)
    return catalog
