"""Executing verified plans on the (fake) Airwallex sandbox, and proving the outcome."""
import asyncio
from decimal import Decimal

import httpx
import pytest

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
from venthora.testing.fake_airwallex import FakeAirwallex

FINANCE_MANAGER = Approval(approver="Dana (Head of Finance)", role="finance_manager")
WALLET = {"USD": "22500", "EUR": "0", "GBP": "0"}  # exactly the scenario's cash


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


def run_plan_and_execute(fake, tmp_path, name="kit01_initial", approval=None, mutate=None, **req_kw):
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


def paid(fake):
    return sorted(t["reference"].split()[0] for t in fake.transfers.values())


def test_initial_plan_executes_converts_pays_and_reconciles(tmp_path):
    fake = FakeAirwallex(balances=WALLET)
    plan, result, executor = run_plan_and_execute(fake, tmp_path)
    assert plan.decision == "APPROVED"
    assert result.status == "COMPLETED", result.message
    # Minimum conversions, each booked once with a single-use quote.
    bought = sorted((c["buy_currency"], c["buy_amount"]) for c in fake.conversions.values())
    assert bought == [("EUR", "4012.85"), ("GBP", "2500.00")]
    assert sum(q["used"] for q in fake.quotes.values()) == 2
    # Shipping (operations halt), payroll and the time-sensitive supplier are paid; the escalated
    # policy exception and the deferred cheapest bill are not.
    assert paid(fake) == ["PAYROLL-NOV", "SHIPPING-UK", "SUPPLIER-EU"]
    assert {t["status"] for t in fake.transfers.values()} == {"PAID"}
    swift = next(t for t in fake.transfers.values() if t["transfer_method"] == "SWIFT")
    assert swift["fee_amount"] == 12.85
    recon = result.reconciliation
    assert recon.discrepancies_minor == {}
    assert recon.live_after_minor == {"USD": 2_250_000 - 435_394 - 317_500 - 800_000, "EUR": 0, "GBP": 0}
    assert recon.base_floor_ok and "confirmed at USD 6,971.06" in result.message
    assert executor.audit.verify()["verified"]
    # Guide: schema and validate are called before each beneficiary is created.
    calls = [path for _m, path in fake.calls]
    assert calls.count("/api/v1/beneficiaries/schema") == 3 and calls.count("/api/v1/beneficiaries/validate") == 3


def test_requires_approval_is_refused_without_a_person(tmp_path):
    fake = FakeAirwallex(balances=WALLET)
    plan, result, _ = run_plan_and_execute(fake, tmp_path, "kit01_receipt_delayed")
    assert plan.decision == "REQUIRES_APPROVAL"
    assert result.status == "REFUSED" and "requires a person's approval" in result.message
    assert fake.quotes == {} and fake.transfers == {}


def test_wrong_role_cannot_approve(tmp_path):
    fake = FakeAirwallex(balances=WALLET)
    _, result, _ = run_plan_and_execute(fake, tmp_path, "kit01_receipt_delayed",
                                        approval=Approval(approver="Sam", role="treasury_agent"))
    assert result.status == "REFUSED" and fake.transfers == {}


def test_finance_manager_approval_executes_and_escalated_items_are_not_paid(tmp_path):
    fake = FakeAirwallex(balances=WALLET)
    _, result, _ = run_plan_and_execute(fake, tmp_path, "kit01_receipt_delayed", approval=FINANCE_MANAGER)
    assert result.status == "COMPLETED", result.message
    assert "CONTRACTOR-US" not in paid(fake) and "SAAS-USD" not in paid(fake)
    assert result.steps[0].detail["approved_by"] == FINANCE_MANAGER.approver


def test_plan_hash_must_match_what_was_approved(tmp_path):
    fake = FakeAirwallex(balances=WALLET)

    async def go():
        agent, executor = harness(fake, tmp_path)
        try:
            state = TreasuryState.model_validate(scenario("kit01_initial"))
            return await executor.execute(ExecutionRequest(state=state, plan_hash="0" * 64))
        finally:
            await agent.close()

    result = asyncio.run(go())
    assert result.status == "REFUSED" and "changed since it was reviewed" in result.message
    assert fake.transfers == {}


