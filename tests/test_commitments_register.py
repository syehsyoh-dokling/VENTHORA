# ruff: noqa: E501
"""Commitments shows only what is in force (an open PO that has not expired) and is locked on the PO number and its
value. Everything else, and every new record, goes through Intake: the register lists every PO, contract and
engagement received, entered by hand, uploaded or synced, with why it is or is not a commitment."""
import pytest
from fastapi.testclient import TestClient

FM = "role=finance_manager&by=Dana%20Finch"


@pytest.fixture
def client():
    from venthora.api import app
    with TestClient(app) as c:
        yield c


def test_only_open_unexpired_pos_are_commitments_the_rest_stay_in_intake(client):
    org = client.app.state.org
    org._put("po", org.get("po", "PO-2026-0103") | {"expiry_date": "2026-01-31"}, "dashboard:Dana Finch")  # expired
    org._put("po", org.get("po", "PO-2026-0102") | {"status": "CLOSED"}, "dashboard:Dana Finch")
    d = client.get("/v1/commitments").json()
    shown = {c["po_id"] for v in d["vendors"] + d["consultants"] for c in v["contracts"]}
    assert "PO-2026-0103" not in shown and "PO-2026-0102" not in shown and "PO-2026-0101" in shown
    assert all(v["contracts"] for v in d["vendors"] + d["consultants"])  # a vendor without an active PO is not listed
    reg = {r["number"]: r for r in client.get("/v1/intake/commitments").json()["rows"]}
    assert reg["PO-2026-0103"]["state"] == "Expired" and reg["PO-2026-0102"]["state"] == "Closed" and reg["PO-2026-0101"]["state"] == "In Commitments"
    assert reg["ENG-2026-001"]["state"] == "Needs payee" and reg["ENG-2026-001"]["locked"] is False
    assert reg["ENG-2026-002"]["state"] == "In Commitments" and reg["ENG-2026-002"]["locked"] is True
    assert reg["PO-2026-0101"]["source"]["kind"] == "seed" and reg["PO-2026-0103"]["source"]["kind"] == "manual"


def test_a_po_is_locked_on_its_number_and_value(client):
    po = client.app.state.org.get("po", "PO-2026-0101")
    r = client.post(f"/v1/org/purchase-orders?{FM}", json=po | {"total_minor": 600_000})
    assert r.status_code == 409 and "locked on its number and value" in r.json()["detail"] and "value" in r.json()["detail"]
    assert client.post(f"/v1/org/purchase-orders?{FM}", json=po | {"supplier_id": "ben-eu-components", "currency": "EUR"}).status_code == 409
    # what is not its value may change: the description, the dates, closing it
    assert client.post(f"/v1/org/purchase-orders?{FM}", json=po | {"description": "Brand refresh", "expiry_date": "2027-06-30"}).status_code == 200
    # a new PO is registered freely
    new = po | {"po_id": "PO-2026-0901", "total_minor": 600_000, "terms": []}
    assert client.post(f"/v1/org/purchase-orders?{FM}", json=new).status_code == 200


def test_an_engagement_is_locked_once_it_has_its_po(client):
    e = client.app.state.org.get("engagement", "ENG-2026-002")
    assert client.post(f"/v1/org/engagements?{FM}", json=e | {"fee_minor": 150_000}).status_code == 409
    assert client.post(f"/v1/org/engagements?{FM}", json=e | {"note": "room 2"}).status_code == 200
    k = client.app.state.org.get("engagement", "ENG-2026-001")  # no payee yet: no PO, so not locked
    assert client.post(f"/v1/org/engagements?{FM}", json=k | {"fee_minor": 300_000}).status_code == 200


def test_an_upload_adds_pos_but_cannot_change_the_value_of_one(client):
    csv = (b"PO number,Vendor ID,Currency,PO total,Charge code,Description\n"
           b"PO-2026-0101,ben-lumen-design,USD,9999.00,US-200.BRAND26.6600,Brand refresh\n"
           b"PO-2026-0950,ben-lumen-design,USD,1000.00,US-200.BRAND26.6600,Brand extras\n")
    up = client.post(f"/v1/sources/uploads?filename=po.csv&entity=po&{FM}", content=csv).json()
    rep = client.post(f"/v1/sources/uploads/{up['upload_id']}/commit?{FM}", json={"mapping": up["mapping"]}).json()
    assert [x["row"] for x in rep["rejected"]] == [1] and "locked on its number and value" in rep["rejected"][0]["error"]
    assert "PO-2026-0950" in rep["created"]
    reg = {r["number"]: r for r in client.get("/v1/intake/commitments").json()["rows"]}
    assert reg["PO-2026-0950"]["source"] == {"kind": "upload", "label": "Dataset upload", "detail": "po.csv"}
