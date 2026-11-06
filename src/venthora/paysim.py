# ruff: noqa: E501
"""Demo-Only → the phone pay simulator: a cashier and a payer, each on a phone, paying with an advance's virtual card on
the Airwallex sandbox.

Cashier (opened with the cashier key from Sandbox simulator): names the shop, the amount, the currency and how the
person pays (QR scan, tap, card number), and shows a QR code. Payer: scans it, chooses their advance card (for "card
number" they type its last four digits) and pays. VENTHORA sends the charge to Airwallex's sandbox simulation; the
result shows on both phones and the charge lands in the Visa, FX and cash-advance reports like any other.

Each request is one payment: a random token, the amount fixed by the cashier, used once, expired after 15 minutes.
Demo mode on and the sandbox only (edition.is_demo_path; the charge itself is refused off the sandbox).
"""
from __future__ import annotations

import secrets
import time
from datetime import UTC, datetime

KIND, KEY = "paysim", "paysim_key"
MODES = {"qr": "QR scan", "contactless": "Tap", "card_number": "Card number"}
TTL_S = 15 * 60


def cashier_key(org, by: str, new: bool = False) -> str:
    rec = org.get(KEY, "key")
    if rec and not new:
        return rec["key"]
    key = secrets.token_urlsafe(16)
    org._put(KEY, {"_id": "key", "key": key, "at": _now(), "by": by}, by)
    return key


def key_ok(org, key: str | None) -> bool:
    rec = org.get(KEY, "key")
    return bool(rec and key and secrets.compare_digest(rec["key"], key))


def new_request(org, *, merchant: str, amount_minor: int, currency: str, mcc: str, mode: str) -> dict:
    tok = secrets.token_urlsafe(12)
    rec = {"_id": tok, "merchant": merchant.strip()[:40] or "Demo shop", "amount_minor": amount_minor, "currency": currency.upper(),
           "mcc": mcc or "5812", "mode": mode, "status": "WAITING", "created": time.time(), "at": _now(), "result": None}
    org._put(KIND, rec, "pay-sim cashier")
    return rec


def get(org, token: str) -> dict | None:
    rec = org.get(KIND, token) if token else None
    if rec and rec["status"] == "WAITING" and time.time() - rec["created"] > TTL_S:
        rec = rec | {"status": "EXPIRED"}
        org._put(KIND, rec, "pay-sim")
    return rec


def advance_cards(org) -> list[dict]:
    """The cards a payer may choose: advance cards in use (not frozen, their request still being spent)."""
    out = []
    for c in org.list("card"):
        fr = org.get("fund_request", c.get("fund_request") or "") if c.get("fund_request") else None
        if c.get("frozen") or not fr or fr.get("status") != "IN_PAYMENT":
            continue
        out.append({"card_id": c["_id"], "last4": str(c.get("masked") or "")[-4:], "label": c.get("payee"), "currency": c.get("currency"),
                    "limit_minor": c.get("all_time_minor") or c.get("per_tx_minor"), "request": fr["request_number"], "purpose": fr.get("purpose")})
    return out


def finish(org, token: str, result: dict) -> dict:
    rec = (org.get(KIND, token) or {}) | {"status": result["status"], "result": result, "paid_at": _now()}
    org._put(KIND, rec, "pay-sim payer")
    return rec


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
