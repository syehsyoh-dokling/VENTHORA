"""M0, the leaderboard's denominator, rebuilt from its published specification.

Source: Agenthon-2026/track2-forecasting-public docs/M0-BASELINE.md (published 2026-09-18). Every
card's score is our composite divided by M0's on that card, so a forecast equal to M0's draws
scores 1.000000 (the document measured exactly that on all 103 released cards). The earlier
`m0.py` vendored the reference CLI, which the same document says is NOT M0 (it averages ~1.29):
no drift on level targets, no cross-horizon path, full history, seed 0.

The procedure, section by section (section numbers are the document's):
  3.1 per asset: the first *.parquet (sorted filename) holding the asset; rows <= as-of, last 300
  3.2 steps: `level` -> first differences; `log_return` -> the rows themselves
  3.3 gap rule: drop a step spanning an interval > max(10 x median spacing, 5 days)
  3.4 steps indexed by date; the intersection of dates across assets
  3.5 mu = mean step, Sigma = numpy.cov (sample), both on that frame
  3.6 anchor: last observation (level) or 0.0 (log_return)
  3.7 horizon -> panel steps (the month-counting override on monthly panels)
  3.8 mean = anchor + s mu; cov[i,j] = min(s_i, s_j) Sigma; cholesky with the stated jitter
  3.9 seed = crc32(unit_id) & 0x7FFFFFFF; default_rng; Z (500 x d) in sorted (asset, horizon) order

One input M0 has and we do not: each cell's target date (sealed). 3.7 falls back to the declared
horizon without it; that is exact on daily panels, but on monthly panels M0 counts months to the
target date. We estimate the target date as as-of + horizon business days, which reproduces the
document's table for the four released monthly cards (8, 9 / 8, 9 / 2 / 2).
"""
from __future__ import annotations

import functools
import pathlib
import zlib
from typing import Any

import numpy as np
import pandas as pd

N_DRAWS = 500
WINDOW = 300


class M0Refused(RuntimeError):
    """The specification refuses this card (e.g. the log_return tripwire)."""


def _asset_rows(df: pd.DataFrame, asset: str) -> pd.DataFrame | None:
    for col in ("asset", "asset_id"):
        if col in df.columns:
            rows = df[df[col].astype(str) == asset]
            if len(rows):
                return rows
    return None


@functools.lru_cache(maxsize=64)
def full_series(panels_dir: pathlib.Path, asset: str) -> pd.Series:
    """The asset's whole date-sorted series from the first panel (sorted filename) holding it."""
    files = sorted(panels_dir.glob("*.parquet"), key=lambda p: p.name)
    if not files:
        files = sorted(panels_dir.rglob("*.parquet"), key=lambda p: p.name)
    for f in files:
        rows = _asset_rows(pd.read_parquet(f), asset)
        if rows is None:
            continue
        s = pd.Series(rows["value"].astype(float).to_numpy(),
                      index=pd.to_datetime(rows["date"]).to_numpy())
        return s.sort_index(kind="mergesort")
    raise M0Refused(f"asset {asset} is in no panel")


def history(panels_dir: pathlib.Path, asset: str, asof: pd.Timestamp) -> pd.Series:
    """3.1: date-indexed values of the trailing window (<= as-of, last 300)."""
    s = full_series(panels_dir, asset)
    return s[s.index <= asof].iloc[-WINDOW:]


def _hole(days: np.ndarray) -> float:
    return max(10.0 * float(np.median(days)), 5.0) if days.size else 5.0


def steps(s: pd.Series, target_type: str) -> pd.Series:
    """3.2 + 3.3: the step series, each step carrying the date of the row it ends on."""
    if len(s) < 2:
        return pd.Series(dtype=float)
    days = np.diff(s.index.values).astype("timedelta64[s]").astype(np.int64) / 86400.0
    keep = days <= _hole(days)
    vals = s.to_numpy()
    if target_type == "log_return":
        if float(np.median(np.abs(vals))) >= 0.2:
            raise M0Refused("log_return panel does not look like per-step returns")
        st = vals[1:]
    else:
        st = np.diff(vals)
    return pd.Series(st[keep], index=s.index[1:][keep])


