"""Feed what execution learned back into the next plan.

* Fees Airwallex actually charged (per 'METHOD:CCY') replace assumed fees. The Kit 01 guide says
  EUR 12.85 per SWIFT payout and free LOCAL payouts; the sandbox charged EUR 13.37 and USD 3.00
  on 23 Oct 2026. A plan built on stale fees overspends its budget by exactly that difference.
* The managed budget continues from the last reconciliation (money already paid out is gone),
  capped by the scenario allocation and floored at zero.
* Obligations already PAID (by invoice fingerprint or id) become 'settled', so the next plan
  neither proposes nor re-pays them. The payout claim would block a re-payment anyway; this keeps
  the plan honest instead of relying on the block.
"""
from __future__ import annotations

from ..domain.models import TreasuryState
from ..domain.money import fmt
from .store import ExecutionStore


def payout_key(obligation) -> str:
    fp = obligation.get("invoice_fingerprint") if isinstance(obligation, dict) else obligation.invoice_fingerprint
    oid = obligation.get("obligation_id") if isinstance(obligation, dict) else obligation.obligation_id
    return f"invoice:{fp}" if fp else f"obligation:{oid}"


def apply_operational_facts(state: TreasuryState, store: ExecutionStore) -> tuple[TreasuryState, list[str]]:
    data = state.model_dump()
    notes: list[str] = []
    fees = store.observed_fees()
    if fees:
        merged = data["policy"]["payout_fees_minor"] | fees
        data["policy"]["payout_fees_minor"] = merged
        notes.append("observed Airwallex fees: " + ", ".join(
            f"{k} {fmt(v, k.split(':')[1])}" for k, v in sorted(fees.items())))
    last = store.last_managed_budget(state.book_id)
    if last:
        execution_id, managed = last
        for ccy in data["balances"]:
            if ccy in managed:
                value = managed[ccy]
                if value < 0:
                    notes.append(f"{ccy} budget was overdrawn by {fmt(-value, ccy)} in {execution_id[:8]}; starting from 0")
                data["balances"][ccy] = max(0, min(value, data["balances"][ccy]))
        notes.append("budget carried forward from execution " + execution_id[:8] + ": "
                     + ", ".join(fmt(v, c) for c, v in sorted(data["balances"].items())))
    paid = store.paid_payouts()
    for o in data["obligations"]:
        if o["record_kind"] == "payable" and payout_key(o) in paid:
            o["record_kind"] = "settled"
            notes.append(f"{o['obligation_id']} already paid (transfer {paid[payout_key(o)]['transfer_id']})")
    return TreasuryState.model_validate(data), notes
