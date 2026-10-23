"""Probabilistic reserve proof: a discrete-event simulation of the planning window.

The deterministic plan proves the reserve holds *today*, at today's rates, if every reliable
receipt lands. This module asks the question a treasurer actually has: over the next 72 hours,
with FX moving and receipts uncertain, how likely is it that the reserve is breached at any
moment, and that a deferred obligation cannot be funded when it falls due?

Events on the clock (hours from now), applied to every path at once (vectorised numpy):
  t=0          the plan's payments and conversions (cost fixed by the plan)
  receipts     each receivable arrives with probability = its confidence (the payer's posterior
               of keeping promises), at a time drawn uniformly in its 90% arrival window;
               otherwise it does not arrive inside the window
  due dates    a deferred obligation is paid at its due time if, on that path, cash is still
               above the floor after the payment, converting at that path's FX rate
  stress       the policy's unexpected outflow lands at a uniformly random hour, with an uncertain
               size (lognormal, median = the budgeted amount, sigma 0.5)
FX rates per path come from the original Horizon M0 engine: joint draws for day 1, 2 and 3,
interpolated to the hour. Without ECB history the rates stay at spot and the result says so.

Honest performance accounting: the schedule holds a handful of events; the work is the paths.
Throughput is reported as path-events per second (one event applied to one path), measured
with a wall clock around the whole run. It is vectorised Python, not a C kernel.
"""
from __future__ import annotations

import heapq
import zlib
from dataclasses import dataclass
from time import perf_counter

import numpy as np

from ..domain.models import TreasuryState
from ..domain.money import PPM
from ..domain.projection import obligation_needs, project
from ..engines import horizon_m0

PAYING = ("PAY", "CONVERT_AND_PAY")
HOURS_PER_BUSINESS_DAY = 24
STRESS_SIGMA = 0.5  # lognormal sigma of the unexpected-outflow size around the budgeted amount


@dataclass
class Simulation:
    available: bool
    paths: int
    breach_probability_bp: int
    unfunded_probability_bp: dict[str, int]
    min_balance_p05_minor: int
    min_balance_p50_minor: int
    events: int
    path_events: int
    elapsed_ms: float
    fx_setup_ms: float
    loop_ms: float
    path_events_per_second: int  # measured over the event loop only (wall clock), vectorised numpy
    fx_model: str
    note: str

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def _fx_at(factors: np.ndarray | None, ccy_index: int, hour: float, horizon_hours: int) -> np.ndarray | float:
    """Per-path FX multiplier at `hour`, linear between M0's day-1/2/3 draws (1.0 at hour 0)."""
    if factors is None:
        return 1.0
    days = np.array(horizon_m0.PATH_HORIZONS, dtype=float) * HOURS_PER_BUSINESS_DAY
    xs = np.concatenate(([0.0], days))
    ys = np.concatenate((np.ones((factors.shape[0], 1)), factors[:, ccy_index, :]), axis=1)
    h = min(max(hour, 0.0), float(days[-1]))
    j = int(np.searchsorted(xs, h, side="right")) - 1
    j = min(j, len(xs) - 2)
    w = (h - xs[j]) / (xs[j + 1] - xs[j])
    return ys[:, j] * (1 - w) + ys[:, j + 1] * w


