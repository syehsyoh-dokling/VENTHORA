# ruff: noqa: E501
"""Intake -> Purchase request: asked by one person, decided by another within their limit, then raised as a PO that
may not exceed the approved estimate. Balance received: what other parties paid into the Airwallex account, recorded
here and confirmed by the deposit Airwallex reports."""
import pytest
from fastapi.testclient import TestClient

from venthora.masterdata import MasterStore

FM = "role=finance_manager&by=Dana%20Finch"
PR = {"title": "Two laptops for the UK office", "requested_by": "Amira Shah", "supplier_id": "ben-saas-vendor", "currency": "USD",
      "estimated_minor": 300_000, "charge_code": "US-200.OPS.6300", "needed_by": "2026-12-01", "justification": "new starters"}


@pytest.fixture
def client():
    from venthora.api import app
    with TestClient(app) as c:
        yield c


def test_a_purchase_request_is_decided_by_someone_else_then_raised_as_a_po(client):
    r = client.post(f"/v1/org/purchase-requests?{FM}", json=PR)
    assert r.status_code == 200, r.text
    pr = r.json()
    assert pr["pr_number"].startswith("PR-") and pr["status"] == "SUBMITTED"
    n = pr["pr_number"]
    # the requester cannot decide it, a requester role cannot decide at all
    assert client.post(f"/v1/purchase-requests/{n}/decision?role=country_finance_lead&by=Amira%20Shah", json={"decision": "approve"}).status_code == 403
    assert client.post(f"/v1/purchase-requests/{n}/decision?role=requester&by=Ola%20Reed", json={"decision": "approve"}).status_code == 403
    # it cannot be saved as approved
    assert client.post(f"/v1/org/purchase-requests?{FM}", json=pr | {"status": "APPROVED"}).status_code == 409
    # a PO from it before the approval is refused
    po = {"po_id": "PO-2026-0777", "supplier_id": "ben-saas-vendor", "currency": "USD", "total_minor": 280_000, "account_code": "US-200.OPS.6300",
          "description": "Laptops", "terms": [], "pr_number": n}
    assert client.post(f"/v1/org/purchase-orders?{FM}", json=po).status_code == 409
    r = client.post(f"/v1/purchase-requests/{n}/decision?{FM}", json={"decision": "approve", "note": "within budget"})
    assert r.status_code == 200 and r.json()["status"] == "APPROVED" and r.json()["decided_by"].startswith("Dana Finch")
    assert client.post(f"/v1/org/purchase-requests?{FM}", json=r.json()).status_code == 409  # decided: no more changes
    # the PO may not exceed the approved estimate
    assert "approved estimate" in client.post(f"/v1/org/purchase-orders?{FM}", json=po | {"total_minor": 310_000}).json()["detail"]
    assert client.post(f"/v1/org/purchase-orders?{FM}", json=po).status_code == 200
    done = next(x for x in client.get("/v1/purchase-requests").json() if x["pr_number"] == n)
    assert done["status"] == "ORDERED" and done["po_id"] == "PO-2026-0777" and done["po"]["total_minor"] == 280_000


def test_an_approver_decides_only_within_their_limit_and_a_rejection_is_final(client):
    big = client.post(f"/v1/org/purchase-requests?{FM}", json=PR | {"estimated_minor": 900_000_000}).json()
    r = client.post(f"/v1/purchase-requests/{big['pr_number']}/decision?{FM}", json={"decision": "approve"})
    assert r.status_code == 403 and "may approve up to" in r.json()["detail"]
    r = client.post(f"/v1/purchase-requests/{big['pr_number']}/decision?{FM}", json={"decision": "reject", "note": "too large"})
    assert r.status_code == 200 and r.json()["status"] == "REJECTED"
    assert client.post(f"/v1/purchase-requests/{big['pr_number']}/decision?{FM}", json={"decision": "approve"}).status_code == 409


def test_a_receipt_records_money_paid_in_by_another_party(client):
    rv = MasterStore().upsert("receivable", [{"receivable_id": "AR-T1", "customer_id": "acme-retail", "invoice_no": "AR-7001", "amount_minor": 400_000,
                                              "currency": "USD", "due_date": "2026-11-30"}], source="upload:ar.csv")
    assert not rv["rejected"], rv
    r = client.post(f"/v1/org/receipts?{FM}", json={"payer_name": "Acme Retail Ltd", "customer_id": "acme-retail", "amount_minor": 400_000, "currency": "USD",
                                                   "received_date": "2026-11-04", "reference": "AR-7001", "receivable_id": "AR-T1"})
    assert r.status_code == 200, r.text
    assert r.json()["receipt_id"].startswith("RC-")
    d = client.get("/v1/receipts").json()
    row = next(x for x in d["rows"] if x["receipt_id"] == r.json()["receipt_id"])
    assert row["state"] == "Awaiting the deposit" and row["invoice"]["invoice_no"] == "AR-7001" and row["customer"]
    assert d["airwallex_error"] and any(c["id"] == "acme-retail" for c in d["customers"])  # no sandbox in the tests
    # a receipt in another currency than its invoice is refused
    bad = client.post(f"/v1/org/receipts?{FM}", json={"payer_name": "Acme", "amount_minor": 1, "currency": "EUR", "received_date": "2026-11-04", "receivable_id": "AR-T1"})
    assert bad.status_code == 409
