"""Production auto-refill: off-session top-ups with hard guardrails.

When available balance falls below a workspace's configured threshold, GNSIS
charges the saved card off-session and (once Stripe confirms) credits the wallet.
Every dangerous edge is guarded:

* explicit, timestamped **consent** is required before any off-session charge;
* a per-workspace **row lock** + a single-active-attempt invariant means many
  simultaneous usage events cannot trigger duplicate refills;
* each attempt carries a **deterministic Stripe idempotency key**;
* crediting is **payment-level idempotent** (never credits the same PaymentIntent
  twice, sync path or webhook);
* per-attempt / per-day-count / **daily + monthly dollar caps** are enforced
  server-side;
* declines / expired cards / insufficient funds / authentication-required are
  classified, drive a **cooldown**, and **auto-pause** after repeated failures;
* an **immutable attempt record** captures trigger balance, threshold, amount,
  PaymentIntent id, outcome, and timestamps.

No balance is credited until Stripe confirms a successful payment.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import List, Optional

from . import orm, stripe_client, stripe_customers
from .billing import BillingStore
from .db import session_scope
from .rates import to_money_str
from ..orchestration.models import new_id

# State machine.
PENDING = "pending"
PROCESSING = "processing"
SUCCEEDED = "succeeded"
REQUIRES_ACTION = "requires_action"
FAILED = "failed"
CANCELLED = "cancelled"

# Guardrail defaults.
MAX_CONSECUTIVE_FAILURES = 3          # pause after this many in a row
COOLDOWN_SECONDS = 3600               # wait this long after a failed/blocked attempt
_ACTIVE = (PENDING, PROCESSING, REQUIRES_ACTION)
# Attempts that count toward caps (money committed or in-flight); failed/cancelled don't.
_COUNTS = (PENDING, PROCESSING, REQUIRES_ACTION, SUCCEEDED)
_ZERO = Decimal("0")


class AutoRefillError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class ConfigView:
    workspace_id: str
    enabled: bool
    threshold: str
    refill_amount: str
    max_refill_amount: str
    max_refills_per_day: int
    daily_cap: str
    monthly_cap: Optional[str]
    payment_method_id: Optional[str]
    consent: bool
    consent_at: Optional[str]
    paused: bool
    pause_reason: Optional[str]
    consecutive_failures: int
    cooldown_until: Optional[str]
    last_attempt_at: Optional[str]
    active: bool


@dataclass(frozen=True)
class AttemptView:
    id: str
    workspace_id: str
    status: str
    trigger_balance: str
    threshold: str
    refill_amount: str
    currency: str
    stripe_payment_intent_id: Optional[str]
    failure_code: Optional[str]
    failure_message: Optional[str]
    created_at: str
    updated_at: str


def _is_active(c: orm.AutoRefillConfig) -> bool:
    return bool(c.enabled and c.consent and c.payment_method_id and not c.paused)


def _config_view(c: orm.AutoRefillConfig) -> ConfigView:
    return ConfigView(
        workspace_id=c.workspace_id, enabled=c.enabled, threshold=c.threshold,
        refill_amount=c.refill_amount, max_refill_amount=c.max_refill_amount,
        max_refills_per_day=c.max_refills_per_day, daily_cap=c.daily_cap,
        monthly_cap=c.monthly_cap, payment_method_id=c.payment_method_id,
        consent=c.consent, consent_at=c.consent_at.isoformat() if c.consent_at else None,
        paused=c.paused, pause_reason=c.pause_reason,
        consecutive_failures=c.consecutive_failures,
        cooldown_until=c.cooldown_until.isoformat() if c.cooldown_until else None,
        last_attempt_at=c.last_attempt_at.isoformat() if c.last_attempt_at else None,
        active=_is_active(c),
    )


def _attempt_view(a: orm.AutoRefillAttempt) -> AttemptView:
    return AttemptView(
        id=a.id, workspace_id=a.workspace_id, status=a.status,
        trigger_balance=a.trigger_balance, threshold=a.threshold,
        refill_amount=a.refill_amount, currency=a.currency,
        stripe_payment_intent_id=a.stripe_payment_intent_id,
        failure_code=a.failure_code, failure_message=a.failure_message,
        created_at=a.created_at.isoformat() if a.created_at else "",
        updated_at=a.updated_at.isoformat() if a.updated_at else "",
    )


def _dec(value, field: str) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise AutoRefillError(f"{field} is not a valid amount") from exc
    if d.is_nan() or d.is_infinite():
        raise AutoRefillError(f"{field} is not a valid amount")
    return d


def _default_view(workspace_id: str) -> ConfigView:
    return ConfigView(
        workspace_id=workspace_id, enabled=False, threshold="0", refill_amount="0",
        max_refill_amount="0", max_refills_per_day=3, daily_cap="0", monthly_cap=None,
        payment_method_id=None, consent=False, consent_at=None, paused=False,
        pause_reason=None, consecutive_failures=0, cooldown_until=None,
        last_attempt_at=None, active=False,
    )


def get_config(workspace_id: str) -> ConfigView:
    with session_scope() as s:
        c = s.get(orm.AutoRefillConfig, workspace_id)
        return _config_view(c) if c else _default_view(workspace_id)


def save_config(
    settings,
    workspace_id: str,
    *,
    enabled: bool,
    threshold,
    refill_amount,
    max_refill_amount,
    max_refills_per_day: int,
    daily_cap,
    monthly_cap=None,
    payment_method_id: Optional[str],
    consent: bool,
) -> ConfigView:
    """Validate + persist the policy. Enabling requires consent + a payment method."""
    threshold_d = _dec(threshold, "threshold")
    refill_d = _dec(refill_amount, "refill_amount")
    max_refill_d = _dec(max_refill_amount, "max_refill_amount")
    daily_cap_d = _dec(daily_cap, "daily_cap")
    monthly_cap_d = _dec(monthly_cap, "monthly_cap") if monthly_cap not in (None, "") else None
    hard_max = Decimal(str(settings.refill_max_usd))

    if threshold_d < 0:
        raise AutoRefillError("threshold must be zero or more")
    if refill_d <= 0:
        raise AutoRefillError("refill amount must be positive")
    if max_refill_d < refill_d:
        raise AutoRefillError("max refill amount must be at least the refill amount")
    if max_refill_d > hard_max:
        raise AutoRefillError(f"max refill amount exceeds the account limit of {hard_max}")
    if int(max_refills_per_day) < 1:
        raise AutoRefillError("max refills per day must be at least 1")
    if daily_cap_d < refill_d:
        raise AutoRefillError("daily cap must be at least the refill amount")
    if monthly_cap_d is not None and monthly_cap_d < daily_cap_d:
        raise AutoRefillError("monthly cap must be at least the daily cap")
    if enabled and not consent:
        raise AutoRefillError("enabling auto-refill requires explicit consent to off-session charges")
    if enabled and not payment_method_id:
        raise AutoRefillError("a default payment method is required to enable auto-refill")

    now = datetime.now(timezone.utc)
    with session_scope() as s:
        c = s.get(orm.AutoRefillConfig, workspace_id)
        if c is None:
            c = orm.AutoRefillConfig(workspace_id=workspace_id)
            s.add(c)
        # Record fresh, timestamped consent when it is (re)granted.
        if consent and not c.consent:
            c.consent_at = now
        if not consent:
            c.consent_at = None
        c.consent = bool(consent)
        c.enabled = bool(enabled)
        c.threshold = to_money_str(threshold_d)
        c.refill_amount = to_money_str(refill_d)
        c.max_refill_amount = to_money_str(max_refill_d)
        c.max_refills_per_day = int(max_refills_per_day)
        c.daily_cap = to_money_str(daily_cap_d)
        c.monthly_cap = to_money_str(monthly_cap_d) if monthly_cap_d is not None else None
        c.payment_method_id = payment_method_id or None
        # Re-enabling clears a prior auto-pause and its failure streak.
        if enabled:
            c.paused = False
            c.pause_reason = None
            c.paused_at = None
            c.consecutive_failures = 0
            c.cooldown_until = None
        s.flush()
        return _config_view(c)


def list_attempts(workspace_id: str, *, limit: int = 20) -> List[AttemptView]:
    with session_scope() as s:
        rows = (
            s.query(orm.AutoRefillAttempt)
            .filter(orm.AutoRefillAttempt.workspace_id == workspace_id)
            .order_by(orm.AutoRefillAttempt.created_at.desc())
            .limit(limit)
            .all()
        )
        return [_attempt_view(r) for r in rows]


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalise a DB datetime to UTC-aware (SQLite returns naive; PG returns aware)."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _day_month_starts(now: datetime):
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return day, month


def _reserve_attempt(settings, workspace_id: str, now: datetime) -> Optional[dict]:
    """Locked decision: may we start a refill? If yes, create the attempt row.

    Returns ``{"attempt_id", "amount", "currency", "customer_id", "pm_id",
    "idempotency_key"}`` to charge outside the lock, or ``None`` to skip. The
    Stripe network call is intentionally NOT made while holding the lock.
    """
    billing = BillingStore()
    with session_scope() as s:
        # Serialise per workspace (Postgres). The billing anchor is the lock.
        anchor = s.get(orm.WorkspaceBilling, workspace_id)
        if anchor is None:
            anchor = orm.WorkspaceBilling(workspace_id=workspace_id)
            s.add(anchor)
            s.flush()
        s.query(orm.WorkspaceBilling).filter(
            orm.WorkspaceBilling.workspace_id == workspace_id
        ).with_for_update().all()

        c = s.get(orm.AutoRefillConfig, workspace_id)
        if c is None or not _is_active(c):
            return None
        cooldown = _aware(c.cooldown_until)
        if cooldown and cooldown > now:
            return None

        available = billing.available(workspace_id)
        if available >= Decimal(c.threshold or "0"):
            return None

        # One active attempt at a time — this is what makes simultaneous triggers
        # collapse into a single refill.
        active = (
            s.query(orm.AutoRefillAttempt)
            .filter(
                orm.AutoRefillAttempt.workspace_id == workspace_id,
                orm.AutoRefillAttempt.status.in_(_ACTIVE),
            )
            .count()
        )
        if active:
            return None

        # Caps: per-day count + daily/monthly dollar totals (in-flight counts too).
        day_start, month_start = _day_month_starts(now)
        today = (
            s.query(orm.AutoRefillAttempt)
            .filter(
                orm.AutoRefillAttempt.workspace_id == workspace_id,
                orm.AutoRefillAttempt.status.in_(_COUNTS),
                orm.AutoRefillAttempt.created_at >= day_start,
            )
            .all()
        )
        amount = Decimal(c.refill_amount or "0")
        if len(today) >= c.max_refills_per_day:
            return None
        day_total = sum((Decimal(a.refill_amount or "0") for a in today), _ZERO)
        if day_total + amount > Decimal(c.daily_cap or "0"):
            return None
        if c.monthly_cap:
            month_rows = (
                s.query(orm.AutoRefillAttempt.refill_amount)
                .filter(
                    orm.AutoRefillAttempt.workspace_id == workspace_id,
                    orm.AutoRefillAttempt.status.in_(_COUNTS),
                    orm.AutoRefillAttempt.created_at >= month_start,
                )
                .all()
            )
            month_total = sum((Decimal(r[0] or "0") for r in month_rows), _ZERO)
            if month_total + amount > Decimal(c.monthly_cap):
                return None

        customer_id = anchor.stripe_customer_id
        if not customer_id:
            return None  # no Customer yet → nothing to charge off-session

        attempt_id = new_id("arf")
        attempt = orm.AutoRefillAttempt(
            id=attempt_id, workspace_id=workspace_id, status=PROCESSING,
            trigger_balance=to_money_str(available), threshold=c.threshold,
            refill_amount=c.refill_amount, currency=(settings.default_currency or "USD"),
            idempotency_key=f"gnsis-autorefill:{attempt_id}",
        )
        s.add(attempt)
        c.last_attempt_at = now
        s.flush()
        return {
            "attempt_id": attempt_id,
            "amount": amount,
            "currency": settings.default_currency or "USD",
            "customer_id": customer_id,
            "pm_id": c.payment_method_id,
            "idempotency_key": attempt.idempotency_key,
        }


def _finish(attempt_id: str, *, status: str, pi_id=None, failure_code=None, failure_message=None):
    """Advance an attempt's terminal/soft state + update the failure streak."""
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        a = s.get(orm.AutoRefillAttempt, attempt_id)
        if a is None:
            return
        a.status = status
        if pi_id:
            a.stripe_payment_intent_id = pi_id
        a.failure_code = failure_code
        a.failure_message = (failure_message or None) and failure_message[:500]
        c = s.get(orm.AutoRefillConfig, a.workspace_id)
        if c is not None:
            if status == SUCCEEDED:
                c.consecutive_failures = 0
                c.cooldown_until = None
            elif status in (FAILED, REQUIRES_ACTION):
                c.consecutive_failures = (c.consecutive_failures or 0) + 1
                c.cooldown_until = now + timedelta(seconds=COOLDOWN_SECONDS)
                if c.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    c.paused = True
                    c.paused_at = now
                    c.pause_reason = f"auto-paused after {c.consecutive_failures} failed attempts"
        s.flush()


