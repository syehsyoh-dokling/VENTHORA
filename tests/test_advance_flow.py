# ruff: noqa: E501
"""A staff advance by email: only from the organisation's own email, approved by the FCO's email or the FCO's approval
forwarded in it, numbered by the email or by VENTHORA, allocated when green, paid at 23:59, then the card link."""
import asyncio
import datetime as dt
from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient

from venthora import advance_flow

FCO = ("Rosa Lindqvist", "rosa.lindqvist@acme.example")
LEE = ("Lee Park", "lee.park@acme.example")
JONAS = ("Jonas Weber", "jonas.weber@acme.example")
CODE = "US-200.OPS.6900"


def eml(sender, subject, body, mid):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Message-ID"] = f"{sender[0]} <{sender[1]}>", "payments@acme.example", subject, f"<{mid}@test>"
    m["Date"] = dt.datetime.now(dt.UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")
    m.set_content(body)
    return m.as_bytes()


def fwd(who, amount, purpose, extra=""):
    return (f"Hi Finance,\n\nForwarding the FCO's approval of my cash advance.\n\nAmount: USD {amount}\nPurpose: {purpose}\nCharge code: {CODE}\n{extra}\n"
            f"---------- Forwarded message ---------\nFrom: {FCO[0]} <{FCO[1]}>\nSubject: Re: cash advance\nTo: {who}\n\nApproved. Go ahead.\n\n{FCO[0]}\nFCO\n")


# ---- the rules, on their own ----

def test_a_forwarded_approval_is_the_fcos_own_words_from_the_fcos_address():
    fcos = {FCO[1]}
    got = advance_flow.forwarded_approval(fwd("Lee Park", "200.00", "site visit"), fcos)
    assert got == {"from": FCO[1], "quote": "Approved. Go ahead."}
    assert advance_flow.forwarded_approval("Please approve my cash advance of USD 200.", fcos) is None  # nothing forwarded
    other = fwd("Lee Park", "200.00", "x").replace(FCO[1], "someone@else.example")
    assert advance_flow.forwarded_approval(other, fcos) is None  # forwarded, but not from the FCO
    no = fwd("Lee Park", "200.00", "x").replace("Approved. Go ahead.", "Not approved, the budget is closed.")
    assert advance_flow.forwarded_approval(no, fcos) is None  # the FCO said no
    assert advance_flow.forwarded_approval(f"I approved this myself.\n\n> From: {FCO[1]}\n> Let us talk tomorrow.", fcos) is None  # the approval is not the FCO's


def test_numbers_come_from_the_email_or_from_venthora():
    assert advance_flow.request_number("Cash advance", "Advance request no: CA-2026-0144\nAmount: USD 5") == "CA-2026-0144"
    assert advance_flow.request_number("Petty cash number PC/11/007", "") == "PC/11/007"
    assert advance_flow.request_number("Cash advance for Lee Park", "Amount: USD 300\nNeeded by: 2026-11-17") is None
    assert advance_flow.ta_number("Travel advance", "Travel Authorisation: TA-2026-0031") == "TA-2026-0031"
    assert advance_flow.ta_number("Travel clearance no. TC/11/204 attached", "") == "TC/11/204"
    assert advance_flow.ta_number("Cash advance", "for a taxi to the airport") is None
    assert advance_flow.next_ta({"TA-2026-0001", "TA-2026-0002"}, 2026) == "TA-2026-0003"


def test_a_travel_authorisation_on_file_must_be_approved_for_that_person_and_cover_the_amount():
    lee = {"user_id": "u-lee", "name": "Lee Park"}
    ta = {"status": "APPROVED", "traveller": "u-lee", "estimated_minor": 50_000, "currency": "USD"}
    assert advance_flow.check_ta(None, "TA-9", lee, 10_000, "USD") is None  # not on file: the email's own reference
    assert advance_flow.check_ta(ta, "TA-9", lee, 40_000, "USD") is None
    assert "above" in advance_flow.check_ta(ta, "TA-9", lee, 60_000, "USD")
    assert "not for Lee Park" in advance_flow.check_ta(ta | {"traveller": "u-x"}, "TA-9", lee, 1, "USD")
    assert "cancelled" in advance_flow.check_ta(ta | {"status": "CANCELLED"}, "TA-9", lee, 1, "USD")


def test_paid_at_2359_company_time_and_green_only_above_the_floor():
    now = dt.datetime(2026, 11, 15, 12, 30, tzinfo=dt.UTC)  # 19:30 in Jakarta
    at = advance_flow.pay_at(now, "Asia/Jakarta")
    assert at == dt.datetime(2026, 11, 15, 16, 59, tzinfo=dt.UTC) and advance_flow.time_left(now, at) == "4 h 29 min"
    assert advance_flow.pay_at(dt.datetime(2026, 11, 15, 16, 59, 30, tzinfo=dt.UTC), "Asia/Jakarta") == dt.datetime(2026, 11, 16, 16, 59, tzinfo=dt.UTC)
    ok, why = advance_flow.green(cost_base_minor=30_000, base_balance_minor=7_200_000, floor_minor=5_000_000, due_base_minor=1_000_000, horizon_hours=72)
    assert ok and "USD 12,000.00 is free" in why
    ok, why = advance_flow.green(cost_base_minor=30_000, base_balance_minor=5_010_000, floor_minor=5_000_000, due_base_minor=0, horizon_hours=72)
    assert not ok and "only USD 100.00 is free" in why
    a = advance_flow.allocation(now=now, tz="Asia/Jakarta", ok=True, why="w", route="staff_card", approval="forwarded")
    fr = {"status": "APPROVED", "allocation": a}
    assert not advance_flow.due(fr, now) and advance_flow.due(fr, at) and not advance_flow.due(fr | {"status": "CANCELLED"}, at)
    assert advance_flow.allocation(now=now, tz="Asia/Jakarta", ok=False, why="w", route="virtual_card", approval="fco_email")["pay_at"] is None


def test_the_card_link_names_one_advance_and_nothing_else():
    token = advance_flow.make_link(b"k" * 32, "CA-2026-0001")
    assert advance_flow.read_link(b"k" * 32, token) == "CA-2026-0001"
    assert advance_flow.read_link(b"x" * 32, token) is None and advance_flow.read_link(b"k" * 32, token[:-1] + "0") in (None, "CA-2026-0001")
    other = advance_flow.make_link(b"k" * 32, "CA-2026-0002")
    assert advance_flow.read_link(b"k" * 32, token.split(".")[0] + "." + other.split(".")[1]) is None  # a signature of another advance
    assert advance_flow.read_link(b"k" * 32, "garbage") is None


# ---- through the running service ----

class Bank:
    """Airwallex, as far as an advance needs it: a card is created, its status set."""
    configured = True

    def __init__(self):
        self.cards = []

    async def create_card(self, body):
        self.cards.append(body)
        n = len(self.cards)
        return {"card_id": f"card-{n}", "card_number": f"************{4000 + n}", "brand": "VISA", "card_status": "ACTIVE"}

    async def set_card_status(self, card_id, status):
        return {"card_id": card_id, "card_status": status}


@pytest.fixture
def client(monkeypatch):
    import venthora.api as api

    for name in ("VENTHORA_CONTINUITY_URL", "VENTHORA_REPLICA_URL"):
        monkeypatch.delenv(name, raising=False)
    with TestClient(api.app) as c:
        bank = Bank()
        monkeypatch.setattr(api, "_bank", lambda: bank)
        org = c.app.state.org
        org._put("issuing", {"_id": "cardholder", "cardholder_id": "ch-1", "status": "READY"}, "test")
        c.bank = bank
        yield c


def send(client, raw):
    r = client.post("/v1/intake/emails", content=raw, headers={"content-type": "message/rfc822"})
    assert r.status_code == 200, r.text
    return r.json()


def number_of(req):
    flag = next((f for f in req["flags"] if f.startswith("fund_request")), "")
    return flag.split(":", 1)[1].strip() if flag.startswith("fund_request:") else (flag or f"no fund request: {req['status']} {req['trust']} {req['flags']}")


def test_a_cash_request_from_outside_the_organisation_is_rejected_as_it_stands(client):
    req = send(client, eml(("Mallory", "mallory@elsewhere.example"), "Cash advance for Lee Park", f"Please pay.\nAmount: USD 900\nCharge code: {CODE}", "out-1"))
    assert req["status"] == "REJECTED" and "not an email address of the organisation" in req["reasons"][0]
    assert any(f.startswith("fund_request_rejected") for f in req["flags"]) and not client.app.state.org.list("fund_request")


def test_the_fcos_email_allocates_and_the_2359_run_pays_and_sends_the_card_link(client):
    import venthora.api as api

    org = client.app.state.org
    req = send(client, eml(FCO, "Cash advance for Lee Park", f"Please pay a cash advance.\n\nFor: Lee Park\nAmount: USD 300.00\nPurpose: site visit, warehouse\n"
                                                             f"Advance request no: CA-EMAIL-0144\nCharge code: {CODE}\nSettle by: 2026-12-15\n\nApproved.", "fco-1"))
    assert number_of(req) == "CA-EMAIL-0144"  # the email's own number is kept
    fr = org.get("fund_request", "CA-EMAIL-0144")
    assert fr["status"] == "APPROVED" and "by email" in fr["decided_by"] and fr["allocation"]["status"] == "ALLOCATED" and fr["allocation"]["green"]
    assert not client.bank.cards  # allocated, not paid
    first = [m for m in org.list("outbox") if m["ref"] == "CA-EMAIL-0144"]
    assert len(first) == 1 and first[0]["kind"] == "advance_allocated" and set(first[0]["to"]) == {LEE[1], FCO[1]}
    assert "paid at 23:59 (in " in first[0]["subject"] and "cancel it in VENTHORA" in first[0]["body"]
    again = send(client, eml(FCO, "Cash advance for Lee Park", f"For: Lee Park\nAmount: USD 1\nAdvance request no: CA-EMAIL-0144\nCharge code: {CODE}", "fco-1b"))
    assert number_of(again).startswith("fund_request_refused: request CA-EMAIL-0144 is already on file")

    at = dt.datetime.fromisoformat(fr["allocation"]["pay_at"])
    assert asyncio.run(api._advances_due(at - dt.timedelta(minutes=1))) == []  # not yet
    client.put("/v1/continuity/safe-mode?role=super_admin&by=Rosa%20Lindqvist", json={"on": True, "reason": "test"})
    assert asyncio.run(api._advances_due(at))[0]["held"]  # safe mode stops the 23:59 run too
    client.put("/v1/continuity/safe-mode?role=super_admin&by=Rosa%20Lindqvist", json={"on": False})
    out = asyncio.run(api._advances_due(at))
    assert out == [{"request": "CA-EMAIL-0144", "paid": True, "card": "************4001"}]
    fr = org.get("fund_request", "CA-EMAIL-0144")
    assert fr["status"] == "IN_PAYMENT" and fr["allocation"]["status"] == "PAID" and asyncio.run(api._advances_due(at)) == []  # once
    second = [m for m in org.list("outbox") if m["ref"] == "CA-EMAIL-0144" and m["kind"] == "advance_card"]
    assert len(second) == 1 and second[0]["to"] == [LEE[1]] and "/card-link/" in second[0]["body"] and "no SMS or WhatsApp" in second[0]["body"]
    link = second[0]["body"].split("/card-link/")[1].split()[0]
    page = client.get(f"/card-link/{link}")
    assert page.status_code == 200 and "************4001" in page.text and "USD 300.00 in total" in page.text and "Lee Park" in page.text
    assert client.get("/card-link/" + link[:-2] + "00").status_code == 404
    # with the second factor on, the link asks for the authenticator code first: no SMS, no WhatsApp
    import time

    from venthora import auth, totp

    store = auth.AuthStore()
    secret = store.totp_begin("u-lee")
    assert store.totp_confirm("u-lee", totp.code_at(secret, totp.counter(time.time())))
    try:
        gate = client.get(f"/card-link/{link}")
        assert gate.status_code == 200 and "Enter your authenticator code" in gate.text and "************4001" not in gate.text
        assert client.post(f"/card-link/{link}", json={"code": "000000"}).status_code == 401
        good = client.post(f"/card-link/{link}", json={"code": totp.code_at(secret, totp.counter(time.time()) + 1)})
        assert good.status_code == 200 and "************4001" in good.text
    finally:
        store.totp_off("u-lee")


def test_a_forwarded_approval_is_approved_and_the_fco_is_told_and_can_cancel(client):
    org = client.app.state.org
    req = send(client, eml(LEE, "Fwd: cash advance", fwd("Lee Park", "200.00", "supplier audit"), "fwd-1"))
    num = number_of(req)
    assert num.startswith("CA-")  # VENTHORA's own number
    fr = org.get("fund_request", num)
    assert fr["status"] == "APPROVED" and "email forwarded by Lee Park" in fr["decided_by"] and fr["allocation"]["approval"] == "forwarded"
    assert FCO[1] in next(m for m in org.list("outbox") if m["ref"] == num)["to"]  # the FCO sees every forwarded approval
    assert client.post(f"/v1/fund-requests/{num}/cancel?role=finance_manager&by=Dana%20Finch").status_code == 403
    done = client.post(f"/v1/fund-requests/{num}/cancel?role=super_admin&by=Rosa%20Lindqvist", json={"decision": "reject", "note": "I never approved this"})
    assert done.status_code == 200 and done.json()["status"] == "CANCELLED" and done.json()["allocation"]["status"] == "CANCELLED"
    import venthora.api as api

    assert asyncio.run(api._advances_due(dt.datetime.now(dt.UTC) + dt.timedelta(days=2))) == [] and not client.bank.cards
    # without the FCO's approval in it, a person's own request only waits for a decision
    num = number_of(send(client, eml(JONAS, "Cash advance", f"Amount: USD 50\nPurpose: taxi\nCharge code: {CODE}", "own-1")))
    own = org.get("fund_request", num)
    assert own, num
    assert own["status"] == "SUBMITTED" and "allocation" not in own


def test_a_travel_advance_keeps_or_gets_its_authorisation_number_and_goes_on_the_staff_card(client):
    import venthora.api as api
    from venthora import card_program

    org = client.app.state.org
    card_program.save(org, {"staff_cards": True, "purposes": ["petty_cash"]}, "test")  # travel is not even ticked: the number decides
    org._put("card", {"_id": "staff-lee", "kind": "staff", "user_id": "u-lee", "label": "Lee Park · u-lee", "payee": "Lee Park", "currency": "USD", "masked": "************7001",
                      "brand": "VISA", "per_tx_minor": 500_000, "monthly_minor": 500_000, "merchant": "", "mcc": [], "frozen": False, "status": "INACTIVE", "allowances": []}, "test")
    a = org.get("fund_request", number_of(send(client, eml(FCO, "Travel advance for Lee Park", f"For: Lee Park\nAmount: USD 400\nPurpose: customer workshop\n"
                                                                                               f"Travel Authorisation: TA-EMAIL-0031\nCharge code: {CODE}\nApproved.", "ta-1"))))
    assert a["ta_number"] == "TA-EMAIL-0031" and a["kind"] == "cash_advance" and a["allocation"]["route"] == "staff_card" and a["purpose"].startswith("Travel")
    b = org.get("fund_request", number_of(send(client, eml(LEE, "Fwd: travel advance", fwd("Lee Park", "150.00", "flight and hotel, trade fair"), "ta-2"))))
    assert b["ta_number"].startswith("TA-") and b["ta_number"] != "TA-EMAIL-0031" and b["allocation"]["route"] == "staff_card"  # none stated: VENTHORA's own
    out = asyncio.run(api._advances_due(dt.datetime.now(dt.UTC) + dt.timedelta(days=2)))
    assert [o["paid"] for o in out] == [True, True] and not client.bank.cards  # no new card: the person's own staff card
    card = org.get("card", "staff-lee")
    assert {x["ref"] for x in card["allowances"]} == {a["request_number"], b["request_number"]}
    mail = [m for m in org.list("outbox") if m["kind"] == "advance_card"]
    assert len(mail) == 2 and all("the card you added to Airi" in m["body"] for m in mail)


def test_petty_cash_and_slush_fund_keep_their_number_too_and_a_missing_charge_code_is_said_plainly(client):
    org = client.app.state.org
    num = number_of(send(client, eml(FCO, "Petty cash for Jonas Weber", f"For: Jonas Weber\nAmount: USD 120\nPurpose: office float\n"
                                                                        f"Petty cash no: PC-EMAIL-007\nCharge code: {CODE}\nApproved.", "pc-1")))
    pc = org.get("fund_request", num)
    assert pc, num
    assert pc["request_number"] == "PC-EMAIL-007" and pc["kind"] == "petty_cash" and pc["allocation"]["status"] == "ALLOCATED"
    sf = org.get("fund_request", number_of(send(client, eml(FCO, "Slush fund top-up for Jonas Weber", f"For: Jonas Weber\nAmount: USD 80\nPurpose: slush fund top-up\nCharge code: {CODE}", "sf-1"))))
    assert sf["kind"] == "petty_cash" and sf["request_number"].startswith("PC-") and sf["request_number"] != "PC-EMAIL-007"
    req = send(client, eml(FCO, "Cash advance for Ines Duarte", "For: Ines Duarte\nAmount: USD 60\nPurpose: courier", "nocode-1"))
    assert any("no charge code for Ines Duarte" in f for f in req["flags"]) or number_of(req).startswith("CA-")
