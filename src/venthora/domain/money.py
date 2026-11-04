"""Exact integer money helpers.

Every amount is an integer in minor units (cents, pence). FX rates are stored as
integer parts-per-billion (RATE_SCALE) of the base currency: a millionth was too coarse for
currencies worth a fraction of a cent (IDR 0.0000556 USD rounded to 56 ppm is 0.7% off;
at 1e9 it is 55,600, off by under 0.002%). Every rounding direction is explicit: outflows
round up, inflows round down, so the plan never overstates the cash it has. PPM stays the
scale of relative moves (stress add-ons).
"""
from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation

PPM = 1_000_000  # relative moves (stress)
RATE_SCALE = 1_000_000_000  # FX rates: base units per 1 unit of a currency, x 1e9


def rate_ppm(rate: str | Decimal, *, round_up: bool) -> int:
    """Convert a decimal FX rate (base units per 1 unit of currency) to integer parts per RATE_SCALE."""
    try:
        value = Decimal(str(rate))
    except InvalidOperation as exc:
        raise ValueError(f"invalid FX rate: {rate!r}") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError(f"invalid FX rate: {rate!r}")
    rounding = ROUND_CEILING if round_up else ROUND_FLOOR
    return int((value * RATE_SCALE).to_integral_value(rounding=rounding))


def cost_in_base(amount_minor: int, ppm: int) -> int:
    """Base-currency cost of buying `amount_minor` of a currency, rounded up (`ppm` = rate x RATE_SCALE)."""
    return -(-amount_minor * ppm // RATE_SCALE)


def value_in_base(amount_minor: int, ppm: int) -> int:
    """Base-currency value of holding `amount_minor` of a currency, rounded down (`ppm` = rate x RATE_SCALE)."""
    return amount_minor * ppm // RATE_SCALE


def apply_ppm_up(amount_minor: int, ppm: int) -> int:
    """`amount_minor * ppm / 1e6`, rounded up. Used for stress add-ons."""
    return -(-amount_minor * ppm // PPM)


def fmt(amount_minor: int, currency: str) -> str:
    sign = "-" if amount_minor < 0 else ""
    whole, cents = divmod(abs(amount_minor), 100)
    return f"{sign}{currency} {whole:,}.{cents:02d}"
