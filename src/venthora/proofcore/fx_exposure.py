# ruff: noqa: E501
"""FX exposure limits: how much foreign currency a plan may leave open, sell in a day, and leave unhedged.

Per foreign currency, from the projection the gate already proves:
  open position   cash in that currency AFTER the plan (held + bought − paid) > max_open_position[ccy]   -> open_position_above_cap
  hedge ratio     (cash held before + bought now) / obligations in that currency due inside the planning horizon
                  < min_hedge_ratio                                                                        -> hedge_ratio_below_minimum
                  (obligations that are paid are covered by construction; a deferred or escalated foreign obligation is the
                  exposure; with nothing due in a currency there is nothing to hedge)
And across currencies:
  daily sales     base sold in conversions over the last 24 h (every book, from the journal) + this plan's conversions
                  > max_conversion_daily                                                                    -> daily_conversion_cap_exceeded
A limit that is not set does not apply. Above a limit the agent may not act alone: REQUIRES_APPROVAL with the numbers named.
"""
from __future__ import annotations

from ..domain.models import ObligationDecision, TreasuryState
from ..domain.projection import Projection


def fx_facts(state: TreasuryState, decisions: list[ObligationDecision], proj: Projection) -> dict:
    p = state.policy
    base = state.base_currency
    horizon = p.planning_horizon_hours
    rejected = {d.obligation_id for d in decisions if d.action == "REJECT"}
    bought: dict[str, int] = {}
    sold_base = 0
    for c in proj.conversions:
        bought[c.buy_currency] = bought.get(c.buy_currency, 0) + c.buy_amount_minor
        if c.sell_currency == base:
            sold_base += c.estimated_sell_minor
    rows = []
    for ccy in sorted(k for k in state.balances if k != base):
        # A rejected obligation (a duplicate, an unverified claim) is owed by nobody: it is not an exposure to hedge.
        due = sum(o.amount_minor for o in state.obligations
                  if o.currency == ccy and o.record_kind == "payable" and o.due_in_hours <= horizon
                  and o.obligation_id not in rejected)
        held = state.balance(ccy)
        covered = held + bought.get(ccy, 0)
        coverage_bp = None if due == 0 else min(10_000, covered * 10_000 // due)
        rows.append({"currency": ccy, "held_before_minor": held, "bought_now_minor": bought.get(ccy, 0),
                     "position_after_minor": proj.balances_after.get(ccy, held), "due_in_horizon_minor": due,
                     "coverage_bp": coverage_bp, "cap_minor": p.max_open_position_minor.get(ccy)})
    h = state.history
    return {"rows": rows, "min_hedge_ratio_bp": p.min_hedge_ratio_bp, "max_conversion_daily_minor": p.max_conversion_daily_minor,
            "converted_24h_minor": h.converted_24h_minor if h else 0, "converting_now_minor": sold_base,
            "horizon_hours": horizon, "base_currency": base}


def fx_reasons(f: dict) -> list[str]:
    """Mirrored one-to-one in policy/treasury.rego."""
    reasons = set()
    for r in f.get("rows", []):
        if r.get("cap_minor") is not None and r["position_after_minor"] > r["cap_minor"]:
            reasons.add("open_position_above_cap")
        if (f.get("min_hedge_ratio_bp") is not None and r.get("coverage_bp") is not None
                and r["coverage_bp"] < f["min_hedge_ratio_bp"]):
            reasons.add("hedge_ratio_below_minimum")
    cap = f.get("max_conversion_daily_minor")
    if cap is not None and f.get("converted_24h_minor", 0) + f.get("converting_now_minor", 0) > cap:
        reasons.add("daily_conversion_cap_exceeded")
    return sorted(reasons)


def active(f: dict) -> bool:
    return (f.get("min_hedge_ratio_bp") is not None or f.get("max_conversion_daily_minor") is not None
            or any(r.get("cap_minor") is not None for r in f.get("rows", [])))


def describe(f: dict, reasons: list[str]) -> str:
    bits = []
    if "open_position_above_cap" in reasons:
        bits.append("open position above cap: " + ", ".join(f"{r['currency']} {r['position_after_minor']} > {r['cap_minor']}"
                                                           for r in f["rows"] if r.get("cap_minor") is not None and r["position_after_minor"] > r["cap_minor"]))
    if "hedge_ratio_below_minimum" in reasons:
        bits.append(f"hedge below {f['min_hedge_ratio_bp'] / 100:.0f}%: " + ", ".join(
            f"{r['currency']} {r['coverage_bp'] / 100:.0f}% of {r['due_in_horizon_minor']} due in {f['horizon_hours']}h"
            for r in f["rows"] if r.get("coverage_bp") is not None and r["coverage_bp"] < f["min_hedge_ratio_bp"]))
    if "daily_conversion_cap_exceeded" in reasons:
        bits.append(f"24h conversions {f['converted_24h_minor']} + now {f['converting_now_minor']} > {f['max_conversion_daily_minor']} {f['base_currency']}")
    return "; ".join(bits) if bits else "inside every FX limit"