def evaluate_and_maybe_refill(settings, workspace_id: str) -> Optional[AttemptView]:
    """Trigger entry point: charge off-session if the policy says to.

    Safe to call from many concurrent usage events — the lock + single-active
    invariant guarantee at most one in-flight refill per workspace.
    """
    if not settings.stripe_secret_key:
        return None
    now = datetime.now(timezone.utc)
    plan = _reserve_attempt(settings, workspace_id, now)
    if plan is None:
        return None

    attempt_id = plan["attempt_id"]
    amount_cents = int((plan["amount"] * 100).to_integral_value())
    try:
        pi = stripe_client.create_off_session_payment_intent(
            settings,
            customer_id=plan["customer_id"],
            payment_method_id=plan["pm_id"],
            amount_cents=amount_cents,
            currency=(plan["currency"] or "USD").lower(),
            workspace_id=workspace_id,
            idempotency_key=plan["idempotency_key"],
        )
    except stripe_client.StripeError as exc:
        # Classify the outcome. Authentication-required is recoverable on-session;
        # everything else (declined / expired / insufficient funds) is a failure.
        status = REQUIRES_ACTION if exc.code == "authentication_required" else FAILED
        _finish(
            attempt_id, status=status, pi_id=exc.payment_intent_id,
            failure_code=exc.decline_code or exc.code, failure_message=exc.message,
        )
        with session_scope() as s:
            a = s.get(orm.AutoRefillAttempt, attempt_id)
            return _attempt_view(a) if a else None

    pi_id = pi.get("id")
    pi_status = pi.get("status")
    if pi_status == "succeeded":
        # Stripe confirmed: credit exactly once (payment-level idempotent; the
        # webhook for this same PI will converge and not double-credit).
        BillingStore().top_up(
            workspace_id, plan["amount"],
            idempotency_key=f"autorefill-pi:{pi_id}",
            stripe_payment_reference=pi_id, payment_reference=pi_id,
            currency=plan["currency"],
        )
        _finish(attempt_id, status=SUCCEEDED, pi_id=pi_id)
    elif pi_status in ("requires_action", "requires_confirmation", "requires_payment_method"):
        _finish(attempt_id, status=REQUIRES_ACTION, pi_id=pi_id,
                failure_code="authentication_required",
                failure_message="Payment needs authentication; complete it on-session.")
    elif pi_status == "processing":
        # Async settlement — leave as processing; the webhook will finalise it.
        with session_scope() as s:
            a = s.get(orm.AutoRefillAttempt, attempt_id)
            if a is not None:
                a.stripe_payment_intent_id = pi_id
                s.flush()
    else:
        _finish(attempt_id, status=FAILED, pi_id=pi_id, failure_code=pi_status,
                failure_message=f"unexpected PaymentIntent status: {pi_status}")

    with session_scope() as s:
        a = s.get(orm.AutoRefillAttempt, attempt_id)
        return _attempt_view(a) if a else None


