# ruff: noqa: E501
"""Outlook through Microsoft Graph: a mailbox connected with Graph permissions is read (oldest first, raw MIME, own
notices skipped), marked read and answered through Graph; one connected with IMAP permissions stays on IMAP."""
import json
import smtplib
import time
from email.message import EmailMessage

import httpx
import pytest

from venthora import email_settings, mail_oauth, ms_graph, outbox

GRAPH_SECRET = json.dumps({"kind": "oauth", "provider": "outlook", "access_token": "tok-graph", "refresh_token": "r",
                           "expires_at": int(time.time()) + 3600,
                           "scope": "https://graph.microsoft.com/Mail.ReadWrite https://graph.microsoft.com/Mail.Send"})
IMAP_SECRET = json.dumps({"kind": "oauth", "provider": "outlook", "access_token": "tok-imap", "refresh_token": "r",
                          "expires_at": int(time.time()) + 3600, "scope": "https://outlook.office.com/IMAP.AccessAsUser.All"})
INVOICE = b"Message-ID: <inv-1@vendor.com>\r\nFrom: billing@vendor.com\r\nSubject: Invoice 7\r\n\r\nPlease pay."
NOTICE = b"Message-ID: <n-1@x>\r\nX-Venthora-Notice: fco\r\nSubject: [VENTHORA] approve\r\n\r\nhi"


class FakeGraph:
    def __init__(self):
        self.calls = []

    def request(self, method, url, headers=None, timeout=None, **kw):
        assert headers["Authorization"] == "Bearer tok-graph"
        path = url.replace(ms_graph.GRAPH, "")
        self.calls.append((method, path, kw))
        req = httpx.Request(method, url)
        if method == "GET" and path == "/me/mailFolders/inbox/messages":
            assert "isRead eq false" in kw["params"]["$filter"] and kw["params"]["$orderby"] == "receivedDateTime asc"
            return httpx.Response(200, json={"value": [{"id": "A1", "internetMessageId": "<inv-1@vendor.com>"},
                                                       {"id": "A2", "internetMessageId": "<n-1@x>"},
                                                       {"id": "A3", "internetMessageId": "<seen@x>"}]}, request=req)
        if method == "GET" and path.endswith("/$value"):
            return httpx.Response(200, content=INVOICE if "A1" in path else NOTICE, request=req)
        if method == "GET" and path == "/me/mailFolders/inbox":
            return httpx.Response(200, json={"id": "inbox", "totalItemCount": 78, "unreadItemCount": 3}, request=req)
        if method in ("PATCH", "POST"):
            return httpx.Response(202 if path == "/me/sendMail" else 200, json={}, request=req)
        return httpx.Response(404, json={"error": {"message": "unexpected " + path}}, request=req)


@pytest.fixture
def graph(monkeypatch):
    g = FakeGraph()
    monkeypatch.setattr(ms_graph.httpx, "request", g.request)
    return g


def raw(secret):
    return {"inbox_id": "ib1", "address": "me@outlook.com", "auth": "oauth2", "provider": "outlook", "folder": "INBOX",
            "since_days": 7, "only_unread": True, "after": "mark_read", "_secret": secret}


def test_outlook_signs_in_for_graph_mail_permissions_not_imap():
    scope = mail_oauth.PROVIDERS["outlook"]["scope"]
    assert "graph.microsoft.com/Mail.ReadWrite" in scope and "graph.microsoft.com/Mail.Send" in scope and "IMAP" not in scope
    assert ms_graph.is_graph(GRAPH_SECRET) and not ms_graph.is_graph(IMAP_SECRET)


def test_a_graph_mailbox_is_tested_read_and_marked_through_graph(graph):
    t = email_settings.test_inbox(raw(GRAPH_SECRET))
    assert t["ok"] and t["messages"] == 78 and t["unread"] == 3 and t["via"] == "Microsoft Graph"
    got = email_settings.fetch_new(raw(GRAPH_SECRET), seen={"<seen@x>"})
    assert [(u, m) for u, m, _ in got] == [(b"A1", "<inv-1@vendor.com>")]  # the own notice and the seen one are skipped
    assert got[0][2] == INVOICE
    email_settings.finish(raw(GRAPH_SECRET), [b"A1"])
    assert ("PATCH", "/me/messages/A1", {"json": {"isRead": True}}) in graph.calls


def test_an_imap_mailbox_does_not_go_to_graph(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("Graph must not be called for an IMAP sign-in")

    monkeypatch.setattr(ms_graph.httpx, "request", boom)
    assert email_settings._graph_token(raw(IMAP_SECRET)) is None


def test_confirmations_from_a_graph_mailbox_are_sent_with_sendmail(graph):
    s = {"host": "smtp.office365.com", "port": 587, "security": "starttls", "user": "me@outlook.com", "sender": "me@outlook.com"}
    conn = outbox._connect(s, password=GRAPH_SECRET)
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = "me@outlook.com", "v@vendor.com", "Paid"
    m.set_content("done")
    conn.send_message(m)
    conn.quit()
    sent = [c for c in graph.calls if c[1] == "/me/sendMail"]
    import base64

    assert len(sent) == 1 and b"Subject: Paid" in base64.b64decode(sent[0][2]["content"])  # the MIME message, base64, as Graph wants it


def test_a_graph_refusal_is_an_smtp_error_for_the_outbox(monkeypatch):
    def refuse(method, url, headers=None, timeout=None, **kw):
        return httpx.Response(403, json={"error": {"message": "Access is denied"}}, request=httpx.Request(method, url))

    monkeypatch.setattr(ms_graph.httpx, "request", refuse)
    with pytest.raises(smtplib.SMTPException):
        ms_graph.SendConnection("tok-graph").send_message(EmailMessage())
