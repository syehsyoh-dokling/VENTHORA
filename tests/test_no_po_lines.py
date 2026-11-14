"""Bills owed by law or contract (payroll, taxes, benefits, loans, rent, advances) are never bought on a purchase order,
so the PO rule does not hold them; a supplier's bill without a PO still waits for a person."""
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


def test_payroll_needs_no_po_but_a_supplier_still_does(client):
    _upload(client, "v.csv", "vendor",
            "Vendor ID,Vendor name,Currency,Budget line,Email,Account number,SWIFT,Bank name,Bank country\n"
            "pay-bureau,Pay Bureau Pte Ltd,SGD,payroll,pay@bureau.example,0123456789,DBSSSGSG,DBS Bank,SG\n"
            "box-supplier,Box Supplier Pte Ltd,SGD,suppliers,ar@box.example,0123456789,DBSSSGSG,DBS Bank,SG\n")
    rep = _upload(client, "b.csv", "bill",
                  "Bill ID,Supplier ID,Invoice number,Amount due,Currency,Due date\n"
                  "B1,pay-bureau,PAY-1,\"1,000.00\",SGD,2026-12-01\nB2,box-supplier,BOX-1,\"500.00\",SGD,2026-12-01\n")
    follow = " | ".join(rep["followed"])
    assert "PAY-1: VERIFIED" in follow and "BOX-1: NEEDS_REVIEW (po_missing)" in follow


def test_the_book_keeps_the_most_urgent_twelve_not_the_oldest(client):
    """Fourteen verified bills: the twelve the plan holds are the highest priority, whatever arrived first."""
    vend = "Vendor ID,Vendor name,Currency,Budget line,Payment priority,Email,Account number,SWIFT,Bank name,Bank country\n"
    vend += "".join(f"old-{i},Old Supplier {i} Ltd,USD,rent,5,ar{i}@old.example,12345678{i:02d},CHASUS33,Chase,US\n" for i in range(12))
    vend += "pay-now,Pay Now Bureau,USD,payroll,10,pay@now.example,1234567899,CHASUS33,Chase,US\n"
    _upload(client, "v.csv", "vendor", vend)
    bills = "Bill ID,Supplier ID,Invoice number,Amount due,Currency,Due date\n"
    bills += "".join(f"O{i},old-{i},OLD-{i},\"100.00\",USD,2026-12-20\n" for i in range(12))
    _upload(client, "b1.csv", "bill", bills)
    _upload(client, "b2.csv", "bill", "Bill ID,Supplier ID,Invoice number,Amount due,Currency,Due date\nP1,pay-now,PAYROLL-1,\"900.00\",USD,2026-12-20\n")
    import datetime as dt

    from venthora.api import _live_base, app

    base = _live_base().model_copy(update={"obligations": [], "receivables": [], "evidence": []})
    queue = app.state.intake.store.list(1000)
    book = app.state.intake.to_state(base, as_of=dt.date(2026, 12, 18), requests=queue)
    ids = [o.obligation_id for o in book.obligations]
    assert len(ids) == 12 and "REQ-PAYROLL-1" in ids


def test_a_manual_call_back_is_kept_on_the_request_with_its_time(client):
    _upload(client, "v.csv", "vendor",
            "Vendor ID,Vendor name,Currency,Budget line,Email,Account number,SWIFT,Bank name,Bank country\n"
            "cb-supplier,Callback Supplier Pte Ltd,SGD,suppliers,ar@cb.example,0123456789,DBSSSGSG,DBS Bank,SG\n")
    _upload(client, "b.csv", "bill", "Bill ID,Supplier ID,Invoice number,Amount due,Currency,Due date\nC1,cb-supplier,CB-1,\"500.00\",SGD,2026-12-01\n")
    rid = next(r["intake_id"] for r in client.get("/v1/intake?limit=1000").json() if r["invoice_no"] == "CB-1")
    assert client.post(f"/v1/intake/requests/{rid}/callbacks?role=super_admin", json={"pic_name": "A", "company": "X", "phone": "1", "revision": "x"}).status_code == 422
    r = client.post(f"/v1/intake/requests/{rid}/callbacks?role=super_admin&by=Fitri", json={
        "pic_name": "Budi Hartono", "company": "Callback Supplier Pte Ltd", "phone": "+65 6123 4567", "revision": "quote the purchase order"})
    assert r.status_code == 200 and r.json()["at"] and r.json()["by"].startswith("Fitri")
    d = client.get(f"/v1/intake/requests/{rid}/correspondence").json()
    assert [c["pic_name"] for c in d["callbacks"]] == ["Budi Hartono"]
    # the held bill was answered automatically: which invoice, received and checked when, what is missing; on the log,
    # and not sent because the vendor's address is a reserved test domain
    m = d["vendor_messages"]
    assert len(m) == 1 and m[0]["kind"] == "vendor_hold" and m[0]["status"] == "skipped" and "test address" in m[0]["error"]
    assert "CB-1" in m[0]["subject"] and "received your invoice CB-1" in m[0]["body"] and "purchase order" in m[0]["body"]
    assert d["feedback"]["state"] == "undelivered"
    sheet = client.get(f"/v1/intake/requests/{rid}").json()["callback"]
    assert sheet["company"] == "Callback Supplier Pte Ltd" and {f["field"] for f in sheet["to_correct"]} >= {"po_number"}
    assert client.get(f"/v1/intake/requests/{rid}").json()["status"] == "NEEDS_REVIEW"  # a call-back record changes no status
