"""The rate service — the single place a retail price is computed.

    service_fee = upstream_cost × markup_rate
    retail_cost = upstream_cost + service_fee

The markup is config-driven and versioned; it is never hardcoded across routes,
workers, or models. The service returns the full pricing decision (upstream,
markup rate, service fee, retail, rate-card version) so the caller can store the
exact applied values on the charge — historical charges are therefore immutable
facts and are never recomputed when the current markup changes.

All arithmetic is :class:`decimal.Decimal`; nothing here uses binary floating
point for money.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

# Money is carried at 8 decimal places (well beyond cent precision) so tiny
# per-token upstream costs are not lost before aggregation.
_MONEY = Decimal("0.00000001")


def to_money_str(value) -> str:
    """Exact, normalised decimal string for storage (never a float)."""
    dec = value if isinstance(value, Decimal) else Decimal(str(value))
    return format(dec.quantize(_MONEY, rounding=ROUND_HALF_UP).normalize(), "f")


@dataclass(frozen=True)
class RateQuote:
    upstream_cost: Decimal
    markup_rate: Decimal
    service_fee: Decimal
    retail_cost: Decimal
    rate_card_version: str
    currency: str

    def as_strings(self) -> dict:
        return {
            "upstream_cost": to_money_str(self.upstream_cost),
            "markup_rate": format(self.markup_rate.normalize(), "f"),
            "service_fee": to_money_str(self.service_fee),
            "retail_cost": to_money_str(self.retail_cost),
            "rate_card_version": self.rate_card_version,
            "currency": self.currency,
        }


def quote(
    settings,
    *,
    upstream_cost,
    workspace_id: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    timestamp=None,
    currency: Optional[str] = None,
) -> RateQuote:
    """Price one upstream cost. ``workspace``/``provider``/``model``/``timestamp``
    are accepted for future per-dimension rate cards; the beta uses one global,
    versioned markup."""
    upstream = upstream_cost if isinstance(upstream_cost, Decimal) else Decimal(str(upstream_cost))
    if upstream < 0:
        upstream = Decimal("0")
    markup = Decimal(str(settings.markup_rate))
    service_fee = (upstream * markup).quantize(_MONEY, rounding=ROUND_HALF_UP)
    retail = (upstream + service_fee).quantize(_MONEY, rounding=ROUND_HALF_UP)
    return RateQuote(
        upstream_cost=upstream,
        markup_rate=markup,
        service_fee=service_fee,
        retail_cost=retail,
        rate_card_version=settings.rate_card_version,
        currency=currency or settings.default_currency,
    )
