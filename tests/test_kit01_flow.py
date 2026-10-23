"""Starter Kit 01, sentence by sentence (airwallexdev.com/guide), on scenarios derived from the inbox."""
import pytest

from conftest import actions, decide, scenario
from venthora.proofcore.policy import conversion_limit


def test_initial_plan_does_what_kit01_describes():
    r = decide(scenario("kit01_initial"))
    assert actions(r) == {
        "PAYROLL-NOV": "PAY",
        "SHIPPING-UK": "CONVERT_AND_PAY",     # "funds the shipping invoice whose non-payment would halt operations"
        "SUPPLIER-EU": "CONVERT_AND_PAY",     # "converts only the minimum needed for a time-sensitive supplier"
        "SUPPLIER-EU-RESENT": "REJECT",       # duplicate protection
        "CONTRACTOR-US": "ESCALATE",          # "escalates a policy exception" (payee bank details changed)
        "SAAS-USD": "DEFER",                  # "defers the cheapest obligation"
    }
    assert r.decision == "APPROVED"  # strong bank + invoice data: the larger autonomous limit applies
    assert r.forecast_confidence_bp == 9000 and r.conversion_limit_minor == 1_000_000
    contractor = next(d for d in r.obligations if d.obligation_id == "CONTRACTOR-US")
    assert "policy exception" in contractor.reason


def test_conversions_buy_exactly_the_minimum():
    r = decide(scenario("kit01_initial"))
    conv = {c.buy_currency: (c.buy_amount_minor, c.estimated_sell_minor) for c in r.conversions}
    # EUR 4,000 + EUR 12.85 SWIFT fee (guide) at 1.0850, rounded up; GBP 2,500 LOCAL at 1.2700.
    assert conv == {"EUR": (401_285, 435_395), "GBP": (250_000, 317_500)}


def test_new_information_lowers_the_limit_and_a_person_approves_the_same_conversion():
    initial = decide(scenario("kit01_initial"))
    delayed = decide(scenario("kit01_receipt_delayed"))
    assert [c.model_dump() for c in delayed.conversions] == [c.model_dump() for c in initial.conversions]
    assert delayed.forecast_confidence_bp == 7000  # payer kept 6 of 8 promises incl. this delay
    assert delayed.conversion_limit_minor == 500_000
    assert delayed.decision == "REQUIRES_APPROVAL"
    policy = next(s for s in delayed.stages if s.engine == "Policy")
    reasons = policy.details["local"]["requires_approval"]
    assert "conversion_above_autonomous_limit" in reasons
    # With the receipt pushed outside the window, the simulated breach risk also exceeds tolerance.
    assert "reserve_breach_risk_above_tolerance" in reasons and delayed.simulation["breach_probability_bp"] > 500
    assert actions(delayed)["SAAS-USD"] == "ESCALATE"  # the receipt it was deferred on is no longer reliable


@pytest.mark.parametrize(("confidence", "limit"), [
    (None, 1_000_000), (9500, 1_000_000), (9000, 1_000_000), (7000, 500_000), (5001, 250), (5000, 0), (3500, 0)])
def test_conversion_limit_tied_to_forecast_confidence(confidence, limit):
    inp = {"autonomous_conversion_limit_minor": 1_000_000, "forecast_confidence_bp": confidence,
           "full_confidence_bp": 9000, "zero_limit_below_bp": 5000}
    assert conversion_limit(inp) == limit


def test_reserve_floor_holds_and_escalations_do_not_block_the_safe_part():
    r = decide(scenario("kit01_initial"))
    assert r.projected_base_balance_minor >= r.required_base_floor_minor
    assert r.decision == "APPROVED" and "ESCALATE" in actions(r).values()


def test_duplicate_invoice_is_never_paid():
    r = decide(scenario("kit01_initial"))
    reason = next(d.reason for d in r.obligations if d.obligation_id == "SUPPLIER-EU-RESENT")
    assert "duplicate of SUPPLIER-EU" in reason


def test_tighter_cash_still_never_breaches_floor():
    s = scenario("kit01_initial")
    s["scenarios"] = [{"name": "large_chargeback", "unexpected_outflow_minor": 700_000}]
    r = decide(s)
    assert r.decision != "BLOCKED"
    assert r.projected_base_balance_minor >= r.required_base_floor_minor


def test_viewer_cannot_plan_payments():
    s = scenario("kit01_initial")
    s["actor_role"] = "viewer"
    assert decide(s).decision == "BLOCKED"


def test_settled_records_are_not_payables():
    s = scenario("kit01_initial")
    s["obligations"][0]["record_kind"] = "settled"
    assert actions(decide(s))["PAYROLL-NOV"] == "REJECT"


def test_not_payable_as_posed_states_the_missing_cash():
    s = scenario("kit01_initial")
    s["balances"]["USD"] = 1_000_000  # cannot cover payroll + shipping above the reserve
    r = decide(s)
    frame = r.frame["answer"]["not_payable_as_posed"]
    assert set(frame["critical_unpaid"]) <= {"PAYROLL-NOV", "SHIPPING-UK"}
    assert frame["minimum_additional_base_cash_minor"] > 0
