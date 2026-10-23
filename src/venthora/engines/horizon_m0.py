"""Bridge to the original Horizon scenario engine (`originals/horizon_core/m0_exact.py`).

Horizon's M0 engine fits trailing-window drift and covariance on a level series and draws
seeded joint paths. Here the series is the base-currency cost of one unit of a foreign
currency (e.g. USD per EUR), built from ECB reference rates. The FX stress is the p95
*adverse* move over the payment horizon: how much more the planned conversion could cost
by the time it is booked.
"""
from __future__ import annotations

import datetime as dt
import functools
import math
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from ..data.ecb import MIN_OBSERVATIONS, ecb_path, load_rates
from ..originals.horizon_core import m0_exact

N_DRAWS = 2000


def base_cost_series(currency: str, base: str, path: Path | None = None) -> pd.Series:
    rows = [(date, rates[base] / rates[currency]) for date, rates in load_rates(path)
            if currency in rates and base in rates]
    return pd.Series([float(v) for _, v in rows], index=pd.to_datetime([d for d, _ in rows]), dtype=float)


def fx_stress_ppm(currency: str, base: str = "USD", horizon_days: int = 3, quantile: float = 0.95,
                  path: Path | None = None) -> dict:
    """p95 adverse relative move of `currency` priced in `base` after `horizon_days` business days.

    ECB history changes once a day, and the engine is seeded, so the result is a pure function of
    (file, mtime, pair, horizon, quantile): cached so the hot path costs microseconds, not the
    ~0.4 s of parsing the CSV and drawing paths."""
    path = path or ecb_path()
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        stamp = None
    return dict(_cached_stress(str(path), stamp, currency, base, horizon_days, quantile))


@functools.lru_cache(maxsize=64)
def _cached_stress(path: str, _mtime_ns, currency: str, base: str, horizon_days: int, quantile: float) -> dict:
    return _compute_stress(currency, base, horizon_days, quantile, Path(path))


def _compute_stress(currency: str, base: str, horizon_days: int, quantile: float, path: Path) -> dict:
    series = base_cost_series(currency, base, path)
    if len(series) < MIN_OBSERVATIONS:
        return {"available": False, "stress_ppm": 0, "observations": len(series), "engine": "horizon-m0",
                "reason": f"needs at least {MIN_OBSERVATIONS} ECB observations"}
    asset = f"{currency}{base}"
    asof = series.index[-1]
    card = {"task": {"id": f"venthora-{asset}-{asof.date()}"},
            "provenance": {"data_cutoff": str(asof.date())},
            "targets": {"asset_ids": [asset], "horizons": [horizon_days], "target_type": "level"}}
    with tempfile.TemporaryDirectory(prefix="venthora-m0-") as tmp:
        panel = pd.DataFrame({"date": series.index, "asset": asset, "value": series.to_numpy()})
        panel.to_parquet(Path(tmp) / "panel.parquet", index=False)
        samples, _cells, info = m0_exact.draws(card, Path(tmp), n_draws=N_DRAWS)
    last = float(series.iloc[-1])
    moves = samples[:, 0] / last - 1.0
    adverse = max(0.0, float(np.quantile(moves, quantile)))
    # Horizon's self-check before anything is used: finite draws, monotone quantiles, no data after
    # today (embargo). A failed check makes the stress unavailable, which fails the plan closed.
    q05, q50, q95 = (float(np.quantile(samples[:, 0], q)) for q in (0.05, 0.5, 0.95))
    check = {"finite": bool(np.isfinite(samples).all()), "monotone_quantiles": q05 <= q50 <= q95,
             "no_future_data": asof.date() <= dt.date.today()}
    if not all(check.values()):
        return {"available": False, "stress_ppm": 0, "engine": "horizon-m0", "self_check": check,
                "reason": "Horizon self-check failed: " + ", ".join(k for k, v in check.items() if not v)}
    return {"available": True, "stress_ppm": math.ceil(adverse * 1_000_000), "engine": "horizon-m0", "self_check": check,
            "pair": f"{currency}/{base}", "as_of": str(asof.date()), "horizon_business_days": horizon_days,
            "observations": int(info["window_rows"][asset]), "n_draws": N_DRAWS, "seed": info["seed"],
            "spot": last, "p05_p50_p95": [q05, q50, q95],
            "method": f"Horizon M0 joint paths, p{int(quantile * 100)} adverse move at {horizon_days} business days"}
