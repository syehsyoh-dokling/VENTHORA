# ruff: noqa: E501
"""An Outlook / Microsoft 365 mailbox read and written through Microsoft Graph instead of IMAP / SMTP.

Why: Microsoft refuses IMAP and SMTP sign-ins to personal Outlook.com mailboxes from cloud data centres ("User is
authenticated but not connected"), even with a valid OAuth token; the same token works from a home connection.
Graph is Microsoft's own API for applications and answers from anywhere. A mailbox connected with Graph permissions
(Mail.ReadWrite, Mail.Send) is read and answered here; one connected earlier with the IMAP permissions keeps IMAP.

The same token as before: one sign-in with Microsoft (Email Sync → Test connect), refreshed with its refresh token.
"""
from __future__ import annotations

import base64
import datetime as dt
import json

import httpx

GRAPH = "https://graph.microsoft.com/v1.0"
WELL_KNOWN = {"inbox": "inbox", "junk": "junkemail", "junk email": "junkemail", "archive": "archive", "sent": "sentitems"}


class GraphError(RuntimeError):
    pass


def is_graph(secret: str) -> bool:
    """A kept sign-in whose token is for Microsoft Graph (not for IMAP / SMTP)."""
    try:
        return "graph.microsoft.com" in str(json.loads(secret).get("scope") or "")
    except (ValueError, AttributeError):
        return False


def _call(token: str, method: str, path: str, *, client: httpx.Client | None = None, **kw) -> httpx.Response:
    url = path if path.startswith("https://") else GRAPH + path
    headers = {"Authorization": f"Bearer {token}"} | kw.pop("headers", {})
    try:
        r = (client or httpx).request(method, url, headers=headers, timeout=30, **kw)
    except httpx.HTTPError as exc:
        raise GraphError(f"Microsoft Graph did not answer: {type(exc).__name__}") from exc
    if r.status_code >= 400:
        try:
            msg = r.json().get("error", {}).get("message") or r.text[:200]
        except ValueError:
            msg = r.text[:200]
        if r.status_code in (401, 403):
            msg = f"Microsoft refused the mailbox access ({msg}): connect the mailbox again in Email Sync and accept the mail permissions"
        raise GraphError(msg)
    return r


def folder_ref(token: str, folder: str, *, create: bool = False, client: httpx.Client | None = None) -> str:
    """A folder path segment: Microsoft's well-known name (inbox …) or the id of a top-level folder by its name."""
    name = (folder or "INBOX").strip()
    if name.lower() in WELL_KNOWN:
        return WELL_KNOWN[name.lower()]
    shown = name.replace("/", " ")
    safe = shown.replace("'", "''")
    found = _call(token, "GET", "/me/mailFolders", params={"$filter": f"displayName eq '{safe}'", "$select": "id"}, client=client).json().get("value", [])
    if found:
        return found[0]["id"]
    if not create:
        raise GraphError(f"the mailbox has no folder named {shown}")
    return _call(token, "POST", "/me/mailFolders", json={"displayName": shown}, client=client).json()["id"]


def test(token: str, folder: str = "INBOX", *, client: httpx.Client | None = None) -> dict:
    f = _call(token, "GET", f"/me/mailFolders/{folder_ref(token, folder, client=client)}", params={"$select": "totalItemCount,unreadItemCount"}, client=client).json()
    return {"messages": int(f.get("totalItemCount") or 0), "unread": int(f.get("unreadItemCount") or 0)}


def list_new(token: str, folder: str = "INBOX", *, since_days: int = 7, only_unread: bool = True, limit: int = 50,
             client: httpx.Client | None = None) -> list[tuple[str, str | None]]:
    """(Graph message id, Internet Message-ID) of the messages to read, oldest first."""
    parts = []
    if since_days and since_days > 0:
        since = (dt.datetime.now(dt.UTC) - dt.timedelta(days=since_days)).strftime("%Y-%m-%dT00:00:00Z")
        parts.append(f"receivedDateTime ge {since}")
    else:
        parts.append("receivedDateTime ge 1900-01-01T00:00:00Z")  # Graph sorts only on a property that is filtered first
    if only_unread:
        parts.append("isRead eq false")
    params = {"$filter": " and ".join(parts), "$orderby": "receivedDateTime asc", "$select": "id,internetMessageId", "$top": str(min(limit, 100))}
    items = _call(token, "GET", f"/me/mailFolders/{folder_ref(token, folder, client=client)}/messages", params=params, client=client).json().get("value", [])
    return [(m["id"], m.get("internetMessageId")) for m in items]


def mime(token: str, message_id: str, *, client: httpx.Client | None = None) -> bytes:
    """The message exactly as it arrived (RFC 822), like an IMAP BODY.PEEK[]: reading it does not mark it read."""
    return _call(token, "GET", f"/me/messages/{message_id}/$value", client=client).content


def mark_read(token: str, ids: list[str], *, client: httpx.Client | None = None) -> None:
    for i in ids:
        _call(token, "PATCH", f"/me/messages/{i}", json={"isRead": True}, client=client)


def move(token: str, ids: list[str], target: str, *, client: httpx.Client | None = None) -> None:
    dest = folder_ref(token, target, create=True, client=client)
    for i in ids:
        _call(token, "POST", f"/me/messages/{i}/move", json={"destinationId": dest}, client=client)


class SendConnection:
    """What outbox.deliver needs from an SMTP connection (send_message, quit), sending through Graph's sendMail."""

    def __init__(self, token: str, client: httpx.Client | None = None):
        self.token, self.client = token, client

    def send_message(self, msg) -> None:
        import smtplib

        try:
            _call(self.token, "POST", "/me/sendMail", content=base64.b64encode(msg.as_bytes()), headers={"Content-Type": "text/plain"}, client=self.client)
        except GraphError as exc:  # the outbox records it as a failed send, like an SMTP error
            raise smtplib.SMTPException(f"Microsoft Graph refused the message: {exc}") from exc

    def ehlo(self) -> None:
        _call(self.token, "GET", "/me/mailFolders/inbox", params={"$select": "id"}, client=self.client)  # proves the token reaches the mailbox

    def quit(self) -> None:
        return None

    def close(self) -> None:
        return None
