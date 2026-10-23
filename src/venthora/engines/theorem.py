"""Theorem: which verified obligations to pay now, convert for, defer or escalate.

Optimization model (solved exactly):
    maximize   sum_i score_i * x_i               (strict priority, then earliest due)
    subject to base_paid + sum_c cost_c(shortfall_c) <= base_balance - reserve - stress
               shortfall_c = max(0, need_c - balance_c)  for each foreign currency c
Uses OR-Tools CP-SAT when installed, otherwise exact enumeration (n <= 12).
Unpaid obligations are deferred only when a reliable receipt lands before they
are due; otherwise they are escalated to a person.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from itertools import product
from time import perf_counter

from ..domain.models import Obligation, ObligationDecision, StageResult, TreasuryState
from ..domain.money import PPM, cost_in_base
from ..domain.projection import obligation_needs
from .horizon import HorizonResult


@dataclass
class TheoremResult:
    selected: list[str]
    decisions: list[ObligationDecision]
    solver: str
    budget_minor: int
    stage: StageResult


def _scores(items: list[Obligation]) -> list[int]:
    # One obligation of priority p outweighs every lower-priority combination.
    base = len(items) + 1
    return [(base ** o.priority) * 1000 * base + (999 - min(o.due_in_hours, 999)) for o in items]


def _cost(state: TreasuryState, items: list[Obligation], bits) -> int:
    need: dict[str, int] = {}
    for o, b in zip(items, bits, strict=True):
        if b:
            for ccy, amount in obligation_needs(o, state).items():
                need[ccy] = need.get(ccy, 0) + amount
    total = need.pop(state.base_currency, 0)
    for ccy, amount in need.items():
        total += cost_in_base(max(0, amount - state.balance(ccy)), state.ppm_up(ccy))
    return total


def _solve_exact(state: TreasuryState, items: list[Obligation], budget: int) -> tuple[list[str], str]:
    scores = _scores(items)
    best, best_bits = -1, ()
    for bits in product((0, 1), repeat=len(items)):
        score = sum(s for s, b in zip(scores, bits, strict=True) if b)
        if score > best and _cost(state, items, bits) <= budget:
            best, best_bits = score, bits
    return [o.obligation_id for o, b in zip(items, best_bits, strict=True) if b], "exact-enumeration"


def _solve_cp_sat(state: TreasuryState, items: list[Obligation], budget: int) -> tuple[list[str], str]:
    from ortools.sat.python import cp_model

    model = cp_model.CpModel()
    x = [model.new_bool_var(f"x_{i}") for i in range(len(items))]
    needs = [obligation_needs(o, state) for o in items]
    base_terms = [n.get(state.base_currency, 0) * xi for n, xi in zip(needs, x, strict=True)]
    currencies = sorted({c for n in needs for c in n} - {state.base_currency})
    for ccy in currencies:
        total = sum(n.get(ccy, 0) for n in needs)
        shortfall = model.new_int_var(0, total, f"short_{ccy}")
        model.add(shortfall >= sum(n.get(ccy, 0) * xi for n, xi in zip(needs, x, strict=True)) - state.balance(ccy))
        cost = model.new_int_var(0, cost_in_base(total, state.ppm_up(ccy)), f"cost_{ccy}")
        model.add(cost * PPM >= shortfall * state.ppm_up(ccy))  # integer ceiling
        base_terms.append(cost)
    model.add(sum(base_terms) <= budget)
    model.maximize(sum(s * xi for s, xi in zip(_scores(items), x, strict=True)))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 2.0
    solver.parameters.num_search_workers = 1
    status = solver.solve(model)
    if status != cp_model.OPTIMAL:
        raise RuntimeError(f"CP-SAT status {solver.status_name(status)}")
    return [o.obligation_id for o, xi in zip(items, x, strict=True) if solver.value(xi)], "ortools-cp-sat-optimal"


async def theorem(state: TreasuryState, eligible: dict[str, Obligation], rejected: dict[str, str],
                  risk: HorizonResult) -> TheoremResult:
    started = perf_counter()
    budget = max(0, state.balance(state.base_currency) - state.policy.reserve_floor_minor - risk.stress_minor)
    items = sorted(eligible.values(), key=lambda o: o.obligation_id)
    try:
        import ortools  # noqa: F401
    except ImportError:
        selected, solver = await asyncio.to_thread(_solve_exact, state, items, budget)
    else:
        selected, solver = await asyncio.to_thread(_solve_cp_sat, state, items, budget)

    decisions = classify(state, selected, eligible, rejected, risk)
    summary = f"pay {len(selected)} of {len(eligible)} verified obligations within {budget} minor budget"
    return TheoremResult(selected, decisions, solver, budget, StageResult(
        engine="Theorem · optimizer", status="INFO", summary=summary,
        details={"selected": selected, "budget_minor": budget, "solver": solver,
                 "actions": {d.obligation_id: d.action for d in decisions}},
        duration_ms=(perf_counter() - started) * 1000))


def classify(state, selected, eligible, rejected, risk) -> list[ObligationDecision]:
    chosen = set(selected)
    decisions = []
    reliable = [r for r in risk.receivables if r.reliable]
    committed = 0

    for o in sorted(state.obligations, key=lambda x: (-x.priority, x.due_in_hours, x.obligation_id)):
        base_cost = _cost(state, [o], (1,))
        if o.obligation_id in rejected:
            decisions.append(ObligationDecision(obligation_id=o.obligation_id, action="REJECT",
                             amount_minor=o.amount_minor, currency=o.currency, base_cost_minor=base_cost,
                             reason=rejected[o.obligation_id]))
            continue
        if o.obligation_id in chosen:
            foreign = any(c != state.base_currency for c in obligation_needs(o, state))
            decisions.append(ObligationDecision(obligation_id=o.obligation_id,
                             action="CONVERT_AND_PAY" if foreign else "PAY",
                             amount_minor=o.amount_minor, currency=o.currency, base_cost_minor=base_cost,
                             reason=f"priority {o.priority}, due in {o.due_in_hours}h; fits above reserve floor"))
            continue
        inflow = sum(r.base_value_minor for r in reliable if r.expected_in_hours <= o.due_in_hours)
        if inflow - committed >= base_cost:
            committed += base_cost
            source = ", ".join(r.receivable_id for r in reliable if r.expected_in_hours <= o.due_in_hours)
            decisions.append(ObligationDecision(obligation_id=o.obligation_id, action="DEFER",
                             amount_minor=o.amount_minor, currency=o.currency, base_cost_minor=base_cost,
                             reason=f"no room above reserve now; reliable receipt {source} lands before due"))
        else:
            decisions.append(ObligationDecision(obligation_id=o.obligation_id, action="ESCALATE",
                             amount_minor=o.amount_minor, currency=o.currency, base_cost_minor=base_cost,
                             reason="cannot be funded before due without breaching reserve; needs a person"))
    return decisions
