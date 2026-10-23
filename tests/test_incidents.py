"""Kit 03: Payment Ops Incident Commander, sentence by sentence."""
import asyncio

import httpx
import pytest

from conftest import scenario
from venthora.audit import AuditStore
from venthora.domain.models import TreasuryState
from venthora.execution.executor import Executor
from venthora.execution.incidents import IncidentCommander, IncidentRequest, decide
from venthora.execution.models import ExecutionRequest
from venthora.execution.store import ExecutionStore
from venthora.gateways.airwallex import SANDBOX_URL, Airwallex, TransferResult
from venthora.orchestrator import Orchestrator
from venthora.proofcore.gate import ProofCore
from venthora.testing.fake_airwallex import FakeAirwallex


@pytest.fixture(autouse=True)
def creds(monkeypatch):
    monkeypatch.setenv("AIRWALLEX_CLIENT_ID", "c")
    monkeypatch.setenv("AIRWALLEX_API_KEY", "k")


def transfer(status, failure=None, request_id="req-original-0001"):
    return TransferResult(transfer_id="t-orig", status=status, source_currency="EUR", transfer_currency="EUR",
                          transfer_amount_minor=150_000, amount_payer_pays_minor=151_285, fee_minor=1_285,
                          failure_reason="simulation" if failure else None, raw={"request_id": request_id},
                          failure_type=failure)


POLICY = TreasuryState.model_validate(scenario("kit03_incident")).policy


# -- decision table (code, not prompt) -----------------------------------------------------

@pytest.mark.parametrize(("status", "failure", "hours", "action"), [
    ("PAID", None, 0, "RESOLVE"),
    ("SENT", None, 12, "WAIT"),                       # SENT is in flight, never final
    ("PROCESSING", None, 47, "WAIT"),
    ("SENT", None, 49, "ESCALATE"),                    # beyond the wait window: a person chases, nobody pays twice
    ("CANCELLED", "BENEFICIARY_BANK_RETURNED", 0, "REPLACE"),
    ("CANCELLED", "CHANNEL_TIMEOUT", 0, "REPLACE"),
    ("CANCELLED", "INVALID_ACCOUNT_NAME_OR_NUMBER", 0, "ESCALATE"),  # wrong payee details: never auto-replace
    ("CANCELLED", "ACCOUNT_CLOSED", 0, "ESCALATE"),
    ("CANCELLED", "RECALL_REQUESTED", 0, "ESCALATE"),
    ("CANCELLED", "EXCEEDED_TRANSACTION_AMOUNT_OR_LIMIT", 0, "ESCALATE"),
    ("CANCELLED", None, 0, "ESCALATE"),                # CANCELLED with no type: a person decides
])
def test_decision_table(status, failure, hours, action):
    assert decide(transfer(status, failure), hours, POLICY, 0).action == action


def test_only_one_replacement_then_a_person():
    assert decide(transfer("CANCELLED", "BENEFICIARY_BANK_RETURNED"), 0, POLICY, 1).action == "ESCALATE"


# -- end to end on the fake sandbox ---------------------------------------------------------

def harness(fake, tmp_path):
    client = httpx.AsyncClient(base_url=SANDBOX_URL, transport=httpx.MockTransport(fake.handler))
    agent = Orchestrator(ProofCore(airwallex=Airwallex(base_url=SANDBOX_URL, client=client, backoff_s=0)))
    executor = Executor(agent, ExecutionStore(str(tmp_path / "e.db")), AuditStore(str(tmp_path / "a.db")), poll_interval_s=0)
    return agent, executor


async def pay_then_incident(fake, tmp_path, failure, hours=0, settle_original=False, twice=False, eur_budget_minor=None):
    agent, executor = harness(fake, tmp_path)
    try:
        s = scenario("kit03_incident")
        if eur_budget_minor is not None:  # the book's own EUR allocation (the plan converts for any shortfall)
            s["balances"]["EUR"] = eur_budget_minor
        state = TreasuryState.model_validate(s)
        plan = await agent.decide(state)
        # Pay, but leave the transfer in an intermediate state (guide demo flow).
        result = await executor.execute(ExecutionRequest(state=state, plan_hash=plan.plan_hash, simulate_settlement=False))
        assert result.status == "IN_FLIGHT", result.message
        if settle_original:
            [tid] = [t["id"] for t in fake.transfers.values()]
            fake.transfers[tid]["status"] = "SENT"
            fake.transfers[tid]["status"] = "PAID"
        cmd = IncidentCommander(executor)
        req = IncidentRequest(state=state, obligation_id="SUPPLIER-EU-NOV", simulate_failure_type=failure,
                              hours_in_flight=hours)
        first = await cmd.handle(req)
        second = await cmd.handle(req) if twice else None
        return executor, first, second
    finally:
        await agent.close()


