# ruff: noqa: E501
"""Settings → Email: a person ticked in User management may order a vendor payment by email (it waits for someone
else); everyone else is refused with the reason; inboxes are read over IMAP (app password or OAuth 2.0), marked or
moved afterwards; push services post Postmark JSON, raw mail or a multipart form."""
import datetime as dt
from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient

from venthora import email_settings

TODAY = dt.datetime.now(dt.UTC)


@pytest.fixture
def client():
    from venthora.api import app

    with TestClient(app) as c:
        yield c


def _mail(sender, subject, body, mid, auth="mx; spf=pass; dkim=pass", to="ap@acme.example"):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Message-ID"] = sender, to, subject, f"<{mid}@test>"
    m["Date"] = TODAY.strftime("%a, %d %b %Y %H:%M:%S +0000")
    if auth:
        m["Authentication-Results"] = auth
    m.set_content(body)
    return m.as_bytes()


def _person(client, name):
    return next(u for u in client.get("/v1/org/users").json() if u["name"] == name)


ORDER = "Please pay Lumen Design Studio invoice {inv}, against PO-2026-0101. Amount due: USD 300.00."


def test_a_ticked_person_orders_a_vendor_payment_by_email(client):
    dana = _person(client, "Dana Finch")
    assert dana.get("may_order_by_email") is not True
    rec = client.post("/v1/intake/emails", content=_mail(dana["email"], "Pay Lumen LD-9200", ORDER.format(inv="LD-9200"), "o1")).json()
    assert "internal_order" not in rec["flags"]
    got = next(r for r in client.get("/v1/intake").json() if r["intake_id"] == rec["intake_id"])
    assert "email_order_refused" in got["flags"] and any("may not order vendor payments by email" in r for r in got["reasons"])
    assert client.post("/v1/org/users", json=dana | {"may_order_by_email": True}).status_code == 200
    order = client.post("/v1/intake/emails", content=_mail(dana["email"], "Pay Lumen LD-9201", ORDER.format(inv="LD-9201"), "o2")).json()
    assert "internal_order" in order["flags"] and order["status"] == "NEEDS_REVIEW" and order["counterparty_id"] == "ben-lumen-design"
    assert order["reasons"][0].startswith("payment order by email from Dana Finch") and order["po_number"] == "PO-2026-0101"
    assert (order["invoice_no"], order["amount_minor"], order["currency"]) == ("LD-9201", 30_000, "USD")
    original = next(r for r in client.get("/v1/intake").json() if r["message_id"] == "<o2@test>")
    assert original["status"] == "DUPLICATE" and original["duplicate_of"] == order["intake_id"] and "became_payment_order" in original["flags"]
    mine = client.post(f"/v1/intake/{order['intake_id']}/review", json={"decision": "verify", "reviewer": "Dana Finch", "role": "finance_manager", "note": "x"})
    assert mine.status_code == 403  # never approved by the person who asked


@pytest.mark.parametrize("policy,auth,subject,why", [
    ({"orders_by_email": False}, "mx; spf=pass", "Pay Lumen", "payment orders by email are turned off"),
    ({"company_domains": ["other.example"]}, "mx; spf=pass", "Pay Lumen", "is not a company domain"),
    ({"require_authentication": True}, "", "Pay Lumen", "did not authenticate the sender"),
    ({"order_tag": "[PAY]"}, "mx; spf=pass", "Pay Lumen", "does not carry [PAY]"),
])
def test_the_policy_refuses_with_the_reason(client, policy, auth, subject, why):
    dana = _person(client, "Dana Finch")
    client.post("/v1/org/users", json=dana | {"may_order_by_email": True})
    assert client.put("/v1/settings/email/policy", json=policy).status_code == 200
    n = abs(hash(why)) % 10_000
    rec = client.post("/v1/intake/emails", content=_mail(dana["email"], subject, ORDER.format(inv=f"LD-{n}"), f"p{n}", auth=auth)).json()
    assert "internal_order" not in rec["flags"]
    got = next(r for r in client.get("/v1/intake").json() if r["intake_id"] == rec["intake_id"])
    assert any(why in r for r in got["reasons"]), got["reasons"]