def test_quote_slippage_aborts_before_anything_is_booked(tmp_path):
    fake = FakeAirwallex(balances=WALLET)
    fake.quote_markup = Decimal("0.05")  # quote 5% worse than the planning rate
    _, result, executor = run_plan_and_execute(fake, tmp_path)
    assert result.status == "FAILED" and "slippage limit" in result.message
    assert fake.conversions == {} and fake.transfers == {}
    assert executor.store.payout("invoice:northwind:PR-2026-11")["status"] == "RELEASED"


def test_same_invoice_cannot_be_paid_twice_across_executions(tmp_path):
    fake = FakeAirwallex(balances=WALLET)
    run_plan_and_execute(fake, tmp_path)
    before = len(fake.transfers)
    fake.balances["USD"] += Decimal("15528.94")  # top up so the second plan is feasible again
    _, second, _ = run_plan_and_execute(fake, tmp_path)
    assert second.status == "FAILED" and "duplicate payment blocked" in second.message
    assert len(fake.transfers) == before and len(fake.conversions) == 2  # nothing moved the second time


def test_lost_response_is_recovered_by_request_id_not_paid_twice(tmp_path):
    fake = FakeAirwallex(balances=WALLET)
    fake.lose_response_once = {"/api/v1/transfers/create", "/api/v1/fx/conversions/create"}
    _, result, _ = run_plan_and_execute(fake, tmp_path)
    assert result.status == "COMPLETED", result.message
    assert paid(fake) == ["PAYROLL-NOV", "SHIPPING-UK", "SUPPLIER-EU"]
    assert len(fake.conversions) == 2


def test_failed_payout_is_reported_refunded_and_retryable(tmp_path):
    fake = FakeAirwallex(balances=WALLET)
    fake.fail_on_paid = {"SHIPPING-UK"}
    _, result, executor = run_plan_and_execute(fake, tmp_path)
    assert result.status == "FAILED" and "SHIPPING-UK" in result.message
    assert result.reconciliation.discrepancies_minor == {}  # the refund is reflected in expected balances
    assert executor.store.payout("invoice:thames-freight:TF-311")["status"] == "FAILED"
    assert executor.store.payout("invoice:northwind:PR-2026-11")["status"] == "PAID"


def test_resume_after_crash_reuses_request_ids(tmp_path):
    fake = FakeAirwallex(balances=WALLET)

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
            state = TreasuryState.model_validate(scenario("kit01_initial"))
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
    assert paid(fake) == ["PAYROLL-NOV", "SHIPPING-UK", "SUPPLIER-EU"]  # no duplicates
    assert len(fake.conversions) == 2


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
            assert (await bank.list_deposits())[-1]["status"] == "SETTLED"
            return await with_live_balances_and_rates(TreasuryState.model_validate(scenario("kit01_baseline")), bank)
        finally:
            await agent.close()

    state = asyncio.run(go())
    assert state.balances["USD"] == 3_000_000
    assert Decimal(state.fx_to_base["EUR"]) == Decimal("1.1") and Decimal(state.fx_to_base["GBP"]) == Decimal("1.25")


def test_beneficiary_failing_the_corridor_schema_is_never_created(tmp_path):
    fake = FakeAirwallex(balances=WALLET)

    async def go():
        agent, executor = harness(fake, tmp_path)
        import copy
        executor.beneficiaries = copy.deepcopy(executor.beneficiaries)
        del executor.beneficiaries["payroll-group-us"]["payload"]["beneficiary"]["bank_details"]["bank_name"]
        try:
            state = TreasuryState.model_validate(scenario("kit01_initial"))
            plan = await agent.decide(state)
            return await executor.execute(ExecutionRequest(state=state, plan_hash=plan.plan_hash))
        finally:
            await agent.close()

    result = asyncio.run(go())
    assert result.status == "FAILED" and "corridor schema" in result.message
    assert "PAYROLL-NOV" not in paid(fake)


def test_simulation_and_execution_are_sandbox_only(tmp_path):
    fake = FakeAirwallex(balances=WALLET)

    async def go():
        agent, executor = harness(fake, tmp_path, base_url=PRODUCTION_URL)
        try:
            with pytest.raises(AirwallexError):
                await agent.proof.airwallex.simulate_deposit(global_account_id="x", amount_minor=1, payer_name="x")
            state = TreasuryState.model_validate(scenario("kit01_initial"))
            plan = await agent.decide(state)
            return await executor.execute(ExecutionRequest(state=state, plan_hash=plan.plan_hash))
        finally:
            await agent.close()

    result = asyncio.run(go())
    assert result.status == "REFUSED" and "sandbox" in result.message
    assert fake.transfers == {}
