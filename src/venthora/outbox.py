# ruff: noqa: E501
"""The outbox: every email VENTHORA prepares for someone outside (a notice to a vendor), queued, then sent.

A message joins the queue first (status queued). If outgoing email is set up (Platform Setup -> Email Sync -> Outgoing
email: an SMTP server, its user and an app password, the sender address), it is sent at once and marked sent, or
failed with the server's answer (and can be sent again). Without SMTP it stays queued and the person sends it from
their own mail program (a prepared mailto link). Every message, sent or not, stays on the payment's log.
"""
from __future__ import annotations

import datetime as dt
import smtplib
import ssl
import uuid
from email.message import EmailMessage

from . import secret_values

PASSWORD = "email.smtp.password"


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def smtp(org) -> dict:
    rec = org.get("outbox_smtp", "smtp") or {}
    return {"host": rec.get("host", ""), "port": rec.get("port", 587), "user": rec.get("user", ""), "sender": rec.get("sender", ""),
            "security": rec.get("security", "starttls"), "password": secret_values.info(PASSWORD),
            "configured": bool(rec.get("host") and rec.get("sender") and (not rec.get("user") or secret_values.info(PASSWORD)["set"]))}


def save_smtp(org, body: dict, by: str) -> dict:
    """Outgoing email. With `provider` (a preset of email_settings.PRESETS other than "other") and `sender` the address,
    VENTHORA fills the server, port, security and user itself: the person types only the address and the app password."""
    from .email_settings import PRESETS

    p = PRESETS.get(body.get("provider") or "")
    if p and p.get("smtp_host"):
        addr = (body.get("sender") or "").strip()
        body = body | {"host": p["smtp_host"], "port": p["smtp_port"], "security": p["smtp_security"], "user": addr, "sender": addr}
    sec = body.get("security") or "starttls"
    if sec not in ("starttls", "ssl", "none"):
        raise ValueError("security is starttls, ssl or none")
    if not body.get("host") or not body.get("sender") or "@" not in body.get("sender", ""):
        raise ValueError("the SMTP host and the sender address are needed")
    org._put("outbox_smtp", {"_id": "smtp", "host": body["host"].strip(), "port": int(body.get("port") or 587), "user": (body.get("user") or "").strip(),
                             "sender": body["sender"].strip(), "security": sec, "updated_by": by}, by)
    if body.get("password"):
        secret_values.put(PASSWORD, body["password"], by)
    return smtp(org)


def _connect(s: dict, timeout: float = 15.0):
    if s["security"] == "ssl":
        conn = smtplib.SMTP_SSL(s["host"], s["port"], timeout=timeout, context=ssl.create_default_context())
    else:
        conn = smtplib.SMTP(s["host"], s["port"], timeout=timeout)
        if s["security"] == "starttls":
            conn.starttls(context=ssl.create_default_context())
    if s["user"]:
        conn.login(s["user"], secret_values.get(PASSWORD))
    return conn


def check(org) -> dict:
    """Connect and log in to the SMTP server without sending anything."""
    s = smtp(org)
    if not s["configured"]:
        return {"ok": False, "detail": "outgoing email is not set up"}
    try:
        conn = _connect(s)
        conn.quit()
    except (OSError, smtplib.SMTPException) as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {str(exc)[:200]}"}
    return {"ok": True, "detail": f"connected to {s['host']}:{s['port']} as {s['user'] or 'anonymous'}"}


def queue(org, *, to: list[str], subject: str, body: str, ref: str, by: str, kind: str = "vendor_notice") -> dict:
    rec = {"_id": str(uuid.uuid4()), "kind": kind, "ref": ref, "to": to, "subject": subject[:200], "body": body[:8000], "status": "queued",
           "created_at": _now(), "created_by": by, "attempts": 0, "sent_at": None, "error": None}
    org._put("outbox", rec, by)
    return rec


def deliver(org, msg_id: str) -> dict:
    """Send one queued message over SMTP (when set up); the result is kept on the message."""
    rec = org.get("outbox", msg_id)
    if rec is None:
        raise KeyError(msg_id)
    s = smtp(org)
    if not s["configured"]:
        return rec
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = s["sender"], ", ".join(rec["to"]), rec["subject"]
    m.set_content(rec["body"])
    rec["attempts"] = rec.get("attempts", 0) + 1
    try:
        conn = _connect(s)
        conn.send_message(m)
        conn.quit()
        rec |= {"status": "sent", "sent_at": _now(), "error": None}
    except (OSError, smtplib.SMTPException) as exc:
        rec |= {"status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    org._put("outbox", rec, "VENTHORA (outbox)")
    return rec


MAX_ATTEMPTS = 5


def flush(org, max_attempts: int = MAX_ATTEMPTS) -> list[dict]:
    """The queue's worker: once outgoing email is set up, send every message still queued, and retry a failed one
    (at most max_attempts in all). Oldest first; one message's failure never stops the next."""
    if not smtp(org)["configured"]:
        return []
    done = []
    for rec in sorted(org.list("outbox"), key=lambda r: r.get("created_at", "")):
        if rec.get("status") in ("queued", "failed") and rec.get("attempts", 0) < max_attempts:
            done.append(deliver(org, rec["_id"]))
    return done


def items(org, ref: str | None = None) -> list[dict]:
    rows = [r for r in org.list("outbox") if ref is None or r.get("ref") == ref]
    return sorted(rows, key=lambda r: r.get("created_at", ""), reverse=True)
