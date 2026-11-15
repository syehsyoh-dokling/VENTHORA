# ruff: noqa: E501
"""Which currencies Airwallex pays out, which it can fund a transfer from, and what a currency exchange really cost.

Learned from the real sandbox (5 Nov 2026):
* a beneficiary bank account may be held in any of PAYOUT_CURRENCIES (the beneficiary schema's account_currency list);
* a transfer can only be FUNDED from SOURCE_CURRENCIES (POST /transfers/create refuses any other source_currency with
  error 016 and lists these). A payout in any other currency (IDR, INR, MYR, NGN, ...) is "paid through": the transfer
  is funded in the base currency and Airwallex converts inside the transfer (USD 2.78 + fee USD 5.00 paid IDR 50,000);
* every quote carries the client rate AND the mid-market rate, so the cost of an exchange is measurable: what was
  sold, minus what the bought amount is worth at the mid-market rate. Payout fees are a separate cost.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

SOURCE_CURRENCIES = frozenset({
    "AED", "AUD", "BRL", "CAD", "CHF", "CNY", "CZK", "DKK", "EUR", "GBP", "HKD", "HUF", "ILS", "JPY", "KRW", "MXN", "NOK",
    "NZD", "PHP", "PLN", "RON", "SEK", "SGD", "THB", "USD", "VND", "ZAR"})

PAYOUT_CURRENCIES = frozenset({
    "AED", "ALL", "AMD", "AOA", "ARS", "AUD", "AZN", "BAM", "BBD", "BDT", "BGN", "BHD", "BOB", "BRL", "BSD", "BWP", "BZD",
    "CAD", "CHF", "CLP", "CNY", "COP", "CRC", "CZK", "DKK", "DOP", "DZD", "EGP", "EUR", "FJD", "GBP", "GEL", "GHS", "GMD",
    "GTQ", "GYD", "HKD", "HNL", "HUF", "IDR", "ILS", "INR", "ISK", "JMD", "JOD", "JPY", "KES", "KGS", "KRW", "KWD", "KZT",
    "LKR", "LSL", "MAD", "MGA", "MUR", "MWK", "MXN", "MYR", "NAD", "NGN", "NIO", "NOK", "NPR", "NZD", "OMR", "PEN", "PHP",
    "PKR", "PLN", "PYG", "QAR", "RON", "RSD", "RWF", "SAR", "SCR", "SEK", "SGD", "THB", "TND", "TRY", "TTD", "TZS",
    "USD", "UYU", "VND", "XAF", "XOF", "ZAR", "ZMW"}) | SOURCE_CURRENCIES



def pays_through(ccy: str, base: str = "USD") -> bool:
    """A payout Airwallex cannot fund from a wallet in its own currency: funded in the base currency, converted in the
    transfer itself (no separate conversion, no balance in that currency is ever used)."""
    return ccy != base and ccy in PAYOUT_CURRENCIES and ccy not in SOURCE_CURRENCIES


def value_at_mid(buy_minor: int, buy_ccy: str, sell_ccy: str, pair: str | None, mid) -> int | None:
    """What `buy_minor` of buy_ccy is worth in sell_ccy at the mid-market rate. Airwallex quotes a pair XY as Y per X
    (EURUSD 1.12 = USD per EUR, USDIDR 18,157 = IDR per USD)."""
    if mid in (None, "", 0) or not pair:
        return None
    mid = Decimal(str(mid))
    if mid <= 0:
        return None
    if pair == f"{buy_ccy}{sell_ccy}":
        v = Decimal(buy_minor) * mid
    elif pair == f"{sell_ccy}{buy_ccy}":
        v = Decimal(buy_minor) / mid
    else:
        return None
    return int(v.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def exchange_cost(*, sell_ccy: str, sell_minor: int, buy_ccy: str, buy_minor: int, pair: str | None, mid, client_rate=None) -> dict:
    """The spread actually paid on one exchange, in the sell currency: sold - (bought at mid-market). None if the
    mid-market rate is unknown (never guessed)."""
    at_mid = value_at_mid(buy_minor, buy_ccy, sell_ccy, pair, mid)
    cost = None if at_mid is None else sell_minor - at_mid
    return {"sell_currency": sell_ccy, "sell_minor": sell_minor, "buy_currency": buy_ccy, "buy_minor": buy_minor,
            "currency_pair": pair, "mid_rate": None if mid is None else str(mid),
            "client_rate": None if client_rate is None else str(client_rate),
            "at_mid_minor": at_mid, "spread_minor": cost,
            "spread_bp": None if not at_mid or cost is None else int((Decimal(cost) * 10_000 / Decimal(at_mid)).quantize(Decimal(1), rounding=ROUND_HALF_UP))}
