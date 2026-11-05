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
LINES = [{"employee": "Amira Shah", "payee_id": "payroll-group-us", "currency": "USD", "amount_minor": 420_000, "charge_code": CC},
         {"employee": "Lee Park", "payee_id": "ben-eu-components", "currency": "EUR", "amount_minor": 380_000, "charge_code": CC},
         {"employee": "Ines Duarte", "payee_id": "ben-uk-logistics", "currency": "GBP", "amount_minor": 300_000, "charge_code": CC}]


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



def test_each_line_is_paid_in_its_own_accounts_currency_when_the_policy_allows_it(client):
    bad = LINES[:1] + [LINES[1] | {"currency": "USD"}]  # Ben's account receives EUR
    assert client.post("/v1/doa/registers?role=finance_manager&by=Dana%20Finch", json={"period": _period(), "lines": bad}).status_code == 200
    _approve(client)
    v = {x["employee"]: x for x in _now(client)["lines"]}
    assert v["Lee Park"]["verdict"] == "never" and "receives EUR, not USD" in v["Lee Park"]["reasons"][0]
    assert client.put(f"/v1/doa/policies/personnel?{FCO}", json={"currencies": ["USD"]}).status_code == 200
    client.post("/v1/doa/registers?role=finance_manager&by=Dana%20Finch", json={"period": _period(), "lines": LINES})
    _approve(client)
    v = {x["employee"]: x for x in _now(client)["lines"]}
    assert "EUR is not a payout currency the delegation allows" in v["Lee Park"]["reasons"]
    assert v["Amira Shah"]["verdict"] == "approve"  # first payroll, but the line itself is fine


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
    assert {"IDR", "JPY", "SGD", "AUD", "INR", "NGN"} <= set(d["currencies"]) and d["currencies"][:3] == ["USD", "EUR", "GBP"]
    assert d["engine_currencies"] == d["currencies"]  # the agent pays every one (converted first, or paid through from USD)
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


# -- due payments: what stops them first, the run only when nothing does ---------------------------------------------


def test_no_salary_after_the_contract_end_date_or_for_someone_not_in_people_and_roles(client):
    lee = next(u for u in client.get("/v1/org/users").json() if u["name"] == "Lee Park")
    ended = (dt.date.today().replace(day=1) - dt.timedelta(days=1)).isoformat()  # the last day of last month
    assert client.post(f"/v1/org/users?{FCO}", json=lee | {"contract_end": ended}).status_code == 200
    lines = LINES + [{"employee": "Nobody Known", "payee_id": "payroll-group-us", "currency": "USD", "amount_minor": 100_000, "charge_code": CC}]
    client.post("/v1/doa/registers?role=finance_manager&by=Dana%20Finch", json={"period": _period(), "lines": lines})
    _approve(client)
    v = {x["employee"]: x for x in _now(client)["lines"]}
    assert v["Lee Park"]["verdict"] == "never" and f"the contract ended on {ended}" in v["Lee Park"]["reasons"][0]
    assert v["Nobody Known"]["verdict"] == "never" and "not a person in People and roles" in v["Nobody Known"]["reasons"]
    assert v["Amira Shah"]["verdict"] == "approve"  # still running; the first payroll goes to a person once


# -- payroll prepared by VENTHORA from People and roles: only what changed waits, never for the one who changed it ----
def _paid_last_month(c, name, amount, ccy):
    last = (dt.date.today().replace(day=1) - dt.timedelta(days=1)).strftime("%Y-%m")
    c.app.state.org._put("doa_run", {"_id": f"doa:personnel:{last}:{doa._slug(name)}", "category": "personnel", "period": last, "employee": name,
                                     "amount_minor": amount, "currency": ccy, "status": "PAID", "execution_id": "x", "approved_by": None, "by": "t",
                                     "message": "paid", "at": "2000-01-01T00:00:00+00:00"}, "t")


def _no_account(c, uid):
    org = c.app.state.org
    org._put("user", org.get("user", uid) | {"payee_id": None}, "t")


def _accounts(c):
    org = c.app.state.org
    _no_account(c, "u-lee")  # Lee has no own account in these tests; the others are fixed below
    org._put("user", org.get("user", "u-dana") | {"payee_id": "payroll-group-us"}, "seed")  # USD, active
    org._put("user", org.get("user", "u-amira") | {"payee_id": "ben-uk-logistics"}, "seed")  # GBP, active


