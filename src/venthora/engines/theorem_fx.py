"""Bridge to the original Theorem quant toolkit (`originals/theorem_qf/fx.py`).

Theorem's `net_currency_exposure` nets FX trades leg by leg with explicit pair orientation.
ProofCore uses it as an independent second computation: the planned conversions, booked as
BUY <ccy>/<base> trades at the planning rate, must net to exactly the currency shortfalls the
projection found (+buy amount in each foreign currency, -estimated cost in base).
"""
from __future__ import annotations

from decimal import Decimal

from ..domain.models import Conversion, TreasuryState
from ..originals.theorem_qf import fx

TOLERANCE_MINOR = 1  # the plan rounds each base cost up to a whole cent


def check_conversion_legs(state: TreasuryState, conversions: list[Conversion]) -> tuple[bool, dict]:
    trades = [{"pair": f"{c.buy_currency}/{c.sell_currency}", "direction": "buy",
               "notional": c.buy_amount_minor / 100, "rate": float(Decimal(state.fx_to_base[c.buy_currency]))}
              for c in conversions]
    net = fx.net_currency_exposure(trades)
    problems = []
    for c in conversions:
        bought = round(net.get(c.buy_currency, 0.0) * 100)
        if bought != c.buy_amount_minor:
            problems.append(f"{c.buy_currency} leg nets {bought}, plan buys {c.buy_amount_minor}")
    base_net = round(-net.get(state.base_currency, 0.0) * 100, 4)
    estimated = sum(c.estimated_sell_minor for c in conversions)
    if not 0 <= estimated - base_net <= TOLERANCE_MINOR * max(1, len(conversions)):
        problems.append(f"{state.base_currency} leg nets {base_net}, plan estimates {estimated}")
    return not problems, {"theorem_net_exposure": {k: round(v, 6) for k, v in net.items()},
                          "plan_estimated_sell_minor": estimated, "problems": problems}
