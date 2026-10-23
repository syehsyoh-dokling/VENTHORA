"""The model explains exceptions to the person who must decide (guide: "explain an exception").

The explanation is written from the plan's own facts only (amount, reason, cited evidence), is
labelled as model-written, and changes nothing: it is not part of the plan or its hash. Without a
model, a deterministic template says the same facts.
"""
from __future__ import annotations

from ..domain.models import PlanResult, TreasuryState
from ..domain.money import fmt
from .llm import ChatModel, LLMUnavailable


def _facts(state: TreasuryState, plan: PlanResult, obligation_id: str) -> str:
    d = next(x for x in plan.obligations if x.obligation_id == obligation_id)
    o = next(x for x in state.obligations if x.obligation_id == obligation_id)
    cited = [e.summary for e in state.evidence if e.subject_id == obligation_id and e.source.startswith("email:")]
    parts = [f"Obligation {o.obligation_id} to {o.payee_name or o.beneficiary_id}: {fmt(o.amount_minor, o.currency)}, "
             f"due in {o.due_in_hours}h.", f"Why it is escalated: {d.reason}."]
    if cited:
        parts.append("Cited evidence: " + " | ".join(f'"{c}"' for c in cited))
    return " ".join(parts)


async def explain_exceptions(state: TreasuryState, plan: PlanResult, model: ChatModel | None = None) -> list[dict]:
    model = model if model is not None else ChatModel()
    out = []
    for d in plan.obligations:
        if d.action != "ESCALATE":
            continue
        facts = _facts(state, plan, d.obligation_id)
        note, author = None, "template"
        if model.configured:
            try:
                note = (await model.complete([
                    {"role": "system", "content": "You brief a finance manager. Use only the facts given. Two sentences."},
                    {"role": "user", "content": f"Facts: {facts}\nExplain why this payment was not made automatically "
                                                "and what the manager should check before approving it."}],
                    max_tokens=120)).strip()
                author = f"model {model.model} (non-authoritative)"
            except LLMUnavailable:
                note = None
        if not note:
            note = facts + " A person must review it before any payment."
        out.append({"obligation_id": d.obligation_id, "note": note, "author": author, "facts": facts})
    return out
