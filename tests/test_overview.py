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
    assert acts["invoices and claims to approve or reject"]["count"] >= 1 and acts["invoices and claims to approve or reject"]["href"] == "#/pending?needs"
    assert o["actions"]["count"] == sum(a["count"] for a in o["actions"]["items"])
    client.state["pos"] = _pos(20_000, [_req("NEEDS_REVIEW", 30_000, intake_id=rec["intake_id"])])
    o = client.get("/v1/treasury/overview?span=day").json()
    assert o["balance"]["shortfall"] is True and o["pending_payment"]["exceeds_balance"] is True and o["span_label"] == "Today"


def test_no_approval_the_balance_cannot_cover(client):
    rec = client.post("/v1/intake/emails", content=(
        b"From: lumen.design.studio.billing@gmail.com\r\nTo: ap@acme.example\r\nSubject: Invoice LD-4402\r\nMessage-ID: <ov2@t>\r\n\r\n"
        b"Invoice LD-4402 against PO-2026-0101. Amount due: USD 300.00, due 30 Nov 2026.")).json()  # an open PO, as the finance standards ask
    client.state["pos"] = _pos(20_000, [_req("VERIFIED", 5_000, intake_id="other")])  # USD 200 available, USD 50 already approved
    review = {"decision": "verify", "reviewer": "Dana Finch", "role": "finance_manager", "note": "ok"}
    r = client.post(f"/v1/intake/{rec['intake_id']}/review", json=review)
    assert r.status_code == 409 and "USD 300.00 asked, USD 150.00 free" in r.json()["detail"]
    assert client.post(f"/v1/intake/{rec['intake_id']}/review", json=review | {"decision": "reject", "note": "no funds this month"}).status_code == 200
    rec2 = client.post("/v1/intake/emails", content=(
        b"From: lumen.design.studio.billing@gmail.com\r\nTo: ap@acme.example\r\nSubject: Invoice LD-4403\r\nMessage-ID: <ov3@t>\r\n\r\n"
        b"Invoice LD-4403 against PO-2026-0101. Amount due: USD 300.00, due 30 Nov 2026.")).json()
    client.state["pos"] = _pos(10_000_000)
    assert client.post(f"/v1/intake/{rec2['intake_id']}/review", json=review).status_code == 200


def test_rates_are_read_at_0001_and_1159_utc():
    at = lambda h, m: dt.datetime(2026, 11, 5, h, m, 30, tzinfo=dt.UTC)  # noqa: E731
    assert position.rate_slot(at(0, 0)) == "2026-11-04T11:59:00+00:00"  # before 00:01: yesterday's noon read
    assert position.rate_slot(at(0, 1)) == "2026-11-05T00:01:00+00:00"
    assert position.rate_slot(at(11, 58)) == "2026-11-05T00:01:00+00:00"
    assert position.rate_slot(at(18, 0)) == "2026-11-05T11:59:00+00:00"
    assert position.next_rate_slot(at(18, 0)) == "2026-11-06T00:01:00+00:00"
    assert position.next_rate_slot(at(6, 0)) == "2026-11-05T11:59:00+00:00"


def test_due_in_72_hours_counts_overdue_and_skips_paid_and_undated():
    today = dt.date(2026, 11, 5)
    reqs = [NS(**vars(_req("VERIFIED", 10_000, intake_id="a")) | {"due_date": "2026-11-08"}),
            NS(**vars(_req("NEEDS_REVIEW", 5_000, ccy="EUR", intake_id="b")) | {"due_date": "2026-11-01"}),  # overdue
            NS(**vars(_req("VERIFIED", 7_000, intake_id="c")) | {"due_date": "2026-11-09"}),  # after 72 hours
            NS(**vars(_req("VERIFIED", 9_000, intake_id="d", fp="paid")) | {"due_date": "2026-11-06"}),  # already paid
            _req("VERIFIED", 3_000, intake_id="e")]  # no due date
    d = position.due_within(reqs, {"paid"}, {"EUR": Decimal("1.10")}, "USD", today, 72)
    assert d["count"] == 2 and d["base_minor"] == 10_000 + 5_500 and d["overdue_count"] == 1 and d["overdue_base_minor"] == 5_500
    assert d["undated_count"] == 1 and [x["intake_id"] for x in d["items"]] == ["b", "a"]


def test_overview_carries_commitments_and_the_rate_snapshot(client):
    o = client.get("/v1/treasury/overview?span=month").json()
    cm, rs = o["commitments"], o["rates_snapshot"]
    assert cm["balance_after_commitments_base_minor"] == o["balance"]["available_base_minor"] - cm["total_base_minor"]
    assert cm["available_after_72h_base_minor"] == o["balance"]["available_base_minor"] - cm["due_72h"]["base_minor"] + cm["incoming_72h_base_minor"]
    assert cm["total_base_minor"] == sum(x["base_minor"] for x in cm["by_report"])
    assert rs["slots_utc"] == ["00:01", "11:59"] and rs["fetched_at"] and rs["slot"] <= rs["fetched_at"]
    # the snapshot is kept until the next slot: a second view uses the same rates even if Airwallex's moved
    client.state["pos"] = client.state["pos"] | {"rates": {**client.state["pos"]["rates"], "EUR": "2.00"}}
    again = client.get("/v1/treasury/overview?span=month").json()
    assert again["rates_snapshot"]["fetched_at"] == rs["fetched_at"]
    assert {e["currency"]: e["rate"] for e in again["balance"]["equivalents"]}["EUR"] != "2.00"
