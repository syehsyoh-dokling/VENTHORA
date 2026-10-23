package venthora.treasury

# Mirrors src/venthora/proofcore/policy.py. ProofCore requires both to agree.

import rego.v1

full := input.autonomous_conversion_limit_minor

# Autonomous FX conversion limit tied to forecast confidence (Kit 01).
conversion_limit := full if input.forecast_confidence_bp == null

conversion_limit := full if {
	input.forecast_confidence_bp != null
	input.forecast_confidence_bp >= input.full_confidence_bp
}

conversion_limit := 0 if {
	input.forecast_confidence_bp != null
	input.forecast_confidence_bp <= input.zero_limit_below_bp
}

conversion_limit := floor((full * (input.forecast_confidence_bp - input.zero_limit_below_bp)) / (input.full_confidence_bp - input.zero_limit_below_bp)) if {
	input.forecast_confidence_bp != null
	input.forecast_confidence_bp > input.zero_limit_below_bp
	input.forecast_confidence_bp < input.full_confidence_bp
}

approval_reasons contains "conversion_above_autonomous_limit" if {
	input.conversion_base_minor > conversion_limit
}

decision := {
	"allow": input.actor_role != "viewer",
	"requires_approval": sort([r | some r in approval_reasons]),
	"conversion_limit_minor": conversion_limit,
	"escalations": input.escalations,
}
