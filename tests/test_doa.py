# ruff: noqa: E501
"""Delegation of Authority: standing payment instructions per internal category, the payroll register's two
approvals, the verdict (auto / approve / never), the payout currency, and a run paid on the fake sandbox."""
import asyncio
import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient
from test_execution import harness, sandbox_credentials  # noqa: F401 (the fixture)

from venthora import autonomy, doa
from venthora.config import ROOT
from venthora.domain.models import TreasuryState
from venthora.execution.models import ExecutionRequest
from venthora.testing.fake_airwallex import FakeAirwallex

FCO = "role=super_admin&by=Rosa%20Lindqvist"
LINES = [{"employee": "Ana Silva", "amount_minor": 420_000, "charge_code": "US-200.OPS.6010"},
         {"employee": "Ben Ortiz", "amount_minor": 380_000, "charge_code": "US-200.OPS.6010"}]


@pytest.fixture
def client():
    from venthora.api import app
    with TestClient(app) as c:
        yield c


def _now(c):
    return {x["key"]: x["now"] for x in c.get("/v1/doa").json()["internal"]}


def _period():
    return dt.date.today().strftime("%Y-%m")


def test_every_category_is_there_with_its_rules(client):
    d = client.get("/v1/doa?role=super_admin").json()
    assert [c["key"] for c in d["internal"]] == ["personnel", "fringe", "consultants", "travel", "office", "other_direct"]
    assert [c["key"] for c in d["external"]] == ["vendors", "specialists"]
    assert all(c["auto"] and c["approve"] and c["never"] for c in d["internal"] + d["external"])
    assert d["authority"]["you_may_change"] is True and d["autopilot"] is False and d["currencies"] == ["USD", "EUR", "GBP"]


def test_only_the_holder_of_the_delegation_sets_an_instruction(client):
    assert client.put("/v1/doa/instructions/office?role=finance_manager&by=Dana", json={"amount_minor": 50_000}).status_code == 403
    r = client.put(f"/v1/doa/instructions/office?{FCO}", json={"amount_minor": 50_000})
    assert r.status_code == 200 and r.json()["amount_minor"] == 50_000
    assert client.put(f"/v1/doa/instructions/office?{FCO}", json={"charge_code": "XX-1.OPS.6300"}).status_code == 422
    assert client.put(f"/v1/doa/instructions/office?{FCO}", json={"currency": "JPY"}).status_code == 422


def test_payroll_is_never_paid_without_both_approvals_and_the_first_one_needs_a_person(client):
    assert "no payroll register" in _now(client)["personnel"]["reasons"][0]
    r = client.post("/v1/doa/registers?role=finance_manager&by=Dana%20Finch", json={"period": _period(), "currency": "USD", "lines": LINES})
    assert r.status_code == 200 and r.json()["total_minor"] == 800_000
    assert _now(client)["personnel"]["verdict"] == "never"
    # the preparer approves nothing; HR first, then the FCO, two different people
    assert client.post(f"/v1/doa/registers/{_period()}/approve?step=hr&role=finance_manager&by=Dana%20Finch").status_code == 403
    assert client.post(f"/v1/doa/registers/{_period()}/approve?step=fco&{FCO}").status_code == 422
    assert client.post(f"/v1/doa/registers/{_period()}/approve?step=hr&role=hq_admin&by=Hana%20HR").status_code == 200
    assert client.post(f"/v1/doa/registers/{_period()}/approve?step=fco&{FCO}").status_code == 200
    now = _now(client)["personnel"]
    assert now["amount_minor"] == 800_000 and now["verdict"] == "approve" and "first payroll" in now["reasons"][0]


def test_the_payout_currency_must_be_one_the_payees_account_receives(client):
    assert client.put(f"/v1/doa/instructions/consultants?{FCO}", json={"currency": "EUR"}).status_code == 200
    now = _now(client)["consultants"]
    assert now["verdict"] == "never" and "receives USD, not EUR" in now["reasons"][0]
    assert client.put(f"/v1/doa/instructions/consultants?{FCO}", json={"currency": "USD"}).status_code == 200
    assert _now(client)["consultants"]["verdict"] == "auto"


def test_an_instruction_above_its_limit_waits_for_a_person(client):
    assert client.put(f"/v1/doa/instructions/office?{FCO}", json={"amount_minor": 300_000}).status_code == 200
    now = _now(client)["office"]
    assert now["verdict"] == "approve" and "above the limit" in now["reasons"][0]


def test_a_due_run_is_paid_on_the_agents_own_authority(tmp_path):
    """The book a run builds passes the treasury proof and executes on the fake sandbox without an approval."""
    from venthora.intake.pipeline import Registry

    payees = Registry().payees
    today = dt.date.today()
    rows = [{"key": "office", "period": _period(), "pay_date": today.isoformat(), "amount_minor": 45_000, "currency": "USD",
             "payee_id": "ben-saas-vendor", "charge_code": "US-200.OPS.6300"},
            {"key": "consultants", "period": _period(), "pay_date": today.isoformat(), "amount_minor": 320_000, "currency": "USD",
             "payee_id": "ben-contractor-okafor", "charge_code": "US-200.OPS.6200"}]
    base = TreasuryState.model_validate(json.loads((ROOT / "data" / "scenarios" / "demo_live.json").read_text(encoding="utf-8"))["state"])
    state = doa.book(base, rows, payees, today)
    fake = FakeAirwallex(balances={k: str(v / 100) for k, v in state.balances.items()})

    async def go():
        agent, executor = harness(fake, tmp_path)
        try:
            s = await autonomy.split(agent, state)
            r = await executor.execute(ExecutionRequest(state=s.auto_state, plan_hash=s.auto_plan.plan_hash))
            again = await executor.execute(ExecutionRequest(state=s.auto_state, plan_hash=s.auto_plan.plan_hash))
            return s, r, again
        finally:
            await agent.close()
    s, r, again = asyncio.run(go())
    assert s.full.decision == "APPROVED" and r.status == "COMPLETED", r.message
    assert sorted(t["reference"].split()[0] for t in fake.transfers.values()) == [f"DOA-CONSULTANTS-{_period()}", f"DOA-OFFICE-{_period()}"]
    assert again.status != "COMPLETED" or len(fake.transfers) == 2  # the period's fingerprint never pays twice
