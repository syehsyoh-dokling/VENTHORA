"""ECB euro reference rates: empirical adverse-move stress for Horizon.

The file is imported with `python scripts/import_ecb.py`; nothing is synthesized
when it is missing. Reference rates are a risk benchmark, never an executable
quote; execution always uses a fresh Airwallex quote.
"""
from __future__ import annotations

import csv
import hashlib
import math
import os
from datetime import datetime
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path

from ..config import ROOT

DEFAULT_PATH = ROOT / "data" / "imports" / "eurofxref-hist.csv"
MIN_OBSERVATIONS = 30


def ecb_path() -> Path:
    return Path(os.getenv("VENTHORA_ECB_CSV", str(DEFAULT_PATH)))


def load_rates(path: Path | None = None) -> list[tuple[str, dict[str, Decimal]]]:
    """Rows of (date, {currency: units per 1 EUR}), oldest first."""
    path = path or ecb_path()
    if not path.is_file():
        return []
    out = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            row = {str(k).strip(): (v or "").strip() for k, v in row.items() if k}
            date = row.pop("Date", "")
            try:
                datetime.strptime(date, "%Y-%m-%d")
            except ValueError:
                continue
            rates = {}
            for ccy, raw in row.items():
                try:
                    value = Decimal(raw)
                except InvalidOperation:
                    continue
                if value.is_finite() and value > 0:
                    rates[ccy.upper()] = value
            rates["EUR"] = Decimal(1)
            out.append((date, rates))
    return sorted(out)


def fx_stress_ppm(currency: str, base: str = "USD", horizon_days: int = 5,
                  quantile: float = 0.95, path: Path | None = None) -> dict:
    """p95 of overlapping `horizon_days` increases in the base cost of 1 unit of `currency`."""
    path = path or ecb_path()
    series = []
    for date, rates in load_rates(path):
        if currency in rates and base in rates:
            series.append((date, rates[base] / rates[currency]))  # base units per 1 currency
    if len(series) < MIN_OBSERVATIONS:
        return {"available": False, "stress_ppm": 0, "observations": len(series),
                "reason": f"needs at least {MIN_OBSERVATIONS} ECB observations"}
    moves = sorted(max(Decimal(0), series[i][1] / series[i - horizon_days][1] - 1)
                   for i in range(horizon_days, len(series)))
    pick = moves[min(len(moves) - 1, max(0, math.ceil(len(moves) * quantile) - 1))]
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"available": True, "stress_ppm": int((pick * 1_000_000).to_integral_value(rounding=ROUND_CEILING)),
            "observations": len(series), "as_of": series[-1][0], "pair": f"{currency}/{base}",
            "method": f"p{int(quantile * 100)} adverse {horizon_days}-day move", "sha256": digest}