def simulate(state: TreasuryState, decisions, stress_minor: int, receivable_views, *, n_paths: int | None = None,
             seed: int | None = None) -> Simulation:
    started = perf_counter()
    base = state.base_currency
    floor = state.policy.reserve_floor_minor
    horizon = state.policy.planning_horizon_hours
    n = n_paths or state.policy.simulation_paths
    rng = np.random.default_rng(seed if seed is not None else zlib.crc32(state.case_id.encode()) & 0x7FFFFFFF)
    by_id = {o.obligation_id: o for o in state.obligations}

    # t = 0: the plan executes at the plan's own prices.
    paid_now = [d.obligation_id for d in decisions if d.action in PAYING]
    proj = project(state, paid_now)
    cash = np.full(n, float(proj.balances_after.get(base, state.balance(base))))
    foreign = {c: float(v) for c, v in proj.balances_after.items() if c != base}

    currencies = tuple(sorted({c for o in state.obligations for c in obligation_needs(o, state) if c != base}))
    fx_started = perf_counter()
    fx = horizon_m0.fx_path_factors(currencies, base) if currencies and state.use_ecb_stress else None
    fx_setup_ms = (perf_counter() - fx_started) * 1000  # M0 draw (cached per day); not counted in throughput
    factors = None
    fx_model = "spot (no FX uncertainty)"
    if fx and fx.get("available"):
        f = fx["factors"]
        factors = f if f.shape[0] >= n else f[rng.integers(0, f.shape[0], size=n)]
        factors = factors[:n]
        fx_model = f"Horizon M0 joint paths, seed {fx['seed']}, as of {fx['as_of']}"

    # Event schedule: (hour, order, kind, payload). A small heap: the work is the paths.
    schedule: list[tuple[float, int, str, object]] = []
    order = 0
    receipt_hour: dict[str, np.ndarray] = {}
    for r in receivable_views:
        rv = next(x for x in state.receivables if x.receivable_id == r.receivable_id)
        lo, hi = (rv.arrival_window_hours or [rv.expected_in_hours, rv.expected_in_hours])
        arrives = rng.random(n) < rv.confidence_bp / 10_000
        hour = np.where(arrives, rng.uniform(lo, max(lo, hi), size=n), np.inf)
        receipt_hour[r.receivable_id] = hour
        for h in sorted({float(x) for x in np.unique(np.floor(hour[np.isfinite(hour)]))}):
            heapq.heappush(schedule, (h, order, "receipt", (r.receivable_id, h)))
            order += 1
    deferred = [d for d in decisions if d.action == "DEFER"]
    for d in deferred:
        heapq.heappush(schedule, (float(by_id[d.obligation_id].due_in_hours), order, "due", d.obligation_id))
        order += 1
    if stress_minor:
        stress_hour = rng.uniform(0, horizon, size=n)
        # The policy's unexpected outflow is a budget, not a forecast: its real size is uncertain.
        # Lognormal with median = the budgeted amount and sigma 0.5 (p95 about 2.3x). The plan
        # covers the median deterministically; the simulation asks how often a larger shock,
        # landing before the receipts, breaches the floor.
        stress_amount = stress_minor * np.exp(rng.normal(0.0, STRESS_SIGMA, size=n))
        for h in sorted({float(x) for x in np.unique(np.floor(stress_hour))}):
            heapq.heappush(schedule, (h, order, "stress", h))
            order += 1

    min_cash = cash.copy()
    unfunded = {d.obligation_id: np.zeros(n, dtype=bool) for d in deferred}
    events = 0
    path_events = 0
    loop_started = perf_counter()
    while schedule:
        hour, _, kind, payload = heapq.heappop(schedule)
        if hour > horizon:
            break
        events += 1
        if kind == "receipt":
            rid, h = payload
            mask = np.floor(receipt_hour[rid]) == h
            rv = next(x for x in state.receivables if x.receivable_id == rid)
            amount = rv.amount_minor * (state.ppm_down(rv.currency) / PPM)
            cash = cash + np.where(mask, amount, 0.0)
            path_events += int(mask.sum())
        elif kind == "stress":
            mask = np.floor(stress_hour) == payload
            cash = cash - np.where(mask, stress_amount, 0.0)
            path_events += int(mask.sum())
        else:  # a deferred obligation falls due: pay if the path can, converting at that path's rate
            o = by_id[payload]
            cost = np.zeros(n)
            for ccy, amount in obligation_needs(o, state).items():
                if ccy == base:
                    cost += amount
                else:
                    short = max(0.0, amount - foreign.get(ccy, 0.0))
                    cost += short * (state.ppm_up(ccy) / PPM) * _fx_at(factors, currencies.index(ccy), hour, horizon)
            can_pay = cash - cost >= floor
            cash = np.where(can_pay, cash - cost, cash)
            unfunded[payload] = ~can_pay
            path_events += n
        min_cash = np.minimum(min_cash, cash)

    loop = perf_counter() - loop_started
    elapsed = perf_counter() - started
    note = ("receipts arrive with probability = confidence, uniformly in their 90% window; deferred obligations "
            "are paid at due only if the path stays above the floor; FX per path from Horizon M0" if factors is not None
            else "FX held at spot: run scripts/import_ecb.py and set use_ecb_stress for Horizon paths")
    return Simulation(
        available=True, paths=n, breach_probability_bp=int(round(float((min_cash < floor).mean()) * 10_000)),
        unfunded_probability_bp={k: int(round(float(v.mean()) * 10_000)) for k, v in unfunded.items()},
        min_balance_p05_minor=int(np.quantile(min_cash, 0.05)), min_balance_p50_minor=int(np.quantile(min_cash, 0.5)),
        events=events, path_events=path_events, elapsed_ms=elapsed * 1000, fx_setup_ms=fx_setup_ms,
        loop_ms=loop * 1000, path_events_per_second=int(path_events / loop) if loop > 0 else 0,
        fx_model=fx_model, note=note)
