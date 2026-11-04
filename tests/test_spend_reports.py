# ruff: noqa: E501
"""Reporting per spending category: each line with its name, request date, number, amount, paid, left and status,
placed in the organisation so it can be filtered by country office, branch, unit and person."""
import pytest
from fastapi.testclient import TestClient

FM = "role=finance_manager&by=Dana%20Finch"


@pytest.fixture
def client():
    from venthora.api import app
    with TestClient(app) as c:
        yield c


def book(client, po, amount, ccy, code, n):
    client.app.state.executor.store.book_payment({"transfer_id": f"tr-{n}", "book_id": "b", "obligation_id": f"o-{n}", "invoice_fingerprint": f"fp-{n}",
                                                  "installment": "partial", "contract_total_minor": None, "line": None, "currency": ccy, "amount_minor": amount,
                                                  "base_cost_minor": amount, "paid_at": "2026-11-02T10:00:00+00:00", "reporting_period": "2026-11",
                                                  "beneficiary_id": "x", "po_number": po, "account_code": code})


def test_goods_and_services_lists_pos_contracts_engagements_and_requests(client):
    book(client, "PO-2026-0103", 400_000, "GBP", "UK-300.OPS.6100", 1)
    client.post(f"/v1/org/purchase-requests?{FM}", json={"title": "Chairs", "requested_by": "Amira Shah", "currency": "GBP", "estimated_minor": 90_000,
                                                         "charge_code": "UK-300.OPS.6700"})
    d = client.get("/v1/reports/spend?category=goods_services").json()
    rows = {r["reference"].split(" ·")[0]: r for r in d["rows"]}
    t = rows["PO-2026-0103"]
    assert (t["status"], t["paid_minor"], t["remaining_minor"], t["paid_date"], t["kind"]) == ("Partial", 400_000, 600_000, "2026-11-02", "goods")
    assert t["country_office_id"] == "co-uk" and t["unit_id"] == "co-uk"
    assert rows["PO-2026-0104"]["type"] == "consultant contract" and rows["PO-2026-0104"]["kind"] == "services"
    assert rows["ENG-2026-001"]["type"] == "keynote speaker" and rows["ENG-2026-001"]["status"] == "New"
    pr = next(r for r in d["rows"] if r["reference_kind"] == "PR")
    assert pr["type"] == "purchase request" and pr["person"] == "Amira Shah" and pr["country_office_id"] == "co-uk"
    assert [r["no"] for r in d["rows"]] == list(range(1, len(d["rows"]) + 1)) and "goods" in [r["kind"] for r in d["rows"]]
    assert d["totals"]["GBP"]["paid_minor"] == 400_000 and any(u["unit_id"] == "br-uk-manchester" for u in d["units"])


def test_cash_advances_and_travel_are_listed_with_their_person(client):
    client.post(f"/v1/org/fund-requests?{FM}", json={"kind": "petty_cash", "requested_by": "u-amira", "amount_minor": 30_000, "currency": "GBP",
                                                    "charge_code": "UK-300.OPS.1400", "purpose": "Office float"})
    client.post(f"/v1/org/travel-authorisations?{FM}", json={"traveller": "u-dana", "origin": "United States/New York", "destination": "United Kingdom/London",
                                                            "start": "2026-11-16", "end": "2026-11-19", "estimated_minor": 240_000, "currency": "USD"})
    ca = client.get("/v1/reports/spend?category=cash_advance").json()["rows"]
    assert ca[0]["type"] == "petty cash / slush fund" and ca[0]["person"] == "Amira Shah" and ca[0]["status"] == "New"
    tr = client.get("/v1/reports/spend?category=travel").json()["rows"]
    assert tr[0]["type"] == "travel authorisation" and tr[0]["person"] == "Dana Finch" and tr[0]["country_office_id"] == "co-us"
    for cat in ("payroll", "fringe", "operation", "odc"):
        assert client.get(f"/v1/reports/spend?category={cat}").status_code == 200
    assert client.get("/v1/reports/spend?category=nope").status_code == 422
