"""Post-action cash projection: what every wallet looks like after a plan runs.

Used by ProofCore to check a plan independently of how Theorem produced it.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .models import Conversion, Obligation, TreasuryState
from .money import cost_in_base


def obligation_needs(o: Obligation, state: TreasuryState) -> dict[str, int]:
    """Cash each currency wallet must supply for one payout, fees included."""
    needs: dict[str, int] = defaultdict(int)
    needs[o.currency] += o.amount_minor
    if o.payout_method == "SWIFT":
        needs[state.policy.swift_fee_currency] += state.policy.swift_fee_minor
    return dict(needs)


@dataclass
class Projection:
    needs: dict[str, int]
    conversions: list[Conversion]
    balances_after: dict[str, int]
    base_cost_minor: int
    errors: list[str] = field(default_factory=list)


def project(state: TreasuryState, selected: list[str]) -> Projection:
    by_id = {o.obligation_id: o for o in state.obligations}
    needs: dict[str, int] = defaultdict(int)
    for oid in selected:
        for ccy, amount in obligation_needs(by_id[oid], state).items():
            needs[ccy] += amount

    base = state.base_currency
    conversions: list[Conversion] = []
    after = {ccy: state.balance(ccy) for ccy in set(state.balances) | set(needs)}
    base_cost = needs.get(base, 0)
    after[base] = after.get(base, 0) - needs.get(base, 0)
    for ccy in sorted(c for c in needs if c != base):
        shortfall = needs[ccy] - state.balance(ccy)
        if shortfall > 0:
            sell = cost_in_base(shortfall, state.ppm_up(ccy))
            conversions.append(Conversion(sell_currency=base, buy_currency=ccy,
                                          buy_amount_minor=shortfall, estimated_sell_minor=sell))
            after[base] -= sell
            base_cost += sell
            after[ccy] = 0
        else:
            after[ccy] = state.balance(ccy) - needs[ccy]

    errors = [f"{ccy} wallet goes negative" for ccy, v in after.items() if v < 0]
    return Projection(needs=dict(needs), conversions=conversions, balances_after=after,
                      base_cost_minor=base_cost, errors=errors)
