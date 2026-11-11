# ruff: noqa: E501
"""Platform Setup -> Airwallex Cards and Card Authorization: vendors paid by card, under the same mandate as transfers.

* Airwallex Cards: one company cardholder, then one Visa virtual card per card-billed vendor (a software
  subscription), with Airwallex spending controls set from the mandate: per transaction and per month, never above
  the mandate's "single payout on its own" limit; the vendor's merchant category when known. VENTHORA keeps the card
  id and the masked number, never the full card number. In the sandbox a merchant charge is simulated.
* Card Authorization: Airwallex asks VENTHORA, within 2.5 s, whether each charge may go (remote authorization). The
  request is signed: HMAC-SHA256 of the x-nonce header with the shared secret, base64, in x-signature; the nonce starts
  with the epoch time in milliseconds. VENTHORA answers from the mandate alone (no model, no network call): a card it
  issued and has not frozen, the amount inside the card's per-transaction and monthly limits and the mandate's single
  payout limit, the vendor's merchant when the card names one, a charge not already decided. Every decision is kept
  with its reason (Card Authorization, and the payment's transaction log).
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import time
import uuid
from decimal import Decimal

SECRET = "issuing.remote_auth.shared_secret"
NONCE_TOLERANCE_MS = 5 * 60 * 1000
PURPOSES = ("SUBSCRIPTIONS", "ONLINE_PURCHASING", "BUSINESS_EXPENSES", "OFFICE_SUPPLIES", "TRAVEL_EXPENSES", "OTHER")


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def major(minor: int) -> str:
    return f"{Decimal(minor) / 100:.2f}"


def to_minor(v) -> int:
    return int((Decimal(str(v or 0)) * 100).to_integral_value())


# -- the mandate ceiling ---------------------------------------------------------------------------------------------

def mandate_ceiling(org) -> int | None:
    """The mandate's 'single payout on its own' limit (USD minor), the ceiling of every card limit; None when revoked."""
    from . import mandate
    from .live import MANDATES

    st = mandate.standing(org) if org is not None else {"values": {}, "enabled": {}}
    spec = next(m for m in MANDATES if m["key"] == "pay_single")
    if not st["enabled"].get("pay_single", True):
        return None
    return int(st["values"].get("pay_single", spec["value"]))


# -- cardholder and cards --------------------------------------------------------------------------------------------

def cardholder_body(f: dict) -> dict:
    """A DELEGATE cardholder: the company's card user (the person who answers for the cards)."""
    addr = f.get("address") or {}
    return {"type": "DELEGATE", "email": f["email"], "mobile_number": f.get("mobile_number") or None,
            "individual": {"name": {"first_name": f["first_name"], "last_name": f["last_name"]}, "date_of_birth": f["date_of_birth"],
                           "express_consent_obtained": "yes",
                           "address": {"line1": addr.get("line1"), "city": addr.get("city"), "postcode": addr.get("postcode"),
                                       "country": addr.get("country"), **({"state": addr["state"]} if addr.get("state") else {})}}}


def card_body(*, cardholder_id: str, payee: dict, per_tx_minor: int, monthly_minor: int, currency: str, by: str,
              purpose: str = "SUBSCRIPTIONS", mcc: list[str] | None = None, active_to: str | None = None,
              all_time_minor: int | None = None, nick_name: str | None = None) -> dict:
    limits = [{"amount": float(major(per_tx_minor)), "interval": "PER_TRANSACTION"}, {"amount": float(major(monthly_minor)), "interval": "MONTHLY"}]
    if all_time_minor:  # an advance: never more than the advance over the card's whole life
        limits.append({"amount": float(major(all_time_minor)), "interval": "ALL_TIME"})
    controls = {"allowed_transaction_count": "MULTIPLE", "transaction_limits": {"currency": currency, "limits": limits}}
    if mcc:
        controls["allowed_merchant_categories"] = mcc
    if active_to:
        controls["active_to"] = active_to
    return {"request_id": str(uuid.uuid4()), "cardholder_id": cardholder_id, "created_by": by[:60], "form_factor": "VIRTUAL",
            "is_personalized": False, "program": {"purpose": "COMMERCIAL"}, "purpose": purpose if purpose in PURPOSES else "OTHER",
            "brand": "VISA", "nick_name": (nick_name or f"VENTHORA · {payee['name']}")[:40], "authorization_controls": controls,
            "metadata": {"payee_id": payee["_id"], "issued_by": "VENTHORA"}}


