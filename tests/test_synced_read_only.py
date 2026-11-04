# ruff: noqa: E501
"""A record pulled through Auto Connect (API) is read only on the dashboard: the next sync would overwrite a change,
so it is changed in the source system. The lists say which records are synced."""
import pytest
from fastapi.testclient import TestClient

from venthora.masterdata import MasterStore

FM = "role=finance_manager&by=Dana%20Finch"


@pytest.fixture
def client():
    from venthora.api import app
    with TestClient(app) as c:
        yield c


def test_a_synced_po_cannot_be_changed_or_deleted(client):
    org = client.app.state.org
    po = org.get("po", "PO-2026-0103")
    org._put("po", po, "sync:legacy-erp-demo")
    assert next(r for r in client.get("/v1/procurement/purchase-orders").json() if r["po"]["po_id"] == "PO-2026-0103")["synced_from"]["connector_id"] == "legacy-erp-demo"
    r = client.post(f"/v1/org/purchase-orders?{FM}", json=po | {"description": "changed here"})
    assert r.status_code == 409 and "synced from" in r.json()["detail"] and "source system" in r.json()["detail"]
    assert client.delete(f"/v1/org/purchase-orders/PO-2026-0103?{FM}").status_code == 409
    line = next(c for v in client.get("/v1/commitments").json()["vendors"] for c in v["contracts"] if c["po_id"] == "PO-2026-0103")
    assert line["synced_from"]["connector_id"] == "legacy-erp-demo"
    # a PO entered on the dashboard stays editable
    other = org.get("po", "PO-2026-0101")
    assert client.post(f"/v1/org/purchase-orders?{FM}", json=other | {"description": "Brand refresh, phases 1-2"}).status_code == 200


def test_a_synced_vendor_and_customer_cannot_be_changed(client):
    m = MasterStore()
    rep = m.upsert("customer", [{"customer_id": "synced-co", "name": "Synced Co", "currency": "USD", "contacts": ["ap@synced.example"]}],
                   source="sync:legacy-erp-demo", external_ids=["C-1"])
    assert not rep["rejected"], rep
    c = next(x for x in client.get("/v1/beneficiaries?kind=customer").json() if x["id"] == "synced-co")
    assert c["synced_from"]["connector_id"] == "legacy-erp-demo"
    r = client.post(f"/v1/beneficiaries?kind=customer&{FM}", json={"record": {"customer_id": "synced-co", "name": "Synced Co", "currency": "USD"}})
    assert r.status_code == 409 and "Auto Connect" in r.json()["detail"]
    # one added on the dashboard is not synced, and stays editable
    r = client.post(f"/v1/beneficiaries?kind=customer&{FM}", json={"record": {"name": "Manual Co", "currency": "USD", "contacts": ["ap@manual.example"]}})
    assert r.status_code == 200, r.text
    manual = next(x for x in client.get("/v1/beneficiaries?kind=customer").json() if x["name"] == "Manual Co")
    assert manual["synced_from"] is None
    assert client.post(f"/v1/beneficiaries?kind=customer&{FM}", json={"record": {"id": manual["id"], "name": "Manual Co", "currency": "USD"}}).status_code == 200


def test_a_synced_vendor_without_a_kind_keeps_its_budget_line_kind(client):
    """A source that does not say vendor or consultant: a contractors budget line is a consultant (a sync once turned
    one into a vendor, because the record's default kind was vendor)."""
    rep = MasterStore().upsert("vendor", [{"vendor_id": "synced-expert", "name": "Synced Expert Ltd", "currency": "USD", "payout_method": "LOCAL",
                                           "contacts": ["billing@synced-expert.example"], "budget_line": "contractors", "default_priority": 4}],
                               source="sync:legacy-erp-demo", external_ids=["V-9"])
    assert not rep["rejected"], rep
    assert "synced-expert" in {x["id"] for x in client.get("/v1/beneficiaries?kind=consultant").json()}
    assert "synced-expert" not in {x["id"] for x in client.get("/v1/beneficiaries?kind=vendor").json()}
