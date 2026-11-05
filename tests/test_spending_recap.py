# ruff: noqa: E501
"""Reporting -> Payment reports: one recap of every report. A PO paid in GBP counts as spent at the USD Airwallex
converted at, on the day it was paid, with its place and charge code; what is left on it is a commitment."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from venthora.api import app
    with TestClient(app) as c:
        yield c


def test_the_recap_carries_each_payment_at_its_converted_cost_and_what_is_still_owed(client):
    client.app.state.executor.store.book_payment({"transfer_id": "tr-recap-1", "book_id": "b", "obligation_id": "o-r1", "invoice_fingerprint": "fp-r1",
                                                  "installment": "partial", "contract_total_minor": None, "line": None, "currency": "GBP", "amount_minor": 100_000,
                                                  "base_cost_minor": 127_000, "paid_at": "2026-11-02T10:00:00+00:00", "reporting_period": "2026-11",
                                                  "beneficiary_id": "x", "po_number": "PO-2026-0103", "account_code": "UK-300.OPS.6100"})
    d = client.get("/v1/reports/spending-recap").json()
    assert d["base_currency"] == "USD" and {"payroll", "goods_services", "fx", "card"} <= set(d["reports"])
    po = next(line for line in d["lines"] if line["report"] == "goods_services" and str(line["reference"]).startswith("PO-2026-0103"))
    pay = next(p for p in po["payments"] if p["transfer_id"] == "tr-recap-1")
    assert pay["base_minor"] == 127_000 and pay["basis"] == "actual" and pay["paid_at"].startswith("2026-11-02")
    assert po["charge_code"] == "UK-300.OPS.6100" and po["country_office"] and po["remaining_minor"] == po["amount_minor"] - po["paid_minor"]
    assert po["left_base_minor"] > 0 and all({"report_label", "payments", "left_base_minor", "branch", "unit"} <= set(line) for line in d["lines"])
