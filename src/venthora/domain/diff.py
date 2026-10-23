"""What changed between two treasury states, and which decisions that change touches.

Kit 01: "when a deposit arrives, it recalculates only the affected decisions". A payment the
earlier plan already chose stays chosen unless its own inputs changed (amount, currency, due
date, payee, evidence); only open decisions (deferred, escalated, rejected, new) are
re-optimised. If keeping the earlier payments no longer fits, Theorem falls back to a full
recalculation and says so.
"""
from __future__ import annotations

from .models import PlanChange, PlanResult, TreasuryState
from .money import fmt

PAYING = ("PAY", "CONVERT_AND_PAY")


def input_changes(prev: TreasuryState, new: TreasuryState) -> list[str]:
    out = []
    for ccy in sorted(set(prev.balances) | set(new.balances)):
        delta = new.balance(ccy) - prev.balance(ccy)
        if delta:
            out.append(f"{ccy} budget {'+' if delta > 0 else ''}{fmt(delta, ccy)}")
    for ccy in sorted(set(prev.fx_to_base) | set(new.fx_to_base)):
        if prev.fx_to_base.get(ccy) != new.fx_to_base.get(ccy):
            out.append(f"{ccy} rate {prev.fx_to_base.get(ccy)} -> {new.fx_to_base.get(ccy)}")
    before = {r.receivable_id: r for r in prev.receivables}
    after = {r.receivable_id: r for r in new.receivables}
    for rid in sorted(set(before) - set(after)):
        out.append(f"{rid} received/settled")
    for rid in sorted(set(after) - set(before)):
        out.append(f"{rid} new expected receipt")
    for rid in sorted(set(before) & set(after)):
        a, b = before[rid], after[rid]
        if (a.expected_in_hours, a.confidence_bp) != (b.expected_in_hours, b.confidence_bp):
            out.append(f"{rid} forecast {a.expected_in_hours}h/{a.confidence_bp // 100}% -> "
                       f"{b.expected_in_hours}h/{b.confidence_bp // 100}%")
    if prev.policy != new.policy:
        out.append("treasury policy changed")
    if [s.model_dump() for s in prev.scenarios] != [s.model_dump() for s in new.scenarios]:
        out.append("stress scenarios changed")
    return out


def changed_obligations(prev: TreasuryState, new: TreasuryState) -> set[str]:
    """Obligations whose own inputs differ (or that are new)."""
    before = {o.obligation_id: o for o in prev.obligations}
    ev_before = {e.ref: e for e in prev.evidence}
    ev_after = {e.ref: e for e in new.evidence}
    out = set()
    for o in new.obligations:
        old = before.get(o.obligation_id)
        if old is None or old != o or ev_before.get(o.evidence_ref) != ev_after.get(o.evidence_ref):
            out.add(o.obligation_id)
    return out


def locked_payments(prev: TreasuryState, prev_plan: PlanResult, new: TreasuryState) -> set[str]:
    """Earlier payment decisions that the new information does not touch."""
    touched = changed_obligations(prev, new)
    still_there = {o.obligation_id for o in new.obligations}
    return {d.obligation_id for d in prev_plan.obligations
            if d.action in PAYING and d.obligation_id not in touched and d.obligation_id in still_there}


def decision_changes(prev_plan: PlanResult, new_plan: PlanResult) -> list[PlanChange]:
    before = {d.obligation_id: d for d in prev_plan.obligations}
    out = []
    for d in new_plan.obligations:
        old = before.get(d.obligation_id)
        if old is None or old.action != d.action:
            out.append(PlanChange(obligation_id=d.obligation_id, before=old.action if old else None,
                                  after=d.action, reason=d.reason))
    return out