def sweep(settings, *, limit: int = 500) -> int:
    """Evaluate every eligible workspace (the periodic trigger).

    Runs off the request path (a Celery beat task), so a broker/worker outage can
    never block metering. Each workspace is re-checked under its own lock inside
    :func:`evaluate_and_maybe_refill`, so overlapping sweeps can't double-charge.
    Returns the number of workspaces that started a refill.
    """
    if not settings.stripe_secret_key:
        return 0
    with session_scope() as s:
        rows = (
            s.query(orm.AutoRefillConfig.workspace_id)
            .filter(
                orm.AutoRefillConfig.enabled.is_(True),
                orm.AutoRefillConfig.consent.is_(True),
                orm.AutoRefillConfig.paused.is_(False),
                orm.AutoRefillConfig.payment_method_id.isnot(None),
            )
            .limit(limit)
            .all()
        )
        workspace_ids = [r[0] for r in rows]
    started = 0
    for wid in workspace_ids:
        try:
            result = evaluate_and_maybe_refill(settings, wid)
        except Exception:  # noqa: BLE001 — one workspace must not abort the sweep
            continue
        if result is not None and result.status in (SUCCEEDED, PROCESSING, REQUIRES_ACTION):
            started += 1
    return started


# -- webhook reconciliation (single source of truth for the final outcome) ----

def mark_attempt_by_pi(pi_id: str, *, succeeded: bool, failure_code=None, failure_message=None):
    """Advance the attempt matching a PaymentIntent from its webhook.

    Crediting on success is handled by the webhook's ``top_up`` (payment-level
    idempotent); here we only reconcile the attempt + failure streak.
    """
    if not pi_id:
        return
    with session_scope() as s:
        a = (
            s.query(orm.AutoRefillAttempt)
            .filter(orm.AutoRefillAttempt.stripe_payment_intent_id == pi_id)
            .one_or_none()
        )
        if a is None or a.status in (SUCCEEDED, FAILED, CANCELLED):
            return
        attempt_id = a.id
    if succeeded:
        _finish(attempt_id, status=SUCCEEDED, pi_id=pi_id)
    else:
        status = REQUIRES_ACTION if failure_code == "authentication_required" else FAILED
        _finish(attempt_id, status=status, pi_id=pi_id,
                failure_code=failure_code, failure_message=failure_message)
