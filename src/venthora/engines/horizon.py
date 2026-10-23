"""Horizon: what could change before the money moves, and how much does it cost?

Two independent risk inputs:
* liquidity stress  - worst supplied scenario outflow, plus an optional empirical
  FX add-on from ECB history on the base-equivalent foreign-currency exposure;
* receivable risk   - which expected receipts are confident enough to rely on
  for deferring an obligation. Forecasts never count as spendable cash.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import perf_counter

from ..data.ecb import fx_stress_ppm
from ..domain.models import Obligation, StageResult, TreasuryState
from ..domain.money import apply_ppm_up, cost_in_base, value_in_base
from ..domain.projection import obligation_needs
from . import horizon_m0


@dataclass
class ReceivableView:
    receivable_id: str
    payer: str
    base_value_minor: int
    expected_in_hours: int
    confidence_bp: int
    reliable: bool


@dataclass
class HorizonResult:
    stress_minor: int
    receivables: list[ReceivableView]
    low_confidence: bool
    stage: StageResult


async def horizon(state: TreasuryState, eligible: dict[str, Obligation] | None = None) -> HorizonResult:
    started = perf_counter()
    policy = state.policy
    stress = max((s.unexpected_outflow_minor for s in state.scenarios), default=0)
    status, notes, ecb = "INFO", [], None

    if state.use_ecb_stress:
        pool = eligible if eligible is not None else {o.obligation_id: o for o in state.obligations}
        exposure: dict[str, int] = {}
        for o in pool.values():
            for ccy, amount in obligation_needs(o, state).items():
                if ccy != state.base_currency:
                    exposure[ccy] = exposure.get(ccy, 0) + amount
        ecb = {}
        model = horizon_m0.fx_stress_ppm if state.fx_stress_model == "horizon_m0" else fx_stress_ppm
        for ccy, amount in sorted(exposure.items()):
            try:
                result = await asyncio.to_thread(model, ccy, state.base_currency)
            except Exception as exc:  # the original engine refused the series: fail closed
                result = {"available": False, "stress_ppm": 0, "reason": f"{type(exc).__name__}: {exc}"}
            ecb[ccy] = result
            if not result["available"]:
                status = "FAIL"
                notes.append(f"ECB history for {ccy}/{state.base_currency} unavailable; planning blocked")
                continue
            add_on = apply_ppm_up(cost_in_base(amount, state.ppm_up(ccy)), result["stress_ppm"])
            stress += add_on
            notes.append(f"{ccy} exposure: +{add_on} minor stress ({result.get('method', 'p95 adverse move')})")

    views = []
    for r in sorted(state.receivables, key=lambda x: x.expected_in_hours):
        reliable = (r.confidence_bp >= policy.min_receivable_confidence_bp
                    and r.expected_in_hours <= policy.planning_horizon_hours)
        views.append(ReceivableView(r.receivable_id, r.payer, value_in_base(r.amount_minor, state.ppm_down(r.currency)),
                                    r.expected_in_hours, r.confidence_bp, reliable))
    low_confidence = any(not v.reliable for v in views)
    if low_confidence and status != "FAIL":
        status = "WARN"

    summary = f"stress buffer {stress} minor; {sum(v.reliable for v in views)}/{len(views)} receipts reliable"
    return HorizonResult(stress, views, low_confidence, StageResult(
        engine="Horizon · risk", status=status, summary=summary,
        details={"stress_minor": stress, "scenarios": [s.model_dump() for s in state.scenarios],
                 "receivables": [v.__dict__ for v in views], "ecb": ecb, "notes": notes},
        duration_ms=(perf_counter() - started) * 1000))