def test_replace_under_duplicate_lock_with_second_fee_and_resolution(tmp_path):
    fake = FakeAirwallex(balances={"USD": "8000", "EUR": "1700", "GBP": "0"})
    executor, r, _ = asyncio.run(pay_then_incident(fake, tmp_path, "BENEFICIARY_BANK_RETURNED"))
    assert r.decision == "REPLACE" and r.status == "RESOLVED"
    assert r.original_status == "CANCELLED" and r.original_failure_type == "BENEFICIARY_BANK_RETURNED"
    assert r.replacement_status == "PAID" and r.replacement_transfer_id != r.original_transfer_id
    ids = {t["request_id"] for t in fake.transfers.values()}
    assert len(ids) == 2  # the original and the replacement never share a request_id
    # Two SWIFT fees left the wallet: EUR 1,700 - 1,500 - 12.85 (lost on the original) - 12.85 - ... = one live payout
    assert r.reconciliation["discrepancy_minor"] == 0 and r.reconciliation["second_fee_minor"] == 1_285
    assert sum(t["status"] == "PAID" for t in fake.transfers.values()) == 1
    assert executor.store.payout("invoice:eu-components:INV-2026-0601")["status"] == "PAID"
    assert executor.audit.verify()["verified"]


def test_wrong_payee_details_are_escalated_not_replaced(tmp_path):
    fake = FakeAirwallex(balances={"USD": "8000", "EUR": "1700", "GBP": "0"})
    _, r, _ = asyncio.run(pay_then_incident(fake, tmp_path, "INVALID_ACCOUNT_NAME_OR_NUMBER"))
    assert r.decision == "ESCALATE" and r.status == "ESCALATED" and r.replacement_transfer_id is None
    assert len(fake.transfers) == 1


def test_in_flight_transfer_is_waited_on_never_replaced(tmp_path):
    fake = FakeAirwallex(balances={"USD": "8000", "EUR": "1700", "GBP": "0"})
    _, r, _ = asyncio.run(pay_then_incident(fake, tmp_path, None, hours=12))
    assert r.decision == "WAIT" and r.status == "WAITING" and len(fake.transfers) == 1
    _, r2, _ = asyncio.run(pay_then_incident(FakeAirwallex(balances={"USD": "8000", "EUR": "1700", "GBP": "0"}),
                                             tmp_path / "b", None, hours=60))
    assert r2.decision == "ESCALATE" and len(fake.transfers) == 1


def test_second_incident_on_the_same_invoice_does_not_pay_a_third_time(tmp_path):
    fake = FakeAirwallex(balances={"USD": "8000", "EUR": "1700", "GBP": "0"})
    _, first, second = asyncio.run(pay_then_incident(fake, tmp_path, "BENEFICIARY_BANK_RETURNED", twice=True))
    assert first.status == "RESOLVED"
    assert second.decision == "RESOLVE"  # the replacement is PAID: the invoice is settled
    assert sum(t["status"] == "PAID" for t in fake.transfers.values()) == 1 and len(fake.transfers) == 2


def test_replacement_without_funds_for_the_second_fee_is_refused(tmp_path):
    # EUR 1,512.85 covers exactly one transfer + fee; after the failed original the fee is gone,
    # so the replacement plus its second fee no longer fits: refused, a person re-plans.
    fake = FakeAirwallex(balances={"USD": "8000", "EUR": "1512.85", "GBP": "0"})
    _, r, _ = asyncio.run(pay_then_incident(fake, tmp_path, "BENEFICIARY_BANK_RETURNED", eur_budget_minor=151_285))
    assert r.decision == "REPLACE" and r.status == "ESCALATED"
    assert any(e.step == "budget" and e.status == "REFUSED" for e in r.events)
    assert len(fake.transfers) == 1


def test_incident_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from venthora.api import app

    monkeypatch.delenv("AIRWALLEX_CLIENT_ID")
    with TestClient(app) as client:
        state = client.get("/api/scenarios/kit03_incident").json()["state"]
        res = client.post("/v1/incidents", json={"state": state, "obligation_id": "SUPPLIER-EU-NOV"})
        assert res.status_code == 404  # nothing paid yet: no transfer on record
        assert client.get("/v1/incidents").json() == []
