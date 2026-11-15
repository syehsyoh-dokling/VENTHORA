# ruff: noqa: E501
"""A SWIFT payout in a currency not paid by SWIFT before holds an estimated fee in that currency, not euros.

Found on the live demo (15 Nov 2026): the plan bought EUR 12.85 for the fee of a GBP payout; Airwallex takes the fee
in pounds, so the transfer failed for want of a few pounds and the euros stayed in the wallet unused."""
from conftest import scenario
from venthora.domain.models import TreasuryState
from venthora.domain.projection import obligation_needs
from venthora.execution import facts

RATES = {"EUR": "1.109742", "GBP": "1.331227", "JPY": "0.006303670156878556"}


def _data(fees, obligations):
    raw = scenario("kit01_baseline")
    base = raw["obligations"][0]
    raw["fx_to_base"] = raw["fx_to_base"] | RATES
    raw["policy"]["payout_fees_minor"] = dict(fees)
    raw["obligations"] = [base | {"obligation_id": f"O-{i}", "evidence_ref": base["evidence_ref"], "currency": c, "payout_method": m, "amount_minor": a}
                          for i, (c, m, a) in enumerate(obligations)]
    return raw


def test_the_fee_seen_in_one_currency_is_carried_to_another_with_a_margin():
    data = _data({"SWIFT:EUR": 1343}, [("GBP", "SWIFT", 240_000), ("JPY", "SWIFT", 1_800_000_000), ("EUR", "SWIFT", 2_400_000), ("GBP", "LOCAL", 500)])
    out = facts._estimate_swift_fees(data)
    # EUR 13.43 is USD 14.90; twice that is GBP 22.39 and JPY 4,729 (minor units are hundredths in every currency)
    assert out == {"SWIFT:GBP": 2240, "SWIFT:JPY": 472900}  # whole yen: JPY has no minor unit
    assert data["policy"]["payout_fees_minor"] == {"SWIFT:EUR": 1343, "SWIFT:GBP": 2240, "SWIFT:JPY": 472900}  # the plan reads it there
    assert facts._estimate_swift_fees(data) == {}  # nothing is estimated twice, and a fee that was seen is never replaced


def test_the_plan_then_holds_the_fee_in_the_payout_currency():
    data = _data({"SWIFT:EUR": 1343}, [("GBP", "SWIFT", 240_000)])
    before = TreasuryState.model_validate(data)
    assert obligation_needs(before.obligations[0], before) == {"GBP": 240_000, "EUR": 1_285}  # the Kit's flat fee, in euros
    facts._estimate_swift_fees(data)
    after = TreasuryState.model_validate(data)
    assert obligation_needs(after.obligations[0], after) == {"GBP": 240_000 + 2240}  # pounds for the pounds payout, no euros bought


def test_without_any_observed_swift_fee_the_kits_rule_stands():
    data = _data({"LOCAL:USD": 300}, [("GBP", "SWIFT", 240_000)])
    assert facts._estimate_swift_fees(data) == {} and "SWIFT:GBP" not in data["policy"]["payout_fees_minor"]
    data = _data({"SWIFT:EUR": 1343}, [("IDR", "SWIFT", 8_500_000_000)])  # paid through from the base currency: its own rule
    assert facts._estimate_swift_fees(data) == {}
