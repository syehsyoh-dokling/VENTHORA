# ruff: noqa: E501
"""Transfers → a person moves money by hand (never the agent on its own), one form per route.

Every route follows the same path: REQUESTED (by a finance writer) → the destination is checked → AWAITING_APPROVAL →
APPROVED by the FCO (Super admin), never the person who asked → SENT (request_id kept first: a retry never sends
twice) → PAID / FAILED read back from Airwallex. Saved destinations (a Xendit or Midtrans top-up account, a bank
account) are checked once and reused.

Routes (the money always leaves the company's Airwallex account: the one this VENTHORA works for):
  airwallex      to another company's Airwallex account. A company on this VENTHORA platform: moved inside Airwallex
                 (no fee). Any other Airwallex customer: an Airwallex transfer to its Airwallex account number.
  card_account   to the card-issuing account (e.g. Malaysia) the Visa cards spend from: moved inside Airwallex, no fee
  virtual_card   to one Visa virtual card: the card account is funded and the card's limit raised by the amount
  airi_card      to the Airi card: not offered by Airwallex (no API funds an Airi card); refused with the reason
  id_bank        to a bank account in Indonesia (IDR, local transfer), name checked by Xendit where it can
  xendit         to the company's Xendit balance (its Xendit top-up account, IDR)
  midtrans       to the company's Midtrans balance (its Midtrans / Iris top-up account, IDR)
  international  to a bank account in any other country (local or SWIFT)
"""
from __future__ import annotations

import datetime as dt
import uuid

from . import bankcheck

KIND = "manual_transfer"
DEST = "transfer_destination"
ROUTES = {
    "airwallex": "Another company's Airwallex account",
    "card_account": "Card-issuing account (MyCard)",
    "virtual_card": "A Visa virtual card",
    "airi_card": "The Airi card",
    "id_bank": "A bank account in Indonesia",
    "xendit": "The company's Xendit balance",
    "midtrans": "The company's Midtrans balance",
    "international": "A bank account in another country",
}
BANK_ROUTES = ("id_bank", "xendit", "midtrans", "international")
APPROVERS = ("super_admin",)


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _hist(rec: dict, by: str, what: str) -> None:
    rec.setdefault("history", []).append({"at": _now(), "by": by, "what": what})


def bank_payload(dest: dict) -> dict:
    """Airwallex's beneficiary payload for a bank destination (the vendor builder, the destination's fields)."""
    from .masterdata import BankDetails, beneficiary_payload

    bank = BankDetails.model_validate(dest["bank"]).model_dump()
    return beneficiary_payload({"name": dest["bank"]["account_name"], "entity_type": dest.get("entity_type") or "COMPANY",
                                "payout_method": dest.get("payout_method") or "LOCAL", "transfer_reason": "business_expenses",
                                "bank": bank, "address": dest.get("address") or {}})


def wallet_payload(account_number: str, account_name: str) -> dict:
    return {"transfer_reason": "business_expenses", "payload": {"nickname": account_name[:50], "transfer_methods": ["LOCAL"], "payment_methods": ["LOCAL"],
            "beneficiary": {"type": "DIGITAL_WALLET", "digital_wallet": {"provider": "AIRWALLEX", "id_type": "account_number",
                                                                         "id_value": account_number.strip(), "account_name": account_name.strip()}}}}


def destinations(org, route: str | None = None) -> list[dict]:
    rows = [d for d in org.list(DEST) if not route or d["route"] == route]
    return sorted(rows, key=lambda d: d["label"])


def public_dest(d: dict) -> dict:
    out = dict(d)
    if out.get("bank"):
        b = dict(out["bank"])
        for k in ("account_number", "iban"):
            if b.get(k):
                b[k] = "•••• " + str(b[k])[-4:]
        out["bank"] = b
    out.pop("payload", None)
    return out


async def save_destination(org, bank, xendit, route: str, body: dict, by: str) -> dict:
    """Check a bank destination (format on Airwallex, name where a bank check exists) before it is kept; refused
    details are not kept."""
    if route not in BANK_ROUTES:
        raise ValueError("only bank destinations are saved")
    b = dict(body.get("bank") or {})
    if route in ("id_bank", "xendit", "midtrans"):
        b |= {"bank_country_code": "ID", "account_currency": "IDR"}
    if not b.get("account_name") or not (b.get("account_number") or b.get("iban")):
        raise ValueError("the account name and the account number (or IBAN) are required")
    label = (body.get("label") or f"{ROUTES[route]} · {b.get('bank_name') or b.get('bank_country_code')} {str(b.get('account_number') or b.get('iban'))[-4:]}")[:120]
    dest = {"_id": "D-" + uuid.uuid4().hex[:10].upper(), "route": route, "label": label, "bank": b, "address": body.get("address") or {},
            "entity_type": body.get("entity_type") or "COMPANY", "payout_method": body.get("payout_method") or "LOCAL", "saved_by": by, "saved_at": _now()}
    p = bank_payload(dest)
    res = await bankcheck.run(bank, p, xendit)
    if res["verdict"] in bankcheck.REFUSED:
        raise ValueError(f"the account is refused ({res['verdict']}): {res['detail']}")
    bankcheck.save(org, f"dest:{dest['_id']}", res, by)
    dest["bank_check"] = {k: res.get(k) for k in ("verdict", "detail", "at", "resolved_account_name", "name_check_by")}
    org._put(DEST, dest, by)
    return dest


def create(org, *, route: str, amount_minor: int, currency: str, by: str, purpose: str, destination: dict) -> dict:
    if route not in ROUTES:
        raise ValueError("unknown route")
    if route == "airi_card":
        raise ValueError("Airwallex offers no way to fund the Airi card: Airi spends from the card linked to it. Use a Visa virtual card, "
                         "or ask Airwallex for the Airi card's funding (Developer Console → Airi)")
    if amount_minor <= 0:
        raise ValueError("the amount must be above zero")
    rec = {"_id": "TR-" + dt.datetime.now(dt.UTC).strftime("%y%m%d") + "-" + uuid.uuid4().hex[:6].upper(), "route": route, "route_label": ROUTES[route],
           "amount_minor": int(amount_minor), "currency": currency.upper(), "purpose": purpose[:200], "destination": destination,
           "status": "AWAITING_APPROVAL", "requested_by": by, "requested_at": _now(), "request_id": str(uuid.uuid4())}
    _hist(rec, by, f"requested: {ROUTES[route]}, {currency.upper()} {amount_minor / 100:,.2f}")
    org._put(KIND, rec, by)
    return rec


def get(org, tid: str) -> dict:
    rec = org.get(KIND, tid)
    if rec is None:
        raise LookupError(f"no transfer {tid}")
    return rec


def approve_check(rec: dict, role: str, approver: str) -> None:
    if rec["status"] != "AWAITING_APPROVAL":
        raise ValueError(f"a {rec['status'].lower().replace('_', ' ')} transfer cannot be approved")
    if role not in APPROVERS:
        raise PermissionError("a manual transfer is approved by the FCO (Super admin)")
    if approver.strip().lower() == rec["requested_by"].split(" (")[0].strip().lower():
        raise PermissionError("the person who asked for a transfer cannot approve it (four eyes)")


def mark(org, rec: dict, status: str, by: str, what: str, **extra) -> dict:
    rec |= {"status": status} | extra
    _hist(rec, by, what)
    org._put(KIND, rec, by)
    return rec


def listing(org) -> list[dict]:
    return sorted(org.list(KIND), key=lambda r: r["requested_at"], reverse=True)
