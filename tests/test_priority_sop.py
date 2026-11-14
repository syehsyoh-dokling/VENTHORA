# ruff: noqa: E501
"""System Setting -> Payment Priority SOP: the FCO sets each spending category High, Medium or Low and its place;
that is the priority every obligation of the organisation carries into the plan. Nothing changes until it is saved."""
import datetime as dt

import pytest
from fastapi.testclient import TestClient

from venthora import priority_sop
from venthora.domain.models import TreasuryState

FCO = "role=super_admin&by=Rosa%20Lindqvist"


@pytest.fixture
def client():
    from venthora.api import app
    with TestClient(app) as c:
        yield c


def test_the_default_list_follows_the_standards_and_changes_nothing_until_saved(client):
    d = client.get("/v1/priority-sop?role=super_admin").json()
    rows = {r["key"]: r for r in d["items"]}
    assert len(rows) == 12 and not d["saved"] and d["authority"]["you_may_change"] is True
    assert (rows["wages"]["level"], rows["wages"]["priority"]) == ("high", 10)
    assert [rows[k]["priority"] for k in ("statutory", "critical_suppliers", "fringe")] == [10, 9, 8]  # taxes share 10 with wages
    assert rows["consultants"]["level"] == "medium" and rows["odc"]["priority"] == 3 and rows["deferrable"]["priority"] == 2
    assert (rows["cash_advance"]["level"], rows["cash_advance"]["priority"]) == ("low", 4)  # advances and slush funds wait first
    assert priority_sop.priorities(client.app.state.org) is None  # no SOP saved: each payee's own priority applies


def test_only_the_mandate_authority_saves_it_and_every_category_is_listed(client):
    items = [{"key": r["key"], "level": r["level"]} for r in client.get("/v1/priority-sop").json()["items"]]
    assert client.put("/v1/priority-sop?role=finance_manager&by=Dana%20Finch", json={"items": items}).status_code == 403
    assert client.put(f"/v1/priority-sop?{FCO}", json={"items": items[:-1]}).status_code == 422
    assert client.put(f"/v1/priority-sop?{FCO}", json={"items": [items[0] | {"level": "urgent"}] + items[1:]}).status_code == 422
    # suppliers moved to the top of High, consultants down to Low
    order = [{"key": "suppliers", "level": "high"}] + [i | ({"level": "low"} if i["key"] == "consultants" else {}) for i in items if i["key"] != "suppliers"]
    r = client.put(f"/v1/priority-sop?{FCO}", json={"items": order})
    assert r.status_code == 200, r.text
    rows = {x["key"]: x for x in r.json()["items"]}
    assert rows["suppliers"]["priority"] == 10 and rows["wages"]["priority"] == 10 and rows["statutory"]["priority"] == 9 and rows["consultants"]["level"] == "low"
    assert r.json()["saved"] and r.json()["updated_by"].startswith("Rosa Lindqvist")


def test_the_plan_ranks_the_waiting_payments_by_the_sop(client):
    r = client.post("/v1/intake/payment-orders?role=country_finance_lead&by=Amira%20Shah",
                    json={"vendor_id": "ben-lumen-design", "invoice_no": "LDS-7001", "amount_minor": 100_000, "currency": "USD", "due_date": "2026-12-01",
                          "po_number": "PO-2026-0101", "description": "phase 1"})
    assert r.status_code == 200, r.text
    pv = next(x for x in client.get("/v1/priority-sop").json()["preview"] if x["obligation_id"] == "REQ-LDS-7001")
    assert pv["category"] == "consultants" and pv["priority"] == 6  # second in Medium, after debt service; Lumen's account code 6600 (marketing and design) is contracted services
    items = [{"key": x["key"], "level": "low" if x["key"] == "consultants" else x["level"]} for x in client.get("/v1/priority-sop").json()["items"]]
    client.put(f"/v1/priority-sop?{FCO}", json={"items": items})
    pv = next(x for x in client.get("/v1/priority-sop").json()["preview"] if x["obligation_id"] == "REQ-LDS-7001")
    assert pv["priority"] <= 4
    # the state the plan is built from carries it
    import json

    from venthora.config import ROOT
    base = TreasuryState.model_validate(json.loads((ROOT / "data" / "scenarios" / "intake_only.json").read_text(encoding="utf-8"))["state"])
    st = client.app.state.intake.to_state(base, as_of=dt.date.today(), requests=client.app.state.intake.store.list())
    assert next(o for o in st.obligations if o.obligation_id == "REQ-LDS-7001").priority == pv["priority"]