def check_limits(org, per_tx_minor: int, monthly_minor: int) -> None:
    ceiling = mandate_ceiling(org)
    if ceiling is None:
        raise ValueError("the mandate revokes payouts on the agent's own: a card cannot be issued for it")
    if per_tx_minor <= 0 or monthly_minor <= 0:
        raise ValueError("the limits must be above zero")
    if per_tx_minor > ceiling:
        raise ValueError(f"the per-transaction limit is above the mandate's single payout limit (USD {major(ceiling)})")
    from . import mandate

    cmax = (mandate.standing(org).get("book") or {}).get("card_max_minor", mandate.BOOK_DEFAULTS["card_max_minor"])
    if cmax is not None and per_tx_minor > cmax:
        raise ValueError("the FCO allows no virtual cards: pay it by bank transfer" if not cmax else
                         f"above the FCO's card limit (USD {major(cmax)}, FCO Authorize → Delegation of Authority): pay it by bank transfer")
    if monthly_minor < per_tx_minor:
        raise ValueError("the monthly limit cannot be below the per-transaction limit")


def record_card(org, resp: dict, *, payee: dict, per_tx_minor: int, monthly_minor: int, currency: str, merchant: str, mcc: list[str], by: str) -> dict:
    rec = {"_id": resp["card_id"], "payee_id": payee["_id"], "payee": payee["name"], "masked": resp.get("card_number"), "brand": resp.get("brand"),
           "status": resp.get("card_status"), "per_tx_minor": per_tx_minor, "monthly_minor": monthly_minor, "currency": currency,
           "merchant": merchant, "mcc": mcc, "frozen": False, "issued_at": _now(), "issued_by": by}
    org._put("card", rec, by)
    return rec


# -- remote authorization ---------------------------------------------------------------------------------------------

INTERVALS = {"PER_TRANSACTION": "per_tx_minor", "MONTHLY": "monthly_minor"}


def compare_limits(card: dict, live: dict, ceiling: int | None) -> dict:
    """Airwallex's live limits beside what VENTHORA issued: a limit raised or added on Airwallex outside VENTHORA, or
    one above the mandate ceiling, is drift a person must see (the remote authorization still answers from the mandate)."""
    ccy = (live.get("currency") or card.get("currency") or "").upper()
    rows, drift = [], []
    for lim in live.get("limits") or []:
        interval = str(lim.get("interval") or "").upper()
        amount, remaining = to_minor(lim.get("amount") or 0), lim.get("remaining")
        ours = card.get(INTERVALS[interval]) if interval in INTERVALS else None
        row = {"interval": interval, "amount_minor": amount, "remaining_minor": to_minor(remaining) if remaining is not None else None,
               "venthora_minor": ours, "currency": ccy}
        if ours is None:
            row["drift"] = f"{interval.lower().replace('_', ' ')} limit set on Airwallex, not by VENTHORA"
        elif amount > ours:
            row["drift"] = f"Airwallex allows {ccy} {major(amount)}, VENTHORA issued {ccy} {major(ours)}"
        if ceiling is not None and amount > ceiling:
            row["drift"] = (row.get("drift", "") + "; " if row.get("drift") else "") + f"above the mandate ceiling {major(ceiling)}"
        if row.get("drift"):
            drift.append(row["drift"])
        rows.append(row)
    seen = {r["interval"] for r in rows}
    for interval, key in INTERVALS.items():
        if card.get(key) and interval not in seen:
            drift.append(f"VENTHORA issued a {interval.lower().replace('_', ' ')} limit that Airwallex does not show")
    return {"card_id": card["_id"], "currency": ccy, "limits": rows, "drift": drift, "ok": not drift}


def sign(nonce: str, secret: str) -> str:
    return base64.b64encode(hmac.new(secret.encode(), nonce.encode(), hashlib.sha256).digest()).decode()


def new_nonce() -> str:
    return f"{int(time.time() * 1000)}.{uuid.uuid4().hex}"


def verify(nonce: str | None, signature: str | None, secret: str, now_ms: int | None = None) -> tuple[bool, str]:
    if not secret:
        return False, "no shared secret is configured"
    if not nonce or not signature:
        return False, "the request is not signed"
    if not hmac.compare_digest(sign(nonce, secret), signature.strip()):
        return False, "the signature does not match"
    try:
        stamp = int(nonce.split(".", 1)[0])
    except ValueError:
        return False, "the nonce carries no timestamp"
    if abs((now_ms or int(time.time() * 1000)) - stamp) > NONCE_TOLERANCE_MS:
        return False, "the request is too old"
    return True, "signed by Airwallex"


def _month_spent(org, card_id: str) -> int:
    month = dt.datetime.now(dt.UTC).strftime("%Y-%m")
    return sum(d["amount_minor"] for d in org.list("card_auth")
               if d.get("card_id") == card_id and d.get("decision") == "AUTHORIZED" and str(d.get("at", "")).startswith(month))


