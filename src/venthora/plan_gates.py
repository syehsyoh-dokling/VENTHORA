# ruff: noqa: E501
"""What the executor would refuse, known before the plan is made.

The plan proves cash, FX and the reserve floor. Two refusals were only found at execution, after a person had already
approved the payment:

  1. budget allocation  a payment with a charge code that does not fit the allocation governing it (the executor's
                        budget gate, execution/executor.py).
  2. transfer limit     an amount the bank refuses for the transfer method ("above the maximum limit for this transfer
                        method"). The bank publishes no limit to ask for in advance, so the limit is learned: the smallest
                        amount it refused per currency and method is kept, and a payment at or above it is not planned
                        until it is split.

`blockers` gives one reason per payment the executor would refuse. Such a payment is left out of the book the plan
proves (its cash is not spent, a batch is not failed by it) and shown with its reason. The executor stays the authority:
it checks the budget again, on actual costs, when the money moves.
"""
from __future__ import annotations

from datetime import UTC, datetime

KIND = "transfer_limit"
LIMIT_WORDS = "above the maximum limit"


def _key(currency: str, method: str) -> str:
    return f"{currency}:{method or 'LOCAL'}"


def learn_limit(org, currency: str, method: str, amount_minor: int, error: str) -> dict | None:
    """Keep the smallest amount the bank refused as too large for this currency and transfer method."""
    if org is None or LIMIT_WORDS not in str(error).lower():
        return None
    old = org.get(KIND, _key(currency, method)) or {}
    if old.get("refused_minor") and old["refused_minor"] <= amount_minor:
        return old
    rec = {"_id": _key(currency, method), "currency": currency, "method": method or "LOCAL", "refused_minor": amount_minor,
           "at": datetime.now(UTC).isoformat(timespec="seconds"), "error": str(error)[:300]}
    org._put(KIND, rec, "VENTHORA (executor)")
    return rec


def _money(minor: int, currency: str) -> str:
    return f"{currency} {minor / 100:,.2f}"


def blockers(org, store, state) -> dict[str, str]:
    """obligation_id -> why the executor would refuse it now. Most urgent first, so a budget is used in the order the
    payments would be made."""
    if org is None:
        return {}
    out: dict[str, str] = {}
    items = []
    base = state.base_currency
    for o in sorted(state.obligations, key=lambda o: (-o.priority, o.due_in_hours, o.obligation_id)):
        lim = org.get(KIND, _key(o.currency, o.payout_method))
        if lim and o.amount_minor >= lim["refused_minor"]:
            out[o.obligation_id] = (f"transfer limit: the bank refused {_money(lim['refused_minor'], o.currency)} as above its maximum for a "
                                    f"{lim['method']} transfer on {lim['at'][:10]}; split this payment into smaller ones")
            continue
        if not o.account_code:
            continue
        try:
            from .domain.money import cost_in_base

            cost = o.amount_minor if o.currency == base else cost_in_base(o.amount_minor, state.ppm_up(o.currency))
        except (KeyError, TypeError, ValueError):
            continue  # no rate: the plan itself says so
        items.append({"obligation_id": o.obligation_id, "account_code": o.account_code, "currency": o.currency,
                      "amount_minor": o.amount_minor, "base_cost_minor": cost})
    if items:
        from .budget_allocation import Budget
        from .reporting import load_config, reporting_period

        for v in Budget(org, store).check(items, reporting_period(datetime.now(UTC), load_config())):
            if not v["ok"]:
                out[v["obligation_id"]] = f"budget allocation: {v['reason']}"
    return out


def apply(org, store, state):
    """(the book without what the executor would refuse, or None when nothing is left; the refused obligations with
    their reasons)."""
    try:
        why = blockers(org, store, state)
    except Exception:  # noqa: BLE001 - a plan is never lost to this check: the executor still refuses at execution
        why = {}
    if not why:
        return state, []
    blocked = [(o, why[o.obligation_id]) for o in state.obligations if o.obligation_id in why]
    left = [o for o in state.obligations if o.obligation_id not in why]
    return (state.model_copy(update={"obligations": left}) if left else None), blocked
