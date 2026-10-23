"""Counterfactual decision proof: why this plan, and not the obvious alternatives.

Every alternative is evaluated with the same independent projection ProofCore uses for the
chosen plan, against the same floor (reserve + stress). The output answers the questions a
CFO asks before approving:

* Is the chosen plan safe, and with how much headroom?
* What if we pay nothing?            (which critical obligations are missed)
* What if we pay everything now?     (how far below the reserve that lands)
* What if we also pay X now?         (for every deferred / escalated obligation)
* What if the receipt we defer on never arrives?
* What if we also pay the duplicate? (the invoice would be paid twice)
"""
from __future__ import annotations

from ..domain.models import Counterfactual, ObligationDecision, TreasuryState
from ..domain.money import fmt
from ..domain.projection import project

PAYING = ("PAY", "CONVERT_AND_PAY")


def _eval(state: TreasuryState, pays: list[str], floor: int) -> tuple[bool, int, int]:
    base = state.base_currency
    proj = project(state, pays)
    after = proj.balances_after.get(base, state.balance(base))
    spends = proj.base_cost_minor > 0
    feasible = not proj.errors and (not spends or after >= floor)
    return feasible, after, after - floor


def counterfactuals(state: TreasuryState, selected: list[str], decisions: list[ObligationDecision],
                    eligible: set[str], stress_minor: int, reliable_receipts: list) -> list[Counterfactual]:
    base = state.base_currency
    floor = state.policy.reserve_floor_minor + stress_minor
    by_id = {o.obligation_id: o for o in state.obligations}
    out: list[Counterfactual] = []

    def add(name, question, pays, consequence_fn):
        feasible, after, headroom = _eval(state, pays, floor)
        out.append(Counterfactual(name=name, question=question, pays=sorted(pays), feasible=feasible,
                                  base_after_minor=after, floor_minor=floor, headroom_minor=headroom,
                                  consequence=consequence_fn(feasible, headroom)))

    def reserve_text(feasible, headroom):
        if feasible:
            return f"reserve kept with {fmt(headroom, base)} headroom"
        return f"breaches the reserve floor by {fmt(-headroom, base)}"

    add("chosen", "Is the chosen plan safe?", list(selected), reserve_text)

    critical = [o for o in state.obligations if o.obligation_id in eligible and o.priority >= 9]
    add("pay_nothing", "What if we pay nothing?", [], lambda f, h: (
        "misses " + ", ".join(f"{o.obligation_id} (priority {o.priority}, due {o.due_in_hours}h)" for o in critical))
        if critical else "no critical obligation missed")

    everything = sorted(eligible)
    if set(everything) != set(selected):
        add("pay_everything", "What if we pay every verified obligation now?", everything, reserve_text)

    for d in decisions:
        o = by_id[d.obligation_id]
        if d.action in ("DEFER", "ESCALATE"):
            exception = sorted(set(o.policy_flags) & set(state.policy.escalate_flags))

            def text(feasible, headroom, exception=exception):
                cash = reserve_text(feasible, headroom)
                if exception:  # the policy, not the cash, is what stops this payment
                    return ("policy exception (" + ", ".join(f.replace("_", " ") for f in exception)
                            + f"): never auto-paid, even with cash; cash check: {cash}")
                return cash

            add(f"also_pay_{o.obligation_id}", f"What if we also pay {o.obligation_id} now?",
                [*selected, o.obligation_id], text)
            if exception:
                out[-1] = out[-1].model_copy(update={"feasible": False})
        if d.action == "DEFER":
            _, _, headroom = _eval(state, list(selected), floor)
            sources = [r.receivable_id for r in reliable_receipts if r.latest_arrival_hours <= o.due_in_hours]
            cost = d.base_cost_minor
            out.append(Counterfactual(
                name=f"receipt_fails_{o.obligation_id}",
                question=f"What if {', '.join(sources) or 'the expected receipt'} never arrives?",
                pays=sorted(selected), feasible=cost <= headroom, base_after_minor=_eval(state, list(selected), floor)[1],
                floor_minor=floor, headroom_minor=headroom - cost,
                consequence=(f"{o.obligation_id} ({fmt(o.amount_minor, o.currency)}) would be unfunded at its due date; "
                             "VENTHORA re-plans and escalates then") if cost > headroom else
                            f"{o.obligation_id} could still be paid from current headroom"))
        if d.action == "REJECT" and o.invoice_fingerprint:
            twin = next((x for x in selected if by_id[x].invoice_fingerprint == o.invoice_fingerprint), None)
            if twin:
                out.append(Counterfactual(
                    name=f"also_pay_{o.obligation_id}", question=f"What if we also pay {o.obligation_id}?",
                    pays=sorted([*selected, o.obligation_id]), feasible=False,
                    base_after_minor=_eval(state, [*selected, o.obligation_id], floor)[1], floor_minor=floor,
                    headroom_minor=_eval(state, [*selected, o.obligation_id], floor)[2],
                    consequence=f"pays invoice {o.invoice_fingerprint} twice (already paid via {twin})"))
    return out
