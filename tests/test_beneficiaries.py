# ruff: noqa: E501
"""Users & Beneficiaries: vendors, consultants and customers hold every field a payment or a receipt reads, and a
change made on the dashboard goes through the same trust rules as a sync."""
import json

import pytest
from fastapi.testclient import TestClient

from venthora.config import ROOT
from venthora.masterdata import Vendor, bank_from_payload, beneficiary_payload

FM = "role=finance_manager&by=Dana%20Finch"


@pytest.fixture
def client():
    from venthora.api import app
    with TestClient(app) as c:
        yield c


def test_vendors_and_consultants_list_what_a_payment_reads_with_the_bank_masked(client):
    vendors = {v["id"]: v for v in client.get("/v1/beneficiaries?kind=vendor").json()}
    consultants = {v["id"]: v for v in client.get("/v1/beneficiaries?kind=consultant").json()}
    assert "ben-eu-components" in vendors and "ben-contractor-okafor" in consultants and not set(vendors) & set(consultants)
    v = vendors["ben-eu-components"]
    assert v["payout_method"] == "SWIFT" and v["currency"] == "EUR" and v["contacts"] and v["registered_phone"]
    assert v["bank"]["iban"].startswith("•••• ") and v["bank"]["swift_code"] and v["address"]
    assert any(p["po_id"] == "PO-2026-0102" for p in v["purchase_orders"])
    assert consultants["ben-contractor-okafor"]["engagement"] in ("regular", "short_term")


def test_the_page_fields_rebuild_every_payee_the_scenarios_pay():
    """Every payee of data/payees.json with its bank details of data/beneficiaries.json (what the 136-scenario matrix
    and the live runs pay) fits the vendor record of this page, and the record gives Airwallex the same beneficiary."""
    payees = json.loads((ROOT / "data" / "payees.json").read_text(encoding="utf-8"))["payees"]
    bens = {k: v for k, v in json.loads((ROOT / "data" / "beneficiaries.json").read_text(encoding="utf-8")).items() if not k.startswith("_")}
    assert set(bens) <= set(payees)
    for k, ben in bens.items():
        p, b = payees[k], ben["payload"]["beneficiary"]
        rec = Vendor.model_validate({"vendor_id": k, "name": p["name"], "currency": p["currency"], "payout_method": p.get("payout_method", "LOCAL"),
                                     "contacts": p.get("contacts", []), "default_priority": p.get("default_priority", 5), "budget_line": p.get("budget_line") or "",
                                     "entity_type": b["entity_type"], "transfer_reason": ben.get("transfer_reason", "business_expenses"),
                                     "registered_phone": (p.get("lifecycle") or {}).get("registered_phone") or "", "address": b.get("address") or {},
                                     "bank": bank_from_payload(ben)}).model_dump()
        rebuilt = beneficiary_payload(rec)["payload"]["beneficiary"]
        assert rebuilt["bank_details"] == b["bank_details"], k
        assert rebuilt["entity_type"] == b["entity_type"] and {x: y for x, y in rebuilt["address"].items()} == {x: y for x, y in (b.get("address") or {}).items() if y}, k


def test_a_vendor_added_on_the_dashboard_is_new_until_the_call_back(client):
    rec = {"name": "Harbour Print Co", "currency": "USD", "payout_method": "LOCAL", "contacts": ["billing@harbourprint.example"], "registered_phone": "+1 415 555 0142",
           "default_priority": 5, "budget_line": "suppliers", "entity_type": "COMPANY", "tax_id": "94-1234567",
           "address": {"street_address": "1 Harbour Way", "city": "San Francisco", "state": "CA", "postcode": "94105", "country_code": "US"},
           "bank": {"account_name": "Harbour Print Co", "account_currency": "USD", "bank_country_code": "US", "bank_name": "Bank of America",
                    "account_number": "000123456789", "routing_type": "aba", "routing_value": "026009593"}}
    r = client.post(f"/v1/beneficiaries?kind=vendor&{FM}", json={"record": rec})
    assert r.status_code == 200, r.text
    assert any("registered NEW" in f for f in r.json()["followed"])
    v = next(x for x in client.get("/v1/beneficiaries?kind=vendor").json() if x["name"] == "Harbour Print Co")
    assert v["status"] == "NEW" and v["bank"]["account_number"] == "•••• 6789" and v["tax_id"] == "94-1234567"
    # a change of the phone with the bank kept: no bank change
    r = client.post(f"/v1/beneficiaries?kind=vendor&{FM}", json={"record": rec | {"vendor_id": v["id"], "registered_phone": "+1 415 555 0199", "bank": None}, "keep_bank": True})
    assert r.status_code == 200 and not any("bank details changed" in f for f in r.json()["followed"])
    assert client.post(f"/v1/beneficiaries?kind=vendor&{FM}", json={"record": rec | {"bank": rec["bank"] | {"account_number": "", "iban": ""}}}).status_code == 422
    assert client.post("/v1/beneficiaries?kind=vendor&role=auditor&by=Ines", json={"record": rec}).status_code == 403