def test_payroll_is_prepared_from_people_and_roles_and_only_what_changed_waits(client):
    _accounts(client)
    lines = {x["employee"]: x for x in _now(client)["lines"]}  # no register prepared by anyone
    assert lines["Dana Finch"]["amount_minor"] == 780_000 and lines["Dana Finch"]["source"] == "people"
    assert lines["Dana Finch"]["verdict"] == "approve" and "first salary" in lines["Dana Finch"]["reasons"][0]
    assert lines["Lee Park"]["verdict"] == "never" and "no own bank account" in lines["Lee Park"]["reasons"][0]
    _paid_last_month(client, "Dana Finch", 780_000, "USD")
    assert {x["employee"]: x for x in _now(client)["lines"]}["Dana Finch"]["verdict"] == "auto"  # the same as last month: paid alone
    dana = client.app.state.org.get("user", "u-dana")
    assert client.post(f"/v1/org/users?{FCO}", json=dana | {"salary_minor": 820_000}).status_code == 200
    d = {x["employee"]: x for x in _now(client)["lines"]}["Dana Finch"]
    assert d["verdict"] == "approve" and "USD 7,800.00 to USD 8,200.00" in d["reasons"][0] and d["changed_by"].startswith("Rosa Lindqvist")
    # the one who changed it cannot approve it
    r = client.post("/v1/doa/run", json={"refs": [d["ref"]], "approver": "Rosa Lindqvist", "role": "super_admin"})
    assert r.status_code == 403 and "cannot approve" in r.json()["detail"]
    # HR adjusts one line for this period only: that line waits, the person's record is not changed
    r = client.post("/v1/doa/registers?role=finance_manager&by=Hana%20HR", json={"period": _period(), "lines": [
        {"employee": "Amira Shah", "payee_id": "ben-uk-logistics", "currency": "GBP", "amount_minor": 560_000, "charge_code": "UK-300.OPS.6010"}]})
    assert r.status_code == 200
    a = {x["employee"]: x for x in _now(client)["lines"]}["Amira Shah"]
    assert a["source"] == "adjusted" and a["verdict"] == "approve" and "adjusted for this period by Hana HR" in a["reasons"][0]
    assert client.app.state.org.get("user", "u-amira")["salary_minor"] == 520_000


def test_what_is_ready_is_paid_and_what_waits_is_listed_with_the_fix(client):
    _accounts(client)
    _paid_last_month(client, "Dana Finch", 780_000, "USD")
    assert client.put(f"/v1/doa/policies/personnel?{FCO}", json={"pay_day": "1"}).status_code == 200  # due today
    p = client.get("/v1/doa").json()["pending"]
    assert p["may_run"] and [x["who"] for x in p["ready"]] == ["Dana Finch"]  # one blocked line stops nothing else
    assert {b["where"] for b in p["blocked"]} <= {"#/users", "#/executions", "#/payee-lifecycle", "#/mandate", "#/codes"} and p["blocked"]
    e = client.get("/v1/exceptions").json()
    pay = {x["employee"]: x for x in e["payroll"]}
    assert "Dana Finch" not in pay and pay["Amira Shah"]["inside_mandate"] and not pay["Lee Park"]["inside_mandate"]
    assert e["counts"]["payroll"] == len(e["payroll"]) and e["total"] >= e["counts"]["payroll"]
    assert pay["Lee Park"]["where"] == "#/users"


def test_a_line_outside_the_delegation_goes_to_pending_payment_once(client):
    _accounts(client)
    assert client.put(f"/v1/doa/policies/personnel?{FCO}", json={"currencies": ["USD"]}).status_code == 200  # GBP is outside it
    amira = next(x for x in client.get("/v1/exceptions").json()["payroll"] if x["employee"] == "Amira Shah")
    assert not amira["inside_mandate"] and "GBP is not a payout currency" in " ".join(amira["reasons"])
    r = client.post("/v1/exceptions/payroll/to-review?role=finance_manager&by=Dana%20Finch", json={"ref": amira["ref"]})
    assert r.status_code == 200, r.text
    q = {x["intake_id"]: x for x in client.get("/v1/intake").json()}[r.json()["intake_id"]]
    assert q["amount_minor"] == 520_000 and q["currency"] == "GBP" and q["invoice_no"].startswith("PAY-")
    assert all(x["employee"] != "Amira Shah" for x in client.get("/v1/exceptions").json()["payroll"])  # not twice
    lee = next(x for x in client.get("/v1/exceptions").json()["payroll"] if x["employee"] == "Lee Park")
    assert client.post("/v1/exceptions/payroll/to-review?role=finance_manager&by=Dana%20Finch", json={"ref": lee["ref"]}).status_code == 409  # no account


