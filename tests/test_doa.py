# ruff: noqa: E501
"""Delegation of Authority: a general policy per category (no person, vendor, account or amount); the payroll
register carries the individuals and each line is judged against the policy; the payout currency is the employee's
own account's; a run is paid on the fake sandbox."""
import asyncio
import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient
from test_execution import harness, sandbox_credentials  # noqa: F401 (the fixture)

from venthora import autonomy, doa
from venthora.config import ROOT
from venthora.domain.models import TreasuryState
from venthora.execution.models import ExecutionRequest
from venthora.testing.fake_airwallex import FakeAirwallex

FCO = "role=super_admin&by=Rosa%20Lindqvist"
CC = "US-200.OPS.6010"
# the demo registry has no employees as payees; registered accounts in each currency stand in for theirs
LINES = [{"employee": "Ana Silva", "payee_id": "payroll-group-us", "currency": "USD", "amount_minor": 420_000, "charge_code": CC},
         {"employee": "Ben Ortiz", "payee_id": "ben-eu-components", "currency": "EUR", "amount_minor": 380_000, "charge_code": CC},
         {"employee": "Cleo Hart", "payee_id": "ben-uk-logistics", "currency": "GBP", "amount_minor": 300_000, "charge_code": CC}]


@pytest.fixture
def client():
    from venthora.api import app
    with TestClient(app) as c:
        yield c


def _now(c, key="personnel"):
    return next(x["now"] for x in c.get("/v1/doa").json()["internal"] if x["key"] == key)


def _period():
    return dt.date.today().strftime("%Y-%m")


def _approve(c):
    assert c.post(f"/v1/doa/registers/{_period()}/approve?step=hr&role=hq_admin&by=Hana%20HR").status_code == 200
    assert c.post(f"/v1/doa/registers/{_period()}/approve?step=fco&{FCO}").status_code == 200


def test_the_policy_names_no_individual(client):
    d = client.get("/v1/doa?role=super_admin").json()
    assert [c["key"] for c in d["internal"]] == ["personnel", "fringe", "consultants", "travel", "office", "cash_advance", "other_direct"]
    assert [c["key"] for c in d["external"]] == ["vendors", "specialists"]
    for c in d["internal"]:
        assert not {"payee_id", "amount_minor", "charge_code", "variance_bp", "limit_payment_minor", "limit_run_minor"} & set(c["policy"])
        assert c["auto"] and c["approve"] and c["never"] and c["source"]
    assert d["authority"]["you_may_change"] is True and d["autopilot"] is False and "payees" not in d
    assert _now(client, "fringe")["summary"] == "not_connected"


def test_only_the_holder_of_the_delegation_sets_a_policy(client):
    assert client.put("/v1/doa/policies/personnel?role=finance_manager&by=Dana", json={"pay_day": "25"}).status_code == 403
    r = client.put(f"/v1/doa/policies/personnel?{FCO}", json={"pay_day": "25", "currencies": ["GBP", "USD"]})
    assert r.status_code == 200 and r.json()["pay_day"] == "25" and r.json()["currencies"] == ["USD", "GBP"]
    assert client.put(f"/v1/doa/policies/personnel?{FCO}", json={"currencies": ["XYZ"]}).status_code == 422
    assert client.put(f"/v1/doa/policies/personnel?{FCO}", json={"currencies": []}).status_code == 422
    assert client.put(f"/v1/doa/policies/personnel?{FCO}", json={"payee_id": "x"}).json().get("payee_id") is None  # not a policy field


def test_fringe_benefits_are_a_catalog_each_switched_on_with_its_frequency(client):
    d = client.get("/v1/doa").json()
    fringe = next(c for c in d["internal"] if c["key"] == "fringe")
    cat = d["benefit_catalog"]
    assert set(fringe["policy"]["benefits"]) == {b["key"] for b in cat["benefits"]}
    assert set(cat["groups"]) == {"short_term", "post_employment", "long_term", "termination", "international"}  # IAS 19 classes
    assert fringe["policy"]["benefits"]["health"]["enabled"] is True and fringe["policy"]["benefits"]["life"]["enabled"] is False
    r = client.put(f"/v1/doa/policies/fringe?{FCO}", json={"benefits": {"life": {"enabled": True, "frequency": "monthly"}, "health": {"enabled": False}}})
    assert r.status_code == 200
    b = r.json()["benefits"]
    assert b["life"] == {"enabled": True, "frequency": "monthly"} and b["health"]["enabled"] is False and b["health"]["frequency"] == "monthly"
    assert b["pension_dc"]["enabled"] is True  # untouched benefits keep their setting
    assert client.put(f"/v1/doa/policies/fringe?{FCO}", json={"benefits": {"life": {"frequency": "weekly"}}}).status_code == 422
    assert client.put(f"/v1/doa/policies/fringe?{FCO}", json={"benefits": {"yacht": {"enabled": True}}}).status_code == 422
    assert "benefits" not in next(c for c in d["internal"] if c["key"] == "personnel")["policy"]