def panel_steps(s: pd.Series, horizon: int, asof: pd.Timestamp) -> int:
    """3.7 with an ESTIMATED target date (as-of + horizon business days)."""
    idx = s.index
    if len(idx) < 3:
        return horizon
    target = asof + pd.offsets.BDay(horizon)
    days = np.diff(idx.values).astype("timedelta64[s]").astype(np.int64) / 86400.0
    ok = days[days <= _hole(days)]
    if not ok.size:
        return horizon
    spacing = float(np.mean(ok))
    last = idx[-1]
    if spacing > 20:
        n = 12 * (target.year - last.year) + (target.month - last.month)
    else:
        n = int(round((target - last).days / spacing))
    if n <= 0:
        return horizon
    ratio = max(n, horizon) / max(min(n, horizon), 1)
    return n if ratio > 2 else horizon


def _ewma_var(x: np.ndarray, lam: float) -> float:
    """RiskMetrics EWMA variance of demeaned steps, oldest to newest, seeded with the sample var."""
    x = x - x.mean()
    v = float(np.var(x, ddof=1)) if x.size > 1 else float(x[0] ** 2)
    for e in x:
        v = lam * v + (1.0 - lam) * float(e) * float(e)
    return v


def _apply_variant(X: np.ndarray, mu: np.ndarray, Sigma: np.ndarray, v: dict[str, Any]):
    """Changes to M0's drift and covariance. An empty variant is M0 exactly."""
    n = X.shape[0]
    drift = v.get("drift", "m0")
    if drift == "zero":
        mu = np.zeros_like(mu)
    elif isinstance(drift, (int, float)):
        mu = mu * float(drift)
    elif isinstance(drift, str) and drift.startswith("t"):
        kmin = float(drift[1:])
        se = np.sqrt(np.diag(Sigma) / max(n, 1))
        t = np.divide(mu, se, out=np.zeros_like(mu), where=se > 0)
        mu = np.where(np.abs(t) >= kmin, mu, 0.0)
    if v.get("drift_recent"):
        # dev/backtest only: momentum centre - the mean of the most recent n changes, blended into
        # M0's 300-observation mean with weight w (M0 itself is w = 0)
        n_recent, w = v["drift_recent"]
        recent = X[-int(n_recent):].mean(axis=0)
        mu = w * recent + (1 - w) * mu
    if v.get("ewma"):
        lam, blend = v["ewma"]
        sd = np.sqrt(np.diag(Sigma))
        new = np.array([np.sqrt(blend * _ewma_var(X[:, j], lam) + (1 - blend) * sd[j] ** 2)
                        for j in range(X.shape[1])])
        r = np.divide(new, sd, out=np.ones_like(sd), where=sd > 0)
        Sigma = Sigma * np.outer(r, r)
    if v.get("widen"):
        Sigma = Sigma * float(v["widen"]) ** 2
    return mu, Sigma


