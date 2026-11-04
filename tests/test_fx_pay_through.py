# ruff: noqa: E501
"""Every currency Airwallex pays out: a currency it cannot fund a transfer from (IDR, INR, ...) is paid through, funded in
USD and converted inside the transfer; every exchange and payout fee is recorded at its real cost (vs mid-market)."""
import asyncio
from decimal import Decimal

import pytest
from test_execution import WALLET, harness

from conftest import scenario
from venthora.domain.models import TreasuryState
from venthora.domain.projection import obligation_needs, project
from venthora.execution.models import ExecutionRequest
from venthora.fx_routes import PAYOUT_CURRENCIES, SOURCE_CURRENCIES, exchange_cost, pays_through, value_at_mid
from venthora.testing.fake_airwallex import FakeAirwallex

IDR_MID = Decimal("18157.7")  # IDR per USD (the sandbox's USDIDR mid-market, 5 Nov 2026)
MARKUP = Decimal("0.0000005")  # USD per IDR above mid: the client rate the fake converts at (about 0.9%)


@pytest.fixture(autouse=True)
def sandbox_credentials(monkeypatch):
    monkeypatch.setenv("AIRWALLEX_CLIENT_ID", "sandbox-client")
    monkeypatch.setenv("AIRWALLEX_API_KEY", "sandbox-key")


def _idr_state() -> dict:
    s = scenario("kit01_initial")
    ship = next(o for o in s["obligations"] if o["obligation_id"] == "SHIPPING-UK")
    ship.update(currency="IDR", amount_minor=5_700_000_000)  # IDR 57,000,000.00 (the freight bill, now owed in IDR)
    s["fx_to_base"]["IDR"] = "0.0000556"  # USD per IDR, at or above the client rate
    return s


def test_the_currency_lists_come_from_the_sandbox():
    assert {"IDR", "INR", "MYR", "NGN", "KES"} <= PAYOUT_CURRENCIES and "IDR" not in SOURCE_CURRENCIES
    assert pays_through("IDR") and pays_through("INR") and not pays_through("EUR") and not pays_through("USD")
    assert not pays_through("XYZ")  # not a payout currency at all
    # the model accepts any payout currency and refuses one Airwallex does not pay out
    s = _idr_state()
    TreasuryState.model_validate(s)
    s["obligations"][0]["currency"] = "XYZ"
    with pytest.raises(ValueError, match="not a currency Airwallex pays out"):
        TreasuryState.model_validate(s)


def test_a_paid_through_payout_needs_base_cash_and_no_conversion():
    state = TreasuryState.model_validate(_idr_state())
    o = next(x for x in state.obligations if x.obligation_id == "SHIPPING-UK")
    needs = obligation_needs(o, state)
    assert set(needs) == {"USD"} and needs["USD"] == 316_920  # 57,000,000 x 0.0000556
    p = project(state, ["SHIPPING-UK"])
    assert p.conversions == [] and p.base_cost_minor == 316_920  # no IDR wallet is ever bought or used


def test_the_spread_is_measured_against_mid_market():
    # the real sandbox quotes (5 Nov 2026): USDIDR and EURUSD
    assert value_at_mid(5_000_000, "IDR", "USD", "USDIDR", "18157.7") == 275
    idr = exchange_cost(sell_ccy="USD", sell_minor=278, buy_ccy="IDR", buy_minor=5_000_000, pair="USDIDR", mid="18157.7")
    assert idr["at_mid_minor"] == 275 and idr["spread_minor"] == 3
    eur = exchange_cost(sell_ccy="USD", sell_minor=11_343, buy_ccy="EUR", buy_minor=10_000, pair="EURUSD", mid="1.12278")
    assert eur["at_mid_minor"] == 11_228 and eur["spread_minor"] == 115 and eur["spread_bp"] == 102
    assert exchange_cost(sell_ccy="USD", sell_minor=1, buy_ccy="EUR", buy_minor=1, pair=None, mid=None)["spread_minor"] is None