def test_customers_are_listed_and_added(client):
    assert any(c["name"] for c in client.get("/v1/beneficiaries?kind=customer").json())
    r = client.post(f"/v1/beneficiaries?kind=customer&{FM}", json={"record": {"name": "Northgate Clinics", "currency": "USD", "contacts": ["ap@northgate.example"],
                                                                         "promises_kept": 9, "promises_missed": 1}})
    assert r.status_code == 200, r.text
    c = next(x for x in client.get("/v1/beneficiaries?kind=customer").json() if x["name"] == "Northgate Clinics")
    assert c["history"] == {"kept": 9, "missed": 1}


STAFF_CSV = (b"Full name,Work email,Office,Job title,Staff number,Employment type,Start date,Net pay,Salary currency,Charge code,Benefits\n"
             b"Sari Wulandari,sari.wulandari@acme.example,US-200,Programme officer,E-1042,Fixed-term,2026-01-05,4200.00,USD,US-200.OPS.6200,Group health insurance; Pension, defined contribution (employer contribution)\n"
             b"Tomas Berg,tomas.berg@acme.example,UK Country Office,Finance assistant,E-1043,Permanent,2025-09-01,3100.00,GBP,UK-300.OPS.6100,Group health insurance\n")


def test_a_staff_list_uploads_with_salary_charge_code_and_benefits(client):
    r = client.post(f"/v1/sources/uploads?filename=staff.csv&entity=user&{FM}", content=STAFF_CSV.replace(b"; Pension", b";Pension"))
    assert r.status_code == 200, r.text
    up = r.json()
    r = client.post(f"/v1/sources/uploads/{up['upload_id']}/commit?{FM}", json={"mapping": up["mapping"]})
    assert r.status_code == 200 and not r.json()["rejected"], r.text
    sari = next(u for u in client.get("/v1/org/users").json() if u["name"] == "Sari Wulandari")
    assert sari["unit_id"] == "co-us" and sari["role"] == "requester" and sari["employee_no"] == "E-1042"
    assert sari["salary_minor"] == 420_000 and sari["salary_currency"] == "USD" and sari["charge_code"] == "US-200.OPS.6200"
    assert sari["employment_type"] == "fixed_term" and sari["contract_start"] == "2026-01-05" and "health" in sari["benefits"]
    assert client.get("/v1/doa/registers").json()["people"][0].keys() >= {"salary_minor", "salary_currency", "charge_code"}


def test_a_person_with_a_wrong_charge_code_or_contract_dates_is_refused(client):
    amira = next(u for u in client.get("/v1/org/users").json() if u["user_id"] == "u-amira")
    assert client.post(f"/v1/org/users?{FM}", json=amira | {"charge_code": "XX-1.OPS.6200"}).status_code == 409
    assert client.post(f"/v1/org/users?{FM}", json=amira | {"contract_start": "2026-05-01", "contract_end": "2026-01-01"}).status_code == 409
    r = client.post(f"/v1/org/users?{FM}", json=amira | {"salary_minor": 500_000, "charge_code": "uk-300.ops.6100"})
    assert r.status_code == 200 and r.json()["charge_code"] == "UK-300.OPS.6100" and r.json()["salary_currency"] == "GBP"  # the office's currency


def test_an_employees_own_account_is_a_beneficiary_new_until_the_call_back(client):
    rec = {"name": "Rina Putri", "currency": "GBP", "payout_method": "LOCAL", "entity_type": "PERSONAL", "contacts": ["rina.putri@acme.example"], "registered_phone": "+44 20 7946 0109",
           "address": {"street_address": "1 Fleet Street", "city": "London", "postcode": "EC4Y 1AA", "country_code": "GB"},
           "bank": {"account_name": "Rina Putri", "account_currency": "GBP", "bank_country_code": "GB", "bank_name": "Monzo Bank", "account_number": "87655678",
                    "routing_type": "sort_code", "routing_value": "040004"}}
    r = client.post(f"/v1/beneficiaries?kind=employee&{FM}", json={"record": rec})
    assert r.status_code == 200 and any("registered NEW" in f for f in r.json()["followed"]), r.text
    emps = {x["name"]: x for x in client.get("/v1/beneficiaries?kind=employee").json()}
    assert emps["Amira Shah"]["status"] == "ACTIVE"  # the demo staff's own accounts, verified at onboarding
    e = emps["Rina Putri"]
    assert e["status"] == "NEW" and e["bank"]["account_number"] == "•••• 5678"
