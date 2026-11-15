# ruff: noqa: E501
"""A purchase request by email, end to end: the email's own PR number or VENTHORA's, approved by the FCO's email, five
vendors asked, the agent's award by the fixed rule (every specification first, then price), a person when nobody
meets them, and the winner's invoice held against the purchase order."""
import datetime as dt
from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient

from venthora import pr_email, sourcing

FCO = ("Rosa Lindqvist", "rosa.lindqvist@acme.example")
AMIRA = ("Amira Shah", "amira.shah@acme.example")
BODY = ("Please purchase the following.\n\nItem: HP 206A toner cartridges\nQuantity: 10\nUnit: pcs\nBrand: HP\nEstimate: 1000\nCurrency: USD\n"
        "Needed by: 2026-12-20\nCharge code: US-200.OPS.6700\nSpecs: Original HP 206A black; Yield at least 1,350 pages; Delivered to the US office\n{extra}")


def eml(sender, subject, body, mid):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Message-ID"] = f"{sender[0]} <{sender[1]}>", "procurement@acme.example", subject, f"<{mid}@test>"
    m["Date"] = dt.datetime.now(dt.UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")
    m.set_content(body)
    return m.as_bytes()


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("VENTHORA_PROCUREMENT_DIR", str(tmp_path / "procurement"))
    from venthora.api import app

    with TestClient(app) as c:
        yield c


def send(client, raw):
    r = client.post("/v1/intake/emails", content=raw, headers={"content-type": "message/rfc822"})
    assert r.status_code == 200, r.text
    flag = next((f for f in r.json()["flags"] if f.startswith("purchase_request")), f"no purchase request: {r.json()['flags']}")
    return flag.split(":", 1)[1].strip() if flag.startswith("purchase_request:") else flag


def q(cand, ok, total, specs=3):
    return {"_id": f"PR-1:{cand}", "cand_id": cand, "specs": [{"spec": f"s{i}", "ok": ok or i > 0, "note": ""} for i in range(specs)], "brand": "HP", "currency": "USD",
            "quantity": 10, "valid_until": "2099-01-01", "total_minor": total, "vendor": {"name": cand}}


def test_the_number_is_the_emails_own_or_venthoras():
    assert pr_email.pr_number("Purchase request PR-2026-0412", "") == "PR-2026-0412"
    assert pr_email.pr_number("Please purchase toner", "Purchase request no: 26/OPS/017\nQuantity: 10") == "26/OPS/017"
    assert pr_email.pr_number("Please purchase toner", "Quantity: 10\nEstimate: 1000") is None


def test_every_specification_beats_price_and_nobody_complete_means_a_person():
    pr = {"brand": "HP", "currency": "USD", "quantity": 10, "estimated_minor": 100_000}
    a = sourcing.assess(pr, [q("cheap-offspec", False, 70_000), q("full-dear", True, 95_000), q("full-dearer", True, 99_000)])
    assert a["recommended"] == "full-dear" and a["suggested"] is None  # a cheaper quotation that misses a specification never wins
    a = sourcing.assess(pr, [q("a", False, 70_000), q("b", False, 60_000)])
    assert a["recommended"] is None and a["suggested"] == "b" and "no quotation meets every condition" in a["suggestion"] and "A person chooses" in a["suggestion"]


def test_the_fcos_email_runs_the_request_to_an_award_by_the_rule(client):
    from venthora.organisation import OrgStore

    n = send(client, eml(FCO, "Purchase request PR-EMAIL-0412: toner", BODY.format(extra="Justification: quarterly consumables"), "pr-1"))
    assert n == "PR-EMAIL-0412"  # the email's own number is kept
    org = OrgStore()
    pr = org.get("pr", n)
    assert pr["status"] == "APPROVED" and "by email" in pr["decided_by"]
    again = send(client, eml(FCO, "Purchase request PR-EMAIL-0412: toner", BODY.format(extra=""), "pr-1b"))
    assert "already on file" in again
    invites = [m for m in org.list("outbox") if m["kind"] == "rfq_invite" and m["ref"] == n]
    assert len(invites) == pr_email.INVITE == 5 and all("/rfq/" in m["body"] for m in invites)  # five vendors, each with its own form

    out = client.post(f"/v1/procurement/{n}/demo-replies?role=finance_manager&by=Dana%20Finch").json()
    assert len(out["submitted"]) == 5 and out["decision"]["action"] == "award", out
    quotes = {x["cand_id"]: x for x in sourcing.quotes(org, n)}
    winner, cheapest = quotes[out["decision"]["cand_id"]], min(quotes.values(), key=lambda x: x["total_minor"])
    assert all(s["ok"] for s in winner["specs"]) and winner["brand"] == "HP"
    assert not all(s["ok"] for s in cheapest["specs"]) and cheapest["total_minor"] < winner["total_minor"]  # the cheapest missed a specification and lost
    r = org.get("rfq", n)
    assert r["status"] == "AWARDED" and r["award"]["by"].startswith("VENTHORA agent") and "awarded by the rule" in r["award"]["note"]
    assert org.get("pr", n)["status"] == "ORDERED" and org.get("po", r["award"]["po_id"])["total_minor"] == winner["total_minor"]
    kinds = [m["kind"] for m in org.list("outbox") if m["ref"] in (n, r["award"]["po_id"])]
    assert kinds.count("po_issued") == 1 and kinds.count("rfq_regret") == 4

    # the winner's form: delivery date and invoice. A different amount goes to a person; it is never queued as payable
    token = r["award"]["details_form"].rsplit("/", 1)[1]
    form = {"registered_phone": "+1 555 0100", "street_address": "10 Market Street", "city": "Springfield", "country_code": "US", "account_name": winner["vendor"]["name"],
            "bank_name": "Demo Bank", "bank_country_code": "US", "account_number": "000123456", "routing_type": "aba", "routing_value": "021000021",
            "bank_account_category": "Checking", "delivery_date": "2026-12-10", "invoice_no": "INV-9001", "invoice_date": "2026-12-01", "invoice_amount": "12.00"}
    res = client.post(f"/vendor-details/{token}", json=form)
    assert res.status_code == 200, res.text
    inv = res.json()["invoice"]
    assert inv["status"] != "VERIFIED" and "looked at by a person" in inv["message"]
    assert client.post(f"/vendor-details/{token}", json=form).json()["invoice"] is None  # one invoice per form


def test_a_persons_own_email_is_submitted_and_numbered_by_venthora_and_nobody_complete_goes_to_a_person(client):
    from venthora.organisation import OrgStore

    n = send(client, eml(AMIRA, "Please purchase toner", BODY.format(extra=""), "pr-2"))
    org = OrgStore()
    assert n.startswith("PR-") and org.get("pr", n)["status"] == "SUBMITTED"  # VENTHORA's own number; a person's email only submits
    assert sourcing.decide(org, n)["action"] == "wait"  # nothing is awarded on a request that is not approved
    assert client.post(f"/v1/purchase-requests/{n}/decision?role=finance_manager&by=Dana%20Finch", json={"decision": "approve"}).status_code == 200
    r = org.get("rfq", n)
    for c in [c for c in r["candidates"] if c.get("token")]:  # every vendor answers, and every one misses a specification
        body = {"vendor": {"name": c["name"], "contact_person": "Sales", "email": c["email"], "phone": "+1 555 0100"}, "brand": "HP", "quantity": 10, "unit_price": 80,
                "valid_until": "2099-01-01", "specs": [{"ok": True}, {"ok": True}, {"ok": False, "note": "collected, not delivered"}]}
        assert client.post(f"/rfq/{c['token']}", json=body).status_code == 200
    r = org.get("rfq", n)
    assert r["status"] == "QUOTING" and r["agent_decision"]["action"] == "person" and "no quotation meets every condition" in r["agent_decision"]["reason"]
    mail = [m for m in org.list("outbox") if m["kind"] == "rfq_needs_person" and m["ref"] == n]
    assert len(mail) == 1 and FCO[1] in mail[0]["to"] and AMIRA[1] in mail[0]["to"]