def test_inbox_settings_keep_the_secret_write_only(client):
    s = client.get("/v1/settings/email").json()
    assert {p["key"] for p in s["presets"]} >= {"gmail", "outlook", "yahoo", "icloud", "zoho", "fastmail", "other"}
    assert {p["key"] for p in s["push_providers"]} >= {"postmark", "mailgun", "sendgrid", "cloudflare", "raw", "other"}
    assert s["policy"]["orders_by_email"] is True and any(u["name"] == "Dana Finch" for u in s["senders"]["staff"])
    ib = {"address": "ap.test@gmail.com", "provider": "gmail", "secret": "abcd efgh ijkl mnop", "purpose": "invoices"}
    out = client.put("/v1/settings/email/inboxes/ap-gmail", json=ib).json()
    assert out["secret_set"] and out["secret_hint"] == "…mnop" and "secret" not in out
    assert out["resolved"] == {"host": "imap.gmail.com", "port": 993, "security": "ssl", "username": "ap.test@gmail.com", "auth": "password"}
    assert "abcd efgh" not in str(client.get("/v1/settings/email").json())
    out = client.put("/v1/settings/email/inboxes/ap-gmail", json=ib | {"secret": None, "folder": "Invoices"}).json()
    assert out["secret_set"] and out["folder"] == "Invoices"  # blank keeps the saved password
    assert client.put("/v1/settings/email/inboxes/x1", json=ib | {"provider": "hotmailz"}).status_code == 422
    assert client.put("/v1/settings/email/policy", json={"company_domains": ["not a domain"]}).status_code == 422
    assert client.put("/v1/settings/email/policy?role=auditor", json={}).status_code == 403
    k = client.post("/v1/developer/keys", json={"name": "x", "scopes": ["settings:read", "settings:write"]}).json()["key"]
    assert client.get("/v1/settings/email", headers={"X-API-Key": k}).status_code == 403  # mailboxes are not for keys
    assert client.delete("/v1/settings/email/inboxes/ap-gmail").json() == {"deleted": "ap-gmail"}


def test_an_inbox_is_read_marked_and_never_read_twice(client, monkeypatch):
    dana = _person(client, "Dana Finch")
    mail = _mail("rechnungen@nordlicht-logistik.example", "Rechnung NL-7001", "Invoice NL-7001, PO-2026-0201. Amount due: EUR 100.00, due 30 Nov 2026.",
                 "in1", to="ap.test@gmail.com")
    calls = {"fetch": [], "finish": []}

    def fetch_new(raw, seen, limit=25):
        calls["fetch"].append(set(seen))
        return [] if "<in1@test>" in seen else [(b"7", "<in1@test>", mail)]
    monkeypatch.setattr(email_settings, "fetch_new", fetch_new)
    monkeypatch.setattr(email_settings, "finish", lambda raw, uids: calls["finish"].append(uids))
    client.put("/v1/settings/email/inboxes/ap-gmail", json={"address": "ap.test@gmail.com", "provider": "gmail", "secret": "pw-123456789", "purpose": "claims"})
    r = client.post("/v1/settings/email/inboxes/ap-gmail/fetch").json()
    assert r["fetched"] == 1 and r["queued"][0]["sender"] == "rechnungen@nordlicht-logistik.example" and calls["finish"] == [[b"7"]]
    rec = next(x for x in client.get("/v1/intake").json() if x["intake_id"] == r["queued"][0]["intake_id"])
    assert rec["mailbox"] == "ap.test@gmail.com" and "wrong_mailbox" in rec["flags"]  # an invoice in the claims inbox waits
    assert client.post("/v1/settings/email/inboxes/ap-gmail/fetch").json()["fetched"] == 0  # seen once, read once
    assert client.get("/v1/settings/email").json()["inboxes"][0]["last_report"]["fetched"] == 0
    assert dana  # the staff list is untouched


