"""Executing verified plans on the (fake) Airwallex sandbox, and proving the outcome."""
import asyncio
from decimal import Decimal

import httpx
import pytest
from fake_airwallex import FakeAirwallex

from conftest import scenario
from venthora.audit import AuditStore
from venthora.domain.models import TreasuryState
from venthora.execution.executor import Executor
from venthora.execution.live import with_live_balances_and_rates
from venthora.execution.models import Approval, ExecutionRequest
from venthora.execution.store import ExecutionStore
from venthora.gateways.airwallex import PRODUCTION_URL, SANDBOX_URL, Airwallex, AirwallexError
from venthora.orchestrator import Orchestrator
from venthora.proofcore.gate import ProofCore

FINANCE_MANAGER = Approval(approver="Dana (Head of Finance)", role="finance_manager")


@pytest.fixture(autouse=True)
def sandbox_credentials(monkeypatch):
    monkeypatch.setenv("AIRWALLEX_CLIENT_ID", "sandbox-client")
    monkeypatch.setenv("AIRWALLEX_API_KEY", "sandbox-key")


def harness(fake: FakeAirwallex, tmp_path, base_url: str = SANDBOX_URL):
    client = httpx.AsyncClient(base_url=base_url, transport=httpx.MockTransport(fake.handler))
    bank = Airwallex(base_url=base_url, client=client, backoff_s=0)
    agent = Orchestrator(ProofCore(airwallex=bank))
    executor = Executor(agent, ExecutionStore(str(tmp_path / "exec.db")), AuditStore(str(tmp_path / "audit.db")),
                        poll_interval_s=0)
    return agent, executor


def run_plan_and_execute(fake, tmp_path, name="kit01_baseline", approval=None, mutate=None, **req_kw):
    async def go():
        agent, executor = harness(fake, tmp_path)
        try:
            s = scenario(name)
            if mutate:
                mutate(s)
            state = TreasuryState.model_validate(s)
            plan = await agent.decide(state)
            result = await executor.execute(ExecutionRequest(state=state, plan_hash=plan.plan_hash,
                                                             approval=approval, **req_kw))
            return plan, result, executor
        finally:
            await agent.close()
    return asyncio.run(go())


def transfers_by_reference(fake):
    return sorted(t["reference"].split()[0] for t in fake.transfers.values())


def test_baseline_executes_converts_pays_and_reconciles(tmp_path):
    fake = FakeAirwallex()
    plan, result, executor = run_plan_and_execute(fake, tmp_path)
    assert plan.decision == "APPROVED"
    assert result.status == "COMPLETED", result.message
    # One FX conversion: EUR 4,012.85 (EUR 4,000 + EUR 12.85 SWIFT fee), booked with a single-use quote.
    [conv] = fake.conversions.values()
    assert conv["buy_currency"] == "EUR" and conv["buy_amount"] == "4012.85"
    assert sum(q["used"] for q in fake.quotes.values()) == 1
    # Transfers for every PAY / CONVERT_AND_PAY decision, all driven to PAID.
    assert transfers_by_reference(fake) == ["PAYROLL-USD", "SAAS-USD", "SUPPLIER-EU"]
    assert {t["status"] for t in fake.transfers.values()} == {"PAID"}
    swift = next(t for t in fake.transfers.values() if t["transfer_method"] == "SWIFT")
    assert swift["fee_amount"] == 12.85
    # Reconciliation: live balances equal the expected ones, and the reserve still holds.
    recon = result.reconciliation
    assert recon.discrepancies_minor == {}
    assert recon.live_after_minor == {"USD": 2_000_000 - 435_394 - 800_000 - 120_000, "EUR": 0, "GBP": 0}
    assert recon.base_floor_ok and recon.live_after_minor["USD"] >= recon.base_floor_minor
    assert "reserve confirmed" in result.message
    assert executor.audit.verify()["verified"]


def test_requires_approval_is_refused_without_a_person(tmp_path):
    fake = FakeAirwallex()
    plan, result, _ = run_plan_and_execute(fake, tmp_path, "kit01_receipt_delayed")
    assert plan.decision == "REQUIRES_APPROVAL"
    assert result.status == "REFUSED" and "requires a person's approval" in result.message
    assert fake.quotes == {} and fake.transfers == {}


def test_wrong_role_cannot_approve(tmp_path):
    fake = FakeAirwallex()
    _, result, _ = run_plan_and_execute(fake, tmp_path, "kit01_receipt_delayed",
                                        approval=Approval(approver="Sam", role="treasury_agent"))
    assert result.status == "REFUSED" and fake.transfers == {}


def test_finance_manager_approval_executes_and_escalated_item_is_not_paid(tmp_path):
    fake = FakeAirwallex()
    _, result, _ = run_plan_and_execute(fake, tmp_path, "kit01_receipt_delayed", approval=FINANCE_MANAGER)
    assert result.status == "COMPLETED", result.message
    assert "VENDOR-UK" not in transfers_by_reference(fake)  # escalated, not paid
    assert result.steps[0].detail["approved_by"] == FINANCE_MANAGER.approver


def test_plan_hash_must_match_what_was_approved(tmp_path):
    fake = FakeAirwallex()

    async def go():
        agent, executor = harness(fake, tmp_path)
        try:
            state = TreasuryState.model_validate(scenario("kit01_baseline"))
            return await executor.execute(ExecutionRequest(state=state, plan_hash="0" * 64))
        finally:
            await agent.close()

    result = asyncio.run(go())
    assert result.status == "REFUSED" and "changed since it was reviewed" in result.message
    assert fake.transfers == {}


