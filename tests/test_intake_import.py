# ruff: noqa: E501
"""Intake -> Sync via API / Sync via Dataset: every tab pulls its own records (purchase requests, purchase orders,
consultant contracts, one-off engagements, travel authorisations) from the connected application or from a file,
between two dates or as one number, previewed first; the usual rules hold (a PO is locked on its number and value,
an uploaded request is decided here)."""
import httpx
import pytest
from fastapi.testclient import TestClient

from venthora import connectors

FM = "role=finance_manager&by=Dana%20Finch"


@pytest.fixture
def client(monkeypatch):
    from venthora.api import app

    real = connectors.fetch

    async def through_app(raw, entity, client=None):  # the stand-in ERP is served by the app itself
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
            return await real(raw, entity, client=c)
    monkeypatch.setattr(connectors, "fetch", through_app)
    with TestClient(app) as c:
        c.get("/v1/sources/connectors")  # the demo connection exists
        c.post(f"/v1/sources/connectors/legacy-erp-demo/sync?entity=vendor&{FM}")  # its vendors first: POs are placed with them
        yield c


def imp(client, **kw):
    return client.post(f"/v1/intake/import?{FM}", json={"via": "api", "connector_id": "legacy-erp-demo"} | kw)


def test_one_po_number_is_previewed_then_imported(client):
    r = imp(client, kind="po", number="po-2026-0202")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["dry_run"] and d["matched"] == 1 and d["created"] == ["PO-2026-0202"] and d["left_out"] == {"other kind": 1, "another number": 2}
    assert client.app.state.org.get("po", "PO-2026-0202") is None  # a preview writes nothing
    d = imp(client, kind="po", number="PO-2026-0202", dry_run=False).json()
    assert d["created"] == ["PO-2026-0202"] and client.app.state.org.get("po", "PO-2026-0202")["total_minor"] == 300_000
    assert imp(client, kind="po", number="PO-2099-0001").json()["message"].endswith("is not in the connection")


def test_a_date_range_and_the_consultant_contracts_apart(client):
    d = imp(client, kind="po", date_from="2026-10-01", date_to="2026-10-31").json()
    assert d["matched"] == 3 and d["left_out"] == {"other kind": 1}  # the contract with an LoE is not a purchase order
    c = imp(client, kind="contract", dry_run=False).json()
    assert c["created"] == ["PO-2026-0204"]
    po = client.app.state.org.get("po", "PO-2026-0204")
    assert (po["loe"], po["loe_unit"], po["rate_minor"], po["expiry_date"]) == (20, "day", 65_000, "2027-01-31")
    assert imp(client, kind="po", date_from="2026-12-01", date_to="2026-12-31").json()["matched"] == 0
    assert imp(client, kind="po", date_from="2026-12-01", date_to="2026-11-01").status_code == 422


def test_purchase_requests_engagements_and_travel_authorisations(client):
    d = imp(client, kind="pr", dry_run=False).json()
    assert sorted(d["created"]) == ["PR-2026-0101", "PR-2026-0102"]
    org = client.app.state.org
    assert org.get("pr", "PR-2026-0101")["status"] == "APPROVED"  # the source system's decision, synced
    assert org.get("pr", "PR-2026-0102")["status"] == "SUBMITTED"
    assert imp(client, kind="engagement", dry_run=False).json()["created"] == ["ENG-2026-101"]
    assert org.get("engagement", "ENG-2026-101")["role"] == "resource_person"
    t = imp(client, kind="ta", date_from="2026-11-01", date_to="2026-11-30", dry_run=False).json()
    assert t["created"] == ["TA-2026-0101"] and t["left_out"] == {"outside the dates": 1}
    assert org.get("ta", "TA-2026-0101")["traveller"] == "u-dana"


def test_a_dataset_is_filtered_the_same_way_and_an_uploaded_request_is_decided_here(client):
    csv = (b"PR number,Title,Requested by,Currency,Estimate,Charge code,Request date,Status\n"
           b"PR-2026-0501,Desk chairs,Amira Shah,GBP,900.00,UK-300.OPS.6700,2026-11-03,Approved\n"
           b"PR-2026-0502,Monitors,Amira Shah,GBP,1500.00,UK-300.OPS.6700,2026-09-03,Approved\n")
    up = client.post(f"/v1/sources/uploads?filename=prs.csv&entity=pr&{FM}", content=csv).json()
    body = {"via": "dataset", "kind": "pr", "upload_id": up["upload_id"], "mapping": up["mapping"], "date_from": "2026-11-01", "date_to": "2026-11-30"}
    d = client.post(f"/v1/intake/import?{FM}", json=body).json()
    assert d["matched"] == 1 and d["created"] == ["PR-2026-0501"]
    d = client.post(f"/v1/intake/import?{FM}", json=body | {"dry_run": False}).json()
    assert client.app.state.org.get("pr", "PR-2026-0501")["status"] == "SUBMITTED"  # "Approved" in a file is not a decision
    assert client.post(f"/v1/intake/import?{FM}", json=body | {"kind": "po"}).status_code == 404  # the upload is used up
    # a file of another kind is refused for this tab
    up2 = client.post(f"/v1/sources/uploads?filename=prs.csv&entity=pr&{FM}", content=csv).json()
    assert client.post(f"/v1/intake/import?{FM}", json=body | {"upload_id": up2["upload_id"], "kind": "ta"}).status_code == 422


def test_a_connection_is_tested_before_it_is_used(client, monkeypatch):
    from venthora.api import app

    class Through(httpx.AsyncClient):
        def __init__(self, **kw):
            super().__init__(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    monkeypatch.setattr(httpx, "AsyncClient", Through)
    raw = connectors.ConnectorStore().get_raw("legacy-erp-demo")
    monkeypatch.setattr(connectors.ConnectorStore, "get_raw", lambda self, cid: raw | {"base_url": "http://testserver/demo/legacy-erp"} if cid == "legacy-erp-demo" else None)
    r = client.post(f"/v1/sources/test-connection?{FM}", json={"connector_id": "legacy-erp-demo", "kind": "pr"}).json()
    assert r["ok"] and r["items"] == 2 and "2 purchase requests available" in r["message"]
    bad = client.post(f"/v1/sources/test-connection?{FM}", json={"connection": "http://testserver/demo/legacy-erp/purchase_requests X-Legacy-Key: wrong"}).json()
    assert not bad["ok"] and "refused" in bad["message"]
    assert client.post(f"/v1/sources/test-connection?{FM}", json={"connection": "not a url"}).status_code == 422
