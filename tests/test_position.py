# ruff: noqa: E501
"""The treasury position: what is in the wallet now, and what is about to change it, each figure from its own source."""
from decimal import Decimal
from types import SimpleNamespace

from fastapi.testclient import TestClient

from venthora.position import compose


def req(kind, status, amount, ccy, who, fp=None, po=None, intake_id="i"):
    return SimpleNamespace(kind=kind, status=status, amount_minor=amount, currency=ccy, counterparty_name=who, sender=who,
                           invoice_fingerprint=fp, invoice_no=fp, po_number=po, due_date="2026-11-07", intake_id=intake_id)


def test_position_from_wallet_transfers_deposits_and_queue():
    balances = {"USD": {"available": 2_000_000, "pending": 100_000, "reserved": 50_000, "total": 2_150_000},
                "EUR": {"available": 500_000, "pending": 0, "reserved": 0, "total": 500_000},
                "JPY": {"available": 0, "pending": 0, "reserved": 0, "total": 0}}
    rates = {"EUR": Decimal("1.10"), "GBP": Decimal("1.30")}
    transfers = [{"status": "PROCESSING", "transfer_currency": "USD", "transfer_amount": 300.0, "id": "t1"},
                 {"status": "PAID", "transfer_currency": "USD", "transfer_amount": 999.0, "id": "t2"},
                 {"status": "SENT", "transfer_currency": "EUR", "transfer_amount": 100.0, "id": "t3"}]
    deposits = [{"status": "PENDING", "currency": "USD", "amount": 5000.0, "payer": {"name": "Acme Treasury Top-up"}, "id": "d1"},
                {"status": "PENDING", "currency": "USD", "amount": 1000.0, "payer": {"name": "Acme Retail Ltd"}, "id": "d2"},
                {"status": "SETTLED", "currency": "USD", "amount": 6500.0, "payer": {"name": "Acme Retail Ltd"}, "id": "d3"}]
    requests = [req("supplier_invoice", "VERIFIED", 180_000, "USD", "Lumen", fp="lumen:LD-1"),
                req("supplier_invoice", "NEEDS_REVIEW", 100_000, "GBP", "Thames", fp="thames:TF-1"),
                req("supplier_invoice", "VERIFIED", 70_000, "USD", "Lumen", fp="lumen:LD-0"),  # already paid
                req("supplier_invoice", "QUARANTINED", 9_900_000, "USD", "forged", fp="x"),   # never a bill
                req("remittance_notice", "VERIFIED", 650_000, "USD", "Acme Retail Ltd"),       # landed (d3)
                req("remittance_notice", "VERIFIED", 200_000, "EUR", "Acme Retail Ltd")]       # announced, not arrived
    p = compose(balances=balances, rates=rates, base="USD", transfers=transfers, deposits=deposits, requests=requests,
                paid_fingerprints={"lumen:LD-0"}, customers={"Acme Retail Ltd", "Acme"})
    assert p["available"]["base_minor"] == 2_000_000 + 550_000  # EUR 5,000 at 1.10
    assert [w["currency"] for w in p["available"]["wallet"]] == ["EUR", "USD"]  # empty currencies are left out
    assert p["available"]["pending_minor_by_currency"] == {"USD": 100_000} and p["available"]["reserved_minor_by_currency"] == {"USD": 50_000}
    assert p["processing_out"]["count"] == 2 and p["processing_out"]["base_minor"] == 30_000 + 11_000
    assert (p["bills_unpaid"]["approved_count"], p["bills_unpaid"]["waiting_count"]) == (1, 1)
    assert p["bills_unpaid"]["base_minor"] == 180_000 + 130_000
    assert p["deposits_pending"]["count"] == 1 and p["deposits_pending"]["base_minor"] == 500_000
    inc = p["incoming_uncleared"]
    assert (inc["arrived_count"], inc["announced_count"]) == (1, 1) and inc["base_minor"] == 100_000 + 220_000
    assert p["after_all_base_minor"] == 2_550_000 - 41_000 - 310_000 + 500_000 + 320_000
    assert p["missing_rates"] == []


def test_missing_rate_is_reported_not_guessed():
    p = compose(balances={"USD": {"available": 100, "pending": 0, "reserved": 0, "total": 100},
                          "SGD": {"available": 1000, "pending": 0, "reserved": 0, "total": 1000}}, rates={}, base="USD",
                transfers=[], deposits=[], requests=[], paid_fingerprints=set(), customers=set())
    assert p["available"]["base_minor"] == 100 and p["available"]["wallet"][0]["available_base_minor"] is None
    assert p["missing_rates"] == ["SGD"]


def test_endpoint_needs_airwallex():
    from venthora.api import app
    with TestClient(app) as client:
        assert client.get("/v1/treasury/position").status_code == 503
