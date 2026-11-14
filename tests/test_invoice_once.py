"""Each invoice counts once, and a purchase order is never paid above its value.

A PO of 1,000 paid in parts: 600 fits; the same invoice sent again is a duplicate and holds nothing of the PO; 500
more would pass the PO's value and waits for a person; 400 fits, because the duplicate was not counted."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from venthora.api import app

    with TestClient(app) as c:
        yield c


def _upload(client, name, entity, text):
    s = client.post(f"/v1/sources/uploads?filename={name}&entity={entity}&role=super_admin", content=text.encode())
    assert s.status_code == 200, s.text
    c = client.post(f"/v1/sources/uploads/{s.json()['upload_id']}/commit?role=super_admin", json={"mapping": s.json()["mapping"]})
    assert c.status_code == 200, c.text
    return c.json()


def _bill(client, bill_id, invoice, amount):
    rep = _upload(client, f"{bill_id}.csv", "bill",
                  "Bill ID,Supplier ID,Invoice number,Amount due,Currency,Due date,PO number\n"
                  f'{bill_id},po-supplier,{invoice},"{amount}",SGD,2026-12-01,PO-UNIQ-1\n')
    return rep["followed"][0]


def test_each_invoice_counts_once_and_a_po_is_never_paid_above_its_value(client):
    _upload(client, "v.csv", "vendor",
            "Vendor ID,Vendor name,Currency,Budget line,Email,Account number,SWIFT,Bank name,Bank country\n"
            "po-supplier,PO Supplier Pte Ltd,SGD,suppliers,ar@posup.example,0123456789,DBSSSGSG,DBS Bank,SG\n")
    cc = next(u["cost_centre"] for u in client.get("/v1/org/units").json() if u.get("cost_centre") and u.get("active", True))
    prj, acct = "UNQ", "6100"
    client.post("/v1/org/projects?role=super_admin", json={"code": prj, "name": "Uniqueness test"})
    r = client.post("/v1/org/accounts?role=super_admin", json={"code": acct, "name": "Goods and services", "category": "Suppliers"})
    assert r.status_code in (200, 201, 409), r.text
    po = _upload(client, "po.csv", "po",
                 f'PO number,Supplier ID,Currency,PO total,Account code,Description\nPO-UNIQ-1,po-supplier,SGD,"1,000.00",{cc}.{prj}.{acct},Parts\n')
    assert po["created"] == ["PO-UNIQ-1"], po.get("rejected")
    assert "U-1: VERIFIED" in _bill(client, "U1", "U-1", "600.00")
    assert "U-1: DUPLICATE" in _bill(client, "U1B", "U-1", "600.00")  # the same invoice again: never counted twice
    over = _bill(client, "U2", "U-2", "500.00")
    assert "U-2: NEEDS_REVIEW" in over and "po_exceeded" in over  # 600 + 500 would pass 1,000
    assert "U-3: VERIFIED" in _bill(client, "U3", "U-3", "400.00")  # the duplicate held nothing: 400 is exactly what is left
    assert "U-4: NEEDS_REVIEW" in _bill(client, "U4", "U-4", "1.00")  # the PO is now fully used
