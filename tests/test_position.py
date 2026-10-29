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
    assert p["after_all_base_minor"] == 2_550_000 - 310_000 + 500_000 + 320_000  # processing is already out of available
    assert p["missing_rates"] == []


def test_an_older_deposit_does_not_hide_a_new_announcement():
    # seen live: Acme had paid USD 6,500 on 23 Oct; a new USD 6,500 remittance announced on 29 Oct must still show
    old = {"status": "SETTLED", "currency": "USD", "amount": 6500.0, "payer": {"name": "Acme Retail Ltd"}, "id": "d-old",
           "created_at": "2026-10-23T01:30:34+0000"}
    new_notice = req("remittance_notice", "VERIFIED", 650_000, "USD", "Acme Retail Ltd")
    new_notice.received_at = "2026-10-29T14:00:00+00:00"
    p = compose(balances={}, rates={}, base="USD", transfers=[], deposits=[old], requests=[new_notice],
                paid_fingerprints=set(), customers={"Acme Retail Ltd"})
    assert p["incoming_uncleared"]["announced_count"] == 1 and p["incoming_uncleared"]["base_minor"] == 650_000
    later = old | {"id": "d-new", "created_at": 1793283000000}  # epoch ms, as webhook data carries it: after the notice
    p = compose(balances={}, rates={}, base="USD", transfers=[], deposits=[old, later], requests=[new_notice],
                paid_fingerprints=set(), customers={"Acme Retail Ltd"})
    assert p["incoming_uncleared"]["announced_count"] == 0


def test_missing_rate_is_reported_not_guessed():
    p = compose(balances={"USD": {"available": 100, "pending": 0, "reserved": 0, "total": 100},
                          "SGD": {"available": 1000, "pending": 0, "reserved": 0, "total": 1000}}, rates={}, base="USD",
                transfers=[], deposits=[], requests=[], paid_fingerprints=set(), customers=set())
    assert p["available"]["base_minor"] == 100 and p["available"]["wallet"][0]["available_base_minor"] is None
    assert p["missing_rates"] == ["SGD"]


def test_transfer_list_counts_each_transfer_once(monkeypatch):
    # seen on the sandbox: page_num 0 and 1 return the same page, which counted every processing transfer twice
    import asyncio

    import httpx

    from venthora.gateways.airwallex import Airwallex

    monkeypatch.setenv("AWX_CLIENT_ID", "c")
    monkeypatch.setenv("AWX_API_KEY", "k")
    page = {"items": [{"id": f"t{i}", "status": "PROCESSING"} for i in range(100)], "has_more": True}

    def handler(request):
        if request.url.path.endswith("/authentication/login"):
            return httpx.Response(200, json={"token": "x", "expires_at": "2099-01-01T00:00:00+0000"})
        return httpx.Response(200, json=page)
    bank = Airwallex(base_url="https://api-demo.airwallex.com",
                     client=httpx.AsyncClient(base_url="https://api-demo.airwallex.com", transport=httpx.MockTransport(handler)))
    got = asyncio.run(bank.list_transfers(pages=3))
    assert len(got) == 100 and len({t["id"] for t in got}) == 100


def test_endpoint_needs_airwallex():
    from venthora.api import app
    with TestClient(app) as client:
        assert client.get("/v1/treasury/position").status_code == 503
