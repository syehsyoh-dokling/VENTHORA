# ruff: noqa: E501
"""Intake -> Cash advance, Petty cash / slush fund, Other direct cost: asked by one person, decided by another within
their limit, sent to Pending payment as a payment order to the payee, an advance or a float settled against receipts;
each pulled from the connected application or a file like every other tab."""
import httpx
import pytest
from fastapi.testclient import TestClient

from venthora import connectors

FM = "role=finance_manager&by=Dana%20Finch"


@pytest.fixture
def client(monkeypatch):
    from venthora.api import app

    real = connectors.fetch

    async def through_app(raw, entity, client=None):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
            return await real(raw, entity, client=c)
    monkeypatch.setattr(connectors, "fetch", through_app)
    with TestClient(app) as c:
        c.get("/v1/sources/connectors")
        yield c


def test_a_cash_advance_is_decided_paid_and_settled(client):
    org = client.app.state.org
    org._put("user", org.get("user", "u-amira") | {"payee_id": "ben-uk-logistics"}, "t")  # her own account, for the test: a GBP payee
    r = client.post(f"/v1/org/fund-requests?{FM}", json={"kind": "cash_advance", "requested_by": "u-amira", "amount_minor": 50_000, "currency": "GBP",
                                                        "charge_code": "UK-300.OPS.1400", "purpose": "Field visit, Manchester", "settle_by": "2099-12-31"})
    assert r.status_code == 200, r.text
    fr = r.json()
    n = fr["request_number"]
    assert n.startswith("CA-") and fr["payee_id"] == "ben-uk-logistics" and fr["status"] == "SUBMITTED"  # paid to the person's own account
    assert client.post(f"/v1/fund-requests/{n}/decision?role=country_finance_lead&by=Amira%20Shah", json={"decision": "approve"}).status_code == 403
    assert client.post(f"/v1/fund-requests/{n}/pay?{FM}").status_code == 409  # not approved yet
    assert client.post(f"/v1/fund-requests/{n}/decision?{FM}", json={"decision": "approve"}).json()["status"] == "APPROVED"
    r = client.post(f"/v1/fund-requests/{n}/pay?{FM}")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "IN_PAYMENT" and r.json()["payment_order"]["intake_id"]
    q = {x["intake_id"]: x for x in client.get("/v1/intake").json()}
    assert q[r.json()["intake_id"]]["invoice_no"] == n  # it waits in Pending payment
    s = client.post(f"/v1/fund-requests/{n}/settle?{FM}", json={"settled_minor": 42_000, "note": "receipts"}).json()
    assert s["status"] == "SETTLED" and s["to_return_minor"] == 8_000 and s["to_reimburse_minor"] == 0


def test_an_odc_needs_its_payee_and_is_not_settled(client):
    r = client.post(f"/v1/org/fund-requests?{FM}", json={"kind": "odc", "requested_by": "u-dana", "amount_minor": 125_000, "currency": "USD",
                                                        "charge_code": "US-200.OPS.6900", "purpose": "Workshop venue", "activity": "Partner workshop"})
    n = r.json()["request_number"]
    assert n.startswith("ODC-") and r.json()["payee_id"] is None
    assert client.post(f"/v1/fund-requests/{n}/decision?role=cfo&by=Omar%20Reyes", json={"decision": "approve"}).status_code == 200
    assert "no payee" in client.post(f"/v1/fund-requests/{n}/pay?{FM}").json()["detail"]
    assert client.post(f"/v1/fund-requests/{n}/settle?{FM}", json={"settled_minor": 1}).status_code == 409
    assert client.post(f"/v1/org/fund-requests?{FM}", json={"kind": "odc", "requested_by": "nobody", "amount_minor": 1, "currency": "USD",
                                                           "charge_code": "US-200.OPS.6900", "purpose": "x y"}).status_code == 409


def test_each_tab_imports_only_its_own_kind(client):
    body = {"via": "api", "connector_id": "legacy-erp-demo"}
    d = client.post(f"/v1/intake/import?{FM}", json=body | {"kind": "petty_cash"}).json()
    assert d["matched"] == 1 and d["created"] == ["PC-2026-0101"] and d["left_out"] == {"other kind": 4}
    d = client.post(f"/v1/intake/import?{FM}", json=body | {"kind": "cash_advance", "number": "CA-2026-0101", "dry_run": False}).json()
    assert d["created"] == ["CA-2026-0101"]
    ca = client.app.state.org.get("fund_request", "CA-2026-0101")
    assert ca["requested_by"] == "u-jonas" and ca["status"] == "APPROVED"  # the source system's decision
    rows = client.get("/v1/fund-requests?kind=cash_advance").json()
    assert rows[0]["request_number"] == "CA-2026-0101" and rows[0]["synced_from"]["connector_id"] == "legacy-erp-demo"
    assert client.post(f"/v1/intake/import?{FM}", json=body | {"kind": "odc", "date_from": "2026-11-01", "date_to": "2026-11-30"}).json()["matched"] == 2
