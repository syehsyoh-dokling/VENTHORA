"""Findings from the real sandbox run: real fees differ from the guide, and execution must teach the next plan."""
import asyncio

import httpx

from conftest import scenario
from venthora.audit import AuditStore
from venthora.domain.models import TreasuryState
from venthora.domain.projection import obligation_needs
from venthora.execution.executor import Executor
from venthora.execution.facts import apply_operational_facts
from venthora.execution.models import ExecutionRequest
from venthora.execution.store import ExecutionStore
from venthora.gateways.airwallex import SANDBOX_URL, Airwallex
from venthora.orchestrator import Orchestrator
from venthora.proofcore.gate import ProofCore
from venthora.testing.fake_airwallex import REAL_SANDBOX_FEES, FakeAirwallex


def test_observed_fee_overrides_the_kit_flat_fee():
    s = scenario("kit01_baseline")
    s["policy"]["payout_fees_minor"] = {"SWIFT:EUR": 1337, "LOCAL:USD": 300}
    state = TreasuryState.model_validate(s)
    by_id = {o.obligation_id: o for o in state.obligations}
    assert obligation_needs(by_id["SUPPLIER-EU"], state) == {"EUR": 401_337}
    assert obligation_needs(by_id["PAYROLL-USD"], state) == {"USD": 800_300}
    assert obligation_needs(by_id["VENDOR-UK"], state) == {"GBP": 300_000}  # unknown LOCAL GBP fee: none assumed


def run_cycle(fake, store, audit, state):
    """One plan-execute cycle on a ring-fenced budget (the wallet holds far more than the allocation)."""
    async def go():
        client = httpx.AsyncClient(base_url=SANDBOX_URL, transport=httpx.MockTransport(fake.handler))
        agent = Orchestrator(ProofCore(airwallex=Airwallex(base_url=SANDBOX_URL, client=client, backoff_s=0)))
        try:
            planned, _ = apply_operational_facts(state, store)
            plan = await agent.decide(planned)
            ex = Executor(agent, store, audit, poll_interval_s=0)
            return planned, plan, await ex.execute(ExecutionRequest(state=planned, plan_hash=plan.plan_hash))
        finally:
            await agent.close()
    return asyncio.run(go())


def test_real_fees_show_as_variance_then_are_learned(tmp_path, monkeypatch):
    monkeypatch.setenv("AIRWALLEX_CLIENT_ID", "c")
    monkeypatch.setenv("AIRWALLEX_API_KEY", "k")
    fake = FakeAirwallex(balances={"USD": "10000000", "EUR": "10000000", "GBP": "10000000"})  # like the real sandbox
    fake.fees = dict(REAL_SANDBOX_FEES)
    store, audit = ExecutionStore(str(tmp_path / "x.db")), AuditStore(str(tmp_path / "a.db"))
    state = TreasuryState.model_validate(scenario("kit01_baseline"))

    _, _, first = run_cycle(fake, store, audit, state)
    assert first.status == "COMPLETED" and first.reconciliation.discrepancies_minor == {}
    # Plan assumed EUR 12.85 SWIFT and free LOCAL; Airwallex charged EUR 13.37 and USD 3.00 x2.
    # USD: -600 of fees, +1 because the booked conversion cost a cent less than the rounded-up estimate.
    assert first.reconciliation.plan_variance_minor == {"EUR": -52, "USD": -599}
    assert "budget exceeded by EUR 0.52" in first.message
    assert store.observed_fees() == {"SWIFT:EUR": 1337, "LOCAL:USD": 300}

    # Next cycle: paid invoices are settled (not re-proposed) and the learned fees are planned for.
    planned, plan, _ = run_cycle(fake, store, audit, state)
    kinds = {o.obligation_id: o.record_kind for o in planned.obligations}
    assert kinds["PAYROLL-USD"] == kinds["SUPPLIER-EU"] == kinds["SAAS-USD"] == "settled"
    assert planned.policy.payout_fees_minor["SWIFT:EUR"] == 1337
    assert not any(d.obligation_id == "PAYROLL-USD" and d.action == "PAY" for d in plan.obligations)
