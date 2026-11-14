# ruff: noqa: E501
"""FCO Authorize -> notices to the FCO by email, one per request.

- A request that needs a person (held for review or quarantined): one email with what stopped it and a link to it.
- A first payment to a newly verified payee (it always waits for a person): one email with an Approve & pay link.
  The link opens a page on VENTHORA that shows the payment again and asks for one more click: a mail scanner that
  opens links never approves anything. The link works once, for 72 hours, and only for that payment; the payment still
  passes every check, the reserve floor and the limits when it is approved.

Sent through the company's own sender (Email Sync, step 3); a cap per hour stops a flood. Simulated invoices are
never mailed unless the FCO asks for them.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets

KIND, LINK, SECRET_ID = "fco_notify", "approve_link", "approve_secret"
DEFAULTS = {"enabled": True, "to": "", "needs_review": True, "first_payment": True, "include_simulations": False, "max_per_hour": 20, "vendor_feedback": True}
LINK_HOURS = 72


def settings(org) -> dict:
    rec = org.get(KIND, "settings") or {}
    out = DEFAULTS | {k: rec[k] for k in DEFAULTS if k in rec}
    if not out["to"]:
        out["to"] = fco_email(org) or ""
    return out | {"updated_by": rec.get("updated_by"), "updated_at": rec.get("updated_at")}


def save(org, body: dict, by: str) -> dict:
    cur = settings(org)
    new = {k: body.get(k, cur[k]) for k in DEFAULTS}
    new["to"] = str(new["to"] or "").strip()
    if new["to"] and ("@" not in new["to"] or " " in new["to"]):
        raise ValueError("the FCO's email address is not valid")
    new["max_per_hour"] = max(1, min(int(new["max_per_hour"] or 20), 200))
    for k in ("enabled", "needs_review", "first_payment", "include_simulations", "vendor_feedback"):
        new[k] = bool(new[k])
    org._put(KIND, {"_id": "settings"} | new | {"updated_by": by, "updated_at": _now()}, by)
    return settings(org)


def fco_email(org) -> str | None:
    """The mandate authority's address: the active super admin (the FCO)."""
    users = [u for u in org.list("user") if u.get("active", True)]
    fco = next((u for u in users if u.get("role") == "super_admin" and u.get("email")), None)
    return fco.get("email") if fco else None


def fco_name(org) -> str:
    users = [u for u in org.list("user") if u.get("active", True)]
    fco = next((u for u in users if u.get("role") == "super_admin"), None)
    return (fco or {}).get("name") or "FCO"


def _secret(org) -> bytes:
    rec = org.get(KIND, SECRET_ID)
    if rec is None:
        rec = {"_id": SECRET_ID, "value": secrets.token_hex(32), "created_at": _now()}
        org._put(KIND, rec, "VENTHORA (notices)")
    return bytes.fromhex(rec["value"])


def make_link(org, intake_id: str, kind: str = "first_payment") -> str:
    """A one-time token for this request: nonce + HMAC; kept with its expiry."""
    nonce = secrets.token_urlsafe(12)
    sig = hmac.new(_secret(org), f"{nonce}|{intake_id}".encode(), hashlib.sha256).hexdigest()[:20]
    token = f"{nonce}{sig}"
    exp = (dt.datetime.now(dt.UTC) + dt.timedelta(hours=LINK_HOURS)).isoformat(timespec="seconds")
    org._put(LINK, {"_id": token, "intake_id": intake_id, "kind": kind, "expires_at": exp, "created_at": _now(), "used_at": None}, "VENTHORA (notices)")
    return token


def check_link(org, token: str) -> dict:
    """The link record when the token is genuine, unused and not expired; ValueError (with the reason) otherwise."""
    rec = org.get(LINK, token)
    if rec is None or len(token) < 30:
        raise LookupError("unknown link")
    nonce, sig = token[:-20], token[-20:]
    good = hmac.new(_secret(org), f"{nonce}|{rec['intake_id']}".encode(), hashlib.sha256).hexdigest()[:20]
    if not hmac.compare_digest(sig, good):
        raise LookupError("unknown link")
    if rec.get("used_at"):
        raise ValueError(f"this link was already used on {rec['used_at'][:16].replace('T', ' ')} UTC")
    if dt.datetime.fromisoformat(rec["expires_at"]) < dt.datetime.now(dt.UTC):
        raise ValueError("this link has expired: approve the payment in VENTHORA (Pending payment)")
    return rec


def use_link(org, token: str, result: dict) -> None:
    rec = org.get(LINK, token) or {"_id": token}
    org._put(LINK, rec | {"used_at": _now(), "result": result}, "VENTHORA (notices)")


def sent_last_hour(org) -> int:
    since = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)).isoformat()
    return sum(1 for r in org.list("outbox") if r.get("kind") == "fco_notice" and r.get("created_at", "") >= since)


def already(org, intake_id: str, kind: str) -> bool:
    return any(r.get("kind") == "fco_notice" and r.get("ref") == f"{kind}:{intake_id}" for r in org.list("outbox"))


def compose(req, kind: str, public_url: str, token: str | None, payee: dict | None) -> tuple[str, str]:
    """(subject, body) of one notice, in plain words."""
    who = req.counterparty_name or req.claimed_name or req.display_name or req.sender
    amt = f"{req.currency or ''} {(req.amount_minor or 0) / 100:,.2f}".strip()
    head = f"{who} · {req.invoice_no or req.subject[:60]} · {amt}"
    detail = (f"From: {req.display_name} <{req.sender}>\nInvoice: {req.invoice_no or '-'}\nAmount: {amt}\nDue: {req.due_date or '-'}\n"
              f"Purchase order: {req.po_number or '-'}\nReceived: {req.received_at[:16].replace('T', ' ')} UTC\n")
    open_link = f"{public_url}/#/payment?id={req.intake_id}"
    if kind == "first_payment":
        subject = f"[VENTHORA] Approve first payment: {head}"
        by = f" ({payee['verified_by']})" if payee and payee.get("verified_by") else ""
        body = (f"Dear {payee.get('fco_name') if payee else 'FCO'},\n\nThis is the first payment to {who}. Every check passed and the payee's bank account was "
                f"confirmed by call-back{by}. A first payment always waits for a person.\n\n{detail}\n"
                f"Approve & pay (opens a confirmation page; the link works once, for {LINK_HOURS} hours):\n{public_url}/approve/{token}\n\n"
                f"Not sure? Do nothing: it stays in Pending payment. Open it in VENTHORA: {open_link}\n\n"
                "VENTHORA still checks the balance, the reserve floor and the limits before Airwallex is called.\n")
    else:
        why = "\n".join(f"- {r}" for r in (req.reasons or [])[:6]) or "- held for a person"
        subject = f"[VENTHORA] {'Quarantined' if req.status == 'QUARANTINED' else 'Needs review'}: {head}"
        body = (f"Dear FCO,\n\nVENTHORA did not pay this request. It needs a person:\n{why}\n\n{detail}\n"
                f"Open it to correct, verify or reject it:\n{open_link}\n\nNothing is paid until a person decides.\n")
    return subject, body


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
