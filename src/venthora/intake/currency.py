"""The currencies an emailed invoice or claim may be written in: every currency Airwallex pays out (ISO codes) and the
symbols people write (S$ 2,281.37, Rp 1.500.000, RM 450, HK$ 900). One list for the rules (pipeline.py) and for the
model's reading (reader.py), so a Singapore or Indonesian invoice is read like a US one."""
from __future__ import annotations

import re

from ..fx_routes import PAYOUT_CURRENCIES

# "ALL" (Albanian lek) is also an English word in capitals ("ALL 3 ITEMS"): never read as a currency
CODES = sorted(PAYOUT_CURRENCIES - {"ALL"})
SYMBOLS = {"US$": "USD", "S$": "SGD", "SG$": "SGD", "HK$": "HKD", "A$": "AUD", "AU$": "AUD", "NZ$": "NZD",
           "C$": "CAD", "CA$": "CAD", "Rp": "IDR", "RM": "MYR", "$": "USD", "€": "EUR", "£": "GBP",
           "₹": "INR", "₱": "PHP", "฿": "THB", "₩": "KRW", "₫": "VND"}
_LETTERS = {s for s in SYMBOLS if s[0].isalpha()}  # Rp, RM: only as a word of their own


def _alt(s: str) -> str:
    e = re.escape(s) + (r"\.?" if s == "Rp" else "")
    return rf"(?<![A-Za-z]){e}" if s in _LETTERS else e


SYMBOL_PATTERN = "|".join(_alt(s) for s in sorted(SYMBOLS, key=len, reverse=True))
CODE_PATTERN = rf"(?<![A-Za-z])(?:{'|'.join(CODES)})(?![A-Za-z])"


def code(written: str | None) -> str | None:
    """The ISO code for a currency as written ("S$", "sgd", "Rp.", "SGD"), or None."""
    if not written:
        return None
    s = written.strip().rstrip(".")
    if s in SYMBOLS:
        return SYMBOLS[s]
    u = s.upper()
    return u if u in CODES else SYMBOLS.get(u)
