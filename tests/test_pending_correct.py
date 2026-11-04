# ruff: noqa: E501
"""Pending payment: a person opens a request, corrects what was read wrong, saves (the checks run again: payee,
currency, PO, duplicate), or pays (saved, checked again, approved as that person and paid through the treasury
proof). Nothing that fails a check is approved."""
import pytest
from fastapi.testclient import TestClient

AMIRA = "role=country_finance_lead&by=Amira%20Shah"
DANA = "role=finance_manager&by=Dana%20Finch"


@pytest.fixture
def client():
    from venthora.api import app
    with TestClient(app) as c:
        yield c


def order(client, inv="LDS-9001", **kw):
    body = {"vendor_id": "ben-lumen-design", "invoice_no": inv, "amount_minor": 150_000, "currency": "USD", "due_date": "2026-12-01",
            "description": "design phase 1"} | kw
    r = client.post(f"/v1/intake/payment-orders?{AMIRA}", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_a_correction_runs_the_checks_again(client):
    req = order(client)
    assert req["status"] == "NEEDS_REVIEW" and "po_missing" in req["flags"]
    full = client.get(f"/v1/intake/requests/{req['intake_id']}").json()
    assert full["invoice_no"] == "LDS-9001" and any(p["id"] == "ben-lumen-design" for p in full["payees"]) and full["open_pos"]
    r = client.put(f"/v1/intake/requests/{req['intake_id']}?{DANA}", json={"currency": "EUR"}).json()
    assert not r["checks_pass"] and any("paid in USD" in p for p in r["problems"])
    r = client.put(f"/v1/intake/requests/{req['intake_id']}?{DANA}", json={"currency": "USD", "po_number": "po-2026-0101"}).json()
    assert r["checks_pass"] and r["problems"] == [] and r["request"]["po_number"] == "PO-2026-0101" and "po_missing" not in r["request"]["flags"]
    assert r["request"]["status"] == "NEEDS_REVIEW" and r["request"]["reasons"][0].startswith("corrected by Dana Finch")  # saved, not approved
    assert client.get(f"/v1/intake/requests/{req['intake_id']}").json()["po"]["po_id"] == "PO-2026-0101"
    r = client.put(f"/v1/intake/requests/{req['intake_id']}?{DANA}", json={"amount_minor": 900_000}).json()
    assert not r["checks_pass"] and any("PO-2026-0101 has" in p for p in r["problems"])  # above what the PO has left


def test_a_corrected_duplicate_is_never_paid(client):
    a = order(client, inv="LDS-9100", po_number="PO-2026-0101")
    b = order(client, inv="LDS-9101", po_number="PO-2026-0101", amount_minor=10_000)
    r = client.put(f"/v1/intake/requests/{b['intake_id']}?{DANA}", json={"invoice_no": "LDS-9100"}).json()
    assert not r["checks_pass"] and any("already in the queue" in p for p in r["problems"]) and r["request"]["duplicate_of"] == a["intake_id"]
    p = client.post(f"/v1/intake/requests/{b['intake_id']}/pay?{DANA}", json={})
    assert p.status_code == 409 and p.json()["detail"]["problems"]
    assert client.get(f"/v1/intake/requests/{b['intake_id']}").json()["status"] != "VERIFIED"


def test_pay_approves_as_the_person_and_goes_to_the_bank(client):
    req = order(client, inv="LDS-9200")
    # the person who asked for it cannot pay it
    p = client.post(f"/v1/intake/requests/{req['intake_id']}/pay?{AMIRA}", json={"po_number": "PO-2026-0101"})
    assert p.status_code == 403 and client.get(f"/v1/intake/requests/{req['intake_id']}").json()["status"] == "NEEDS_REVIEW"
    p = client.post(f"/v1/intake/requests/{req['intake_id']}/pay?{DANA}", json={"po_number": "PO-2026-0101"})
    assert client.get(f"/v1/intake/requests/{req['intake_id']}").json()["status"] == "VERIFIED"  # approved by Dana
    assert p.status_code == 503 and "Airwallex" in p.json()["detail"]  # no sandbox in the tests: the payment itself needs the bank
    assert client.put("/v1/intake/requests/nope?" + DANA, json={}).status_code == 404
