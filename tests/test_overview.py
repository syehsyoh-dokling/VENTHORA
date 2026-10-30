# ruff: noqa: E501
"""The overview: the Airwallex balance and what it is worth in the major and reporting currencies, red when unpaid
bills exceed it; pending payments, cash out and cash in (customers, not top-ups) for a period; every action that
waits for a person; and no approval the balance cannot cover."""
import datetime as dt
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from venthora import position

NOW = dt.datetime.now(dt.UTC)


def _req(status, amount, ccy="USD", intake_id="i1", fp=None, kind="supplier_invoice"):
    return NS(kind=kind, status=status, amount_minor=amount, currency=ccy, counterparty_name="Lumen Design Studio", sender="x",
              invoice_no="LD-1", po_number=None, due_date=None, intake_id=intake_id, invoice_fingerprint=fp, received_at=NOW.isoformat())


def _pos(available_usd, requests=()):
    p = position.compose(
        balances={"USD": {"available": available_usd, "pending": 0, "reserved": 0, "total": available_usd},
                  "EUR": {"available": 0, "pending": 0, "reserved": 0, "total": 0}},
        rates={"EUR": Decimal("1.08"), "GBP": Decimal("1.27"), "JPY": Decimal("0.0066"), "IDR": Decimal("0.000064")}, base="USD",
        transfers=[], requests=list(requests), paid_fingerprints=set(), customers={"Acme Retail"},
        deposits=[{"status": "SETTLED", "currency": "USD", "amount": 4000, "payer": {"name": "Acme Retail"}, "id": "d1", "created_at": NOW.isoformat()},
                  {"status": "SETTLED", "currency": "USD", "amount": 50000, "payer": {"name": "Treasury top-up"}, "id": "d2", "created_at": NOW.isoformat()},
                  {"status": "SETTLED", "currency": "USD", "amount": 999, "payer": {"name": "Acme Retail"}, "id": "d3", "created_at": "2020-01-05T00:00:00+00:00"}])
    return p | {"as_of": NOW.isoformat(timespec="seconds")}


@pytest.fixture
def client(monkeypatch):
    from venthora import api

    state = {"pos": _pos(10_000_000)}

    async def fake_position(fresh: bool = False):
        return state["pos"]
    monkeypatch.setattr(api, "treasury_position", fake_position)
    with TestClient(api.app) as c:
        c.state = state
        yield c


def test_balance_equivalents_and_the_four_numbers(client):
    from venthora.api import app

    rec = client.post("/v1/intake/emails", content=(
        b"From: lumen.design.studio.billing@gmail.com\r\nTo: ap@acme.example\r\nSubject: Invoice LD-4401\r\nMessage-ID: <ov1@t>\r\n\r\n"
        b"Invoice LD-4401. Amount due: USD 300.00, due 30 Nov 2026.")).json()
    assert rec["status"] == "NEEDS_REVIEW"  # no PO: it waits for a person
    client.state["pos"] = _pos(10_000_000, [_req("NEEDS_REVIEW", 30_000, intake_id=rec["intake_id"])])
    with app.state.executor.store._connect() as c:
        c.execute("INSERT INTO spend_ledger (transfer_id, book_id, obligation_id, currency, amount_minor, base_cost_minor, paid_at, reporting_period,"
                  " recorded_at, beneficiary_id) VALUES ('t1','b','o','USD',15000,15000,?,?,'x','ben-lumen-design')",
                  (NOW.isoformat(), NOW.strftime("%Y-%m")))
    o = client.get("/v1/treasury/overview?span=month").json()
    eq = {e["currency"]: e["amount"] for e in o["balance"]["equivalents"]}
    assert eq["EUR"] == round(100_000 / 1.08, 2) and eq["JPY"] == round(100_000 / 0.0066, 2) and "IDR" in eq
    assert next(e for e in o["balance"]["equivalents"] if e["currency"] == "IDR")["local"] is True
    assert "SGD" not in eq  # no rate, so no figure is guessed
    assert o["balance"]["shortfall"] is False
    assert o["pending_payment"] == {"base_minor": 30_000, "count": 1, "ready_base_minor": 0, "ready_count": 0, "exceeds_balance": False}
    assert (o["cash_out"]["base_minor"], o["cash_out"]["count"], o["cash_out"]["top_vendor"]) == (15_000, 1, "Lumen Design Studio")
    assert (o["cash_in"]["base_minor"], o["cash_in"]["count"]) == (400_000, 1)  # the customer's; the 2020 one is outside the period
    assert (o["cash_in"]["top_ups_base_minor"], o["cash_in"]["top_ups_count"]) == (5_000_000, 1)  # a top-up is not cash in
    acts = {a["label"]: a for a in o["actions"]["items"]}
    assert acts["invoices and claims to approve or reject"]["count"] >= 1 and acts["invoices and claims to approve or reject"]["href"] == "#/intake?needs"
    assert o["actions"]["count"] == sum(a["count"] for a in o["actions"]["items"])
    client.state["pos"] = _pos(20_000, [_req("NEEDS_REVIEW", 30_000, intake_id=rec["intake_id"])])
    o = client.get("/v1/treasury/overview?span=day").json()
    assert o["balance"]["shortfall"] is True and o["pending_payment"]["exceeds_balance"] is True and o["span_label"] == "Today"


def test_no_approval_the_balance_cannot_cover(client):
    rec = client.post("/v1/intake/emails", content=(
        b"From: lumen.design.studio.billing@gmail.com\r\nTo: ap@acme.example\r\nSubject: Invoice LD-4402\r\nMessage-ID: <ov2@t>\r\n\r\n"
        b"Invoice LD-4402. Amount due: USD 300.00, due 30 Nov 2026.")).json()
    client.state["pos"] = _pos(20_000, [_req("VERIFIED", 5_000, intake_id="other")])  # USD 200 available, USD 50 already approved
    review = {"decision": "verify", "reviewer": "Dana Finch", "role": "finance_manager", "note": "ok"}
    r = client.post(f"/v1/intake/{rec['intake_id']}/review", json=review)
    assert r.status_code == 409 and "USD 300.00 asked, USD 150.00 free" in r.json()["detail"]
    assert client.post(f"/v1/intake/{rec['intake_id']}/review", json=review | {"decision": "reject", "note": "no funds this month"}).status_code == 200
    rec2 = client.post("/v1/intake/emails", content=(
        b"From: lumen.design.studio.billing@gmail.com\r\nTo: ap@acme.example\r\nSubject: Invoice LD-4403\r\nMessage-ID: <ov3@t>\r\n\r\n"
        b"Invoice LD-4403. Amount due: USD 300.00, due 30 Nov 2026.")).json()
    client.state["pos"] = _pos(10_000_000)
    assert client.post(f"/v1/intake/{rec2['intake_id']}/review", json=review).status_code == 200
