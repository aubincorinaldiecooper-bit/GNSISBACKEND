"""Shared UTC datetime helpers.

Small and dependency-free so every layer (service, orchestration, resources,
tracer) can import it without risking a circular import.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return utcnow().isoformat()


def aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalise to a UTC-aware datetime (SQLite hands back naive values)."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
