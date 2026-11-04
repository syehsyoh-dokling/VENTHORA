# ruff: noqa: E501
"""Commitments: per vendor every PO with its active and expiry dates, amount, paid, remaining and status (New, Partial,
Multi Payment, Done); per consultant every contract with LoE, rate, LoE and budget left (Partial x of y LoE); one-off
engagements (consultant, legal specialist, resource person, keynote speaker) with their profile and fee, paid against
their own number; customers with open receivables; staff on payroll."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from venthora import commitments, procurement
from venthora.execution.store import ExecutionStore
from venthora.organisation import Engagement, OrgError, OrgStore, PurchaseOrder

FM = "role=finance_manager&by=Dana%20Finch"


def book(pay, po, amount, ccy, code, n):
    pay.book_payment({"transfer_id": f"tr-{n}", "book_id": "b", "obligation_id": f"o-{n}", "invoice_fingerprint": f"fp-{n}",
                      "installment": "partial", "contract_total_minor": None, "line": None, "currency": ccy, "amount_minor": amount,
                      "base_cost_minor": amount, "paid_at": f"2026-10-2{n}T10:00:00+00:00", "reporting_period": "2026-10",
                      "beneficiary_id": "x", "po_number": po, "account_code": code})


@pytest.fixture
def env(tmp_path):
    return OrgStore(str(tmp_path / "org.db")), ExecutionStore(str(tmp_path / "e.db"))


def line(org, pay, po_id, today="2026-11-04"):
    return commitments.po_line(procurement.status(org, pay, po_id), today)


def test_vendor_po_status_new_partial_multi_payment_done(env):
    org, pay = env
    assert line(org, pay, "PO-2026-0103")["status"] == "New"
    book(pay, "PO-2026-0103", 400_000, "GBP", "UK-300.OPS.6100", 1)
    ln = line(org, pay, "PO-2026-0103")
    assert (ln["status"], ln["paid_minor"], ln["remaining_minor"], ln["active_date"], ln["expiry_date"]) == ("Partial", 400_000, 600_000, "2026-10-01", "2026-12-31")
    book(pay, "PO-2026-0103", 600_000, "GBP", "UK-300.OPS.6100", 2)
    assert line(org, pay, "PO-2026-0103")["status"] == "Done"
    # the monthly subscription is Multi Payment: 12 periods of USD 1,200, two paid
    book(pay, "PO-2026-0105", 120_000, "USD", "US-200.OPS.6300", 3)
    book(pay, "PO-2026-0105", 120_000, "USD", "US-200.OPS.6300", 4)
    sub = line(org, pay, "PO-2026-0105")
    assert (sub["status"], sub["periods"], sub["per_period_minor"], sub["periods_paid"]) == ("Multi Payment", 12, 120_000, 2)
    assert line(org, pay, "PO-2026-0103", today="2027-01-05")["expired"] is False  # Done is never expired
    assert line(org, pay, "PO-2026-0101", today="2027-04-01")["expired"] is True


def test_consultant_contract_loe_rate_and_partial_x_of_y(env):
    org, pay = env
    po = org.get("po", "PO-2026-0104")
    assert (po["loe"], po["loe_unit"], po["rate_minor"]) == (192.0, "hour", 5_000)
    assert line(org, pay, "PO-2026-0104")["status_label"] == "New"
    book(pay, "PO-2026-0104", 240_000, "USD", "US-200.OPS.6200", 1)  # 48 hours at USD 50
    ln = line(org, pay, "PO-2026-0104")
    assert (ln["status_label"], ln["loe_used"], ln["loe_left"], ln["remaining_minor"]) == ("Partial 48 of 192 LoE", 48.0, 144.0, 720_000)


def test_a_contract_value_must_be_loe_times_rate_and_expiry_after_start(env):
    org, _ = env
    base = org.get("po", "PO-2026-0104")
    with pytest.raises(OrgError, match="LoE x rate"):
        org.save("po", PurchaseOrder.model_validate(base | {"loe": 100}), "t")
    with pytest.raises(OrgError, match="both the LoE and the rate"):
        org.save("po", PurchaseOrder.model_validate(base | {"rate_minor": None}), "t")
    with pytest.raises(OrgError, match="before the active date"):
        org.save("po", PurchaseOrder.model_validate(base | {"expiry_date": "2026-09-01"}), "t")
    with pytest.raises(OrgError, match="Multi Payment PO needs an expiry"):
        org.save("po", PurchaseOrder.model_validate(org.get("po", "PO-2026-0105") | {"expiry_date": None}), "t")


def test_an_expired_po_is_not_paid(env):
    org, pay = env
    org.save("po", PurchaseOrder.model_validate(org.get("po", "PO-2026-0103") | {"expiry_date": "2026-10-15"}), "t")
    m = procurement.match(org, pay, "PO-2026-0103", supplier_id="ben-uk-logistics", currency="GBP", amount_minor=10_000)
    assert not m["ok"] and "po_expired" in m["flags"] and "expired on 2026-10-15" in m["reasons"][0]
    assert "po_expired" in procurement.PO_FAILS


def test_a_one_off_engagement_is_paid_against_its_own_number(env):
    org, pay = env
    rec = org.save("engagement", Engagement.model_validate({
        "name": "Adv. Rina Prasetyo", "role": "legal_specialist", "organisation": "Prasetyo & Partners", "payee_id": "ben-contractor-okafor",
        "event": "Review of the EU distribution agreement", "event_date": "2026-11-18", "fee_minor": 90_000, "currency": "USD",
        "charge_code": "US-200.OPS.6200", "documents": ["engagement_letter"]}), "t")
    assert rec["engagement_id"] == "ENG-2026-003"  # the seed holds 001 and 002
    po = org.get("po", "ENG-2026-003")
    assert (po["supplier_id"], po["total_minor"], po["account_code"]) == ("ben-contractor-okafor", 90_000, "US-200.OPS.6200")
    assert po["description"].startswith("Legal specialist: ")
    e = commitments.engagement_line(rec, procurement.status(org, pay, "ENG-2026-003"), {})
    assert e["status_label"] == "Documents missing" and e["documents_missing"] == ["Invoice or fee note"]
    book(pay, "ENG-2026-003", 90_000, "USD", "US-200.OPS.6200", 1)
    assert commitments.engagement_line(rec, procurement.status(org, pay, "ENG-2026-003"), {})["status_label"] == "Done"
    with pytest.raises(OrgError, match="paid in USD"):
        org.save("engagement", Engagement.model_validate(rec | {"engagement_id": None, "currency": "EUR"}), "t")
    with pytest.raises(OrgError, match="unknown payee"):
        org.save("engagement", Engagement.model_validate(rec | {"engagement_id": None, "payee_id": "nobody"}), "t")
    # without a payee it is kept, with no PO, and waits for the bank account
    speaker = org.get("engagement", "ENG-2026-001")
    assert speaker["payee_id"] is None and org.get("po", "ENG-2026-001") is None
    assert commitments.engagement_line(speaker, None, {})["status_label"] == "Needs payee"


@pytest.fixture
def client():
    from venthora.api import app
    with TestClient(app) as c:
        yield c


def test_the_commitments_endpoint_lists_every_tab(client):
    d = client.get("/v1/commitments").json()
    vendors = {v["id"]: v for v in d["vendors"]}
    assert any(c["po_id"] == "PO-2026-0105" and c["status"] == "Multi Payment" for c in vendors["ben-saas-vendor"]["contracts"])
    rhein = vendors["ben-eu-components"]
    assert rhein["contracts"][0]["po_id"] == "PO-2026-0102" and rhein["totals_currency"] == "EUR"
    okafor = next(c for c in d["consultants"] if c["id"] == "ben-contractor-okafor")
    assert [c["po_id"] for c in okafor["contracts"]] == ["PO-2026-0104"]  # its one-off engagement sits in its own tab
    assert okafor["contracts"][0]["loe"] == 192.0
    assert {e["engagement_id"] for e in d["engagements"]} >= {"ENG-2026-001", "ENG-2026-002"}
    assert {e["role_label"] for e in d["engagements"]} >= {"Keynote speaker", "Consultant"}
    assert set(d["roles"]) == {"consultant", "legal_specialist", "resource_person", "keynote_speaker"}
    assert all(k in d for k in ("customers", "staff"))
    amira = next(u for u in d["staff"] if u["name"] == "Amira Shah")
    assert (amira["salary_minor"], amira["salary_currency"], amira["contract_end"]) == (520_000, "GBP", "2026-12-31")


def test_engagements_are_saved_and_deleted_through_the_org_api(client):
    r = client.post(f"/v1/org/engagements?{FM}", json={"name": "Prof. Ayu Lestari", "role": "resource_person", "event": "Treasury training, day 1",
                                                       "event_date": "2026-12-03", "fee_minor": 50_000, "currency": "USD", "charge_code": "US-200.OPS.6200"})
    assert r.status_code == 200, r.text
    eid = r.json()["engagement_id"]
    assert client.delete(f"/v1/org/engagements/{eid}?{FM}").status_code == 200
    r = client.post(f"/v1/org/engagements?{FM}", json={"name": "X", "role": "keynote_speaker", "event": "Y", "event_date": "2026-12-03",
                                                       "fee_minor": 50_000, "currency": "USD", "charge_code": "ZZ-1.OPS.6200"})
    assert r.status_code == 409


ENG_CSV = (b"Full name,Type,Organisation,Email,Event,Event date,Honorarium,Currency,Charge code,Documents\n"
           b"Dr. Budi Santoso,Narasumber,Universitas Contoh,budi@contoh.example,Fraud awareness session,2026-12-10,750.00,USD,US-200.OPS.6200,Engagement letter; Invoice\n"
           b"Sarah Lim,Keynote Speaker,,sarah@lim.example,Annual finance forum,2026-12-12,2000.00,USD,US-200.OPS.6600,\n")
PO_CSV = (b"PO number,Vendor ID,Currency,PO total,Charge code,Description,Active date,Expiry date,Billing frequency,Level of effort,Effort unit,Rate\n"
          b"PO-2026-0301,ben-saas-vendor,USD,600.00,US-200.OPS.6300,Office internet,2026-07-01,2026-12-31,Monthly,,,\n"
          b"PO-2026-0302,ben-contractor-okafor,USD,4000.00,US-200.OPS.6200,Research support,2026-11-01,2027-01-31,,10,days,400.00\n")


def test_engagement_and_contract_lists_upload(client):
    for name, body, entity in (("eng.csv", ENG_CSV, "engagement"), ("po.csv", PO_CSV, "po")):
        r = client.post(f"/v1/sources/uploads?filename={name}&entity={entity}&{FM}", content=body)
        assert r.status_code == 200, r.text
        up = r.json()
        r = client.post(f"/v1/sources/uploads/{up['upload_id']}/commit?{FM}", json={"mapping": up["mapping"]})
        assert r.status_code == 200 and not r.json()["rejected"], r.text
    engs = {e["name"]: e for e in client.get("/v1/org/engagements").json()}
    assert engs["Dr. Budi Santoso"]["role"] == "resource_person" and engs["Dr. Budi Santoso"]["documents"] == ["engagement_letter", "invoice"]
    assert engs["Sarah Lim"]["role"] == "keynote_speaker" and engs["Sarah Lim"]["fee_minor"] == 200_000
    pos = {p["po_id"]: p for p in client.get("/v1/org/purchase-orders").json()}
    assert pos["PO-2026-0301"]["recurring"] == "monthly" and pos["PO-2026-0301"]["start_date"] == "2026-07-01"
    assert (pos["PO-2026-0302"]["loe"], pos["PO-2026-0302"]["loe_unit"], pos["PO-2026-0302"]["rate_minor"]) == (10.0, "day", 40_000)
    assert date.fromisoformat(pos["PO-2026-0302"]["expiry_date"]) == date(2027, 1, 31)