def test_doa_lines_claims_and_payees_are_classified():
    payees = {"crit": {"default_priority": 10, "budget_line": "logistics"}, "pay": {"default_priority": 10, "budget_line": "payroll"},
              "saas": {"default_priority": 3, "budget_line": "saas"}}
    c = priority_sop.classify
    assert c({"obligation_id": "DOA-PERSONNEL-2026-11-001"}, payees) == "wages"
    assert c({"obligation_id": "DOA-OTHER_DIRECT-2026-11-001"}, payees) == "odc"
    assert c({"obligation_id": "TRAVEL-AB12"}, payees) == "travel" and c({"obligation_id": "ADVANCE-AB12"}, payees) == "cash_advance"
    assert c({"obligation_id": "REQ-1", "beneficiary_id": "crit"}, payees) == "critical_suppliers"
    assert c({"obligation_id": "REQ-2", "beneficiary_id": "pay"}, payees) == "wages"
    assert c({"obligation_id": "REQ-3", "beneficiary_id": "saas"}, payees) == "deferrable"
    assert c({"obligation_id": "REQ-4", "account_code": "US-200.OPS.6700"}, payees) == "office"


def test_taxes_and_debt_service_have_their_accounts_and_categories(client):
    codes = {a["code"]: a for a in client.get("/v1/org/accounts").json()}
    assert codes["2200"]["category"] == "Statutory" and codes["2300"]["category"] == "Debt service"
    c = priority_sop.classify
    assert c({"obligation_id": "REQ-T", "account_code": "US-200.OPS.2200"}, {}) == "statutory"
    assert c({"obligation_id": "REQ-L", "account_code": "US-200.OPS.2300"}, {}) == "debt_service"
    assert client.get("/v1/org/account-code?code=US-200.OPS.2200").status_code == 200


def test_a_payment_that_costs_more_when_late_moves_up_one_step_inside_its_level(client):
    org = client.app.state.org
    items = [{"key": r["key"], "level": r["level"]} for r in client.get("/v1/priority-sop").json()["items"]]
    client.put(f"/v1/priority-sop?{FCO}", json={"items": items})
    o = {"obligation_id": "REQ-1", "account_code": "US-200.OPS.6700", "due_date": "2026-10-01"}
    k, p, why = priority_sop.priority_for(org, o, {}, "2026-11-04")
    base = next(r["priority"] for r in priority_sop.current(org) if r["key"] == "office")
    assert k == "office" and p == base + 1 and why == ["overdue"]
    assert priority_sop.priority_for(org, o | {"due_date": "2026-12-01", "dunning_level": 3}, {}, "2026-11-04")[1] == base + 1
    assert priority_sop.priority_for(org, o | {"due_date": "2026-12-01"}, {}, "2026-11-04")[1] == base
    # never above the top of its level; and the FCO may switch it off
    assert priority_sop.priority_for(org, {"obligation_id": "DOA-PERSONNEL-1", "due_date": "2026-10-01"}, {}, "2026-11-04")[1] == 10
    client.put(f"/v1/priority-sop?{FCO}", json={"items": items, "late_cost": False})
    assert priority_sop.priority_for(org, o, {}, "2026-11-04")[1] == base


def test_pay_on_arrival_is_a_standing_mandate_switch_and_needs_the_bank(client):
    assert client.get("/v1/priority-sop").json()["pay_on_arrival"] is False
    row = next(x for x in client.get("/v1/mandate?role=super_admin").json()["limits"] if x["key"] == "pay_on_arrival")
    assert row["switch_only"] and row["enabled"] is False
    assert client.put(f"/v1/mandate?{FCO}", json={"enabled": {"pay_on_arrival": True}}).status_code == 200
    assert client.get("/v1/priority-sop").json()["pay_on_arrival"] is True
    import asyncio

    from venthora.api import _pay_on_arrival
    r = asyncio.run(_pay_on_arrival("nope"))
    assert r["paid"] is False and "bank is not connected" in r["reason"]  # no sandbox in the tests: nothing is sent

