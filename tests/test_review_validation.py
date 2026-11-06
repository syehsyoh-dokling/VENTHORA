# ruff: noqa: E501
"""Review session (6 Nov 2026): what another application sends is put in Airwallex's own terms, and a bad value in a
URL or a body is refused with 4xx, never a server error."""
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from venthora.masterdata import beneficiary_payload

FM = "role=finance_manager&by=Dana%20Finch"


@pytest.fixture
def client():
    from venthora.api import app
    with TestClient(app) as c:
        yield c


def _vendor(country, ccy, routing_type="", routing_value="", state="", category=""):
    return {"name": "Test Supplier Ltd", "entity_type": "COMPANY", "payout_method": "LOCAL",
            "address": {"country_code": country, "city": "X", "street_address": "1 Main St", "postcode": "2000", "state": state},
            "bank": {"account_currency": ccy, "account_name": "Test Supplier Ltd", "bank_country_code": country, "account_number": "12345678",
                     "routing_type": routing_type, "routing_value": routing_value, "bank_account_category": category}}


def _bank(p):
    return p["payload"]["beneficiary"]["bank_details"]


def test_a_finance_systems_routing_names_become_airwallex_routing_types():
    assert _bank(beneficiary_payload(_vendor("IN", "INR", "IFSC", "HDFC0000001")))["account_routing_type1"] == "bank_code"
    assert _bank(beneficiary_payload(_vendor("GB", "GBP", "Sort Code", "040004")))["account_routing_type1"] == "sort_code"
    assert _bank(beneficiary_payload(_vendor("US", "USD", "aba", "021000021")))["account_routing_type1"] == "aba"  # already Airwallex's word


def test_an_australian_state_goes_as_iso_3166_2_and_others_stay_as_sent():
    addr = lambda p: p["payload"]["beneficiary"]["address"]  # noqa: E731
    assert addr(beneficiary_payload(_vendor("AU", "AUD", state="NSW")))["state"] == "AU-NSW"
    assert addr(beneficiary_payload(_vendor("AU", "AUD", state="Victoria")))["state"] == "AU-VIC"
    assert addr(beneficiary_payload(_vendor("US", "USD", state="NY")))["state"] == "NY"


def test_a_us_account_category_is_passed_on():
    assert _bank(beneficiary_payload(_vendor("US", "USD", "aba", "021000021", category="Checking")))["bank_account_category"] == "Checking"
    assert "bank_account_category" not in _bank(beneficiary_payload(_vendor("GB", "GBP", "sort_code", "040004")))


def test_a_period_in_a_path_that_is_not_a_month_is_refused_with_422(client):
    assert client.get("/v1/reporting/periods/REVIEW-NONE").status_code == 422
    assert client.get("/v1/reporting/periods/2026-13").status_code == 422
    assert client.get("/v1/budgets/not-a-month/cutoff").status_code == 422
    assert client.get("/v1/reporting/periods/2026-11").status_code == 200


def test_tenant_evaluation_without_its_inputs_is_refused_with_422(client):
    assert client.post("/v1/tenants/eval", json={}).status_code == 422
    assert client.post("/v1/tenants/eval", json={"registry": "x", "book_id": "b"}).status_code == 422


def test_the_airwallex_check_never_blocks_and_says_when_no_account_is_connected(client, monkeypatch):
    import venthora.api as api

    def no_bank():
        raise HTTPException(503, "not configured")
    monkeypatch.setattr(api, "_bank", no_bank)
    rec = {"name": "Harbour Print Co", "currency": "USD", "payout_method": "LOCAL", "contacts": ["billing@harbourprint.example"], "registered_phone": "+1 415 555 0142",
           "default_priority": 5, "budget_line": "suppliers", "entity_type": "COMPANY",
           "address": {"street_address": "1 Harbour Way", "city": "San Francisco", "state": "CA", "postcode": "94105", "country_code": "US"},
           "bank": {"account_name": "Harbour Print Co", "account_currency": "USD", "bank_country_code": "US", "bank_name": "Bank of America",
                    "account_number": "000123456789", "routing_type": "aba", "routing_value": "026009593", "bank_account_category": "Checking"}}
    saved = client.post(f"/v1/beneficiaries?kind=vendor&{FM}", json={"record": rec})
    assert saved.status_code == 200, saved.text
    assert all(r["ok"] is None for r in saved.json()["airwallex_check"].values())  # the save itself is never refused for it
    out = client.post(f"/v1/beneficiaries/bank-check?{FM}", json={})
    assert out.status_code == 200 and out.json()["checked"] >= 1 and out.json()["accepted"] == 0
    assert all(r["ok"] is None and "payment time" in r["detail"] for r in out.json()["results"].values())


def test_the_finance_app_keeps_every_record_sent_and_a_resend_once(client):
    from concurrent.futures import ThreadPoolExecutor

    from venthora.connectors import DEMO_KEY

    before = len(client.get("/demo/legacy-erp-inbox?limit=500").json())

    def send(i, key=None):
        return client.post("/demo/legacy-erp/inbox/payee_status", json={"id": f"p{i}"},
                           headers={"X-Legacy-Key": DEMO_KEY, "Idempotency-Key": key or f"review-{i}"}).json()
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(send, range(20)))
    assert send(0, "review-0") == {"received": "p0"}  # a resend gets the first reply back and is not stored again
    assert len(client.get("/demo/legacy-erp-inbox?limit=500").json()) == before + 20
    page = client.get("/demo/finance-app/data").json()
    assert page["counts"]["vendors"] >= 5 and all(n >= 5 for k, n in page["counts"].items() if k != "books")
