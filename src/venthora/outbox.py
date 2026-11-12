# ruff: noqa: E501
"""The outbox: every email VENTHORA prepares for someone outside (a notice to a vendor), queued, then sent.

A message joins the queue first (status queued). If outgoing email is set up (Platform Setup -> Email Sync -> Outgoing
email: an SMTP server, its user and an app password, the sender address), it is sent at once and marked sent, or
failed with the server's answer (and can be sent again). Without SMTP it stays queued and the person sends it from
their own mail program (a prepared mailto link). Every message, sent or not, stays on the payment's log.
"""
from __future__ import annotations

import base64
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


def _resolved(body: dict) -> dict:
    """The form with the provider's server, port, security and user filled in."""
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
    return body | {"security": sec, "port": int(body.get("port") or 587), "user": (body.get("user") or "").strip(), "host": body["host"].strip()}


def try_smtp(body: dict) -> dict:
    """Test connect: sign in with the form's values (a blank password: the saved one); nothing is kept or sent."""
    s = _resolved(body)
    pw = body.get("password") or secret_values.get(PASSWORD)
    if s["user"] and not pw:
        return {"ok": False, "detail": "type the app password first"}
    try:
        conn = _connect(s, password=pw)
        conn.quit()
    except smtplib.SMTPAuthenticationError:
        return {"ok": False, "detail": f"{s['host']} refused the sign-in: check the address and the app password"}
    except smtplib.SMTPServerDisconnected:
        return {"ok": False, "detail": f"{s['host']} closed the connection at sign-in: usually a wrong app password, or sending (SMTP) is turned off for this mailbox"}
    except (OSError, smtplib.SMTPException) as exc:
        return {"ok": False, "detail": f"cannot reach {s['host']}:{s['port']}: {str(exc)[:160]}"}
    return {"ok": True, "detail": f"signed in to {s['host']}:{s['port']} as {s['user'] or 'anonymous'}"}


def save_smtp(org, body: dict, by: str) -> dict:
    """Outgoing email. With `provider` (a preset of email_settings.PRESETS other than "other") and `sender` the address,
    VENTHORA fills the server, port, security and user itself: the person types only the address and the app password."""
    body = _resolved(body)
    sec = body["security"]
    org._put("outbox_smtp", {"_id": "smtp", "host": body["host"].strip(), "port": int(body.get("port") or 587), "user": (body.get("user") or "").strip(),
                             "sender": body["sender"].strip(), "security": sec, "updated_by": by}, by)
    if body.get("password"):
        secret_values.put(PASSWORD, body["password"], by)
    return smtp(org)


def _connect(s: dict, timeout: float = 15.0, password: str | None = None):
    if s["security"] == "ssl":
        conn = smtplib.SMTP_SSL(s["host"], s["port"], timeout=timeout, context=ssl.create_default_context())
    else:
        conn = smtplib.SMTP(s["host"], s["port"], timeout=timeout)
        if s["security"] == "starttls":
            conn.starttls(context=ssl.create_default_context())
    if s["user"]:
        from . import mail_oauth

        secret = password if password is not None else secret_values.get(PASSWORD)
        if mail_oauth.is_oauth(secret):  # a kept sign-in (Connect with Microsoft/Google): XOAUTH2 with a fresh token
            try:
                token = mail_oauth.fresh(secret, (lambda v: secret_values.put(PASSWORD, v, "VENTHORA (sign-in renewed)")) if password is None else None)
            except mail_oauth.OAuthError as exc:
                conn.close()
                raise smtplib.SMTPAuthenticationError(535, f"the sign-in could not be renewed: {exc}".encode()) from exc
            conn.ehlo()
            code, resp = conn.docmd("AUTH", "XOAUTH2 " + base64.b64encode(mail_oauth.xoauth2(s["user"], token).encode()).decode())
            if code != 235:
                raise smtplib.SMTPAuthenticationError(code, resp)
        else:
            conn.login(s["user"], secret)
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
    m["X-Venthora-Notice"] = rec.get("kind") or "notice"  # the inbox reader skips VENTHORA's own mail (email_settings.fetch_new)
    m["X-Venthora-Outbox-Id"] = rec["_id"]  # an SES bounce or complaint names this message (apply_feedback)
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


def apply_feedback(org, *, kind: str, msg_id: str | None, addresses: list[str], reason: str, permanent: bool) -> dict | None:
    """An SES bounce or complaint for one of our messages (found by its X-Venthora-Outbox-Id, else by recipient).
    Permanent bounce: the message is "bounced" (never retried) until a person corrects the address; temporary bounce:
    "failed", so the worker retries it; complaint: "complained", never sent to that address again. Each address is kept
    as an email issue for the person to fix. Returns the message, or None when it is not ours."""
    rec = org.get("outbox", msg_id) if msg_id else None
    if rec is None and addresses:
        low = {a.lower() for a in addresses}
        mine = [r for r in org.list("outbox") if low & {t.lower() for t in r.get("to") or []} and r.get("status") == "sent"]
        rec = max(mine, key=lambda r: r.get("sent_at") or "", default=None)
    if rec is None:
        return None
    status = "complained" if kind == "Complaint" else ("bounced" if permanent else "failed")
    rec |= {"status": status, "error": f"{kind}{'' if kind == 'Complaint' else ' (permanent)' if permanent else ' (temporary)'}: {reason}"[:300],
            "feedback_at": _now(), "feedback_addresses": addresses}
    org._put("outbox", rec, "Amazon SES feedback")
    if permanent or kind == "Complaint":
        for a in addresses:
            issue = org.get("email_issue", a.lower()) or {"_id": a.lower(), "address": a, "messages": []}
            issue |= {"kind": kind, "reason": reason[:300], "at": _now(), "resolved": False,
                      "messages": sorted(set(issue.get("messages", [])) | {rec["_id"]})}
            org._put("email_issue", issue, "Amazon SES feedback")
    return rec


def readdress(org, msg_id: str, to: list[str], by: str) -> dict:
    """A person corrected the address of a bounced or failed message: it goes out again to the new address, and the
    old address's issue is marked resolved (who fixed it and to what)."""
    rec = org.get("outbox", msg_id)
    if rec is None:
        raise KeyError(msg_id)
    clean = [t.strip() for t in to if t and "@" in t and "." in t.split("@")[-1]]
    if not clean:
        raise ValueError("enter a valid email address")
    old = list(rec.get("to") or [])
    rec |= {"to": clean, "status": "queued", "error": None, "attempts": 0, "readdressed_from": old, "readdressed_by": by, "readdressed_at": _now()}
    org._put("outbox", rec, by)
    for a in old:
        issue = org.get("email_issue", a.lower())
        if issue and not issue.get("resolved"):
            org._put("email_issue", issue | {"resolved": True, "corrected_to": clean, "resolved_by": by, "resolved_at": _now()}, by)
    return deliver(org, msg_id)


def items(org, ref: str | None = None) -> list[dict]:
    rows = [r for r in org.list("outbox") if ref is None or r.get("ref") == ref]
    return sorted(rows, key=lambda r: r.get("created_at", ""), reverse=True)
