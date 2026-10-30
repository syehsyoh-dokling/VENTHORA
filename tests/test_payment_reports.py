# ruff: noqa: E501
"""One PO may be paid several times while it has room, never past its total, each term once and in full; every
payment lands in the running and accumulated reports; every PO is reconciled against what was paid."""
import datetime as dt
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from venthora import payment_reports as pr
from venthora.execution.executor import Executor, Halt
from venthora.execution.store import ExecutionStore
from venthora.organisation import OrgStore

PO = "PO-2026-0101"  # Lumen, USD 5,000.00: T1 1,800.00 + T2 3,200.00, partial invoices allowed


def _ledger(store, tid, amount, installment=None, po=PO, paid_at="2026-10-10T03:00:00+00:00", period="2026-10", ben="ben-lumen-design", ccy="USD"):
    with store._connect() as c:
        c.execute("INSERT INTO spend_ledger (transfer_id, book_id, obligation_id, installment, currency, amount_minor, base_cost_minor, paid_at,"
                  " reporting_period, recorded_at, beneficiary_id, po_number, account_code) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (tid, "book", f"o-{tid}", installment, ccy, amount, amount, paid_at, period, paid_at, ben, po, "US-200.BRAND26.6600"))


def _gate(tmp_path, items, setup=None):
    store = ExecutionStore(str(tmp_path / "e.db"))
    org = OrgStore(str(tmp_path / "o.db"))
    if setup:
        setup(store, org)
    obs = {i["id"]: NS(obligation_id=i["id"], po_number=i.get("po", PO), currency=i.get("ccy", "USD"), beneficiary_id=i.get("ben", "ben-lumen-design"),
                       amount_minor=i["amount"], installment=i.get("term")) for i in items}
    plan = NS(obligations=[NS(obligation_id=i["id"], action="PAY", amount_minor=i.get("pay", i["amount"])) for i in items])
    steps = []

    def refuse(msg):
        raise Halt("REFUSED", msg)
    try:
        Executor._po_gate(NS(org=org, store=store), plan, obs, steps, refuse)
        return "OK", steps
    except Halt as h:
        return str(h), steps


def test_a_po_is_paid_several_times_but_never_past_its_total(tmp_path):
    ok, steps = _gate(tmp_path, [{"id": "a", "amount": 200_000}, {"id": "b", "amount": 150_000}])
    assert ok == "OK" and steps[0].detail["checked"][-1]["left_after_minor"] == 150_000
    msg, _ = _gate(tmp_path / "2", [{"id": "a", "amount": 300_000}, {"id": "b", "amount": 250_000}])
    assert "b: PO PO-2026-0101 has USD 2,000.00 left of 5,000.00; paying 2,500.00 would overpay it" in msg


def test_every_payment_is_the_next_termin_of_any_amount(tmp_path):
    ok, steps = _gate(tmp_path, [{"id": "a", "amount": 170_000, "term": "T1"}, {"id": "b", "amount": 120_000}],
                      setup=lambda s, o: _ledger(s, "tr1", 180_000, "T1"))
    assert ok == "OK"  # T1 was paid already and 1,700 is no scheduled amount: neither matters while the PO has room
    assert [(c["obligation_id"], c["termin"], c["left_after_minor"]) for c in steps[0].detail["checked"]] == [("a", 2, 150_000), ("b", 3, 30_000)]
    msg, _ = _gate(tmp_path / "2", [{"id": "c", "amount": 330_000}], setup=lambda s, o: _ledger(s, "tr1", 180_000, "T1"))
    assert "has USD 3,200.00 left" in msg and "would overpay it" in msg
    msg, _ = _gate(tmp_path / "3", [{"id": "x", "amount": 100_000, "pay": 60_000}])
    assert "paid in full or not at all" in msg


def test_money_in_flight_holds_its_part_of_the_po(tmp_path):
    def flying(store, org):
        _ledger(store, "tr1", 100_000)
        store.claim_payout("k1", "o-old", "exec-old")  # claimed, transfer not yet PAID
        store.record_po_draw("k1", PO, None, "USD", 300_000, "exec-old")
    msg, _ = _gate(tmp_path, [{"id": "n", "amount": 150_000}], setup=flying)
    assert "has USD 1,000.00 left" in msg  # 5,000 - 1,000 paid - 3,000 in flight
    ok, _ = _gate(tmp_path / "2", [{"id": "n", "amount": 100_000}], setup=flying)
    assert ok == "OK"


def test_wrong_po_supplier_currency_or_status(tmp_path):
    assert "was placed with" in _gate(tmp_path, [{"id": "a", "amount": 100, "ben": "ben-uk-logistics"}])[0]
    assert "is in USD, the payment in EUR" in _gate(tmp_path / "2", [{"id": "a", "amount": 100, "ccy": "EUR"}])[0]
    assert "does not exist" in _gate(tmp_path / "3", [{"id": "a", "amount": 100, "po": "PO-NOPE-1"}])[0]


def test_running_and_accumulated_windows():
    rows = [{"paid_on": d, "reporting_period": p, "payee": "Lumen", "po_number": PO, "installment": None, "termin": 1, "currency": "USD",
             "amount_minor": a, "base_cost_minor": a} for d, p, a in [("2026-10-30", "2026-10", 100), ("2026-10-27", "2026-11", 200),
                                                                     ("2026-10-02", "2026-10", 400), ("2026-09-15", "2026-09", 800), ("2027-01-05", "2027-01", 1600)]]
    day = dt.date(2026, 10, 30)  # a Friday
    assert pr.running(rows, "day", day, 1)["summary"]["by_currency"] == {"USD": 100}
    assert pr.running(rows, "week", day, 1)["from"] == "2026-10-26" and pr.running(rows, "week", day, 1)["summary"]["payments"] == 2
    assert pr.running(rows, "month", day, 1)["summary"]["base_cost_minor"] == 700
    assert pr.running(rows, "quarter", day, 10)["from"] == "2026-10-01"  # Q1 starts in October
    assert pr.running(rows, "quarter", day, 1)["from"] == "2026-10-01" and pr.running(rows, "quarter", day, 7)["from"] == "2026-10-01"
    q = pr.accumulated(rows, "quarter", day, 1)
    assert [b["period"] for b in q["buckets"]] == ["2026-Q1", "2026-Q2", "2026-Q3", "2026-Q4"]
    assert [b["cumulative_base_cost_minor"] for b in q["buckets"]] == [0, 0, 800, 1500]  # by reporting period: the 27 Oct payment counts in November
    m = pr.accumulated(rows, "month", day, 10)
    assert m["fiscal_year"] == 2026 and m["buckets"][0]["period"] == "2026-10" and m["buckets"][3]["base_cost_minor"] == 1600
    assert [b["period"] for b in pr.accumulated(rows, "half", day, 1)["buckets"]] == ["2026-H1", "2026-H2"]
    assert [b["period"] for b in pr.accumulated(rows, "year", day, 1)["buckets"]] == ["2024", "2025", "2026"]


@pytest.fixture
def client():
    from venthora.api import app
    with TestClient(app) as c:
        yield c


def test_report_endpoints_and_po_reconciliation(client):
    from venthora.api import app
    from venthora.organisation import PurchaseOrder

    store = app.state.executor.store
    today = dt.datetime.now(dt.UTC)
    month = today.strftime("%Y-%m")
    _ledger(store, "tr1", 180_000, "T1", paid_at=today.isoformat(), period=month)
    _ledger(store, "tr2", 50_000, "partial", paid_at=today.isoformat(), period=month)
    rep = client.get("/v1/reports/payments?view=running&span=month").json()
    assert rep["summary"]["payments"] == 2 and rep["summary"]["by_currency"] == {"USD": 230_000}
    assert rep["summary"]["by_po"][0] == {"po_number": PO, "payments": 2, "currency": "USD", "amount_minor": 230_000, "terms": ["termin-1", "termin-2"]}
    acc = client.get("/v1/reports/payments?view=accumulated&span=month").json()
    assert sum(b["base_cost_minor"] for b in acc["buckets"]) == 230_000 and acc["buckets"][-1]["cumulative_base_cost_minor"] == 230_000
    csv = client.get("/v1/reports/payments?view=running&span=month&format=csv")
    assert csv.headers["content-type"].startswith("text/csv") and "TOTAL,,,,,,USD,2300.00" in csv.text
    assert client.get("/v1/reports/payments?view=running&span=half").status_code == 422
    rec = {p["po_id"]: p for p in client.get("/v1/reports/purchase-orders").json()}
    p = rec[PO]
    assert (p["paid_minor"], p["left_minor"], p["state"], p["payments"]) == (230_000, 270_000, "OPEN", 2)
    assert [t["state"] for t in p["terms"]] == ["PAID", "OPEN"]  # the schedule beside the termins
    assert [(t["termin"], t["amount_minor"], t["scheduled_term"]) for t in p["termins"]] == [(1, 180_000, "T1"), (2, 50_000, None)] and p["next_termin"] == 3
    assert [r["termin"] for r in rep["rows"]] == [1, 2]
    po = app.state.org.get("po", PO) | {"status": "CLOSED"}
    app.state.org.save("po", PurchaseOrder.model_validate(po), "test")
    assert {p["po_id"]: p for p in client.get("/v1/reports/purchase-orders").json()}[PO]["state"] == "UNDERPAID"
    _ledger(store, "tr3", 400_000, "partial", paid_at=today.isoformat(), period=month)
    assert {p["po_id"]: p for p in client.get("/v1/reports/purchase-orders").json()}[PO]["state"] == "OVERPAID"