def test_payroll_is_never_paid_without_both_approvals_and_the_first_one_needs_a_person(client):
    assert _now(client)["summary"] == "no_document"
    r = client.post("/v1/doa/registers?role=finance_manager&by=Dana%20Finch", json={"period": _period(), "lines": LINES})
    assert r.status_code == 200 and r.json()["totals"] == {"USD": 420_000, "EUR": 380_000, "GBP": 300_000}
    assert _now(client)["summary"] == "never"
    assert client.post(f"/v1/doa/registers/{_period()}/approve?step=hr&role=finance_manager&by=Dana%20Finch").status_code == 403  # the preparer
    assert client.post(f"/v1/doa/registers/{_period()}/approve?step=fco&{FCO}").status_code == 422  # HR first
    _approve(client)
    lines = _now(client)["lines"]
    assert [x["verdict"] for x in lines] == ["approve"] * 3 and "first payroll" in lines[0]["reasons"][0]


def test_each_line_is_paid_in_its_own_accounts_currency_when_the_policy_allows_it(client):
    bad = LINES[:1] + [LINES[1] | {"currency": "USD"}]  # Ben's account receives EUR
    assert client.post("/v1/doa/registers?role=finance_manager&by=Dana%20Finch", json={"period": _period(), "lines": bad}).status_code == 200
    _approve(client)
    v = {x["employee"]: x for x in _now(client)["lines"]}
    assert v["Ben Ortiz"]["verdict"] == "never" and "receives EUR, not USD" in v["Ben Ortiz"]["reasons"][0]
    assert client.put(f"/v1/doa/policies/personnel?{FCO}", json={"currencies": ["USD"]}).status_code == 200
    client.post("/v1/doa/registers?role=finance_manager&by=Dana%20Finch", json={"period": _period(), "lines": LINES})
    _approve(client)
    v = {x["employee"]: x for x in _now(client)["lines"]}
    assert "EUR is not a payout currency the delegation allows" in v["Ben Ortiz"]["reasons"]
    assert v["Ana Silva"]["verdict"] == "approve"  # first payroll, but the line itself is fine


def test_due_lines_are_paid_on_the_agents_own_authority_in_each_currency(tmp_path):
    """The book a run builds passes the treasury proof and executes on the fake sandbox without an approval:
    one payout per line, in the line's currency, bought from base."""
    from venthora.intake.pipeline import Registry

    payees = Registry().payees
    today = dt.date.today()
    row = {"key": "personnel", "period": _period(), "pay_date": today.isoformat()}
    lines = [ln | {"line": i, "ref": f"doa:personnel:{_period()}:{i}"} for i, ln in enumerate(LINES)]
    base = TreasuryState.model_validate(json.loads((ROOT / "data" / "scenarios" / "demo_live.json").read_text(encoding="utf-8"))["state"])
    state = doa.book(base, row, lines, payees, today)
    fake = FakeAirwallex(balances={k: str(v / 100) for k, v in state.balances.items()})

    async def go():
        agent, executor = harness(fake, tmp_path)
        try:
            s = await autonomy.split(agent, state)
            r = await executor.execute(ExecutionRequest(state=s.auto_state, plan_hash=s.auto_plan.plan_hash))
            return s, r
        finally:
            await agent.close()
    s, r = asyncio.run(go())
    assert r.status == "COMPLETED", r.message
    sent = {t["reference"].split()[0]: t["transfer_currency"] for t in fake.transfers.values()}
    paid_ids = {d.obligation_id for d in s.auto_plan.obligations if d.action in autonomy.PAYING}
    assert sent and set(sent) == paid_ids
    assert {state.obligations[i].currency for i in range(3) if state.obligations[i].obligation_id in paid_ids} <= {"USD", "EUR", "GBP"}
    assert all(sent[o.obligation_id] == o.currency for o in state.obligations if o.obligation_id in sent)


# -- experts and consultants: who may send the request, what it must carry, the contract value ---------------------
def _claim(client, monkeypatch, n, sender, name, body, pdf=None):
    from venthora.testing.review_cases import postmark

    monkeypatch.setenv("VENTHORA_INBOUND_EMAIL_SECRET", "s3cret-for-tests")
    case = {"id": f"consult-{n}", "sender": sender, "name": name, "subject": f"Timesheet OK-77{n}", "body": body, "pdf": pdf}
    r = client.post("/webhooks/email", json=postmark(case, f"77{n}"), auth=("postmark", "s3cret-for-tests"))
    return next(x for x in client.get("/v1/intake").json() if x["intake_id"] == r.json()["intake_id"])


