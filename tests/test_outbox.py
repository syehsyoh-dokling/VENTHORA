# ruff: noqa: E501
"""The vendor notice: what is missing in plain words, queued in the outbox, sent over SMTP when outgoing email is set
up (the password is never returned)."""
import pytest
from fastapi.testclient import TestClient

from venthora import agent_today, outbox

FCO = "role=super_admin&by=Rosa%20Lindqvist"


@pytest.fixture
def client():
    from venthora.api import app
    with TestClient(app) as c:
        yield c


def test_the_findings_say_what_the_vendor_must_complete():
    req = {"currency": "GBP", "flags": ["payee_bank_details_changed"],
           "reasons": ["the invoice quotes no purchase order; the finance standards pay supplier invoices only against an open PO; registered mailbox of X; amount and invoice quoted verbatim"],
           "checks": [{"label": "Stamp", "status": "fail", "detail": "the company stamp is missing"}, {"label": "Dates", "status": "pass", "detail": "ok"}]}
    f = agent_today.findings(req, "reject: instalment 'partial' would take the invoice past its contract total: paid 45000 + claimed 1200000 > 1000000")
    assert "Stamp: the company stamp is missing" in f
    assert "The invoice quotes no purchase order" in f and not any("verbatim" in x or "registered mailbox" in x for x in f)
    assert any(x.startswith("Your bank details changed") for x in f)
    assert "The amount goes above what is left on the purchase order: GBP 450.00 already paid, GBP 12,000.00 invoiced now, order total GBP 10,000.00" in f
    assert agent_today.findings({}, "reject: evidence intake:1 is pending") == ["The invoice is still being checked by our finance team"]


def test_a_notice_waits_queued_without_smtp_and_is_sent_with_it(client, monkeypatch):
    org = client.app.state.org
    rec = outbox.queue(org, to=["ap@vendor.example"], subject="Your payment", body="Dear vendor", ref="REQ-1", by="Rosa")
    assert outbox.deliver(org, rec["_id"])["status"] == "queued" and outbox.smtp(org)["configured"] is False
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=15):
            self.host = host

        def starttls(self, context=None):
            pass

        def login(self, user, password):
            assert password == "app-password-123"

        def send_message(self, m):
            sent.append((m["To"], m["Subject"], m["From"]))

        def quit(self):
            pass

    monkeypatch.setattr(outbox.smtplib, "SMTP", FakeSMTP)
    r = client.put(f"/v1/outbox/smtp?{FCO}", json={"host": "smtp.example.com", "port": 587, "user": "payments@acme.example", "password": "app-password-123",
                                                   "sender": "payments@acme.example", "security": "starttls"})
    assert r.status_code == 200 and r.json()["configured"] is True and "app-password-123" not in r.text
    assert client.post(f"/v1/outbox/{rec['_id']}/send?{FCO}").json()["status"] == "sent"
    assert sent == [("ap@vendor.example", "Your payment", "payments@acme.example")]
    assert client.post(f"/v1/outbox/smtp/check?{FCO}").json()["ok"] is True
    listed = client.get("/v1/outbox").json()
    assert listed["items"][0]["status"] == "sent" and "app-password-123" not in str(listed)
    assert client.put("/v1/outbox/smtp?role=auditor&by=Ines", json={"host": "smtp.x.com", "sender": "a@b.co"}).status_code == 403


def test_the_worker_sends_what_waited_once_email_is_set_up_and_retries_at_most_five_times(client, monkeypatch):
    org = client.app.state.org
    waiting = outbox.queue(org, to=["ap@vendor.example"], subject="Queued before SMTP", body="x", ref="REQ-9", by="Rosa")
    assert outbox.flush(org) == []  # no mail server yet: it stays queued
    tries = []

    class Down:
        def __init__(self, host, port, timeout=15):
            tries.append(host)
            raise OSError("connection refused")

    monkeypatch.setattr(outbox.smtplib, "SMTP", Down)
    monkeypatch.setattr(outbox.secret_values, "get", lambda k: "pw")
    monkeypatch.setattr(outbox.secret_values, "info", lambda k: {"set": True})
    outbox.save_smtp(org, {"host": "smtp.example.com", "port": 587, "user": "u", "sender": "payments@acme.example", "security": "starttls"}, "Rosa")
    for _ in range(7):
        outbox.flush(org)
    rec = org.get("outbox", waiting["_id"])
    assert rec["status"] == "failed" and rec["attempts"] == outbox.MAX_ATTEMPTS and len(tries) == outbox.MAX_ATTEMPTS

    class Up:
        def __init__(self, host, port, timeout=15):
            pass

        def starttls(self, context=None):
            pass

        def login(self, user, password):
            pass

        def send_message(self, m):
            pass

        def quit(self):
            pass

    monkeypatch.setattr(outbox.smtplib, "SMTP", Up)
    fresh = outbox.queue(org, to=["ap@vendor.example"], subject="Queued after", body="y", ref="REQ-10", by="Rosa")
    sent = outbox.flush(org)
    assert [r["_id"] for r in sent] == [fresh["_id"]] and org.get("outbox", fresh["_id"])["status"] == "sent"
