# ruff: noqa: E501
"""E-wallet payouts (OVO, DANA, GoPay, ShopeePay, LinkAja) through Xendit: the one Indonesian rail Airwallex lacks.

Indonesian bank accounts are still paid by Airwallex (a bank channel is refused here, with that reason). The same
rules as every other VENTHORA payment hold:

  1. checked     the wallet's holder name is checked before anything else (Xendit's name check). MISMATCH or
                 NOT_FOUND refuses the request. When no name check is available, a person confirms the holder by a
                 call-back on the registered number (AWAITING_CALLBACK) - never paid unchecked
  2. approved    a second person approves (four eyes): the FCO (Super admin) or the CFO, never the requester
  3. sent once   the Idempotency-key is kept before the call; a retry returns the first payout
  4. proven      PAID only when Xendit says SUCCEEDED (read back, or its webhook); a FAILED payout says why

Statuses: REFUSED, AWAITING_CALLBACK, AWAITING_APPROVAL, SENT, PAID, FAILED, CANCELLED.
"""
from __future__ import annotations

import secrets
from datetime import UTC, datetime

from . import secret_values
from .gateways.xendit import WALLET_MAX_MINOR, WALLETS, Xendit, XenditError, channel_name, fingerprint, is_wallet

KIND = "wallet_payout"
APPROVERS = ("super_admin", "cfo")
SECRET, SECRET_ENV = "xendit_secret_key", "XENDIT_SECRET_KEY"
CALLBACK, CALLBACK_ENV = "xendit_callback_token", "XENDIT_CALLBACK_TOKEN"
STATUS_MAP = {"SUCCEEDED": "PAID", "FAILED": "FAILED", "REVERSED": "FAILED", "CANCELLED": "CANCELLED"}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ---- settings ----
def settings(org) -> dict:
    from .edition import demo_on

    mode = (org.get("xendit", "settings") or {}).get("mode", "off")
    demo = demo_on(org)
    effective = "off" if mode == "simulator" and not demo else mode
    return {"mode": mode, "effective_mode": effective, "demo": demo,
            "secret_key": secret_values.info(SECRET, SECRET_ENV), "callback_token": secret_values.info(CALLBACK, CALLBACK_ENV),
            "note": None if effective == mode else "the simulator runs in demo mode only: Xendit is off until demo mode is on or a live key is set"}


def save_settings(org, mode: str, by: str, secret_key: str | None = None, callback_token: str | None = None) -> dict:
    from .edition import demo_on

    if mode not in ("off", "simulator", "live"):
        raise ValueError("mode is off, simulator or live")
    if mode == "simulator" and not demo_on(org):
        raise ValueError("the simulator runs in demo mode only")
    if secret_key:
        if not secret_key.startswith("xnd_development_"):
            raise ValueError("only a Xendit test-mode key (xnd_development_...) is accepted: VENTHORA runs on sandboxes")
        secret_values.put(SECRET, secret_key.strip(), by)
    if callback_token:
        secret_values.put(CALLBACK, callback_token.strip(), by)
    if mode == "live" and not secret_values.get(SECRET, SECRET_ENV):
        raise ValueError("live needs a Xendit test-mode secret key")
    org._put("xendit", {"_id": "settings", "mode": mode}, by)
    return settings(org)


def client(org) -> Xendit:
    s = settings(org)
    return Xendit(s["effective_mode"], secret_values.get(SECRET, SECRET_ENV) if s["effective_mode"] == "live" else "", org)


def callback_ok(token: str | None) -> bool:
    want = secret_values.get(CALLBACK, CALLBACK_ENV)
    return bool(want) and secrets.compare_digest(want, token or "")


# ---- the payout request ----
def public(rec: dict) -> dict:
    """A payout as the dashboard shows it: never the full wallet number."""
    acct = rec.get("account_number") or ""
    return {k: v for k, v in rec.items() if k not in ("account_number", "idempotency_key")} | {"account_masked": "•••• " + acct[-4:] if acct else None}


async def create(org, x: Xendit, body: dict, by: str) -> dict:
    ch = (body.get("channel_code") or "").upper()
    if not is_wallet(ch):
        if ch.startswith("ID_"):
            raise ValueError(f"{channel_name(ch)} is an Indonesian bank: bank accounts are paid through Airwallex (SKN / BI-FAST), not here")
        raise ValueError(f"unknown e-wallet channel '{ch}': one of {', '.join(WALLETS)}")
    if x.mode == "off":
        raise ValueError("Xendit is switched off (Platform Setup → Xendit): e-wallet payouts are not available")
    acct = "".join(c for c in str(body.get("account_number") or "") if c.isdigit())
    if not 9 <= len(acct) <= 15:
        raise ValueError("the wallet number is the holder's registered mobile number (9 to 15 digits)")
    name = (body.get("account_holder_name") or "").strip()
    if len(name) < 3:
        raise ValueError("the holder's name, as registered on the wallet, is required")
    amount = int(body.get("amount_minor") or 0)
    if amount <= 0:
        raise ValueError("the amount must be above zero")
    if amount > WALLET_MAX_MINOR:
        raise ValueError(f"IDR {amount:,} is above what one e-wallet top-up may carry (IDR {WALLET_MAX_MINOR:,}): pay a bank account through Airwallex instead")
    rid = "WP-" + datetime.now(UTC).strftime("%y%m%d") + "-" + secrets.token_hex(3).upper()
    rec = {"_id": rid, "payout_number": rid, "channel_code": ch, "channel": channel_name(ch), "account_number": acct,
           "account_holder_name": name, "amount_minor": amount, "currency": "IDR", "purpose": (body.get("purpose") or "")[:200],
           "vendor_id": body.get("vendor_id"), "budget_line": body.get("budget_line"), "fund_request": body.get("fund_request"),
           "requested_by": by, "requested_at": _now(), "fingerprint": fingerprint(ch, acct, name), "provider": "xendit",
           "simulated": x.simulated, "history": []}
    try:
        chk = await x.check_name(ch, acct, name)
    except XenditError as exc:
        chk = {"available": False, "detail": str(exc)}
    rec["name_check"] = chk | {"at": _now()}
    if not chk.get("available"):
        _move(rec, "AWAITING_CALLBACK", by, "the holder's name could not be checked: call the holder on the registered number, then confirm")
    elif chk["result"] == "MATCH":
        _move(rec, "AWAITING_APPROVAL", by, f"the wallet is registered to {chk['resolved_name']}: a second person approves")
    elif chk["result"] == "MISMATCH":
        _move(rec, "REFUSED", by, f"NAME_MISMATCH: the wallet is registered to '{chk['resolved_name']}', not '{name}'")
    else:
        _move(rec, "REFUSED", by, "ACCOUNT_INVALID: no wallet is registered to this number")
    org._put(KIND, rec, by)
    return rec