def test_quote_slippage_aborts_before_anything_is_booked(tmp_path):
    fake = FakeAirwallex()
    fake.quote_markup = Decimal("0.05")  # quote 5% worse than the planning rate
    _, result, executor = run_plan_and_execute(fake, tmp_path)
    assert result.status == "FAILED" and "slippage limit" in result.message
    assert fake.conversions == {} and fake.transfers == {}
    assert executor.store.payout("obligation:PAYROLL-USD")["status"] == "RELEASED"


def test_same_invoice_cannot_be_paid_twice_across_executions(tmp_path):
    fake = FakeAirwallex()
    run_plan_and_execute(fake, tmp_path)
    paid = len(fake.transfers)
    fake.balances["USD"] += Decimal("13553.94")  # top up so the second plan is feasible
    _, second, _ = run_plan_and_execute(fake, tmp_path)
    assert second.status == "FAILED" and "duplicate payment blocked" in second.message
    assert len(fake.transfers) == paid and len(fake.conversions) == 1  # nothing moved the second time


def test_lost_response_is_recovered_by_request_id_not_paid_twice(tmp_path):
    fake = FakeAirwallex()
    fake.lose_response_once = {"/api/v1/transfers/create", "/api/v1/fx/conversions/create"}
    _, result, _ = run_plan_and_execute(fake, tmp_path)
    assert result.status == "COMPLETED", result.message
    assert transfers_by_reference(fake) == ["PAYROLL-USD", "SAAS-USD", "SUPPLIER-EU"]
    assert len(fake.conversions) == 1


def test_failed_payout_is_reported_refunded_and_retryable(tmp_path):
    fake = FakeAirwallex()
    fake.fail_on_paid = {"SAAS-USD"}
    _, result, executor = run_plan_and_execute(fake, tmp_path)
    assert result.status == "FAILED" and "SAAS-USD" in result.message
    assert result.reconciliation.discrepancies_minor == {}  # refund is reflected in expected balances
    assert executor.store.payout("obligation:SAAS-USD")["status"] == "FAILED"
    assert executor.store.payout("obligation:PAYROLL-USD")["status"] == "PAID"


def test_resume_after_crash_reuses_request_ids(tmp_path):
    fake = FakeAirwallex()

    async def go():
        agent, executor = harness(fake, tmp_path)
        real_create = executor.bank.create_transfer
        calls = {"n": 0}

        async def crash_on_second(**kw):
            calls["n"] += 1
            result = await real_create(**kw)
            if calls["n"] == 2:
                raise RuntimeError("process died after Airwallex accepted the transfer")
            return result

        executor.bank.create_transfer = crash_on_second
        try:
            state = TreasuryState.model_validate(scenario("kit01_baseline"))
            plan = await agent.decide(state)
            with pytest.raises(RuntimeError):
                await executor.execute(ExecutionRequest(state=state, plan_hash=plan.plan_hash))
            [eid] = [r["execution_id"] for r in executor.store.list_executions()]
            executor.bank.create_transfer = real_create
            return await executor.resume(eid)
        finally:
            await agent.close()

    result = asyncio.run(go())
    assert result.status == "COMPLETED", result.message
    assert transfers_by_reference(fake) == ["PAYROLL-USD", "SAAS-USD", "SUPPLIER-EU"]  # no duplicates
    assert len(fake.conversions) == 1


def test_stale_state_is_blocked_by_live_balance_check(tmp_path):
    fake = FakeAirwallex(balances={"USD": "9000", "EUR": "0", "GBP": "0"})
    plan, result, _ = run_plan_and_execute(fake, tmp_path)
    assert plan.decision == "BLOCKED"
    assert result.status == "REFUSED" and fake.transfers == {}


def test_live_state_and_deposit_simulation(tmp_path):
    fake = FakeAirwallex(rates={"EURUSD": "1.1000", "USDGBP": "0.8000"})

    async def go():
        agent, _ = harness(fake, tmp_path)
        bank = agent.proof.airwallex
        try:
            await bank.simulate_deposit(global_account_id="ga-usd-1", amount_minor=1_000_000, payer_name="Acme Retail Ltd")
            return await with_live_balances_and_rates(TreasuryState.model_validate(scenario("kit01_baseline")), bank)
        finally:
            await agent.close()

    state = asyncio.run(go())
    assert state.balances["USD"] == 3_000_000
    assert Decimal(state.fx_to_base["EUR"]) == Decimal("1.1") and Decimal(state.fx_to_base["GBP"]) == Decimal("1.25")


def test_simulation_and_execution_are_sandbox_only(tmp_path):
    fake = FakeAirwallex()

    async def go():
        agent, executor = harness(fake, tmp_path, base_url=PRODUCTION_URL)
        try:
            with pytest.raises(AirwallexError):
                await agent.proof.airwallex.simulate_deposit(global_account_id="x", amount_minor=1, payer_name="x")
            state = TreasuryState.model_validate(scenario("kit01_baseline"))
            plan = await agent.decide(state)
            return await executor.execute(ExecutionRequest(state=state, plan_hash=plan.plan_hash))
        finally:
            await agent.close()

    result = asyncio.run(go())
    assert result.status == "REFUSED" and "sandbox" in result.message
    assert fake.transfers == {}
