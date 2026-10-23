"""The Starter Kit 01 story: plan, new information, revised decision."""
from conftest import actions, decide, scenario


def test_baseline_plan_is_approved_and_keeps_reserve():
    r = decide(scenario("kit01_baseline"))
    assert r.decision == "APPROVED"
    assert actions(r) == {
        "PAYROLL-USD": "PAY",
        "SUPPLIER-EU": "CONVERT_AND_PAY",
        "SUPPLIER-EU-RESENT": "REJECT",
        "VENDOR-UK": "DEFER",
        "SAAS-USD": "PAY",
    }
    # EUR 4,000 + EUR 12.85 SWIFT fee, bought at 1.0850 and rounded up.
    [conv] = r.conversions
    assert (conv.buy_currency, conv.buy_amount_minor, conv.estimated_sell_minor) == ("EUR", 401_285, 435_395)
    assert r.total_base_cost_minor == 800_000 + 435_395 + 120_000
    assert r.projected_base_balance_minor == 2_000_000 - r.total_base_cost_minor
    assert r.projected_base_balance_minor >= r.required_base_floor_minor == 600_000


def test_delayed_receipt_revises_plan_and_escalates_to_a_person():
    base = decide(scenario("kit01_baseline"))
    r = decide(scenario("kit01_receipt_delayed"))
    assert r.decision == "REQUIRES_APPROVAL"
    assert actions(r)["VENDOR-UK"] == "ESCALATE"
    assert actions(base)["VENDOR-UK"] == "DEFER"
    policy = next(s for s in r.stages if s.engine == "Policy")
    assert policy.details["local"]["requires_approval"] == [
        "conversion_above_autonomous_limit", "unfunded_obligation_escalated"]
    assert any("finance manager must approve" in line for line in r.explanation)


def test_duplicate_invoice_is_never_paid():
    r = decide(scenario("kit01_baseline"))
    paid = {d.obligation_id for d in r.obligations if d.action in ("PAY", "CONVERT_AND_PAY")}
    assert "SUPPLIER-EU-RESENT" not in paid
    reason = next(d.reason for d in r.obligations if d.obligation_id == "SUPPLIER-EU-RESENT")
    assert "duplicate of SUPPLIER-EU" in reason


def test_tighter_cash_still_never_breaches_floor():
    s = scenario("kit01_baseline")
    s["scenarios"] = [{"name": "large_chargeback", "unexpected_outflow_minor": 700_000}]
    r = decide(s)
    assert r.decision != "BLOCKED"
    assert r.projected_base_balance_minor >= r.required_base_floor_minor
    assert actions(r)["PAYROLL-USD"] == "PAY"


def test_viewer_cannot_plan_payments():
    s = scenario("kit01_baseline")
    s["actor_role"] = "viewer"
    assert decide(s).decision == "BLOCKED"


def test_settled_records_are_not_payables():
    s = scenario("kit01_baseline")
    s["obligations"][0]["record_kind"] = "settled"
    assert actions(decide(s))["PAYROLL-USD"] == "REJECT"
