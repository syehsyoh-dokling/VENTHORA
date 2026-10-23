"""An AI 'challenger' that recommends what to pay, so ProofCore can be seen refusing it.

This is the demo of VENTHORA's core claim: even if the AI recommends a payment, the payment
happens only if independent checks prove it safe. The language model gets the same facts a
person would see and answers "which obligations should we pay now?". Its answer is executed
nowhere; it goes through `Orchestrator.challenge`, i.e. Verity, Horizon, ProofCore and policy,
exactly like a plan.

Without a model, a deterministic naive policy stands in ("pay everything that is due within
the planning horizon"), which is what a well-meaning script or assistant would do.
"""
from __future__ import annotations

from ..domain.models import TreasuryState
from ..domain.money import fmt
from ..evidence.llm import ChatModel, LLMUnavailable, first_json_object


def naive_proposal(state: TreasuryState) -> list[str]:
    return [o.obligation_id for o in state.obligations if o.due_in_hours <= state.policy.planning_horizon_hours]


async def llm_proposal(state: TreasuryState, model: ChatModel) -> tuple[list[str], str]:
    """Returns (obligation ids to pay, source label)."""
    if not model.configured:
        return naive_proposal(state), "naive script (no model configured)"
    lines = "\n".join(
        f"- {o.obligation_id}: {fmt(o.amount_minor, o.currency)}, due in {o.due_in_hours}h, priority {o.priority}/10"
        for o in state.obligations)
    cash = ", ".join(fmt(v, c) for c, v in sorted(state.balances.items()))
    prompt = (f"Our cash: {cash}. Obligations:\n{lines}\n"
              "Which obligations should we pay now? Reply with JSON only: {\"pay\": [\"ID\", ...]}")
    try:
        reply = await model.complete([{"role": "system", "content": "You are a helpful finance assistant."},
                                      {"role": "user", "content": prompt}], max_tokens=150)
        ids = first_json_object(reply).get("pay", [])
        if not isinstance(ids, list):
            raise ValueError("pay is not a list")
        return [str(i).strip() for i in ids], f"LLM {model.model}"
    except (LLMUnavailable, ValueError):
        return naive_proposal(state), "naive script (model unavailable)"