def draws(card: dict[str, Any], panels_dir: pathlib.Path, n_draws: int = N_DRAWS,
          variant: dict[str, Any] | None = None):
    """M0's draws: (samples [n_draws, d], cells [(asset, horizon)], info).

    `variant` (dev/backtest) changes the drift, the covariance or the innovations; None is M0.
    The standard-normal matrix Z is always M0's own, so a variant differs from M0 only by what it
    changes, never by sampling noise."""
    v = variant or {}
    tgt = card["targets"]
    unit_id = str(card["task"]["id"])
    asof = pd.Timestamp(card["provenance"]["data_cutoff"])
    ttype = str(tgt.get("target_type", "level"))
    assets = sorted(str(a) for a in tgt["asset_ids"])
    horizons = sorted(int(h) for h in tgt["horizons"])

    hist = {a: history(panels_dir, a, asof) for a in assets}
    frame = pd.concat({a: steps(hist[a], ttype) for a in assets}, axis=1, join="inner").dropna()
    if len(frame) < 2:
        raise M0Refused("fewer than two aligned steps")
    X = frame[assets].to_numpy(dtype=float)
    mu = X.mean(axis=0)
    Sigma = np.atleast_2d(np.cov(X, rowvar=False))
    if v:
        mu, Sigma = _apply_variant(X, mu, Sigma, v)

    cells = [(a, h) for a in assets for h in horizons]
    ai = {a: i for i, a in enumerate(assets)}
    sc = {(a, h): panel_steps(hist[a], h, asof) for a, h in cells}
    anchor = {a: (0.0 if ttype == "log_return" else float(hist[a].iloc[-1])) for a in assets}
    d = len(cells)
    mean = np.array([anchor[a] + sc[(a, h)] * mu[ai[a]] for a, h in cells])
    cov = np.empty((d, d))
    for i, (a, h) in enumerate(cells):
        for j, (b, g) in enumerate(cells):
            cov[i, j] = min(sc[(a, h)], sc[(b, g)]) * Sigma[ai[a], ai[b]]
    # widen_gate=G (22 Oct): horizon-gated widening. A cell whose DECLARED horizon is <= G
    # business days keeps M0's own width (the `widen` factor is undone for that cell); cells
    # beyond G keep the variant's widening. Generic rule of the horizon only. Rationale: on the
    # 22 short-horizon practice cards M0's width is calibrated at 1-4 BD (rms z 0.94, 89% inside
    # the 90% band), while the family widenings were measured on long-horizon Dev cards.
    gate = v.get("widen_gate")
    w_all = float(v.get("widen", 1.0))
    if gate and w_all != 1.0:
        g_cell = np.array([1.0 / w_all if float(h) <= float(gate) else 1.0 for _a, h in cells])
        cov = cov * np.outer(g_cell, g_cell)
    cov = cov + 1e-10 * np.eye(d)
    try:
        L = np.linalg.cholesky(cov + 1e-9 * np.eye(d))
    except np.linalg.LinAlgError:
        L = np.diag(np.sqrt(np.diag(cov + 1e-9 * np.eye(d))))
    seed = zlib.crc32(unit_id.encode("utf-8")) & 0x7FFFFFFF
    rng = np.random.default_rng(seed)
    Z = rng.standard_normal((n_draws, d))
    shocks = Z @ L.T
    if v.get("t_nu"):
        # multivariate Student-t with the SAME covariance: scale each draw by sqrt((nu-2)/chi2_nu)
        nu = float(v["t_nu"])
        w = np.random.default_rng(seed + 1).chisquare(nu, size=(n_draws, 1))
        shocks = shocks * np.sqrt((nu - 2.0) / w)
    samples = mean + shocks
    info = {"seed": seed, "sigma_diag": np.diag(Sigma).tolist(), "steps": {f"{a}@{h}": sc[(a, h)] for a, h in cells},
            "n_aligned": int(len(frame)), "mu": mu.tolist(),
            "window_rows": {a: int(len(hist[a])) for a in assets}}
    return samples, cells, info


RATIONALE = "forecast_rationale.md"


