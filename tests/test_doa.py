# ruff: noqa: E501
"""Delegation of Authority: a general policy per category (no person, vendor, account or amount); the payroll
register carries the individuals and each line is judged against the policy; the payout currency is the employee's
own account's; a run is paid on the fake sandbox."""
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
CC = "US-200.OPS.6010"
# the demo registry has no employees as payees; registered accounts in each currency stand in for theirs
LINES = [{"employee": "Ana Silva", "payee_id": "payroll-group-us", "currency": "USD", "amount_minor": 420_000, "charge_code": CC},
         {"employee": "Ben Ortiz", "payee_id": "ben-eu-components", "currency": "EUR", "amount_minor": 380_000, "charge_code": CC},
         {"employee": "Cleo Hart", "payee_id": "ben-uk-logistics", "currency": "GBP", "amount_minor": 300_000, "charge_code": CC}]


@pytest.fixture
def client():
    from venthora.api import app
    with TestClient(app) as c:
        yield c


def _now(c, key="personnel"):
    return next(x["now"] for x in c.get("/v1/doa").json()["internal"] if x["key"] == key)


def _period():
    return dt.date.today().strftime("%Y-%m")


def _approve(c):
    assert c.post(f"/v1/doa/registers/{_period()}/approve?step=hr&role=hq_admin&by=Hana%20HR").status_code == 200
    assert c.post(f"/v1/doa/registers/{_period()}/approve?step=fco&{FCO}").status_code == 200


def test_the_policy_names_no_individual(client):
    d = client.get("/v1/doa?role=super_admin").json()
    assert [c["key"] for c in d["internal"]] == ["personnel", "fringe", "consultants", "travel", "office", "other_direct"]
    assert [c["key"] for c in d["external"]] == ["vendors", "specialists"]
    for c in d["internal"]:
        assert not {"payee_id", "amount_minor", "charge_code", "variance_bp", "limit_payment_minor", "limit_run_minor"} & set(c["policy"])
        assert c["auto"] and c["approve"] and c["never"] and c["source"]
    assert d["authority"]["you_may_change"] is True and d["autopilot"] is False and "payees" not in d
    assert _now(client, "office")["summary"] == "not_connected"


def test_only_the_holder_of_the_delegation_sets_a_policy(client):
    assert client.put("/v1/doa/policies/personnel?role=finance_manager&by=Dana", json={"pay_day": "25"}).status_code == 403
    r = client.put(f"/v1/doa/policies/personnel?{FCO}", json={"pay_day": "25", "currencies": ["GBP", "USD"]})
    assert r.status_code == 200 and r.json()["pay_day"] == "25" and r.json()["currencies"] == ["USD", "GBP"]
    assert client.put(f"/v1/doa/policies/personnel?{FCO}", json={"currencies": ["JPY"]}).status_code == 422
    assert client.put(f"/v1/doa/policies/personnel?{FCO}", json={"currencies": []}).status_code == 422
    assert client.put(f"/v1/doa/policies/personnel?{FCO}", json={"payee_id": "x"}).json().get("payee_id") is None  # not a policy field


def test_payroll_is_never_paid_without_both_approvals_and_the_first_one_needs_a_person(client):
    assert _now(client)["summary"] == "no_document"
    r = client.post("/v1/doa/registers?role=finance_manager&by=Dana%20Finch", json={"period": _period(), "lines": LINES})
    assert r.status_code == 200 and r.json()["totals"] == {"USD": 420_000, "EUR": 380_000, "GBP": 300_000}
    assert _now(client)["summary"] == "never"
    assert client.post(f"/v1/doa/registers/{_period()}/approve?step=hr&role=finance_manager&by=Dana%20Finch").status_code == 403  # the preparer
    assert client.post(f"/v1/doa/registers/{_period()}/approve?step=fco&{FCO}").status_code == 422  # HR first
    _approve(client)
    lines = _now(client)["lines"]
    assert [x["verdict"] for x in lines] == ["approve"] * 3 and "first payroll" in lines[0]["reasons"][0]


def test_each_line_is_paid_in_its_own_accounts_currency_when_the_policy_allows_it(client):
    bad = LINES[:1] + [LINES[1] | {"currency": "USD"}]  # Ben's account receives EUR
    assert client.post("/v1/doa/registers?role=finance_manager&by=Dana%20Finch", json={"period": _period(), "lines": bad}).status_code == 200
    _approve(client)
    v = {x["employee"]: x for x in _now(client)["lines"]}
    assert v["Ben Ortiz"]["verdict"] == "never" and "receives EUR, not USD" in v["Ben Ortiz"]["reasons"][0]
    assert client.put(f"/v1/doa/policies/personnel?{FCO}", json={"currencies": ["USD"]}).status_code == 200
    client.post("/v1/doa/registers?role=finance_manager&by=Dana%20Finch", json={"period": _period(), "lines": LINES})
    _approve(client)
    v = {x["employee"]: x for x in _now(client)["lines"]}
    assert "EUR is not a payout currency the delegation allows" in v["Ben Ortiz"]["reasons"]
    assert v["Ana Silva"]["verdict"] == "approve"  # first payroll, but the line itself is fine


def test_due_lines_are_paid_on_the_agents_own_authority_in_each_currency(tmp_path):
    """The book a run builds passes the treasury proof and executes on the fake sandbox without an approval:
    one payout per line, in the line's currency, bought from base."""
    from venthora.intake.pipeline import Registry

    payees = Registry().payees
    today = dt.date.today()
    row = {"key": "personnel", "period": _period(), "pay_date": today.isoformat()}
    lines = [ln | {"line": i, "ref": f"doa:personnel:{_period()}:{i}"} for i, ln in enumerate(LINES)]
    base = TreasuryState.model_validate(json.loads((ROOT / "data" / "scenarios" / "demo_live.json").read_text(encoding="utf-8"))["state"])
    state = doa.book(base, row, lines, payees, today)
    fake = FakeAirwallex(balances={k: str(v / 100) for k, v in state.balances.items()})

    async def go():
        agent, executor = harness(fake, tmp_path)
        try:
            s = await autonomy.split(agent, state)
            r = await executor.execute(ExecutionRequest(state=s.auto_state, plan_hash=s.auto_plan.plan_hash))
            return s, r
        finally:
            await agent.close()
    s, r = asyncio.run(go())
    assert r.status == "COMPLETED", r.message
    sent = {t["reference"].split()[0]: t["transfer_currency"] for t in fake.transfers.values()}
    paid_ids = {d.obligation_id for d in s.auto_plan.obligations if d.action in autonomy.PAYING}
    assert sent and set(sent) == paid_ids
    assert {state.obligations[i].currency for i in range(3) if state.obligations[i].obligation_id in paid_ids} <= {"USD", "EUR", "GBP"}
    assert all(sent[o.obligation_id] == o.currency for o in state.obligations if o.obligation_id in sent)
