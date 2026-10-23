package venthora.treasury

# Mirrors src/venthora/proofcore/policy.py. ProofCore requires both to agree.

import rego.v1

conversion_limit := input.low_confidence_conversion_limit_minor if input.low_confidence

conversion_limit := input.autonomous_conversion_limit_minor if not input.low_confidence

approval_reasons contains "conversion_above_autonomous_limit" if {
	input.conversion_base_minor > conversion_limit
}

approval_reasons contains "unfunded_obligation_escalated" if {
	count(input.escalations) > 0
}

decision := {
	"allow": input.actor_role != "viewer",
	"requires_approval": sort([r | some r in approval_reasons]),
}