def decide(org, req: dict) -> tuple[str, str]:
    """AUTHORIZED or DECLINED, with the reason Airwallex stores on the transaction."""
    card = org.get("card", req.get("card_id") or "")
    if card is None:
        return "DECLINED", "card not issued by VENTHORA"
    if card.get("frozen"):
        return "DECLINED", "card frozen in VENTHORA"
    life = req.get("card_transaction_lifecycle_id") or req.get("transaction_id")
    if life and req.get("transaction_type", "AUTHORIZATION") == "AUTHORIZATION" and any(
            d.get("lifecycle_id") == life and d.get("decision") == "AUTHORIZED" and d.get("type") == "AUTHORIZATION"
            and d.get("_id") != (req.get("card_transaction_event_id") or req.get("transaction_id")) for d in org.list("card_auth")):
        return "DECLINED", "this charge was already authorized"
    ccy = req.get("transaction_currency")
    amount = to_minor(req.get("transaction_amount"))
    if ccy != card["currency"]:  # the amount in the card's currency, as Airwallex bills it
        billed = next((b for b in (req.get("billing_order") or []) if b.get("currency") == card["currency"]), None)
        if billed is None:
            return "DECLINED", f"charge in {ccy} with no amount in the card currency {card['currency']}"
        amount = to_minor(billed.get("amount"))
    if amount > card["per_tx_minor"]:
        return "DECLINED", f"above the card's per-transaction limit of {card['currency']} {major(card['per_tx_minor'])}"
    ceiling = mandate_ceiling(org)
    if ceiling is None:
        return "DECLINED", "the mandate revokes payouts on the agent's own"
    if card["currency"] == "USD" and amount > ceiling:
        return "DECLINED", f"above the mandate's single payout limit of USD {major(ceiling)}"
    if _month_spent(org, card["_id"]) + amount > card["monthly_minor"]:
        return "DECLINED", f"above the card's monthly limit of {card['currency']} {major(card['monthly_minor'])}"
    merchant = ((req.get("merchant") or {}).get("name") or "").lower()
    if card.get("merchant") and card["merchant"].lower() not in merchant:
        return "DECLINED", f"merchant '{(req.get('merchant') or {}).get('name') or '-'}' is not the card's vendor ({card['merchant']})"
    if card.get("mcc") and str((req.get("merchant") or {}).get("category_code") or "") not in card["mcc"]:
        return "DECLINED", "merchant category not allowed on this card"
    return "AUTHORIZED", f"inside the mandate: {card['payee']}, {card['currency']} {major(amount)}"


def record_decision(org, req: dict, decision: str, reason: str, *, source: str) -> dict:
    card = org.get("card", req.get("card_id") or "") or {}
    rec = {"_id": req.get("card_transaction_event_id") or req.get("transaction_id") or str(uuid.uuid4()), "at": _now(),
           "card_id": req.get("card_id"), "payee": card.get("payee"), "payee_id": card.get("payee_id"),
           "amount_minor": to_minor(req.get("transaction_amount")), "currency": req.get("transaction_currency"),
           "merchant": (req.get("merchant") or {}).get("name"), "mcc": (req.get("merchant") or {}).get("category_code"),
           "type": req.get("transaction_type", "AUTHORIZATION"), "lifecycle_id": req.get("card_transaction_lifecycle_id") or req.get("transaction_id"),
           "decision": decision, "reason": reason, "source": source}
    org._put("card_auth", rec, "VENTHORA (card authorization)")
    return rec


def answer(req: dict, decision: str, reason: str) -> dict:
    """The response Airwallex expects: the request's identifier echoed, the decision and the reason."""
    if int(req.get("version") or 2) >= 2:
        return {"card_transaction_event_id": req.get("card_transaction_event_id"), "response_status": decision, "status_reason": reason}
    return {"transaction_id": req.get("transaction_id"), "response_status": decision, "status_reason": reason}


def test_request(card: dict, amount_minor: int, currency: str, merchant: str, mcc: str | None = None) -> dict:
    """A version-2 authorization request as Airwallex would send it (for the local test on Card Authorization)."""
    life = f"test-{uuid.uuid4().hex[:12]}"
    return {"version": 2, "card_id": card["_id"], "card_transaction_event_id": f"evt-{uuid.uuid4().hex[:12]}", "card_transaction_id": life,
            "card_transaction_lifecycle_id": life, "transaction_type": "AUTHORIZATION", "transaction_amount": float(major(amount_minor)),
            "transaction_currency": currency, "merchant": {"name": merchant, "category_code": mcc or ""}, "masked_card_number": card.get("masked")}
