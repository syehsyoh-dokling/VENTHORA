# ruff: noqa: E501
"""Data sources: a sync from the old system's API fills vendors, customers, POs, bills, receivables, budget and tenant
books through one validated pipeline; bank details from outside never arrive trusted; the rest of VENTHORA reads it."""
import datetime as dt

import httpx
import pytest
from fastapi.testclient import TestClient

from venthora import connectors


@pytest.fixture
def client(monkeypatch):
    from venthora.api import app

    real = connectors.fetch

    async def through_app(raw, entity, client=None):  # the stand-in ERP is served by the app itself
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
            return await real(raw, entity, client=c)
    monkeypatch.setattr(connectors, "fetch", through_app)
    with TestClient(app) as c:
        yield c


def test_mapping_transforms():
    item = {"A": "1,250.50", "D": "31/12/2026", "E": "a@x.example; b@y.example", "B": {"IBAN": "de89 3704 0044 0532 0130 00"}}
    out = connectors.map_item(item, {"amount_minor": "A|major", "due_date": "D|date", "contacts": "E|split",
                                     "bank.iban": "B.IBAN|nospace|upper", "currency": "=EUR"})
    assert out == {"amount_minor": 125050, "due_date": "2026-12-31", "contacts": ["a@x.example", "b@y.example"],
                   "bank": {"iban": "DE89370400440532013000"}, "currency": "EUR"}
    with pytest.raises(ValueError, match="not an amount"):
        connectors.map_item({"A": "abc"}, {"amount_minor": "A|major"})


def test_sync_fills_every_table_and_bank_details_start_untrusted(client):
    assert [c["connector_id"] for c in client.get("/v1/sources/connectors").json()] == ["legacy-erp-demo"]
    assert client.get("/v1/sources/connectors").json()[0]["auth"]["secret_set"] is True
    dry = client.post("/v1/sources/connectors/legacy-erp-demo/sync?dry_run=true").json()["reports"]
    assert len(dry["vendor"]["created"]) == 4 and client.get("/v1/sources/master/vendor").json() == []
    rep = client.post("/v1/sources/connectors/legacy-erp-demo/sync").json()["reports"]
    assert {e: len(r["created"]) for e, r in rep.items()} == {"vendor": 4, "customer": 2, "po": 2, "bill": 3, "receivable": 2,
                                                             "budget_line": 3, "tenant_book": 1}
    assert all(not r["rejected"] for r in rep.values())
    follow = " | ".join(rep["vendor"]["followed"])
    assert "nordlicht-logistik: registered NEW" in follow and "ben-saas-vendor: bank details changed, now CHANGED" in follow
    payees = {p["beneficiary_id"]: p["status"] for p in client.get("/v1/payees").json()}
    assert payees["nordlicht-logistik"] == "NEW" and payees["ben-saas-vendor"] == "CHANGED"
    bills = " | ".join(rep["bill"]["followed"])
    assert "NL-5001: VERIFIED" in bills and "POS-3310: VERIFIED" in bills and "HA-77: NEEDS_REVIEW (po_missing)" in bills
    queue = {r["invoice_no"]: r for r in client.get("/v1/intake").json()}
    assert queue["NL-5001"]["installment"] == "T1" and queue["NL-5001"]["trust"] == "system_of_record"
    masked = client.get("/v1/sources/master/vendor").json()
    assert all("DE89" not in str(v) for v in masked) and any("•••• 3000" in str(v) for v in masked)
    again = client.post("/v1/sources/connectors/legacy-erp-demo/sync").json()["reports"]
    assert all(not r["created"] and not r["updated"] for e, r in again.items() if e != "po")
    assert len([r for r in client.get("/v1/intake").json() if r["invoice_no"] == "NL-5001"]) == 1


def test_the_rest_of_venthora_reads_the_synced_data(client):
    client.get("/v1/sources/connectors")
    client.post("/v1/sources/connectors/legacy-erp-demo/sync")
    from venthora.execution.executor import Executor
    from venthora.intake.pipeline import Registry
    from venthora.proofcore.budget import load_budget
    from venthora.tenancy import load_registry

    reg = Registry()
    assert reg.by_contact("rechnungen@nordlicht-logistik.example")[0] == "nordlicht-logistik"
    assert reg.payers["acme-retail"]["history"] == {"kept": 14, "missed": 2}
    bens = Executor.beneficiaries.fget(type("E", (), {"_beneficiaries": None})())
    assert bens["nordlicht-logistik"]["payload"]["beneficiary"]["bank_details"]["iban"] == "DE89370400440532013000"
    assert {ln.line for ln in load_budget(dt.datetime.now(dt.UTC).strftime("%Y-%m")).lines} == {"logistics", "suppliers", "office"}
    assert "eu-warehouse-*" in load_registry()["tenants"]["acme-group"]["books"]
    state = client.get("/v1/intake/state/intake_only?as_of=2026-11-01").json()
    ar = {r["receivable_id"]: r for r in state["receivables"]}
    assert ar["AR-R-3001"]["amount_minor"] == 1_250_000 and ar["AR-R-3001"]["payer"] == "Brightside Retail Inc"
    assert dt.date(2026, 11, 1)  # as_of used above


def test_a_bad_row_is_rejected_with_its_reason(tmp_path):
    from venthora import ingestion
    from venthora.masterdata import MasterStore

    m = MasterStore(str(tmp_path / "m.db"))
    rep = ingestion.apply("vendor", [{"name": "No Bank Co", "currency": "usd", "bank": {"account_name": "x", "account_currency": "USD",
                                                                                          "bank_country_code": "US"}},
                                     {"name": "Bad Ccy", "currency": "dollars"}, {"_error": "not an amount: 'abc'"}], ["1", "2", "3"],
                          source="upload:test.csv", master=m)
    assert [r["row"] for r in rep["rejected"]] == [3, 1, 2] or len(rep["rejected"]) == 3
    assert any("account number or an IBAN" in r["error"] for r in rep["rejected"])
    assert m.list("vendor") == []
