# ruff: noqa: E501
"""Limits and velocity: how much, to whom, how often — the numbers the policy (Python and Rego) reasons from.

For every payout the plan would make now (base-currency cost):
  single        cost > max_single_payment                      -> single_payment_above_limit
  per payee     paid to this payee this period + paying now > max_per_payee_period -> payee_period_limit_exceeded
  new payee     never PAID before and cost > max_first_payment -> first_payment_to_new_payee_above_limit
  daily         outflow last 24 h + paying now (all payees) > max_outflow_daily   -> daily_outflow_limit_exceeded
  weekly        outflow last 7 d + paying now  > max_outflow_weekly                -> weekly_outflow_limit_exceeded
History comes from the spend ledger (facts.py); without history every payee is new and the rolling outflows are zero.
Above a limit the agent may not act alone: REQUIRES_APPROVAL with the reason, the amounts and the payees named.
"""
from __future__ import annotations

from ..domain.models import ObligationDecision, TreasuryState

PAYING = ("PAY", "CONVERT_AND_PAY")


def limit_facts(state: TreasuryState, decisions: list[ObligationDecision]) -> dict:
    p = state.policy
    h = state.history
    by_id = {o.obligation_id: o for o in state.obligations}
    paying = [(by_id[d.obligation_id], d.base_cost_minor) for d in decisions if d.action in PAYING and d.obligation_id in by_id]
    per_payee_now: dict[str, int] = {}
    for o, cost in paying:
        per_payee_now[o.beneficiary_id] = per_payee_now.get(o.beneficiary_id, 0) + cost
    paid_before = set(h.payees_paid_before) if h else set()
    hist_period = dict(h.paid_per_payee_period_minor) if h else {}
    total_now = sum(cost for _, cost in paying)
    rows = [{"obligation_id": o.obligation_id, "beneficiary_id": o.beneficiary_id, "cost_minor": cost,
             "new_payee": o.beneficiary_id not in paid_before} for o, cost in paying]
    payees = [{"beneficiary_id": b, "paid_this_period_minor": hist_period.get(b, 0), "paying_now_minor": now,
               "total_minor": hist_period.get(b, 0) + now, "new_payee": b not in paid_before} for b, now in sorted(per_payee_now.items())]
    return {"max_single_payment_minor": p.max_single_payment_minor, "max_per_payee_period_minor": p.max_per_payee_period_minor,
            "max_outflow_daily_minor": p.max_outflow_daily_minor, "max_outflow_weekly_minor": p.max_outflow_weekly_minor,
            "max_first_payment_minor": p.max_first_payment_minor,
            "payouts": rows, "payees": payees, "paying_now_total_minor": total_now,
            "outflow_24h_minor": (h.outflow_24h_minor if h else 0), "outflow_7d_minor": (h.outflow_7d_minor if h else 0),
            "history_as_of": h.as_of if h else None}


def limit_reasons(f: dict) -> list[str]:
    """Mirrored one-to-one in policy/treasury.rego."""
    reasons = set()
    ms, mp, md, mw, mf = (f.get("max_single_payment_minor"), f.get("max_per_payee_period_minor"), f.get("max_outflow_daily_minor"),
                          f.get("max_outflow_weekly_minor"), f.get("max_first_payment_minor"))
    for r in f.get("payouts", []):
        if ms is not None and r["cost_minor"] > ms:
            reasons.add("single_payment_above_limit")
        if mf is not None and r["new_payee"] and r["cost_minor"] > mf:
            reasons.add("first_payment_to_new_payee_above_limit")
    for pr in f.get("payees", []):
        if mp is not None and pr["total_minor"] > mp:
            reasons.add("payee_period_limit_exceeded")
    now = f.get("paying_now_total_minor", 0)
    if md is not None and f.get("outflow_24h_minor", 0) + now > md:
        reasons.add("daily_outflow_limit_exceeded")
    if mw is not None and f.get("outflow_7d_minor", 0) + now > mw:
        reasons.add("weekly_outflow_limit_exceeded")
    return sorted(reasons)


def describe(f: dict, reasons: list[str]) -> str:
    bits = []
    if "single_payment_above_limit" in reasons:
        bits.append("single payout above " + str(f["max_single_payment_minor"]) + ": " + ", ".join(r["obligation_id"] for r in f["payouts"] if r["cost_minor"] > f["max_single_payment_minor"]))
    if "first_payment_to_new_payee_above_limit" in reasons:
        bits.append("first payment to a new payee above " + str(f["max_first_payment_minor"]) + ": " + ", ".join(r["obligation_id"] for r in f["payouts"] if r["new_payee"] and r["cost_minor"] > f["max_first_payment_minor"]))
    if "payee_period_limit_exceeded" in reasons:
        bits.append("payee over its period limit " + str(f["max_per_payee_period_minor"]) + ": " + ", ".join(f"{p['beneficiary_id']} ({p['total_minor']})" for p in f["payees"] if p["total_minor"] > f["max_per_payee_period_minor"]))
    if "daily_outflow_limit_exceeded" in reasons:
        bits.append(f"24h outflow {f['outflow_24h_minor']} + now {f['paying_now_total_minor']} > {f['max_outflow_daily_minor']}")
    if "weekly_outflow_limit_exceeded" in reasons:
        bits.append(f"7d outflow {f['outflow_7d_minor']} + now {f['paying_now_total_minor']} > {f['max_outflow_weekly_minor']}")
    return "; ".join(bits) if bits else "within every limit"
