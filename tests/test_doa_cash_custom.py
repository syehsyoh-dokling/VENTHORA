# ruff: noqa: E501
"""06 Cash advance and slush fund, and the categories the FCO adds: paid in real time when the request comes from
the person the delegation names and carries what it requires."""
import pytest
from fastapi.testclient import TestClient

from venthora.testing.review_cases import postmark

FCO = "role=super_admin&by=Rosa%20Lindqvist"


@pytest.fixture
def client(monkeypatch):
    from venthora.api import app
    monkeypatch.setenv("VENTHORA_INBOUND_EMAIL_SECRET", "s3cret-for-tests")
    with TestClient(app) as c:
        amira = next(u for u in c.get("/v1/org/users").json() if u["user_id"] == "u-amira")
        assert c.post(f"/v1/org/users?{FCO}", json=amira | {"payee_id": "payroll-group-us"}).status_code == 200  # a USD account stands in for hers
        yield c


def _mail(c, n, sender, name, subject, body):
    case = {"id": f"cc-{n}", "sender": sender, "name": name, "subject": subject, "body": body}
    r = c.post("/webhooks/email", json=postmark(case, f"99{n}"), auth=("postmark", "s3cret-for-tests"))
    assert r.status_code == 200, r.text
    return next(x for x in c.get("/v1/intake").json() if x["intake_id"] == r.json()["intake_id"])


def test_a_cash_advance_instructed_by_the_one_person_named_is_paid_to_the_person_named(client):
    assert client.put(f"/v1/doa/policies/cash_advance?{FCO}", json={"requester": "u-omar"}).status_code == 200
    got = _mail(client, 1, "omar.reyes@acme.example", "Omar Reyes", "Cash advance", "Please pay a cash advance of USD 500.00 to Amira Shah for the field office float.")
    assert got["kind"] == "expense_claim" and got["counterparty_name"] == "Amira Shah" and got["status"] == "VERIFIED", got["reasons"]
    ob = next(o for o in client.get("/v1/intake/state/demo_live").json()["obligations"] if o["obligation_id"] == f"ADVANCE-{got['intake_id'][:8].upper()}")
    assert ob["beneficiary_id"] == "payroll-group-us" and ob["amount_minor"] == 50_000
    # from anyone else it is not a cash advance
    got = _mail(client, 2, "dana.finch@acme.example", "Dana Finch", "Cash advance", "Please pay a cash advance of USD 450.00 to Amira Shah for the field office float.")
    assert "doa_cash_advance" not in got["flags"] and got["status"] != "VERIFIED"
    # someone without a registered account waits for a person
    got = _mail(client, 3, "omar.reyes@acme.example", "Omar Reyes", "Cash advance", "Please pay a cash advance of USD 300.00 to Jonas Weber for the workshop.")
    assert got["status"] == "NEEDS_REVIEW" and any("no registered bank account" in r for r in got["reasons"])


def test_the_fco_adds_a_category_and_a_complete_email_for_it_is_paid_at_once(client):
    assert client.post("/v1/doa/custom?role=finance_manager&by=Dana", json={"title": "Generator fuel"}).status_code == 403
    r = client.post(f"/v1/doa/custom?{FCO}", json={"title": "Generator fuel", "charge_code": "US-200.OPS.6700", "email_people": ["u-dana"], "login_people": ["u-dana"],
                                                  "requirements": ["pr_number", "vendor_name", "account_number", "bank_name", "country", "amount"]})
    assert r.status_code == 200, r.text
    cat = r.json()
    assert cat["code"] == "08" and cat["key"] == "c-generator-fuel"
    assert [c["key"] for c in client.get("/v1/doa").json()["custom"]] == ["c-generator-fuel"]
    body = ("Generator fuel for the office, PR-2026-0142. Please pay Cloudloom Software LLC USD 300.00, invoice CL-FUEL-1. "
            "Bank of America, account number 483101090002, country United States.")
    got = _mail(client, 4, "dana.finch@acme.example", "Dana Finch", "Generator fuel", body)
    assert got["kind"] == "supplier_invoice" and got["counterparty_id"] == "ben-saas-vendor" and got["status"] == "VERIFIED", got["reasons"]
    assert got["account_code"] == "US-200.OPS.6700"
    got = _mail(client, 5, "dana.finch@acme.example", "Dana Finch", "Generator fuel", body.replace("PR-2026-0142", "the November order").replace("CL-FUEL-1", "CL-FUEL-2"))
    assert got["status"] == "NEEDS_REVIEW" and any("missing a purchase request number" in r for r in got["reasons"])


def test_on_the_dashboard_a_person_named_for_the_category_is_paid_at_once(client):
    client.post(f"/v1/doa/custom?{FCO}", json={"title": "Generator fuel", "charge_code": "US-200.OPS.6700", "login_people": ["u-dana"], "requirements": ["pr_number", "amount"]})
    order = {"vendor_id": "ben-saas-vendor", "invoice_no": "CL-FUEL-9", "amount_minor": 30_000, "currency": "USD", "due_date": "2026-12-01",
             "category": "c-generator-fuel", "pr_number": "PR-2026-0150"}
    r = client.post("/v1/intake/payment-orders?role=finance_manager&by=Dana%20Finch", json=order)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "VERIFIED" and r.json()["account_code"] == "US-200.OPS.6700", r.json()["reasons"]
    r = client.post("/v1/intake/payment-orders?role=finance_manager&by=Dana%20Finch", json=order | {"invoice_no": "CL-FUEL-10", "pr_number": None})
    assert r.json()["status"] == "NEEDS_REVIEW" and any("missing a purchase request number" in x for x in r.json()["reasons"])
    r = client.post("/v1/intake/payment-orders?role=cfo&by=Omar%20Reyes", json=order | {"invoice_no": "CL-FUEL-11"})
    assert r.json()["status"] == "NEEDS_REVIEW"  # Omar is not named for it