MIA = "mia.okafor.design@yahoo.com"
DUE = (dt.date.today() + dt.timedelta(days=7)).strftime("%d %b %Y")


def _body(n, amount="800.00"):
    return f"Invoice OK-77{n}, PO-2026-0104: 16 hours of design support, total USD {amount}, due {DUE}."


CLOUD = "billing@cloudloom.example"
OFFICE_BILL = ("Invoice CL-OPS-{n}. Purchase request PR-2026-01{n} for internet and software, November. Amount due: USD 450.00, due " + DUE + ". "
               "Cloudloom Software LLC, 500 Howard Street, San Francisco, United States, phone +1 415 555 0100, email billing@cloudloom.example. "
               "Pay to Bank of America, account number 483101090002, country United States.")


def test_an_office_bill_without_a_po_is_paid_in_real_time_when_it_carries_everything_ticked(client, monkeypatch):
    got = _claim(client, monkeypatch, 11, CLOUD, "Cloudloom Billing", OFFICE_BILL.format(n=11))
    assert got["kind"] == "supplier_invoice" and "po_missing" not in got["flags"] and got["status"] == "VERIFIED", got["reasons"]
    assert any("everything the delegation requires is on it" in r for r in got["reasons"])
    # the same bill without the bank details is not paid on the agent's own authority
    got = _claim(client, monkeypatch, 12, CLOUD, "Cloudloom Billing", OFFICE_BILL.format(n=12).split(" Pay to")[0])
    assert got["status"] == "NEEDS_REVIEW" and "po_missing" in got["flags"]
    assert any("missing the vendor's bank account number" in r for r in got["reasons"])
    # untick bank details: now the bill without them is paid
    assert client.put(f"/v1/doa/policies/office?{FCO}", json={"requirements": ["pr_number", "vendor_contact", "amount"]}).status_code == 200
    got = _claim(client, monkeypatch, 13, CLOUD, "Cloudloom Billing", OFFICE_BILL.format(n=13).split(" Pay to")[0])
    assert got["status"] == "VERIFIED", got["reasons"]


def test_an_office_bill_from_a_sender_the_delegation_does_not_tick_waits(client, monkeypatch):
    assert client.put(f"/v1/doa/policies/office?{FCO}", json={"senders": ["procurement"], "procurement_people": ["u-dana"]}).status_code == 200
    got = _claim(client, monkeypatch, 14, CLOUD, "Cloudloom Billing", OFFICE_BILL.format(n=14))
    assert got["status"] == "NEEDS_REVIEW" and any("not one the delegation accepts" in r for r in got["reasons"])
    got = _claim(client, monkeypatch, 15, "dana.finch@acme.example", "Dana Finch", OFFICE_BILL.format(n=15))
    assert got["kind"] == "supplier_invoice" and got["counterparty_id"] == "ben-saas-vendor" and got["status"] == "VERIFIED", got["reasons"]


def test_a_consultants_request_without_every_required_document_is_rejected(client, monkeypatch):
    assert client.put(f"/v1/doa/policies/consultants?{FCO}", json={"documents": ["invoice", "timesheet"]}).status_code == 200
    got = _claim(client, monkeypatch, 1, MIA, "Mia Okafor", _body(1))
    assert got["kind"] == "contractor_claim" and got["status"] == "REJECTED"
    assert any("incomplete" in r and "Timesheet" in r for r in got["reasons"])
    got = _claim(client, monkeypatch, 2, MIA, "Mia Okafor", _body(2), pdf=["Timesheet November", "Mia Okafor", "16 hours worked", "Invoice OK-772"])
    assert got["status"] != "REJECTED", got["reasons"]


def test_only_the_senders_the_delegation_names_are_accepted(client, monkeypatch):
    r = client.put(f"/v1/doa/policies/consultants?{FCO}", json={"senders": ["procurement"], "procurement_people": []})
    assert r.status_code == 422  # a team needs at least one person
    assert client.put(f"/v1/doa/policies/consultants?{FCO}", json={"senders": ["procurement"], "procurement_people": ["u-dana"]}).status_code == 200
    got = _claim(client, monkeypatch, 3, MIA, "Mia Okafor", _body(3))
    assert got["status"] == "REJECTED" and any("only on an email from procurement team" in r for r in got["reasons"])
    got = _claim(client, monkeypatch, 4, "dana.finch@acme.example", "Dana Finch", "Please pay Mia Okafor. " + _body(4))
    assert got["kind"] == "contractor_claim" and got["counterparty_id"] == "ben-contractor-okafor" and got["status"] == "VERIFIED", got["reasons"]
    assert any("forwarded by Dana Finch" in r for r in got["reasons"])


