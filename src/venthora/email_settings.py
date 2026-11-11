# ruff: noqa: E501
"""Settings → Email: how mail reaches VENTHORA, who may send what, and the inboxes VENTHORA reads itself.

Two ways in, both through the same intake (sender check, grounded fields, document validation, queue):

PUSH   a mail service posts each email to /webhooks/email the moment it arrives (Postmark, Mailgun, SendGrid
       Inbound Parse, a Cloudflare Email Worker, or anything that can post the raw message). A supplier writes
       or copies the address the company set for VENTHORA.
PULL   VENTHORA reads a mailbox over IMAP on a schedule (every minute by default): Gmail / Google Workspace,
       Microsoft 365 / Outlook.com, Yahoo, iCloud, Zoho, Fastmail, Yandex, AOL, or any other IMAP server.
       Most providers take an app password; Microsoft 365 takes only OAuth 2.0, so its access token is the secret.

Who may send what (the policy): vendors write from the mailboxes registered in the vendor master; staff are the
active people in User management, and only those ticked "may order vendor payments by email" can ask VENTHORA to
pay a vendor by email. Such an order never pays by itself: it enters the queue like a payment order from the
dashboard and a different person approves it. Optionally the staff mailbox must be on a company domain, the
receiving server must have authenticated it (SPF / DKIM / DMARC pass), and the subject must carry a tag.

Secrets (passwords, tokens) are write-only, as everywhere else in VENTHORA.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import email.utils
import imaplib
import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .config import data_path

# provider -> (label, host, port, security, auth, how to get the secret)
PRESETS: dict[str, dict] = {
    "gmail": {"label": "Gmail / Google Workspace", "host": "imap.gmail.com", "port": 993, "security": "ssl", "auth": "password",
              "smtp_host": "smtp.gmail.com", "smtp_port": 587, "smtp_security": "starttls",
              "help": "Turn on 2-Step Verification, then Google Account → Security → App passwords; IMAP must be on in Gmail settings."},
    "outlook": {"label": "Microsoft 365 / Outlook.com", "host": "outlook.office365.com", "port": 993, "security": "ssl", "auth": "oauth2",
              "smtp_host": "smtp.office365.com", "smtp_port": 587, "smtp_security": "starttls",
                "help": "Microsoft turned off passwords for IMAP: register an app in Entra ID with IMAP.AccessAsUser.All and paste its OAuth 2.0 access token. Sending (SMTP) with an app password works only where the admin left SMTP AUTH on for the mailbox."},
    "yahoo": {"label": "Yahoo Mail", "host": "imap.mail.yahoo.com", "port": 993, "security": "ssl", "auth": "password",
              "smtp_host": "smtp.mail.yahoo.com", "smtp_port": 465, "smtp_security": "ssl",
              "help": "Yahoo Account Security → Generate app password."},
    "icloud": {"label": "iCloud Mail", "host": "imap.mail.me.com", "port": 993, "security": "ssl", "auth": "password",
              "smtp_host": "smtp.mail.me.com", "smtp_port": 587, "smtp_security": "starttls",
               "imap_user": "local", "help": "appleid.apple.com → Sign-In and Security → App-Specific Passwords; VENTHORA signs in to IMAP with the part before @icloud.com and to SMTP with the full address."},
    "zoho": {"label": "Zoho Mail", "host": "imap.zoho.com", "port": 993, "security": "ssl", "auth": "password",
              "smtp_host": "smtp.zoho.com", "smtp_port": 465, "smtp_security": "ssl",
             "help": "Zoho Mail → Settings → Mail Accounts → IMAP access on; use an application-specific password if 2FA is on."},
    "fastmail": {"label": "Fastmail", "host": "imap.fastmail.com", "port": 993, "security": "ssl", "auth": "password",
              "smtp_host": "smtp.fastmail.com", "smtp_port": 465, "smtp_security": "ssl",
                 "help": "Settings → Privacy & Security → Integrations → New app password (IMAP)."},
    "yandex": {"label": "Yandex Mail", "host": "imap.yandex.com", "port": 993, "security": "ssl", "auth": "password",
              "smtp_host": "smtp.yandex.com", "smtp_port": 465, "smtp_security": "ssl",
               "help": "Yandex ID → Security → App passwords → Mail."},
    "aol": {"label": "AOL Mail", "host": "imap.aol.com", "port": 993, "security": "ssl", "auth": "password",
              "smtp_host": "smtp.aol.com", "smtp_port": 465, "smtp_security": "ssl",
            "help": "AOL Account Security → Generate app password."},
    "other": {"label": "Other IMAP server", "host": "", "port": 993, "security": "ssl", "auth": "password",
              "smtp_host": "", "smtp_port": 587, "smtp_security": "starttls",
              "help": "The host, port and security your mail provider documents for IMAP."},
}

# push services: how each posts to VENTHORA
PUSH_PROVIDERS: dict[str, dict] = {
    "postmark": {"label": "Postmark inbound", "format": "Postmark JSON",
                 "how": "Servers → your server → Inbound → Webhook URL: {url} (basic auth user postmark, password the inbound secret)."},
    "mailgun": {"label": "Mailgun routes", "format": "multipart with body-mime",
                "how": "Receiving → Create route → Forward to {url_mime} (the URL must end in /mime so Mailgun sends the full message)."},
    "sendgrid": {"label": "SendGrid Inbound Parse", "format": "multipart with email",
                 "how": "Settings → Inbound Parse → Add host & URL: {url}, and tick \"POST the raw, full MIME message\"."},
    "cloudflare": {"label": "Cloudflare Email Routing + Worker", "format": "raw RFC 822",
                   "how": "Email Routing → route the address to a Worker that does fetch('{plain}', {{method:'POST', headers:{{'X-Venthora-Secret': SECRET}}, body: message.raw}})."},
    "raw": {"label": "Any service posting the raw message", "format": "raw RFC 822",
            "how": "POST the message as message/rfc822 to {url}, or with the header X-Venthora-Secret."},
    "other": {"label": "Other", "format": "raw RFC 822, Postmark JSON or multipart",
              "how": "Any of the formats above, to {url}; name the service in the note."},
}


class EmailPolicy(BaseModel):
    orders_by_email: bool = Field(True, description="staff ticked in User management may order vendor payments by email")
    company_domains: list[str] = Field(default_factory=list, description="staff orders only from these domains; empty = any")
    order_domains: list[str] = Field(default_factory=list, description="every active person in User management with an address at "
                                     "one of these domains may order, without being ticked one by one")
    require_authentication: bool = Field(True, description="a staff order needs SPF, DKIM or DMARC pass from the receiving server")
    order_tag: str = Field("", max_length=30, description="a word the subject must carry, e.g. [PAY]; empty = none")
    flag_unknown_mailbox: bool = Field(True, description="mail to an address that is not configured is read and flagged")

    @field_validator("company_domains", "order_domains")
    @classmethod
    def _domains(cls, v):
        out = []
        for d in v:
            d = d.strip().lower().lstrip("@")
            if d and not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", d):
                raise ValueError(f"not a domain: {d}")
            if d:
                out.append(d)
        return sorted(set(out))


class Inbox(BaseModel):
    inbox_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,40}$")
    address: str = Field(min_length=3, max_length=200, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    purpose: Literal["invoices", "claims", "remittances", "payment_orders", "any"] = "any"
    provider: str = "gmail"
    host: str = Field("", max_length=200)
    port: int = Field(993, ge=1, le=65535)
    security: Literal["ssl", "starttls"] = "ssl"
    username: str = Field("", max_length=200, description="empty = the address")
    auth: Literal["password", "oauth2"] = "password"
    secret: str | None = Field(None, max_length=16000, description="app password, OAuth 2.0 access token, or a kept sign-in; write-only, None keeps it")
    folder: str = Field("INBOX", min_length=1, max_length=120)
    only_unread: bool = True
    after: Literal["mark_read", "move", "leave"] = "mark_read"
    move_to: str = Field("VENTHORA/Processed", max_length=120)
    since_days: int = Field(7, ge=0, le=365, description="how far back mail is read, in days; 0 = all mail in the folder")
    schedule_minutes: int = Field(1, ge=0, le=1440, description="1 = every minute; 0 = only when a person presses Fetch now")
    active: bool = True

    @field_validator("provider")
    @classmethod
    def _provider(cls, v):
        if v not in PRESETS:
            raise ValueError(f"unknown provider {v}; one of {', '.join(PRESETS)}")
        return v

    def resolved(self) -> dict:
        """The connection with the preset filled in where the person left a field empty."""
        p = PRESETS[self.provider]
        return {"host": self.host.strip() or p["host"], "port": self.port or p["port"], "security": self.security,
                "username": self.username.strip() or (self.address.split("@")[0] if p.get("imap_user") == "local" else self.address), "auth": self.auth}


SCHEMA = """
CREATE TABLE IF NOT EXISTS email_policy (id INTEGER PRIMARY KEY CHECK (id = 1), record_json TEXT NOT NULL, updated_at TEXT NOT NULL, updated_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS email_inboxes (inbox_id TEXT PRIMARY KEY, record_json TEXT NOT NULL, secret TEXT, updated_at TEXT NOT NULL,
  last_poll_at TEXT, last_report_json TEXT);
CREATE TABLE IF NOT EXISTS email_seen (inbox_id TEXT NOT NULL, message_id TEXT NOT NULL, seen_at TEXT NOT NULL, intake_id TEXT,
  PRIMARY KEY (inbox_id, message_id));
"""


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


class EmailStore:
    def __init__(self, path: str | None = None):
        self.path = Path(path) if path else data_path("VENTHORA_MASTER_DB", "data/venthora_master.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as c:
            c.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    # -- policy -----------------------------------------------------------------------------------------------------
    def policy(self) -> dict:
        with self._connect() as c:
            r = c.execute("SELECT record_json FROM email_policy WHERE id = 1").fetchone()
        return EmailPolicy.model_validate(json.loads(r[0]) if r else {}).model_dump()

    def save_policy(self, p: EmailPolicy, by: str) -> dict:
        with self._lock, self._connect() as c:
            c.execute("INSERT INTO email_policy VALUES (1, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET record_json = excluded.record_json,"
                      " updated_at = excluded.updated_at, updated_by = excluded.updated_by", (p.model_dump_json(), _now(), by))
        return self.policy()

    # -- inboxes ----------------------------------------------------------------------------------------------------
    def save_inbox(self, ib: Inbox) -> dict:
        old = self.raw(ib.inbox_id)
        secret = ib.secret if ib.secret else (old or {}).get("_secret", "")
        rec = ib.model_dump(exclude={"secret"})
        with self._lock, self._connect() as c:
            c.execute("INSERT INTO email_inboxes (inbox_id, record_json, secret, updated_at) VALUES (?,?,?,?) ON CONFLICT(inbox_id) DO UPDATE SET"
                      " record_json = excluded.record_json, secret = excluded.secret, updated_at = excluded.updated_at",
                      (ib.inbox_id, json.dumps(rec), secret, _now()))
        return self.public(ib.inbox_id)

    def set_secret(self, inbox_id: str, secret: str) -> None:
        """A renewed sign-in (OAuth access and refresh token) replaces the kept one."""
        with self._lock, self._connect() as c:
            c.execute("UPDATE email_inboxes SET secret = ?, updated_at = ? WHERE inbox_id = ?", (secret, _now(), inbox_id))

    def raw(self, inbox_id: str) -> dict | None:
        with self._connect() as c:
            r = c.execute("SELECT * FROM email_inboxes WHERE inbox_id = ?", (inbox_id,)).fetchone()
        if r is None:
            return None
        return json.loads(r["record_json"]) | {"_secret": r["secret"] or "", "last_poll_at": r["last_poll_at"],
                                                "last_report": json.loads(r["last_report_json"]) if r["last_report_json"] else None}

    def public(self, inbox_id: str) -> dict | None:
        raw = self.raw(inbox_id)
        if raw is None:
            return None
        s = raw.pop("_secret")
        return raw | {"secret_set": bool(s), "secret_hint": f"…{s[-4:]}" if len(s) >= 8 else ("set" if s else None),
                      "resolved": Inbox.model_validate(raw).resolved()}

    def inboxes(self) -> list[dict]:
        with self._connect() as c:
            ids = [r[0] for r in c.execute("SELECT inbox_id FROM email_inboxes ORDER BY inbox_id")]
        return [self.public(i) for i in ids]

    def delete_inbox(self, inbox_id: str) -> bool:
        with self._lock, self._connect() as c:
            c.execute("DELETE FROM email_seen WHERE inbox_id = ?", (inbox_id,))
            return c.execute("DELETE FROM email_inboxes WHERE inbox_id = ?", (inbox_id,)).rowcount == 1

    def record_poll(self, inbox_id: str, report: dict) -> None:
        with self._lock, self._connect() as c:
            c.execute("UPDATE email_inboxes SET last_poll_at = ?, last_report_json = ? WHERE inbox_id = ?",
                      (_now(), json.dumps(report, default=str), inbox_id))

    def seen(self, inbox_id: str) -> set[str]:
        with self._connect() as c:
            return {r[0] for r in c.execute("SELECT message_id FROM email_seen WHERE inbox_id = ?", (inbox_id,))}

    def mark_seen(self, inbox_id: str, message_id: str, intake_id: str | None) -> None:
        with self._lock, self._connect() as c:
            c.execute("INSERT OR IGNORE INTO email_seen VALUES (?,?,?,?)", (inbox_id, message_id, _now(), intake_id))


# -- IMAP ---------------------------------------------------------------------------------------------------------------
class MailboxError(RuntimeError):
    pass


def _connect_imap(raw: dict, timeout: float = 30.0) -> imaplib.IMAP4:
    ib = Inbox.model_validate(raw | {"secret": None})
    cfg = ib.resolved()
    if not cfg["host"]:
        raise MailboxError("no IMAP host: enter the host your mail provider documents")
    secret = raw.get("_secret") or ""
    if not secret:
        raise MailboxError("no password or token saved for this inbox")
    try:
        if cfg["security"] == "ssl":
            conn = imaplib.IMAP4_SSL(cfg["host"], cfg["port"], timeout=timeout)
        else:
            conn = imaplib.IMAP4(cfg["host"], cfg["port"], timeout=timeout)
            conn.starttls()
        if cfg["auth"] == "oauth2":
            from . import mail_oauth

            if mail_oauth.is_oauth(secret):  # a kept sign-in: a fresh access token from its refresh token
                try:
                    secret = mail_oauth.fresh(secret, lambda s: EmailStore().set_secret(raw["inbox_id"], s))
                except mail_oauth.OAuthError as exc:
                    raise MailboxError(f"the sign-in could not be renewed: {exc}; connect the mailbox again") from exc
            token = f"user={cfg['username']}\x01auth=Bearer {secret}\x01\x01".encode()
            conn.authenticate("XOAUTH2", lambda _: token)
        else:
            conn.login(cfg["username"], secret)
    except imaplib.IMAP4.error as exc:
        raise MailboxError(f"the mail server refused the sign-in: {exc}") from exc
    except OSError as exc:
        raise MailboxError(f"cannot reach {cfg['host']}:{cfg['port']}: {exc}") from exc
    return conn


def _select(conn: imaplib.IMAP4, folder: str) -> int:
    typ, data = conn.select(_quote(folder))
    if typ != "OK":
        raise MailboxError(f"no folder {folder!r} in this mailbox")
    return int(data[0] or 0)


def _quote(folder: str) -> str:
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


def test_inbox(raw: dict) -> dict:
    """Sign in and open the folder: how many messages it holds and how many are unread. Nothing is read or changed."""
    conn = _connect_imap(raw)
    try:
        total = _select(conn, raw.get("folder") or "INBOX")
        typ, data = conn.search(None, "UNSEEN")
        unread = len(data[0].split()) if typ == "OK" and data and data[0] else 0
        return {"ok": True, "folder": raw.get("folder") or "INBOX", "messages": total, "unread": unread}
    finally:
        _logout(conn)


def fetch_new(raw: dict, seen: set[str], limit: int = 25) -> list[tuple[bytes, str, bytes]]:
    """New messages as (uid, message id, raw RFC 822), oldest first; read with BODY.PEEK so nothing is marked yet."""
    conn = _connect_imap(raw)
    try:
        _select(conn, raw.get("folder") or "INBOX")
        days = raw.get("since_days")
        days = 7 if days is None else int(days)  # 0 = every message in the folder, however old
        since = f" SINCE {(dt.date.today() - dt.timedelta(days=days)).strftime('%d-%b-%Y')}" if days > 0 else ""
        crit = f"(UNSEEN{since})" if raw.get("only_unread", True) else (f"({since.strip()})" if since else "ALL")
        typ, data = conn.uid("SEARCH", None, crit)
        if typ != "OK":
            raise MailboxError("the mail server did not answer the search")
        out = []
        for uid in (data[0] or b"").split():
            typ, msg = conn.uid("FETCH", uid, "(BODY.PEEK[])")
            if typ != "OK" or not msg or not isinstance(msg[0], tuple):
                continue
            body = msg[0][1]
            mid = message_id(body) or f"<uid-{uid.decode()}@{raw['inbox_id']}>"
            if mid in seen:
                continue
            out.append((uid, mid, body))
            if len(out) >= limit:
                break
        return out
    finally:
        _logout(conn)


def finish(raw: dict, uids: list[bytes]) -> None:
    """After VENTHORA has read them: mark them read, move them to a folder, or leave them as they are."""
    after = raw.get("after", "mark_read")
    if not uids or after == "leave":
        return
    conn = _connect_imap(raw)
    try:
        _select(conn, raw.get("folder") or "INBOX")
        ids = b",".join(uids).decode()
        conn.uid("STORE", ids, "+FLAGS", "(\\Seen)")
        if after == "move":
            target = raw.get("move_to") or "VENTHORA/Processed"
            conn.create(_quote(target))  # an existing folder answers NO, which is fine
            typ, _ = conn.uid("MOVE", ids, _quote(target))
            if typ != "OK":  # servers without MOVE: copy, then delete the original
                conn.uid("COPY", ids, _quote(target))
                conn.uid("STORE", ids, "+FLAGS", "(\\Deleted)")
                conn.expunge()
    finally:
        _logout(conn)


def _logout(conn) -> None:
    with contextlib.suppress(Exception):  # the session is over either way
        conn.logout()


def message_id(raw: bytes) -> str | None:
    head = raw.split(b"\r\n\r\n", 1)[0].split(b"\n\n", 1)[0]
    m = re.search(rb"(?im)^message-id:\s*(<[^>\r\n]+>)", head)
    return m.group(1).decode(errors="replace") if m else None


def addresses_to(raw: bytes) -> set[str]:
    from email import policy
    from email.parser import BytesParser

    msg = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
    return {a.lower() for _, a in email.utils.getaddresses(msg.get_all("To", []) + msg.get_all("Cc", []) + msg.get_all("Delivered-To", []))}