def test_the_register_page_lists_people_and_the_accounts_a_line_may_pay_into(client):
    g = client.get("/v1/doa/registers").json()
    assert g["registers"] == [] and any(u["user_id"] == "u-amira" for u in g["people"])
    assert {"id": "payroll-group-us", "name": "Northwind Payroll Services Inc", "currency": "USD"}.items() <= next(a for a in g["accounts"] if a["id"] == "payroll-group-us").items()
    assert client.post("/v1/doa/registers?role=finance_manager&by=Dana%20Finch",
                       json={"period": _period(), "lines": [LINES[0] | {"payee_id": None}]}).status_code == 422  # no account, no line


def test_a_changed_line_is_approved_once_and_then_ready_for_the_pay_day(client):
    _accounts(client)
    assert client.put(f"/v1/doa/policies/personnel?{FCO}", json={"pay_day": "1"}).status_code == 200
    dana = next(x for x in client.get("/v1/exceptions").json()["payroll"] if x["employee"] == "Dana Finch")
    assert dana["inside_mandate"] and dana["approved"] is None
    assert client.post("/v1/exceptions/payroll/approve?role=requester&by=Ola%20Reed", json={"ref": dana["ref"]}).status_code == 403
    r = client.post(f"/v1/exceptions/payroll/approve?{FCO}", json={"ref": dana["ref"]})
    assert r.status_code == 200 and r.json()["amount_minor"] == 780_000
    e = client.get("/v1/exceptions").json()
    assert next(x for x in e["payroll"] if x["employee"] == "Dana Finch")["approved"].startswith("Rosa Lindqvist")
    assert "Dana Finch" in [x["who"] for x in client.get("/v1/doa").json()["pending"]["ready"]]
    # the amount changes again: the approval no longer holds
    d = client.app.state.org.get("user", "u-dana")
    client.post("/v1/org/users?role=finance_manager&by=Dana%20Finch", json=d | {"salary_minor": 790_000})
    assert next(x for x in client.get("/v1/exceptions").json()["payroll"] if x["employee"] == "Dana Finch")["approved"] is None
    # ... and the one who changed it now cannot approve it
    ref = next(x for x in client.get("/v1/exceptions").json()["payroll"] if x["employee"] == "Dana Finch")["ref"]
    assert client.post("/v1/exceptions/payroll/approve?role=finance_manager&by=Dana%20Finch", json={"ref": ref}).status_code == 403
    # an adjustment taken away again: the period is paid from People and roles alone
    client.post("/v1/doa/registers?role=finance_manager&by=Hana%20HR", json={"period": _period(), "lines": [
        {"employee": "Amira Shah", "payee_id": "ben-uk-logistics", "currency": "GBP", "amount_minor": 1_000, "charge_code": "UK-300.OPS.6010"}]})
    assert client.post("/v1/doa/registers?role=finance_manager&by=Hana%20HR", json={"period": _period(), "lines": []}).status_code == 200
    assert {x["employee"]: x for x in _now(client)["lines"]}["Amira Shah"]["source"] == "people"


def test_an_approval_is_bound_to_the_account_it_was_given_for(client):
    _accounts(client)
    dana = next(x for x in client.get("/v1/exceptions").json()["payroll"] if x["employee"] == "Dana Finch")
    assert client.post(f"/v1/exceptions/payroll/approve?{FCO}", json={"ref": dana["ref"]}).status_code == 200
    org = client.app.state.org
    org._put("user", org.get("user", "u-dana") | {"payee_id": "ben-saas-vendor"}, "t")  # another account, same amount
    after = next(x for x in client.get("/v1/exceptions").json()["payroll"] if x["employee"] == "Dana Finch")
    assert after["approved"] is None  # approved for one account, not for another

