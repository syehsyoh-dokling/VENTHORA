# ruff: noqa: E501
"""ISO 4217: how many decimals a currency has, and its numeric code.

VENTHORA keeps every amount as an integer of hundredths, in every currency. That is exact for the currencies with two
decimals. For the others the standard decides what an amount may be:

  0 decimals (JPY, KRW, VND ...)   an amount is a whole unit: its hundredths are always 00, and it is written to the
                                    bank without decimals;
  3 decimals (BHD, KWD, OMR ...)   an amount is kept to two of its three decimals (1.23, never 1.234): what the bank
                                    accepts, since fewer decimals than the standard allows are valid;
  4 decimals (CLF, UYW)            units of account, never paid: refused.
"""
from __future__ import annotations

ZERO = frozenset({"BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG", "RWF", "UGX", "UYI", "VND", "VUV", "XAF", "XOF", "XPF"})
THREE = frozenset({"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"})
FOUR = frozenset({"CLF", "UYW"})
# the numeric codes of the currencies a treasury meets most; the alphabetic code is what every interface here uses
NUMERIC = {"AED": "784", "AUD": "036", "BHD": "048", "BRL": "986", "CAD": "124", "CHF": "756", "CNY": "156", "DKK": "208", "EUR": "978", "GBP": "826",
           "HKD": "344", "IDR": "360", "INR": "356", "JPY": "392", "KRW": "410", "KWD": "414", "MXN": "484", "MYR": "458", "NOK": "578", "NZD": "554",
           "PHP": "608", "PLN": "985", "SAR": "682", "SEK": "752", "SGD": "702", "THB": "764", "TRY": "949", "USD": "840", "VND": "704", "ZAR": "710"}


def exponent(currency: str) -> int:
    """The number of decimals ISO 4217 gives the currency (2 unless it is one of the exceptions)."""
    c = currency.upper()
    return 0 if c in ZERO else 3 if c in THREE else 4 if c in FOUR else 2


def check_amount(minor: int, currency: str) -> None:
    """ValueError when hundredths cannot be this currency's amount: a fraction of a unit in a currency without
    decimals, or a unit of account."""
    c = currency.upper()
    if c in FOUR:
        raise ValueError(f"{c} is a unit of account with four decimals: it is not a currency a payment is made in")
    if c in ZERO and minor % 100:
        raise ValueError(f"{c} has no decimals (ISO 4217): {minor / 100:,.2f} is not a whole amount")


def decimal(minor: int, currency: str) -> str:
    """Hundredths as the decimal amount a bank message carries: no decimals for a zero-decimal currency, two otherwise
    (valid for a three-decimal currency too)."""
    if minor < 0:
        raise ValueError("an instructed amount is never negative")
    check_amount(minor, currency)
    whole, cents = divmod(minor, 100)
    return str(whole) if currency.upper() in ZERO else f"{whole}.{cents:02d}"
