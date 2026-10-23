# ruff: noqa: E501
"""Three-way match: contract / PO, what was already paid, and what the invoice now asks for.

For every payable obligation that names a contract total:
  expected = contract_total − paid_so_far        (what may still be owed on this invoice)
  claimed  = amount                              (what the invoice / instalment asks for)
  claimed > expected + tolerance   -> OVER-CLAIM:  amount_mismatch (a person decides; the agent never pays the excess alone)
  claimed < expected − tolerance   -> UNDER-CLAIM: noted (the payee bills less than the contract; paying less is never unsafe)
  otherwise                        -> matched
Tolerance = max(policy.amount_tolerance_minor, expected × policy.amount_tolerance_bp). Instalments that would take the
invoice past its contract total are rejected by Verity before they get here.
"""
from __future__ import annotations

from ..domain.models import ObligationDecision, TreasuryState

PAYING = ("PAY", "CONVERT_AND_PAY")


def three_way_match(state: TreasuryState, decisions: list[ObligationDecision]) -> dict:
    p = state.policy
    actions = {d.obligation_id: d.action for d in decisions}
    rows, over, under = [], [], []
    for o in state.obligations:
        if o.contract_total_minor is None or o.record_kind != "payable":
            continue
        expected = max(0, o.contract_total_minor - o.paid_so_far_minor)
        tolerance = max(p.amount_tolerance_minor, expected * p.amount_tolerance_bp // 10_000)
        diff = o.amount_minor - expected
        if diff > tolerance:
            verdict = "over_claim"
            over.append(o.obligation_id)
        elif diff < -tolerance:
            verdict = "under_claim"
            under.append(o.obligation_id)
        else:
            verdict = "matched"
        rows.append({"obligation_id": o.obligation_id, "installment": o.installment, "contract_total_minor": o.contract_total_minor,
                     "paid_so_far_minor": o.paid_so_far_minor, "expected_minor": expected, "claimed_minor": o.amount_minor,
                     "difference_minor": diff, "tolerance_minor": tolerance, "verdict": verdict, "action": actions.get(o.obligation_id)})
    # Only an over-claim the plan would actually pay needs a person; a rejected or escalated one already has one.
    paying_over = [i for i in over if actions.get(i) in PAYING]
    return {"rows": rows, "over_claims": over, "under_claims": under, "paying_over_claims": paying_over}
