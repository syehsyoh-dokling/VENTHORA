# ruff: noqa: E501
"""Guards a review asked for: a card account is never funded twice for one advance, nobody decides their own request
whichever way it was raised, and the agent does not award on fewer than three quotations."""
import asyncio
import datetime as dt

import pytest
from fastapi.testclient import TestClient
from test_advance_flow import CODE, FCO, eml, number_of, send

from venthora import sourcing
from venthora.gateways.airwallex import AirwallexError
from venthora.organisation import same_person


class Bank:
    configured = True

    def __init__(self):
        self.funded, self.cards, self.fail_cards, self.returned = [], [], 1, []

    async def fund_connected_account(self, *, account_id, amount_minor, currency, reference, request_id):
        self.funded.append(request_id)
        return {"id": f"tr-{len(self.funded)}", "status": "SETTLED"}

    async def create_card(self, body):
        if self.fail_cards:
            self.fail_cards -= 1
            raise AirwallexError("card programme busy")
        self.cards.append(body)
        return {"card_id": "card-1", "card_number": "************4001", "brand": "VISA", "card_status": "ACTIVE"}

    async def balances_of(self, account_id):
        raise AirwallexError("balance unavailable")  # so the money cannot be returned: it must not be funded again

    async def set_card_status(self, card_id, status):
        return {"card_id": card_id, "card_status": status}


@pytest.fixture
def client(monkeypatch):
    import venthora.airwallex_settings as settings
    import venthora.api as api

    with TestClient(api.app) as c:
        c.bank = Bank()
        monkeypatch.setattr(api, "_bank", lambda: c.bank)
        monkeypatch.setattr(settings, "card_account_for", lambda *a, **k: "acct_cards")
        c.app.state.org._put("issuing", {"_id": "cardholder", "cardholder_id": "ch-1", "status": "READY"}, "test")
        yield c


def test_a_card_account_is_funded_once_however_often_the_run_retries(client):
    import venthora.api as api

    org = client.app.state.org
    num = number_of(send(client, eml(FCO, "Cash advance for Lee Park", f"For: Lee Park\nAmount: USD 1,500\nPurpose: site visit\nCharge code: {CODE}\nApproved.", "fund-1")))
    fr = org.get("fund_request", num)
    assert fr["amount_minor"] == 150_000  # "1,500" is fifteen hundred, not 1.5
    at = dt.datetime.fromisoformat(fr["allocation"]["pay_at"])
    first = asyncio.run(api._advances_due(at))
    assert first[0]["paid"] is False and "refused the card" in first[0]["error"]
    fr = org.get("fund_request", num)
    assert fr["status"] == "APPROVED" and fr["funding"]["transfer_id"] == "tr-1" and fr["allocation"]["attempts"] == 1  # the money is there, and that is on the record
    assert any(e["event_type"] == "card_funding_not_returned" for e in client.app.state.audit.list(20))  # said, not swallowed
    second = asyncio.run(api._advances_due(at))
    assert second[0]["paid"] is True and client.bank.funded == [f"fund-{num}-0"]  # the retry found the funding: one transfer, one request id
    assert org.get("fund_request", num)["status"] == "IN_PAYMENT"


def test_an_error_of_any_kind_counts_the_attempt_and_stops_after_three(client, monkeypatch):
    import venthora.api as api

    org = client.app.state.org
    num = number_of(send(client, eml(FCO, "Cash advance for Lee Park", f"For: Lee Park\nAmount: USD 90\nPurpose: taxi\nCharge code: {CODE}\nApproved.", "boom-1")))

    async def boom(number, who):
        raise RuntimeError("disk full")

    monkeypatch.setattr(api, "_issue_advance_card", boom)
    at = dt.datetime.fromisoformat(org.get("fund_request", num)["allocation"]["pay_at"])
    for _ in range(3):
        assert asyncio.run(api._advances_due(at))[0]["paid"] is False
    a = org.get("fund_request", num)["allocation"]
    assert a["status"] == "FAILED" and a["attempts"] == 3 and "disk full" in a["error"] and asyncio.run(api._advances_due(at)) == []


def test_nobody_decides_their_own_request_whichever_way_it_names_them(client):
    org = client.app.state.org
    assert same_person(org, "Amira Shah", "u-amira") and same_person(org, "amira.shah@acme.example", "Amira Shah") and same_person(org, "Amira Shah", "Amira Shah")
    assert not same_person(org, "Dana Finch", "u-amira") and not same_person(org, "", "u-amira")
    from test_pr_by_email import AMIRA, BODY
    from test_pr_by_email import eml as pr_eml

    r = client.post("/v1/intake/emails", content=pr_eml(AMIRA, "Please purchase toner", BODY.format(extra=""), "self-1"), headers={"content-type": "message/rfc822"}).json()
    n = next(f for f in r["flags"] if f.startswith("purchase_request:")).split(":", 1)[1]
    assert org.get("pr", n)["requested_by"] == "u-amira"  # raised by email: the requester is a user id
    own = client.post(f"/v1/purchase-requests/{n}/decision?role=country_finance_lead&by=Amira%20Shah", json={"decision": "approve"})
    assert own.status_code == 403 and "own purchase request" in own.json()["detail"]


def test_the_agent_does_not_award_on_fewer_than_three_quotations():
    class Org:
        def __init__(self):
            self.rec = {("pr", "PR-1"): {"pr_number": "PR-1", "status": "APPROVED", "brand": "", "currency": "USD", "quantity": 1, "estimated_minor": 100_000},
                        ("rfq", "PR-1"): {"_id": "PR-1", "status": "QUOTING", "deadline": "2099-01-01", "candidates": [{"cand_id": "a", "invited_at": "x"}, {"cand_id": "b", "invited_at": "x"}]}}

        def get(self, kind, rid):
            return self.rec.get((kind, rid))

        def list(self, kind):
            return [v for (k, _), v in self.rec.items() if k == kind]

    org = Org()
    for cand, total in (("a", 50_000), ("b", 60_000)):
        org.rec[("quote", f"PR-1:{cand}")] = {"_id": f"PR-1:{cand}", "pr_number": "PR-1", "cand_id": cand, "specs": [{"spec": "s", "ok": True, "note": ""}], "brand": "",
                                              "currency": "USD", "quantity": 1, "valid_until": "2099-01-01", "total_minor": total, "vendor": {"name": cand}}
    d = sourcing.decide(org, "PR-1")
    assert d["action"] == "person" and d["suggested"] == "a" and "only 2 quotation(s)" in d["reason"]
