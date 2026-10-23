"""Exact integer money helpers.

Every amount is an integer in minor units (cents, pence). FX rates are stored as
integer parts-per-million of the base currency, and every rounding direction is
explicit: outflows round up, inflows round down, so the plan never overstates
the cash it has.
"""
from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation

PPM = 1_000_000


def rate_ppm(rate: str | Decimal, *, round_up: bool) -> int:
    """Convert a decimal FX rate (base units per 1 unit of currency) to ppm."""
    try:
        value = Decimal(str(rate))
    except InvalidOperation as exc:
        raise ValueError(f"invalid FX rate: {rate!r}") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError(f"invalid FX rate: {rate!r}")
    rounding = ROUND_CEILING if round_up else ROUND_FLOOR
    return int((value * PPM).to_integral_value(rounding=rounding))


def cost_in_base(amount_minor: int, ppm: int) -> int:
    """Base-currency cost of buying `amount_minor` of a currency, rounded up."""
    return -(-amount_minor * ppm // PPM)


def value_in_base(amount_minor: int, ppm: int) -> int:
    """Base-currency value of holding `amount_minor` of a currency, rounded down."""
    return amount_minor * ppm // PPM


def apply_ppm_up(amount_minor: int, ppm: int) -> int:
    """`amount_minor * ppm / 1e6`, rounded up. Used for stress add-ons."""
    return -(-amount_minor * ppm // PPM)


def fmt(amount_minor: int, currency: str) -> str:
    sign = "-" if amount_minor < 0 else ""
    whole, cents = divmod(abs(amount_minor), 100)
    return f"{sign}{currency} {whole:,}.{cents:02d}"
