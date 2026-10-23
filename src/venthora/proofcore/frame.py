"""Theorem's worked-problem frame, applied to a treasury decision.

Theorem refused to write code until it had a complete frame: Given (every input with its unit),
Asked (every deliverable), Formula (each derivation), Answer (the result plus the identities it
must satisfy), and a checker rejected incomplete frames. VENTHORA builds the same frame for every
plan and ProofCore re-checks it independently:

* every obligation has exactly one action,
* each conversion buys exactly the minimum needed (Kit 01: "converts only the minimum"),
* cost and balance-after identities recompute to the cent,
* and, as in SeismoForge's "not buildable as posed", when the critical obligations cannot all be
  funded the frame says so and states the minimum extra cash that would make it payable.
"""
from __future__ import annotations

from ..domain.models import Conversion, ObligationDecision, TreasuryState
from ..domain.money import PPM, fmt
from ..domain.projection import obligation_needs, project

PAYING = ("PAY", "CONVERT_AND_PAY")
ACTIONS = ("PAY", "CONVERT_AND_PAY", "DEFER", "ESCALATE", "REJECT")
CRITICAL_PRIORITY = 10


def build(state: TreasuryState, decisions: list[ObligationDecision], conversions: list[Conversion],
          stress_minor: int, eligible: set[str]) -> dict:
    base = state.base_currency
    floor = state.policy.reserve_floor_minor + stress_minor
    paid = [d.obligation_id for d in decisions if d.action in PAYING]
    proj = project(state, paid)
    after = proj.balances_after.get(base, state.balance(base))
    budget = max(0, state.balance(base) - floor)
    frame = {
        "given": {
            "balances_minor": dict(state.balances),
            "fx_rate_ppm_to_base": {c: state.ppm_up(c) for c in state.fx_to_base},
            "reserve_floor_minor": state.policy.reserve_floor_minor, "stress_minor": stress_minor,
            "obligations": [{"id": o.obligation_id, "amount_minor": o.amount_minor, "currency": o.currency,
                             "needs_minor": obligation_needs(o, state), "priority": o.priority,
                             "due_in_hours": o.due_in_hours, "eligible": o.obligation_id in eligible}
                            for o in state.obligations],
            "units": "integer minor units; rates in ppm of base per 1 unit",
        },
        "asked": {"one_action_per_obligation": [o.obligation_id for o in state.obligations],
                  "allowed_actions": list(ACTIONS), "minimum_conversions": "per foreign currency"},
        "formula": {
            "shortfall_c": "max(0, need_c - balance_c)",
            "cost_base": "need_base + sum_c ceil(shortfall_c * rate_ppm_c / 1e6)",
            "constraint": "cost_base <= balance_base - reserve_floor - stress",
            "objective": "maximise strict priority (cost of non-payment), then earliest due",
        },
        "answer": {
            "pay": paid,
            "conversions": [c.model_dump() for c in conversions],
            "cost_base_minor": proj.base_cost_minor,
            "base_balance_after_minor": after,
            "floor_minor": floor,
            "identities": [
                f"base_after = {state.balance(base)} - {proj.base_cost_minor} = {after}",
                f"base_after {'>=' if after >= floor else '<'} floor {floor}",
            ],
        },
    }
    unpaid_critical = [o for o in state.obligations
                       if o.priority >= CRITICAL_PRIORITY and o.obligation_id in eligible and o.obligation_id not in paid]
    if unpaid_critical:
        need = project(state, paid + [o.obligation_id for o in unpaid_critical]).base_cost_minor
        short = max(0, need - budget)
        frame["answer"]["not_payable_as_posed"] = {
            "critical_unpaid": [o.obligation_id for o in unpaid_critical],
            "minimum_additional_base_cash_minor": short,
            "statement": f"not payable as posed: critical obligations need at least {fmt(short, base)} more "
                         f"{base} above the reserve floor",
        }
    return frame


def check(state: TreasuryState, frame: dict, decisions: list[ObligationDecision]) -> list[str]:
    """Independent re-check of a frame. Returns problems; empty means the frame is complete and true."""
    problems: list[str] = []
    for section in ("given", "asked", "formula", "answer"):
        if not frame.get(section):
            problems.append(f"frame section '{section}' is missing")
    if problems:
        return problems
    ids = [d.obligation_id for d in decisions]
    expected = [o.obligation_id for o in state.obligations]
    if sorted(ids) != sorted(expected) or len(ids) != len(set(ids)):
        problems.append("not exactly one action per obligation")
    if any(d.action not in ACTIONS for d in decisions):
        problems.append("unknown action in decisions")

    base = state.base_currency
    answer = frame["answer"]
    paid = answer["pay"]
    needs: dict[str, int] = {}
    by_id = {o.obligation_id: o for o in state.obligations}
    for oid in paid:
        for ccy, amount in obligation_needs(by_id[oid], state).items():
            needs[ccy] = needs.get(ccy, 0) + amount
    cost = needs.get(base, 0)
    for conv in answer["conversions"]:
        ccy = conv["buy_currency"]
        minimum = max(0, needs.get(ccy, 0) - state.balance(ccy))
        if conv["buy_amount_minor"] != minimum:
            problems.append(f"{ccy} conversion buys {conv['buy_amount_minor']}, minimum needed is {minimum}")
        sell = -(-minimum * state.ppm_up(ccy) // PPM)
        if conv["estimated_sell_minor"] != sell:
            problems.append(f"{ccy} conversion cost {conv['estimated_sell_minor']} != {sell}")
        cost += sell
    for ccy, need in needs.items():
        if ccy != base and need > state.balance(ccy) and ccy not in {c["buy_currency"] for c in answer["conversions"]}:
            problems.append(f"{ccy} shortfall has no conversion")
    if cost != answer["cost_base_minor"]:
        problems.append(f"cost identity fails: recomputed {cost}, frame says {answer['cost_base_minor']}")
    if state.balance(base) - cost != answer["base_balance_after_minor"]:
        problems.append("balance-after identity fails")
    return problems
