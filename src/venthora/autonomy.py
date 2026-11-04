# ruff: noqa: E501
"""Pay what is inside the mandate now; hold only the exceptions.

A plan's decision is one verdict for the whole book: a single conversion above the agent's limit made every payment in
it wait for a person, payroll and verified invoices included. `split` keeps the proof and finds the largest part of the
plan the agent may execute on its own:

  1. plan the whole book. APPROVED: all of it is autonomous. BLOCKED: none of it is.
  2. otherwise take the obligations the plan pays now, most urgent first (priority, then due time), and add them one
     at a time to the book without the payments not yet accepted (what the plan defers stays in, so the reserve proof
     still counts it). An obligation is accepted when that book's plan is APPROVED and still pays it; it is held, with
     the reasons the policy gave, when it is not.
  3. a hold for cash (the reserve breach risk) stops the walk: nothing less urgent is paid with cash a more urgent
     obligation may need.

The autonomous part is an ordinary plan of an ordinary book, so it passes ProofCore, the policy and the reserve floor
on its own and executes without an approval. The rest is a second book (the same book without what was paid) that a
person approves as usual; nothing is ever planned to be paid twice.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .domain.models import PlanResult, TreasuryState

PAYING = ("PAY", "CONVERT_AND_PAY")
CASH_REASONS = {"reserve_breach_risk_above_tolerance"}


@dataclass
class Split:
    full: PlanResult
    auto_state: TreasuryState | None = None
    auto_plan: PlanResult | None = None
    remaining_state: TreasuryState | None = None
    held: list[dict] = field(default_factory=list)  # obligation_id, reasons
    stopped_for_cash: str | None = None

    def summary(self) -> dict:
        auto = [d for d in (self.auto_plan.obligations if self.auto_plan else []) if d.action in PAYING]
        return {"full_decision": self.full.decision, "full_plan_hash": self.full.plan_hash,
                "autonomous": [{"obligation_id": d.obligation_id, "action": d.action, "amount_minor": d.amount_minor,
                                "currency": d.currency} for d in auto],
                "autonomous_plan_hash": self.auto_plan.plan_hash if self.auto_plan else None,
                "held": self.held, "stopped_for_cash": self.stopped_for_cash}


def _reasons(plan: PlanResult) -> list[str]:
    policy = next((s for s in plan.stages if s.engine == "Policy"), None)
    out = (policy.details.get("local", {}).get("requires_approval", []) if policy else []) or []
    return list(out) or ([f"plan {plan.decision.lower()}"] if plan.decision != "APPROVED" else [])


def _book(state: TreasuryState, leave_out: set[str]) -> TreasuryState:
    """The book without the payments not (yet) accepted; what the plan defers stays in, so the reserve proof still
    sees the cash it will need."""
    return state.model_copy(update={"obligations": [o for o in state.obligations if o.obligation_id not in leave_out]})


async def split(agent, state: TreasuryState) -> Split:
    full = await agent.decide(state)
    if full.decision == "APPROVED":
        return Split(full=full, auto_state=state, auto_plan=full, remaining_state=None)
    if full.decision == "BLOCKED":
        return Split(full=full, remaining_state=state,
                     held=[{"obligation_id": d.obligation_id, "reasons": ["plan blocked"]} for d in full.obligations if d.action in PAYING])
    by_id = {o.obligation_id: o for o in state.obligations}
    paying = [d.obligation_id for d in full.obligations if d.action in PAYING and d.obligation_id in by_id]
    paying.sort(key=lambda i: (-by_id[i].priority, by_id[i].due_in_hours, i))
    accepted: list[str] = []
    best: tuple[TreasuryState, PlanResult] | None = None
    out = Split(full=full)
    # what the whole plan rejects (a duplicate, unverified evidence) never enters a smaller book: leaving the original
    # out must not make its resent copy look new
    rejected = {d.obligation_id for d in full.obligations if d.action == "REJECT"}
    for i, oid in enumerate(paying):
        leave_out = rejected | (set(paying) - set(accepted) - {oid})
        trial_state = _book(state, leave_out)
        trial = await agent.decide(trial_state)
        # only what the whole plan pays now may be paid: an obligation it defers stays deferred, its cash is not freed
        # for it by leaving the exceptions out
        for _ in range(5):
            extra = {d.obligation_id for d in trial.obligations if d.action in PAYING} - set(accepted) - {oid}
            if not extra:
                break
            leave_out |= extra
            trial_state = _book(state, leave_out)
            trial = await agent.decide(trial_state)
        extra = {d.obligation_id for d in trial.obligations if d.action in PAYING} - set(accepted) - {oid}
        pays = any(d.obligation_id == oid and d.action in PAYING for d in trial.obligations)
        if trial.decision == "APPROVED" and pays and not extra:
            accepted.append(oid)
            best = (trial_state, trial)
            continue
        why = _reasons(trial) if trial.decision != "APPROVED" else ["the plan defers it once the exceptions are left out"]
        out.held.append({"obligation_id": oid, "reasons": why})
        if CASH_REASONS & set(why):
            out.stopped_for_cash = oid
            out.held += [{"obligation_id": rest, "reasons": [f"waits behind {oid}: less urgent, and the cash may be needed for it"]}
                         for rest in paying[i + 1:]]
            break
    paid_now: set[str] = set()
    if best is not None:
        out.auto_state, out.auto_plan = best
        paid_now = {d.obligation_id for d in out.auto_plan.obligations if d.action in PAYING}
    out.remaining_state = state.model_copy(update={"obligations": [o for o in state.obligations if o.obligation_id not in paid_now]})
    return out
