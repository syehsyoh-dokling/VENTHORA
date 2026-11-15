# ruff: noqa: E501
"""Procurement around one PR number: every vendor that quotes becomes a preferred vendor with its own number and is
asked first next time; every quotation is ranked on specifications, price and administration with a conclusion; the
winner goes on the vendor list with a draft purchase order or a direct purchase, and its invoice is found in Intake."""
import pytest
from fastapi.testclient import TestClient
from test_pr_by_email import BODY, FCO, eml, send

from venthora import procurement_flow

FM = "role=finance_manager&by=Dana%20Finch"


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("VENTHORA_PROCUREMENT_DIR", str(tmp_path / "procurement"))
    from venthora.api import app

    with TestClient(app) as c:
        yield c


def award(client, number, mid):
    n = send(client, eml(FCO, f"Purchase request {number}: toner", BODY.format(extra=""), mid))
    assert n == number
    out = client.post(f"/v1/procurement/{n}/demo-replies?{FM}").json()
    assert out["decision"]["action"] == "award", out
    return n


def test_administration_is_counted_and_what_is_missing_is_named():
    full = {"vendor": {"legal_name": "A", "registration_no": "R", "tax_id": "T", "contact_person": "C", "email": "a@x.example", "phone": "1", "street_address": "S",
                       "country_code": "US", "account_name": "A", "account_number": "1"}, "file": {"name": "q.pdf"}, "payment_terms": "30 days"}
    assert procurement_flow.admin(full) == {"have": 11, "of": 11, "missing": []}
    thin = {"vendor": {"name": "B", "email": "b@x.example", "contact_person": "C", "phone": "1"}, "file": None, "payment_terms": ""}
    a = procurement_flow.admin(thin)
    assert a["have"] == 3 and {"tax id", "bank account", "quotation document", "payment terms"} <= set(a["missing"])


def test_every_vendor_that_quotes_is_a_preferred_vendor_and_is_asked_first_next_time(client):
    n = award(client, "PR-FLOW-0001", "flow-1")
    pv = client.get("/v1/procurement/preferred-vendors").json()
    assert len(pv["vendors"]) == 5 and [v["_id"] for v in pv["vendors"]] == [f"PV-{i:04d}" for i in range(1, 6)] and pv["minimum_invited"] == 5
    assert all(v["quotations"][0]["pr_number"] == n and v["administration"]["of"] == 11 for v in pv["vendors"])
    assert sum(1 for v in pv["vendors"] if v["wins"]) == 1  # one of them won

    b = next(x for x in client.get("/v1/procurement/board").json()["board"] if x["pr"]["pr_number"] == n)
    assert b["conclusion"]["state"] == "awarded" and b["conclusion"]["text"].startswith("Winner decided")
    rk = b["ranking"]
    assert [x["rank"] for x in rk] == [1, 2, 3, 4, 5] and rk[0]["recommended"] and rk[0]["eligible"] and rk[0]["specs_met"] == rk[0]["specs_of"]
    assert not rk[-1]["eligible"] and all(x["administration"]["of"] == 11 for x in rk)  # the ones that miss a condition rank last, whatever their price
    whats = [e["what"] for e in b["events"]]
    assert whats.count("RFQ sent") == 5 and whats.count("quotation received") == 5 and "awarded" in whats and whats.count("regret letter") == 4  # one PR number, every vendor

    # the next request that fits: the five preferred vendors come first and the web is not searched
    n2 = send(client, eml(FCO, "Purchase request PR-FLOW-0002: toner", BODY.format(extra=""), "flow-2"))
    b2 = next(x for x in client.get("/v1/procurement/board").json()["board"] if x["pr"]["pr_number"] == n2)
    invited = [c for c in b2["rfq"]["candidates"] if c["invited_at"]]
    assert len(invited) == 5 and all(c["source"] == "preferred vendor" and c["preferred_id"].startswith("PV-") for c in invited)
    assert "the web was not searched" in b2["rfq"]["search_note"] and b2["conclusion"]["state"] == "waiting"
    again = client.get("/v1/procurement/preferred-vendors").json()["vendors"]
    assert len(again) == 5  # the same vendors: one record each, never a second number


def test_the_winner_goes_on_the_vendor_list_with_a_po_or_a_direct_purchase(client):
    assert client.get("/v1/procurement/settings").json() == {"po_threshold_minor": 100_000, "po_threshold_currency": "USD"}
    n = award(client, "PR-FLOW-0010", "flow-10")  # 935.00: below the 1,000.00 threshold
    w = next(x for x in client.get("/v1/procurement/winners").json()["winners"] if x["pr_number"] == n)
    assert w["po_required"] is False and "direct purchase" in w["po_note"] and w["preferred_id"].startswith("PV-") and w["state"] == "waiting for the invoice"
    assert [s["step"] for s in w["steps"]] == ["Winner announced", "Direct purchase (no PO document)", "Supplier details and bank account", "Invoice received", "Paid"]
    assert client.get(f"/v1/procurement/{n}/po-document").status_code == 409
    from venthora.organisation import OrgStore

    mail = next(m for m in OrgStore().list("outbox") if m["kind"] == "po_issued" and m["ref"] == w["po_id"])
    assert "No purchase order document is needed" in mail["body"] and f"Order reference: {w['po_id']}" in mail["body"]

    assert client.put("/v1/procurement/settings?role=requester&by=Amira%20Shah", json={"po_threshold_minor": 1}).status_code == 403
    assert client.put(f"/v1/procurement/settings?{FM}", json={"po_threshold_minor": -5}).status_code == 422
    assert client.put(f"/v1/procurement/settings?{FM}", json={"po_threshold_minor": 50_000}).json()["po_threshold_minor"] == 50_000
    n2 = award(client, "PR-FLOW-0011", "flow-11")  # the same 935.00, now above the 500.00 threshold
    w2 = next(x for x in client.get("/v1/procurement/winners").json()["winners"] if x["pr_number"] == n2)
    assert w2["po_required"] is True and w2["steps"][1]["step"] == "Purchase order"
    doc = client.get(f"/v1/procurement/{n2}/po-document")
    assert doc.status_code == 200 and "DRAFT" in doc.text and w2["po_id"] in doc.text and n2 in doc.text and "HP 206A toner cartridges" in doc.text and "Awarded by" in doc.text
    assert client.get("/v1/procurement/PR-NOPE/po-document").status_code == 404

    # the winner's invoice, entered on its form, is found on the vendor list by the same PO number
    token = OrgStore().get("rfq", n2)["award"]["details_form"].rsplit("/", 1)[1]
    form = {"registered_phone": "+1 555 0100", "street_address": "10 Market Street", "city": "Springfield", "country_code": "US", "account_name": w2["vendor"],
            "bank_name": "Demo Bank", "bank_country_code": "US", "account_number": "000123456", "routing_type": "aba", "routing_value": "021000021",
            "bank_account_category": "Checking", "delivery_date": "2026-12-10", "invoice_no": "INV-7001", "invoice_date": "2026-12-01", "invoice_amount": "935.00"}
    assert client.post(f"/vendor-details/{token}", json=form).status_code == 200
    w2 = next(x for x in client.get("/v1/procurement/winners").json()["winners"] if x["pr_number"] == n2)
    assert len(w2["intake_ids"]) == 1 and w2["steps"][2]["done"] and w2["steps"][3]["done"] and "INV-7001" in w2["steps"][3]["detail"]
    assert w2["state"] in ("invoice in payment", "invoice held")
