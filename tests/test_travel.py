# ruff: noqa: E501
"""Travel and transport under the delegation of authority: the places and rates decide, every ticked document is
required, the channel is the delegation's; a complete claim is paid to the traveller's own registered account."""
import base64
import datetime as dt

import pytest
from fastapi.testclient import TestClient

from venthora.testing.pdfs import make_pdf
from venthora.testing.review_cases import postmark

FCO = "role=super_admin&by=Rosa%20Lindqvist"
DANA = "role=finance_manager&by=Dana%20Finch"
START, END = dt.date.today() + dt.timedelta(days=3), dt.date.today() + dt.timedelta(days=6)  # 3 nights, 4 days


@pytest.fixture
def client():
    from venthora.api import app
    with TestClient(app) as c:
        for country, city, lodging, pd in (("United Kingdom", "London", 25_000, 8_000), ("Indonesia", "Jakarta", 16_500, 7_500), ("Kenya", "Nairobi", 21_000, 9_000)):
            assert c.post(f"/v1/org/travel-rates?{DANA}", json={"country": country, "city": city, "currency": "USD", "lodging_minor": lodging, "per_diem_minor": pd}).status_code == 200
        amira = next(u for u in c.get("/v1/org/users").json() if u["user_id"] == "u-amira")
        assert c.post(f"/v1/org/users?{FCO}", json=amira | {"payee_id": "payroll-group-us"}).status_code == 200  # a USD account stands in for hers
        assert c.put(f"/v1/doa/policies/travel?{FCO}", json={"dest_countries": ["Indonesia"], "dest_cities": ["*"], "documents": ["travel_request", "receipts"]}).status_code == 200
        yield c


def _pdf(text):
    return {"name": "file.pdf", "content_type": "application/pdf", "content_b64": base64.b64encode(make_pdf(text)).decode()}


DOCS = {"travel_request": [_pdf(["Travel request", "Approved by the supervisor", "Amira Shah, Jakarta"])],
        "receipts": [_pdf(["Receipt", "Hotel Indonesia Kempinski", "Paid in full"])]}


def _request(c, dest="Indonesia/Jakarta", amount=None, docs=DOCS):
    body = {"traveller": "u-amira", "origin": "United Kingdom/London", "destination": dest, "start": START.isoformat(), "end": END.isoformat(),
            "purpose": "Supervision visit", "amount_minor": amount, "documents": docs}
    r = c.post(f"/v1/doa/travel-requests?{DANA}", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_a_complete_dashboard_request_inside_the_rates_is_paid_to_the_travellers_account(client):
    r = _request(client)
    assert r["kind"] == "expense_claim" and r["status"] == "VERIFIED", r["reasons"]
    assert r["amount_minor"] == 3 * 16_500 + 4 * 7_500  # what the rates allow, by default
    assert any("London, United Kingdom to Jakarta, Indonesia, 3 night(s), 4 day(s)" in x for x in r["reasons"])
    state = client.get("/v1/intake/state/demo_live").json()
    ob = next(o for o in state["obligations"] if o["obligation_id"] == f"TRAVEL-{r['intake_id'][:8].upper()}")
    assert ob["beneficiary_id"] == "payroll-group-us" and ob["amount_minor"] == r["amount_minor"]


def test_a_request_missing_a_ticked_document_is_rejected(client):
    r = _request(client, docs={"travel_request": DOCS["travel_request"]})
    assert r["status"] == "REJECTED" and any("travel claim is incomplete" in x and "Receipt" in x for x in r["reasons"])


def test_above_the_rates_waits_for_a_person_and_outside_the_places_is_rejected(client):
    r = _request(client, amount=3 * 16_500 + 4 * 7_500 + 1)
    assert r["status"] == "NEEDS_REVIEW" and any("above what the rates allow" in x for x in r["reasons"])
    r = _request(client, dest="Kenya/Nairobi")
    assert r["status"] == "REJECTED" and any("Nairobi, Kenya is not a destination the delegation allows" in x for x in r["reasons"])


def test_the_channel_is_the_delegations(client, monkeypatch):
    monkeypatch.setenv("VENTHORA_INBOUND_EMAIL_SECRET", "s3cret-for-tests")
    body = f"Travel claim: trip from London to Jakarta, {START:%d %b %Y} to {END:%d %b %Y}, 3 nights. Total USD 795.00."
    case = {"id": "trip-1", "sender": "amira.shah@acme.example", "name": "Amira Shah", "subject": "Travel claim Jakarta", "body": body,
            "pdf": ["Travel request", "Approved by the supervisor", "Receipt", "Paid in full"]}
    r = client.post("/webhooks/email", json=postmark(case, "881"), auth=("postmark", "s3cret-for-tests"))
    got = next(x for x in client.get("/v1/intake").json() if x["intake_id"] == r.json()["intake_id"])
    assert got["status"] == "VERIFIED", got["reasons"]  # email from the traveller is accepted by default
    assert client.put(f"/v1/doa/policies/travel?{FCO}", json={"channel": "dashboard"}).status_code == 200
    r = client.post("/webhooks/email", json=postmark(case | {"id": "trip-2", "body": body.replace("795.00", "780.00")}, "882"), auth=("postmark", "s3cret-for-tests"))
    got = next(x for x in client.get("/v1/intake").json() if x["intake_id"] == r.json()["intake_id"])
    assert got["status"] == "REJECTED" and any("from the dashboard only" in x for x in got["reasons"])
    assert _request(client, amount=70_000)["status"] == "VERIFIED"  # the dashboard still is
