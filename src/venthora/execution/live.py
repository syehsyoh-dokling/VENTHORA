"""Ground a treasury state in live Airwallex data before planning."""
from __future__ import annotations

from typing import get_args

from ..domain.models import Currency, TreasuryState
from ..gateways.airwallex import Airwallex

SUPPORTED = set(get_args(Currency))


async def with_live_balances_and_rates(state: TreasuryState, bank: Airwallex) -> TreasuryState:
    """Replace supplied balances with live wallet balances and indicative rates."""
    live = await bank.balances_minor()
    balances = {c: live.get(c, 0) for c in (set(state.balances) | set(live)) & SUPPORTED}
    rates = {c: format(await bank.rate_to_base(c, state.base_currency), "f")
             for c in sorted(set(state.fx_to_base) | set(balances)) if c != state.base_currency}
    return TreasuryState.model_validate(state.model_dump() | {"balances": balances, "fx_to_base": rates})