def _move(rec: dict, status: str, by: str, why: str) -> None:
    rec["status"] = status
    rec["status_reason"] = why
    rec["history"].append({"at": _now(), "status": status, "by": by, "why": why})


def _get(org, pid: str) -> dict:
    rec = org.get(KIND, pid)
    if rec is None:
        raise LookupError(f"no e-wallet payout {pid}")
    return rec


def confirm_holder(org, pid: str, by: str, note: str = "") -> dict:
    rec = _get(org, pid)
    if rec["status"] != "AWAITING_CALLBACK":
        raise ValueError(f"a {rec['status'].lower()} payout needs no call-back")
    rec["callback"] = {"by": by, "at": _now(), "note": note[:200]}
    _move(rec, "AWAITING_APPROVAL", by, "the holder confirmed on a call-back to the registered number: a second person approves")
    org._put(KIND, rec, by)
    return rec


async def approve_and_send(org, x: Xendit, pid: str, role: str, approver: str, by: str) -> dict:
    rec = _get(org, pid)
    if rec["status"] != "AWAITING_APPROVAL":
        raise ValueError(f"a {rec['status'].lower()} payout cannot be approved")
    if role not in APPROVERS:
        raise PermissionError("an e-wallet payout is approved by the FCO (Super admin) or the CFO")
    if approver.strip().lower() == rec["requested_by"].split(" (")[0].strip().lower():
        raise PermissionError("the person who requested a payout cannot approve it (four eyes)")
    if fingerprint(rec["channel_code"], rec["account_number"], rec["account_holder_name"]) != rec["fingerprint"]:
        raise ValueError("the wallet details changed after they were checked: make a new request")
    rec["approved_by"], rec["approved_at"] = by, _now()
    rec.setdefault("idempotency_key", f"venthora-{rec['payout_number']}")  # kept before the call: a retry pays once
    org._put(KIND, rec, by)
    try:
        p = await x.create_payout(reference_id=rec["payout_number"], idempotency_key=rec["idempotency_key"], channel_code=rec["channel_code"],
                                  account_number=rec["account_number"], account_holder_name=rec["account_holder_name"],
                                  amount_minor=rec["amount_minor"], description=rec["purpose"] or rec["payout_number"])
    except XenditError as exc:
        _move(rec, "FAILED", by, f"Xendit refused the payout: {exc}")
        rec["failure_code"] = exc.code
        org._put(KIND, rec, by)
        return rec
    rec["xendit"] = p
    _move(rec, "SENT", by, f"Xendit accepted the payout ({p['status']}): PAID once the wallet is credited")
    org._put(KIND, rec, by)
    return rec


def apply_status(org, rec: dict, p: dict, by: str) -> dict:
    rec["xendit"] = p
    new = STATUS_MAP.get(p.get("status") or "")
    if new and rec["status"] == "SENT":
        why = {"PAID": "Xendit: SUCCEEDED, the wallet is credited",
               "FAILED": f"Xendit: {p.get('status')} ({p.get('failure_code') or 'no reason given'}); the money is back on the Xendit balance",
               "CANCELLED": "Xendit: CANCELLED before it was sent"}[new]
        _move(rec, new, by, why)
        if new == "PAID":
            rec["paid_at"] = p.get("updated") or _now()
        rec["failure_code"] = p.get("failure_code")
    org._put(KIND, rec, by)
    return rec


async def refresh(org, x: Xendit, pid: str, by: str) -> dict:
    rec = _get(org, pid)
    if not (rec.get("xendit") or {}).get("id"):
        return rec
    return apply_status(org, rec, await x.get_payout(rec["xendit"]["id"]), by)


async def cancel(org, x: Xendit, pid: str, by: str) -> dict:
    rec = _get(org, pid)
    if rec["status"] in ("AWAITING_CALLBACK", "AWAITING_APPROVAL"):
        _move(rec, "CANCELLED", by, "cancelled before it was sent")
        org._put(KIND, rec, by)
        return rec
    if rec["status"] != "SENT":
        raise ValueError(f"a {rec['status'].lower()} payout cannot be cancelled")
    return apply_status(org, rec, await x.cancel_payout(rec["xendit"]["id"]), by)


def by_reference(org, reference_id: str) -> dict | None:
    return org.get(KIND, reference_id)


def listing(org) -> list[dict]:
    return sorted(org.list(KIND), key=lambda r: r.get("requested_at", ""), reverse=True)