class FakeIMAP:
    """Just enough of an IMAP server: two messages in INBOX, one unread."""
    instances = []
    error = email_settings.imaplib.IMAP4.error  # the real exception, kept when the class stands in for IMAP4

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.log = host, port, []
        FakeIMAP.instances.append(self)

    def login(self, user, pw):
        if pw != "app-pass":
            raise email_settings.imaplib.IMAP4.error("AUTHENTICATIONFAILED")
        self.log.append(("login", user))

    def authenticate(self, mech, cb):
        self.log.append(("auth", mech, cb(b"")))

    def select(self, folder):
        return ("OK", [b"2"]) if folder == '"INBOX"' else ("NO", [b"no such folder"])

    def search(self, charset, crit):
        return "OK", [b"2"]

    def uid(self, cmd, *args):
        self.log.append(("uid", cmd, *args))
        if cmd == "SEARCH":
            return "OK", [b"2"]
        if cmd == "FETCH":
            return "OK", [(b"2 (BODY[] {10}", b"Message-ID: <m2@test>\r\nFrom: a@b.example\r\n\r\nhi")]
        return ("NO", [b"no MOVE"]) if cmd == "MOVE" else ("OK", [b""])

    def create(self, folder):
        return "OK", [b""]

    def expunge(self):
        self.log.append(("expunge",))

    def starttls(self):
        self.log.append(("starttls",))

    def logout(self):
        self.log.append(("logout",))


def test_imap_password_oauth2_folders_and_move(monkeypatch):
    monkeypatch.setattr(email_settings.imaplib, "IMAP4_SSL", FakeIMAP)
    monkeypatch.setattr(email_settings.imaplib, "IMAP4", FakeIMAP)
    raw = {"inbox_id": "x1", "address": "ap@acme.example", "provider": "other", "host": "imap.acme.example", "port": 993, "security": "ssl",
           "auth": "password", "_secret": "app-pass", "folder": "INBOX", "after": "move", "move_to": "Done"}
    assert email_settings.test_inbox(raw) == {"ok": True, "folder": "INBOX", "messages": 2, "unread": 1}
    got = email_settings.fetch_new(raw, set())
    assert got == [(b"2", "<m2@test>", b"Message-ID: <m2@test>\r\nFrom: a@b.example\r\n\r\nhi")]
    assert email_settings.fetch_new(raw, {"<m2@test>"}) == []
    email_settings.finish(raw, [b"2"])
    log = FakeIMAP.instances[-1].log
    assert ("uid", "STORE", "2", "+FLAGS", "(\\Seen)") in log and ("uid", "COPY", "2", '"Done"') in log and ("expunge",) in log  # no MOVE: copy + delete
    with pytest.raises(email_settings.MailboxError, match="refused the sign-in"):
        email_settings.test_inbox(raw | {"_secret": "wrong"})
    with pytest.raises(email_settings.MailboxError, match="no folder"):
        email_settings.test_inbox(raw | {"folder": "Nope"})
    email_settings.test_inbox(raw | {"provider": "outlook", "host": "", "auth": "oauth2", "_secret": "tok", "security": "starttls"})
    last = FakeIMAP.instances[-1]
    assert last.host == "outlook.office365.com" and ("starttls",) in last.log
    assert ("auth", "XOAUTH2", b"user=ap@acme.example\x01auth=Bearer tok\x01\x01") in last.log
    with pytest.raises(email_settings.MailboxError, match="no password"):
        email_settings.test_inbox(raw | {"_secret": ""})


def test_push_takes_a_multipart_form_and_a_secret_header(client, monkeypatch):
    monkeypatch.setenv("VENTHORA_INBOUND_EMAIL_SECRET", "s3cret-value")
    mail = _mail("rechnungen@nordlicht-logistik.example", "Rechnung NL-7101", "Invoice NL-7101. Amount due: EUR 10.00, due 30 Nov 2026.", "push1")
    r = client.post("/webhooks/email", headers={"X-Venthora-Secret": "s3cret-value"}, files={"email": ("m.eml", mail, "message/rfc822")})
    assert r.status_code == 200 and r.json()["kind"]
    mail2 = _mail("rechnungen@nordlicht-logistik.example", "Rechnung NL-7102", "Invoice NL-7102. Amount due: EUR 11.00, due 30 Nov 2026.", "push2")
    assert client.post("/webhooks/email", headers={"X-Venthora-Secret": "s3cret-value"}, data={"body-mime": mail2.decode()}).status_code == 200
    assert client.post("/webhooks/email", headers={"X-Venthora-Secret": "nope"}, content=mail).status_code == 401
