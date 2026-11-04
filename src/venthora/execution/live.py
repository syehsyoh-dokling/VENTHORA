"""Ground a treasury state in live Airwallex data before planning.

Two modes:
* full        - plan on the whole live wallet.
* ring-fenced - plan on the scenario's treasury allocation, capped by what is really in the
                wallet. The agent's mandate is a budget, not the whole company balance: a sandbox
                (or a real company) can hold far more than the agent is authorised to spend.
Rates are always the live indicative Airwallex rates.
"""
from __future__ import annotations

from ..domain.models import TreasuryState
from ..fx_routes import SOURCE_CURRENCIES
from ..gateways.airwallex import Airwallex

SUPPORTED = SOURCE_CURRENCIES  # a wallet can fund a payout only in a currency Airwallex accepts as a transfer source


async def live_rates(state: TreasuryState, bank: Airwallex, currencies) -> dict[str, str]:
    return {c: format(await bank.rate_to_base(c, state.base_currency), "f")
            for c in sorted(set(state.fx_to_base) | set(currencies)) if c != state.base_currency}


async def with_live_balances_and_rates(state: TreasuryState, bank: Airwallex, *, ring_fence: bool = False) -> TreasuryState:
    live = await bank.balances_minor()
    if ring_fence:
        balances = {c: min(live.get(c, 0), allocation) for c, allocation in state.balances.items()}
    else:
        balances = {c: live.get(c, 0) for c in (set(state.balances) | {c for c, v in live.items() if v > 0}) & SUPPORTED}
    rates = await live_rates(state, bank, balances)
    return TreasuryState.model_validate(state.model_dump() | {"balances": balances, "fx_to_base": rates})