def test_an_idr_salary_is_paid_from_usd_and_its_costs_are_recorded(tmp_path):
    fake = FakeAirwallex(balances=WALLET | {"IDR": "10000000"}, rates={"EURUSD": "1.0850", "GBPUSD": "1.2700", "USDIDR": str(IDR_MID)})
    fake.quote_markup = MARKUP
    fake.fees[("LOCAL", "IDR")] = Decimal("5.00")  # charged in USD, the funding currency (as on the sandbox)

    async def go():
        agent, executor = harness(fake, tmp_path)
        try:
            state = TreasuryState.model_validate(_idr_state())
            plan = await agent.decide(state)
            d = next(x for x in plan.obligations if x.obligation_id == "SHIPPING-UK")
            assert d.action == "CONVERT_AND_PAY"
            result = await executor.execute(ExecutionRequest(state=state, plan_hash=plan.plan_hash))
            return result, executor
        finally:
            await agent.close()

    result, executor = asyncio.run(go())
    assert result.status == "COMPLETED", result.message
    t = next(x for x in fake.transfers.values() if x["reference"].startswith("SHIPPING-UK"))
    assert (t["source_currency"], t["transfer_currency"], t["transfer_amount"]) == ("USD", "IDR", 57_000_000.0)
    assert fake.balances["IDR"] == Decimal("10000000.00")  # the IDR wallet is never touched
    assert "IDR" not in {c["buy_currency"] for c in fake.conversions.values()}
    assert result.reconciliation.discrepancies_minor == {}
    # the book: the payout is committed and settled in USD, what really left the wallet
    ref = t["id"]
    entries = [e for e in executor.store.journal() if e["ref"] == ref]
    assert {ln["currency"] for e in entries for ln in e["lines"]} == {"USD"}
    # the costs: the spread against mid-market and the fee, both in USD
    costs = {c["kind"]: c for c in executor.store.fx_costs() if c["obligation_id"] == "SHIPPING-UK"}
    pt = costs["pay_through"]
    funded = round(t["source_amount"] * 100)
    at_mid = round(Decimal(5_700_000_000) / IDR_MID)
    assert pt["exchange"]["at_mid_minor"] == at_mid and pt["spread_base_minor"] == funded - at_mid > 0
    assert pt["fee_base_minor"] == 500 and pt["reporting_period"]
    # every conversion of the run is recorded too (EUR here), with its own spread
    assert any(c["kind"] == "conversion" and c["exchange"]["buy_currency"] == "EUR" for c in executor.store.fx_costs())


def test_without_a_quote_the_paid_through_payout_never_moves(tmp_path):
    fake = FakeAirwallex(balances=WALLET, rates={"EURUSD": "1.0850", "GBPUSD": "1.2700", "USDIDR": str(IDR_MID)})
    fake.quote_markup = Decimal("0.00001")  # far above the plan's rate: beyond the slippage limit

    async def go():
        agent, executor = harness(fake, tmp_path)
        try:
            state = TreasuryState.model_validate(_idr_state())
            plan = await agent.decide(state)
            return await executor.execute(ExecutionRequest(state=state, plan_hash=plan.plan_hash))
        finally:
            await agent.close()

    result = asyncio.run(go())
    assert result.status == "FAILED" and "slippage limit" in result.message
    assert fake.transfers == {}


def test_the_report_sets_every_cost_against_what_was_converted_and_paid(tmp_path):
    from venthora import fx_cost_report
    from venthora.execution.store import ExecutionStore

    store = ExecutionStore(str(tmp_path / "x.db"))
    ex = exchange_cost(sell_ccy="USD", sell_minor=316_000, buy_ccy="IDR", buy_minor=5_700_000_000, pair="USDIDR", mid="18157.7")
    store.record_fx_cost({"ref": "t1", "kind": "pay_through", "obligation_id": "SAL-1", "at": "2026-11-05T01:00:00+00:00",
                          "reporting_period": "2026-11", "base_currency": "USD", "spread_base_minor": ex["spread_minor"],
                          "fee_base_minor": 500, "exchange": ex, "fee_currency": "USD", "fee_minor": 500})
    store.record_fx_cost({"ref": "t1", "kind": "pay_through", "at": "x", "reporting_period": "2026-11", "base_currency": "USD",
                          "fee_base_minor": 999})  # the same Airwallex ref is recorded once
    store.record_fx_cost({"ref": "t2", "kind": "payout_fee", "obligation_id": "V-1", "at": "2026-10-30T01:00:00+00:00",
                          "reporting_period": "2026-10", "base_currency": "USD", "spread_base_minor": None, "fee_base_minor": 300,
                          "fee_currency": "USD", "fee_minor": 300})
    nov = fx_cost_report.build(store, "2026-11")
    assert nov["periods"] == ["2026-11", "2026-10"] and len(nov["rows"]) == 1
    idr = nov["by_currency"][0]
    assert idr["currency"] == "IDR" and idr["spread_base_minor"] == ex["spread_minor"] > 0 and idr["fee_base_minor"] == 500
    assert nov["totals"]["total_base_minor"] == ex["spread_minor"] + 500 and nov["totals"]["converted_base_minor"] == 316_000
    assert fx_cost_report.build(store)["totals"]["fee_base_minor"] == 800
