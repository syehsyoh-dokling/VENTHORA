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


PATH_HORIZONS = (1, 2, 3)  # business days: the 72-hour Kit 01 window


def fx_path_factors(currencies: tuple[str, ...], base: str = "USD", n_draws: int = N_DRAWS,
                    path: Path | None = None) -> dict:
    """Joint Horizon M0 paths for several currencies over 1..3 business days.

    Returns {"available", "factors": ndarray [n_draws, len(currencies), len(PATH_HORIZONS)] of
    (base cost of 1 unit at day d) / spot, "spot": {...}, "seed", ...}. The draws are joint across
    currencies and days (M0's cross-horizon, cross-asset covariance), seeded, and cached per day.
    """
    path = path or ecb_path()
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        stamp = None
    result = _cached_paths(str(path), stamp, tuple(sorted(currencies)), base, n_draws)
    return dict(result) | {"factors": result["factors"]}


@functools.lru_cache(maxsize=16)
def _cached_paths(path: str, _mtime_ns, currencies: tuple[str, ...], base: str, n_draws: int) -> dict:
    series = {c: base_cost_series(c, base, Path(path)) for c in currencies}
    short = [c for c, s in series.items() if len(s) < MIN_OBSERVATIONS]
    if short or not currencies:
        return {"available": False, "engine": "horizon-m0", "factors": None,
                "reason": f"needs at least {MIN_OBSERVATIONS} ECB observations for {short or 'the pair'}"}
    assets = {c: f"{c}{base}" for c in currencies}
    asof = min(s.index[-1] for s in series.values())
    card = {"task": {"id": f"venthora-paths-{'-'.join(currencies)}-{asof.date()}"},
            "provenance": {"data_cutoff": str(asof.date())},
            "targets": {"asset_ids": list(assets.values()), "horizons": list(PATH_HORIZONS), "target_type": "level"}}
    with tempfile.TemporaryDirectory(prefix="venthora-m0-") as tmp:
        frames = [pd.DataFrame({"date": s.index, "asset": assets[c], "value": s.to_numpy()}) for c, s in series.items()]
        pd.concat(frames).to_parquet(Path(tmp) / "panel.parquet", index=False)
        samples, cells, info = m0_exact.draws(card, Path(tmp), n_draws=n_draws)
    spot = {c: float(s[s.index <= asof].iloc[-1]) for c, s in series.items()}
    factors = np.empty((n_draws, len(currencies), len(PATH_HORIZONS)))
    for j, (asset, horizon) in enumerate(cells):
        ccy = next(c for c, a in assets.items() if a == asset)
        factors[:, currencies.index(ccy), PATH_HORIZONS.index(horizon)] = samples[:, j] / spot[ccy]
    finite = bool(np.isfinite(factors).all() and (factors > 0).all())
    if not finite or asof.date() > dt.date.today():
        return {"available": False, "engine": "horizon-m0", "factors": None, "reason": "Horizon self-check failed"}
    return {"available": True, "engine": "horizon-m0", "factors": factors, "currencies": list(currencies),
            "horizons_business_days": list(PATH_HORIZONS), "spot": spot, "as_of": str(asof.date()),
            "n_draws": n_draws, "seed": info["seed"],
            "method": "Horizon M0 joint paths (cross-currency, cross-horizon covariance), seeded"}
