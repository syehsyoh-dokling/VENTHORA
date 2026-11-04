# ruff: noqa: E501
"""Agent plan -> Today's plan: the organisation's real payments (the queue, the payroll ready for the pay day) planned
on the balance, each decision with its reason, the part VENTHORA pays alone and the part for a person; reading it
pays nothing, and paying needs the bank."""
import pytest
from fastapi.testclient import TestClient

AMIRA = "role=country_finance_lead&by=Amira%20Shah"


@pytest.fixture
def client():
    from venthora.api import app
    with TestClient(app) as c:
        yield c


def test_a_first_salary_waits_for_its_approval_and_then_joins_the_plan(client):
    d = client.get("/v1/agent-plan").json()
    assert d["source"]["requests"] == 0 and d["horizon_hours"] == 72 and d["pay_on_arrival"] is False
    # five first salaries: none is the agent's to pay until one person approved it
    assert d["plan"] is None and d["source"]["payroll_lines"] == 0 and len(d["source"]["payroll_waiting"]) == 5
    assert all("first salary" in w["reasons"][0] for w in d["source"]["payroll_waiting"])
    dana = next(x for x in client.get("/v1/exceptions").json()["payroll"] if x["employee"] == "Dana Finch")
    assert client.post("/v1/exceptions/payroll/approve?role=super_admin&by=Rosa%20Lindqvist", json={"ref": dana["ref"]}).status_code == 200
    d = client.get("/v1/agent-plan").json()
    assert d["source"]["payroll_lines"] == 1 and [o["obligation_id"][:13] for o in d["plan"]["obligations"]] == ["DOA-PERSONNEL"]
    assert len(d["source"]["payroll_waiting"]) == 4


def test_todays_plan_ranks_and_explains_the_waiting_payments(client):
    for inv, amt in (("LDS-8101", 120_000), ("LDS-8102", 90_000)):
        r = client.post(f"/v1/intake/payment-orders?{AMIRA}", json={"vendor_id": "ben-lumen-design", "invoice_no": inv, "amount_minor": amt, "currency": "USD",
                                                                    "due_date": "2026-12-01", "po_number": "PO-2026-0101", "description": "phase"})
        assert r.status_code == 200, r.text
    d = client.get("/v1/agent-plan").json()
    assert d["source"]["requests"] == 2 and d["source"]["balances"] == "scenario"  # no sandbox in the tests
    ids = {o["obligation_id"] for o in d["plan"]["obligations"]}
    assert {"REQ-LDS-8101", "REQ-LDS-8102"} <= ids
    assert all(o["reason"] for o in d["plan"]["obligations"]) and d["plan"]["stages"] and d["split"]["full_decision"]
    assert client.post("/v1/agent-plan/execute?role=finance_manager&by=Dana%20Finch").status_code == 503  # paying needs the bank
    assert client.post("/v1/agent-plan/execute?role=auditor&by=Ines").status_code == 403
