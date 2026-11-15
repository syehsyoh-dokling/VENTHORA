# ruff: noqa: E501
"""A request that was paid says so wherever it is read, so it is never planned, queued or shown as waiting again.

Found on the live demo (15 Nov 2026): `payment` was filled only by GET /v1/intake, so the planner's own read never saw
it. Paid invoices kept their places among the twelve a plan proves, and the ones beyond it showed as "next round"."""
import pytest
from fastapi.testclient import TestClient

EMAIL = (b"From: lumen.design.studio.billing@gmail.com\r\nTo: ap@acme.example\r\nSubject: Invoice LD-7001\r\nMessage-ID: <paid1@t>\r\n\r\n"
         b"Invoice LD-7001 against PO-2026-0101. Amount due: USD 300.00, due 30 Nov 2026.")


@pytest.fixture
def client():
    from venthora.api import app
    with TestClient(app) as c:
        yield c


def _pay(store, req, n=1):
    store.book_payment({"transfer_id": f"tr-paid-{n}", "book_id": "b", "obligation_id": f"REQ-{req['invoice_no']}", "currency": req["currency"],
                        "amount_minor": req["amount_minor"], "base_cost_minor": req["amount_minor"], "paid_at": "2026-11-15T09:00:00+00:00",
                        "reporting_period": "2026-11", "invoice_fingerprint": req["invoice_fingerprint"], "beneficiary_id": req["counterparty_id"]})


def test_every_read_of_a_paid_request_carries_its_payment(client):
    rec = client.post("/v1/intake/emails", content=EMAIL).json()
    intake, store = client.app.state.intake.store, client.app.state.executor.store
    assert rec["invoice_fingerprint"] and intake.get(rec["intake_id"]).payment is None
    assert next(q for q in intake.list(100) if q.intake_id == rec["intake_id"]).payment is None
    _pay(store, rec)
    paid = intake.get(rec["intake_id"]).payment  # the planner, the sweeps and the notices all read the store like this
    assert paid["amount_minor"] == 30_000 and paid["transfers"] == ["tr-paid-1"] and paid["currency"] == "USD"
    assert next(q for q in intake.list(100) if q.intake_id == rec["intake_id"]).payment["transfers"] == ["tr-paid-1"]
    listed = next(q for q in client.get("/v1/intake").json() if q["intake_id"] == rec["intake_id"])
    assert listed["payment"]["amount_minor"] == 30_000  # and so does the page


def test_a_paid_request_leaves_todays_plan(client):
    rec = client.post("/v1/intake/emails", content=EMAIL).json()
    ref = rec["intake_id"][:8]

    def waiting():
        plan = client.get("/v1/agent-plan").json()
        today = plan.get("today") or {}
        return {r["intake_ref"] for r in (today.get("ready") or []) + (today.get("others") or [])}

    assert ref in waiting()
    _pay(client.app.state.executor.store, rec)
    assert ref not in waiting()  # neither among the twelve nor in "next round"


def test_the_payment_is_read_never_stored(client):
    rec = client.post("/v1/intake/emails", content=EMAIL).json()
    intake, store = client.app.state.intake.store, client.app.state.executor.store
    _pay(store, rec)
    intake.save(intake.get(rec["intake_id"]))  # saved back with its payment attached
    intake.paid = None  # read without the service's lookup: what is really in the record
    try:
        assert intake.get(rec["intake_id"]).payment is None
    finally:
        import functools

        from venthora import api
        intake.paid = functools.partial(api._paid_by_invoice, store)
