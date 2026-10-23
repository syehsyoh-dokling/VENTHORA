"""Company treasury policy, evaluated in code. Mirrored 1:1 by policy/treasury.rego."""
from __future__ import annotations


def policy_input(state, conversion_base_minor: int, escalations: list[str], low_confidence: bool) -> dict:
    p = state.policy
    return {
        "actor_role": state.actor_role,
        "approver_roles": p.approver_roles,
        "conversion_base_minor": conversion_base_minor,
        "autonomous_conversion_limit_minor": p.autonomous_conversion_limit_minor,
        "low_confidence_conversion_limit_minor": p.low_confidence_conversion_limit_minor,
        "low_confidence": low_confidence,
        "escalations": escalations,
    }


def evaluate(inp: dict) -> dict:
    """Return {"allow": bool, "requires_approval": [reason codes]}."""
    limit = (inp["low_confidence_conversion_limit_minor"] if inp["low_confidence"]
             else inp["autonomous_conversion_limit_minor"])
    reasons = []
    if inp["conversion_base_minor"] > limit:
        reasons.append("conversion_above_autonomous_limit")
    if inp["escalations"]:
        reasons.append("unfunded_obligation_escalated")
    return {"allow": inp["actor_role"] != "viewer", "requires_approval": sorted(reasons)}