def write(card: dict[str, Any], panels_dir: pathlib.Path, out: pathlib.Path,
          variant: dict[str, Any] | None = None,
          vol_mult: dict[str, float] | None = None,
          shift_sd: dict[str, float] | None = None,
          points: dict[tuple[str, int], float] | None = None,
          point_weight: float = 1.0, dir_scale: float = 1.0) -> dict[str, Any]:
    """forecast.parquet + forecast_meta.json + forecast_rationale.md from M0's own draws.

    `variant` (e.g. {"t_nu": 8}) changes the innovations; `vol_mult` {asset: m} rescales each
    asset's draws about their centre (a text-derived width, centre untouched). Both default to
    exact M0. Measured 25 Sep on the 71 live cards vs exact M0: t8 0.9956, text width 0.9845,
    both 0.9797 (both split halves below 1)."""
    import json

    samples, cells, info = draws(card, panels_dir, variant=variant)
    info["variant"] = dict(variant or {})
    info["vol_mult"] = {}
    info["centre_moves"] = {}
    # Centre moves (2 Oct), applied before the width so the width acts about the moved centre:
    #   points   {(asset, horizon): value} - the House model's own estimate for the cell; the
    #            centre goes `point_weight` of the way to it (a point further than 8 horizon sd
    #            from the centre is a unit slip and is ignored)
    #   shift_sd {asset: d in [-1, 1]}     - a document-based direction: centre + d x horizon sd
    if points or shift_sd:
        centre0, sd0 = samples.mean(axis=0), samples.std(axis=0)
        for j, (a, h) in enumerate(cells):
            move = 0.0
            pt = (points or {}).get((a, int(h)))
            if pt is not None and np.isfinite(pt) and abs(pt - centre0[j]) <= 8.0 * sd0[j]:
                move = float(point_weight) * (float(pt) - centre0[j])
                info["centre_moves"][f"{a}@{h}"] = "recall"
            elif shift_sd and a in shift_sd:
                move = float(dir_scale) * max(-1.0, min(1.0, float(shift_sd[a]))) * sd0[j]
                info["centre_moves"][f"{a}@{h}"] = "direction"
            samples[:, j] = samples[:, j] + move
    if vol_mult:
        centre = samples.mean(axis=0)
        for j, (a, _h) in enumerate(cells):
            m = float(vol_mult.get(a, 1.0))
            if m != 1.0:
                samples[:, j] = centre[j] + (samples[:, j] - centre[j]) * m
                info["vol_mult"][a] = m
    tgt = card["targets"]
    unit_id = str(card["task"]["id"])
    asof = str(card["provenance"]["data_cutoff"])
    n = samples.shape[0]
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({
        "draw": np.repeat(np.arange(n), len(cells)),
        "asset": [a for _ in range(n) for a, _h in cells],
        "horizon": [h for _ in range(n) for _a, h in cells],
        "value": samples.reshape(-1).astype(float),
    }).to_parquet(out, index=False)
    (out.parent / "forecast_meta.json").write_text(json.dumps({
        "unit_id": unit_id, "asof": asof, "representation": "samples",
        "asset_ids": list(tgt["asset_ids"]), "horizons": [int(h) for h in tgt["horizons"]],
        "n_draws": int(n), "target": tgt.get("target_type", "level"),
        "rationale": {"file": RATIONALE, "method": (
            "joint random walk (M0) with the centre and width informed by the House model"
            if (info["centre_moves"] or info["vol_mult"]) else
            "text-blind joint gaussian random walk (M0)")},
    }, indent=2) + "\n")
    steps_txt = ", ".join(f"{k} -> {v} panel steps" for k, v in info["steps"].items())
    (out.parent / RATIONALE).write_text(
        f"# Forecast rationale - {unit_id}\n\n"
        f"As-of {asof}. Method: the text-blind joint Gaussian random walk specified in the "
        f"organizers' M0-BASELINE.md - trailing {WINDOW} observations, drift and covariance of "
        f"the date-aligned steps, one path across horizons (cov = min(s_i, s_j) Sigma), "
        f"{n} draws from seed {info['seed']}.\n\n"
        f"Horizons: {steps_txt}. Aligned steps used: {info['n_aligned']}.\n\n"
        + ("No document from the corpus moved this forecast: it is the text-blind reference, "
           "submitted to anchor every card at the baseline before text-driven changes are "
           "measured against it.\n"
           if not (info["variant"] or info["vol_mult"] or info["centre_moves"]) else
           "Changes from M0: "
           + (f"Student-t innovations (nu={info['variant']['t_nu']:g}) with M0's covariance; "
              if info["variant"].get("t_nu") else "")
           + (f"drift setting {info['variant']['drift']}; " if "drift" in info["variant"] else "")
           + (f"centre moved on {len(info['centre_moves'])} cell(s) "
              f"({', '.join(sorted(set(info['centre_moves'].values())))}: the House model's "
              "reading of the dated documents and, where it recognises the period, its own "
              "estimate of the level); " if info["centre_moves"] else "centre otherwise M0's; ")
           + (f"width multipliers from the dated documents before the as-of {info['vol_mult']} "
              "(narrowing only where the documents state the driver as fact).\n"
              if info["vol_mult"] else "no document changed the width.\n")),
        encoding="utf-8")
    return info