def test_a_claim_above_the_contract_value_is_checked_unless_the_delegation_ignores_it(client, monkeypatch):
    got = _claim(client, monkeypatch, 5, MIA, "Mia Okafor", _body(5, "12,000.00"))
    assert "po_exceeded" in got["flags"] and got["status"] == "NEEDS_REVIEW"
    assert client.put(f"/v1/doa/policies/consultants?{FCO}", json={"ignore_exceed_claim": True}).status_code == 200
    got = _claim(client, monkeypatch, 6, MIA, "Mia Okafor", _body(6, "12,000.00"))
    assert "po_exceeded" not in got["flags"] and any("not checked against the contract value" in r for r in got["reasons"])


def test_every_airwallex_payout_currency_can_be_allowed_and_the_engine_ones_are_named(client):
    d = client.get("/v1/doa").json()
    assert {"IDR", "JPY", "SGD", "AUD"} <= set(d["currencies"]) and d["engine_currencies"] == ["USD", "EUR", "GBP"]
    assert client.put(f"/v1/doa/policies/personnel?{FCO}", json={"currencies": ["USD", "IDR"]}).json()["currencies"] == ["USD", "IDR"]


# -- travel and transport: places, rates per destination city, the checklist and the channel ------------------------
RATES_CSV = (b"Destination country,Destination city,Currency,Lodging,Per diem\n"
             b"Indonesia,Jakarta,USD,165.00,75.00\nIndonesia,Surabaya,USD,120.00,60.00\nKenya,Nairobi,USD,210.00,90.00\n")


def _travel(c):
    return next(x for x in c.get("/v1/doa").json()["internal"] if x["key"] == "travel")


def test_travel_asks_for_the_rates_until_they_are_on_file_and_then_shows_them_per_city(client):
    assert _travel(client)["now"]["summary"] == "no_document"
    r = client.post("/v1/sources/uploads?filename=travel-rates.csv&entity=travel_rate&role=finance_manager", content=RATES_CSV)
    assert r.status_code == 200, r.text
    up = r.json()
    r = client.post(f"/v1/sources/uploads/{up['upload_id']}/commit?role=finance_manager&by=Dana%20Finch", json={"mapping": up["mapping"]})
    assert r.status_code == 200, r.text
    places = client.get("/v1/doa").json()["travel_catalog"]["places"]
    assert [x["city"] for x in places["Indonesia"]] == ["Jakarta", "Surabaya"] and places["Kenya"][0]["lodging_minor"] == 21_000
    assert _travel(client)["now"]["summary"] == "via_intake"


def test_travel_places_must_be_on_file_and_the_checklist_and_channel_are_kept(client):
    for city, ccy, lodging, pd in (("Jakarta", "USD", 16_500, 7_500), ("Nairobi", "USD", 21_000, 9_000)):
        country = "Indonesia" if city == "Jakarta" else "Kenya"
        assert client.post("/v1/org/travel-rates?role=finance_manager&by=Dana%20Finch",
                           json={"country": country, "city": city, "currency": ccy, "lodging_minor": lodging, "per_diem_minor": pd}).status_code == 200
    ok = {"dest_countries": ["Indonesia"], "dest_cities": ["Indonesia/Jakarta"], "documents": ["travel_request", "receipts", "travel_report"],
          "channel": "dashboard"}
    r = client.put(f"/v1/doa/policies/travel?{FCO}", json=ok)
    assert r.status_code == 200, r.text
    assert r.json()["dest_cities"] == ["Indonesia/Jakarta"] and r.json()["channel"] == "dashboard" and len(r.json()["documents"]) == 3
    assert client.put(f"/v1/doa/policies/travel?{FCO}", json={"dest_countries": ["Peru"]}).status_code == 422  # no rate on file
    assert client.put(f"/v1/doa/policies/travel?{FCO}", json={"dest_countries": ["Indonesia"], "dest_cities": ["Kenya/Nairobi"]}).status_code == 422
    assert client.put(f"/v1/doa/policies/travel?{FCO}", json={"channel": "email", "senders": ["finance"], "finance_people": []}).status_code == 422
    assert client.put(f"/v1/doa/policies/travel?{FCO}", json={"channel": "email", "senders": ["finance"], "finance_people": ["u-omar"]}).status_code == 200
