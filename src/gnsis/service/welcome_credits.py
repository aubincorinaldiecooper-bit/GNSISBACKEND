"""Automatic welcome credits — the first-connection grant.

After a workspace's first successful GitHub App claim (installation ownership
verified, repositories synced), a fixed, campaign-scoped dollar credit is added
to its prepaid balance. Unlike operator beta grants, this one is triggered by
the user's own action and requires no operator involvement — but it is otherwise
subject to the same guarantees as :mod:`gnsis.service.beta_credits`:

* **Idempotent per (workspace, campaign)** — retries of the claim,
  reconnections, and adding a second installation to the same workspace never
  double-grant. A new campaign identifier resets that eligibility.
* **Capped** — never exceeds ``settings.beta_credit_max_usd`` (the shared
  ledger-safety ceiling).
* **Platform-daily-limited** — a configured total daily provider-spend ceiling
  silently skips further grants once reached, so a spike in signups can never
  become an uncapped bill. The GitHub claim itself always succeeds.
* **Never a gate on success** — a granting error is logged and swallowed;
  connection is orthogonal to funding. The user reaches Settings even if the
  credit was denied.

The underlying storage is :mod:`gnsis.service.beta_credits` (same audit row,
same ledger transaction type ``beta_grant``), so all existing balance /
reservation / spending-limit machinery treats the credit uniformly.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

from . import beta_credits, orm
from .db import session_scope
from .settings import Settings, get_settings

_log = logging.getLogger(__name__)

_ZERO = Decimal("0")


#: The attested operator id recorded on every welcome grant. Distinct from any
#: real operator email — makes the audit trail unambiguous.
WELCOME_OPERATOR = "welcome"


def _idempotency_key(workspace_id: str, campaign: str) -> str:
    """The canonical (workspace, campaign) key. Never regenerate or mutate."""
    return f"welcome:{workspace_id}:{campaign}"


def _positive_decimal(raw: str) -> Optional[Decimal]:
    try:
        d = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d > 0 else None


def _daily_welcome_spend(now: Optional[datetime] = None) -> Decimal:
    """Sum of welcome-credit ledger amounts granted in the last 24 hours.

    Consults the immutable ledger, not the audit row, so a reversed grant still
    counts as spent-for-purposes-of-the-daily-limit (reversing money already sent
    to the provider does not un-send it — the daily cap is about provider
    exposure, not net balance).

    Sums in Python rather than SQL because ``signed_amount`` is a ``String(40)``
    (money as a decimal string, to avoid float drift); PostgreSQL has no
    ``sum(varchar)`` aggregate, so a server-side ``SUM`` would raise here in
    production and silently disable the daily cap on the swallow path above.
    """
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=24)
    with session_scope() as s:
        # Only positive welcome grants are counted; the compensating reversal
        # transactions carry type BETA_GRANT_REVERSAL, so they are excluded.
        rows = (
            s.query(orm.BalanceTransaction.signed_amount)
            .filter(orm.BalanceTransaction.transaction_type == beta_credits.BETA_GRANT)
            .filter(orm.BalanceTransaction.idempotency_key.like(f"{beta_credits.BETA_GRANT}:welcome:%"))
            .filter(orm.BalanceTransaction.created_at >= since)
            .all()
        )
    total = _ZERO
    for (raw,) in rows:
        try:
            total += Decimal(raw or "0")
        except (InvalidOperation, ValueError, TypeError):
            # A malformed row shouldn't blow up the whole ceiling check — skip
            # it and continue; the audit trail preserves the raw value.
            continue
    return total


def try_grant(workspace_id: str, *, settings: Optional[Settings] = None) -> Optional[dict]:
    """Grant the welcome credit if configured and not already granted.

    Returns the grant view (with ``duplicate=True`` on a retry) or ``None`` if
    the credit was skipped: disabled, misconfigured, or blocked by the daily
    ceiling. Never raises for expected skip reasons — the caller can log the
    return value and move on.
    """
    settings = settings or get_settings()
    if not settings.welcome_credit_enabled:
        return None

    workspace_id = (workspace_id or "").strip()
    if not workspace_id:
        _log.warning("welcome_credit: empty workspace_id, skipping")
        return None

    amount = _positive_decimal(settings.welcome_credit_usd)
    if amount is None:
        _log.warning(
            "welcome_credit: GNSIS_WELCOME_CREDIT_USD %r is not a positive number; skipping",
            settings.welcome_credit_usd,
        )
        return None

    # The advertised per-run cap must not exceed the effective per-run cost cap
    # — otherwise the SLA is a lie. A misconfiguration is a skip, not a crash.
    per_run = _positive_decimal(settings.welcome_credit_per_run_usd)
    if per_run is not None and float(per_run) > settings.run_max_cost_usd:
        _log.warning(
            "welcome_credit: per-run cap %s exceeds run_max_cost_usd %s; skipping",
            per_run, settings.run_max_cost_usd,
        )
        return None

    # Platform-wide safety valve. Empty string / whitespace = no ceiling.
    ceiling_raw = (settings.platform_daily_provider_limit_usd or "").strip()
    if ceiling_raw:
        ceiling = _positive_decimal(ceiling_raw)
        if ceiling is None:
            _log.warning(
                "welcome_credit: platform_daily_provider_limit_usd %r invalid; skipping",
                ceiling_raw,
            )
            return None
        already = _daily_welcome_spend()
        if already + amount > ceiling:
            _log.info(
                "welcome_credit: skipped, daily ceiling reached (%s + %s > %s)",
                already, amount, ceiling,
            )
            return None

    campaign = (settings.welcome_credit_campaign or "").strip() or "beta"
    try:
        return beta_credits.grant_credit(
            workspace_id=workspace_id,
            amount=amount,
            reason=f"welcome credit ({campaign})",
            operator=WELCOME_OPERATOR,
            idempotency_key=_idempotency_key(workspace_id, campaign),
            # Both caps apply: the grant amount cannot exceed the shared ledger
            # ceiling regardless of what the welcome-credit config claims.
            max_amount=settings.beta_credit_max_usd,
        )
    except beta_credits.BetaCreditError as exc:
        _log.warning("welcome_credit: rejected — %s", exc)
        return None
