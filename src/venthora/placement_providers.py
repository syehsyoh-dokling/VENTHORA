# ruff: noqa: E501
"""FCO Authorize -> Placement providers: where idle cash may be placed, and how it goes there and comes back.

A provider is a term deposit at a bank, a money market fund, or Airwallex Yield. The FCO keeps the list; a provider
reached by bank transfer has its account checked first (bankcheck.py, with Xendit's name check for an Indonesian
account) and the account must be the company's own (a deposit) or the fund's custodian account (a fund).

A placement with a provider:
  PROPOSED  VENTHORA (or a person) proposes it, never above the idle cash (cash_placement.py)
  APPROVED  the FCO approves, never the person who proposed it
  SENT      at once an Airwallex transfer to the provider's checked account; its request_id is kept first, so a
            retry never sends twice
  PLACED    Airwallex says PAID
  WITHDRAWAL_REQUESTED  the FCO asks for the money back. Airwallex cannot pull money from another bank: the
            redemption is sent to the provider (its portal, an email, its API) with VENTHORA's reference, and the
            provider pays into the Airwallex Global Account
  CLOSED    the deposit that came back is matched to the placement (reference, currency, amount); what came back
            above the principal is the return, kept as income
  FAILED / CANCELLED

Airwallex Yield needs no transfer (the money stays at Airwallex): the FCO moves it in the Airwallex app, marks it
done, and closes it with what came back.
"""
from __future__ import annotations

import datetime as dt
import re
import uuid

from . import bankcheck

KIND = "placement_provider"
KINDS = ("TERM_DEPOSIT", "MONEY_MARKET_FUND", "AIRWALLEX_YIELD")
HOLDERS = ("COMPANY_OWN", "FUND_CUSTODIAN")
REDEEM = ("PORTAL", "EMAIL", "API", "AIRWALLEX_APP")


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _slug(name: str) -> str:
    return "pp-" + (re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or uuid.uuid4().hex[:8])


def check_key(pid: str) -> str:
    """The bank check of a provider is kept apart from the vendors' (its own key)."""
    return f"placement:{pid}"


def payload(p: dict) -> dict | None:
    """Airwallex's beneficiary payload for a provider's account (the vendor builder, the provider's fields)."""
    from .masterdata import BankDetails, beneficiary_payload

    if p["kind"] == "AIRWALLEX_YIELD" or not p.get("bank"):
        return None
    bank = BankDetails.model_validate(p["bank"]).model_dump()
    return beneficiary_payload({"name": p["bank"]["account_name"], "entity_type": "COMPANY", "payout_method": p.get("payout_method", "LOCAL"),
                                "transfer_reason": "business_expenses","bank": bank, "address": p.get("address") or {}})


def validate(org, body: dict) -> dict:
    """A provider record from the form, refused with the reason when something is wrong (nothing is checked yet)."""
    name = (body.get("name") or "").strip()
    if len(name) < 2:
        raise ValueError("the provider's name is required")
    kind = body.get("kind")
    if kind not in KINDS:
        raise ValueError(f"kind is one of {', '.join(KINDS)}")
    ccy = (body.get("currency") or "").upper()
    if not re.fullmatch(r"[A-Z]{3}", ccy):
        raise ValueError("the currency is a 3-letter code")
    tenor = int(body.get("tenor_days") or 0)
    if kind == "TERM_DEPOSIT" and not 1 <= tenor <= 1830:
        raise ValueError("a term deposit has a tenor in days (1 to 1830)")
    rate = float(body.get("indicative_rate_pct") or 0)
    if not 0 <= rate <= 50:
        raise ValueError("the indicative rate is a yearly percentage between 0 and 50")
    rec = {"provider_id": body.get("provider_id") or _slug(name), "name": name[:120], "kind": kind, "currency": ccy,
           "tenor_days": tenor if kind == "TERM_DEPOSIT" else 0, "indicative_rate_pct": rate,
           "minimum_minor": max(int(body.get("minimum_minor") or 0), 0), "redeem_by": body.get("redeem_by") or ("AIRWALLEX_APP" if kind == "AIRWALLEX_YIELD" else "PORTAL"),
           "redeem_contact": (body.get("redeem_contact") or "")[:200], "our_reference": (body.get("our_reference") or "")[:80],
           "note": (body.get("note") or "")[:300], "active": bool(body.get("active", True))}
    if rec["redeem_by"] not in REDEEM:
        raise ValueError(f"redeem_by is one of {', '.join(REDEEM)}")
    if kind == "AIRWALLEX_YIELD":
        return rec | {"bank": None, "account_holder": None, "address": None, "payout_method": None}
    bank = body.get("bank") or {}
    holder = body.get("account_holder") or ("COMPANY_OWN" if kind == "TERM_DEPOSIT" else "FUND_CUSTODIAN")
    if holder not in HOLDERS:
        raise ValueError(f"account_holder is one of {', '.join(HOLDERS)}")
    if (bank.get("account_currency") or ccy).upper() != ccy:
        raise ValueError("the account's currency is the provider's currency")
    company = org.standards().get("company_name", "")
    if holder == "COMPANY_OWN" and _norm(bank.get("account_name")) != _norm(company):
        raise ValueError(f"a deposit goes to the company's own account: the account name must be '{company}' (Finance standards), not '{bank.get('account_name')}'")
    if holder == "FUND_CUSTODIAN" and not rec["our_reference"]:
        raise ValueError("a fund's custodian account needs our investor or account number at the fund, sent as the transfer reference")
    return rec | {"bank": bank | {"account_currency": ccy}, "account_holder": holder, "address": body.get("address") or {},
                  "payout_method": body.get("payout_method") or "LOCAL"}


def _norm(s) -> str:
    return " ".join(re.sub(r"[^A-Z0-9 ]", " ", str(s or "").upper()).split())


async def save(org, bank, xendit, body: dict, by: str) -> dict:
    """Keep a provider; its account is checked first and a refused account is not kept."""
    rec = validate(org, body)
    old = org.get(KIND, rec["provider_id"]) or {}
    p = payload(rec)
    if p is not None:
        res = await bankcheck.run(bank, p, xendit)
        if res["verdict"] in bankcheck.REFUSED:
            raise ValueError(f"the account is refused ({res['verdict']}): {res['detail']}")
        bankcheck.save(org, check_key(rec["provider_id"]), res, by)
        same = old.get("bank") == rec["bank"]
        rec["airwallex_beneficiary_id"] = old.get("airwallex_beneficiary_id") if same else None  # new details: a new beneficiary
        rec["bank_check"] = {k: res.get(k) for k in ("verdict", "detail", "at", "resolved_account_name", "name_check_by")}
    rec |= {"_id": rec["provider_id"], "created_at": old.get("created_at") or _now(), "updated_at": _now(), "updated_by": by}
    org._put(KIND, rec, by)
    return rec


def public(org, p: dict) -> dict:
    out = dict(p)
    if out.get("bank"):
        b = dict(out["bank"])
        for k in ("account_number", "iban"):
            if b.get(k):
                b[k] = "•••• " + str(b[k])[-4:]
        out["bank"] = b
        ok, why = bankcheck.passed(org, check_key(p["provider_id"]), payload(p))
        out["payable"], out["payable_why"] = ok, why
    else:
        out["payable"], out["payable_why"] = True, "Airwallex Yield: the money stays at Airwallex"
    return out


def listing(org) -> list[dict]:
    return sorted(org.list(KIND), key=lambda r: (not r.get("active", True), r["name"]))


def get(org, pid: str) -> dict:
    p = org.get(KIND, pid)
    if p is None:
        raise LookupError(f"no placement provider {pid}")
    return p


def best(org, currency_ok, amount_minor_of) -> dict | None:
    """The provider VENTHORA proposes: active, its account checked, its currency held, its minimum met; the highest
    indicative rate (the FCO's own list and rates: VENTHORA only sorts them)."""
    rows = []
    for p in listing(org):
        if not p.get("active", True) or not currency_ok(p["currency"]):
            continue
        if p["kind"] != "AIRWALLEX_YIELD" and not bankcheck.passed(org, check_key(p["provider_id"]), payload(p))[0]:
            continue
        if amount_minor_of(p["currency"]) < p.get("minimum_minor", 0):
            continue
        rows.append(p)
    return max(rows, key=lambda p: p.get("indicative_rate_pct", 0), default=None)


# ---- the placement's own life after approval ----
def reference(rec: dict) -> str:
    return "VNT-PL-" + rec["_id"].replace("-", "")[:8].upper()


async def send(org, bank, rec: dict, by: str) -> dict:
    """The approved placement goes to the provider: one Airwallex transfer to its checked account."""
    from .gateways.airwallex import AirwallexError

    p = get(org, rec["provider_id"])
    pay = payload(p)
    ok, why = bankcheck.passed(org, check_key(p["provider_id"]), pay)
    if not ok:
        return _fail(org, rec, by, f"not sent: {why}")
    rec.setdefault("request_id", str(uuid.uuid4()))  # kept before the call: a retry finds this transfer, never a second
    rec["reference"] = reference(rec)
    org._put("placement", rec, by)
    try:
        if not p.get("airwallex_beneficiary_id"):
            await bank.validate_beneficiary(pay["payload"])
            p["airwallex_beneficiary_id"] = await bank.create_beneficiary(pay["payload"])
            org._put(KIND, p, by)
        t = await bank.find_transfer(rec["request_id"])
        if t is None:
            ref = f"{rec['reference']} {p.get('our_reference') or ''}".strip()
            t = await bank.create_transfer(request_id=rec["request_id"], beneficiary_id=p["airwallex_beneficiary_id"], currency=rec["currency"],
                                           amount_minor=rec["amount_minor"], transfer_method=p.get("payout_method") or "LOCAL",
                                           reference=ref, reason="business_expenses")
    except AirwallexError as exc:
        return _fail(org, rec, by, f"Airwallex refused the transfer: {exc}")
    rec |= {"status": "SENT", "transfer_id": t.transfer_id, "transfer_status": t.status, "fee_minor": t.fee_minor, "sent_at": _now()}
    _hist(rec, by, f"sent to {p['name']} by Airwallex transfer {t.transfer_id} ({t.status})")
    org._put("placement", rec, by)
    return rec


async def refresh(org, bank, rec: dict, by: str) -> dict:
    if rec.get("status") != "SENT":
        return rec
    t = await bank.get_transfer(rec["transfer_id"])
    rec["transfer_status"] = t.status
    if t.status == "PAID":
        p = get(org, rec["provider_id"])
        days = p.get("tenor_days") or 0
        rec |= {"status": "PLACED", "placed_at": _now(),
                "matures_on": (dt.date.today() + dt.timedelta(days=days)).isoformat() if days else None}
        _hist(rec, by, f"placed with {p['name']}" + (f", matures {rec['matures_on']}" if days else ", flexible"))
    elif t.status in ("FAILED", "CANCELLED"):
        rec["status"] = "FAILED"
        _hist(rec, by, f"the transfer {t.status.lower()}: {t.failure_reason or 'no reason given'}; the money stays in the wallet")
    org._put("placement", rec, by)
    return rec


def request_withdrawal(org, rec: dict, expected_minor: int | None, by: str) -> dict:
    if rec.get("status") not in ("PLACED", "DONE"):
        raise ValueError(f"a {rec.get('status', '').lower()} placement cannot be withdrawn")
    p = get(org, rec["provider_id"])
    exp = int(expected_minor or estimate(rec, p))
    if exp < rec["amount_minor"] * 0.5:
        raise ValueError("the amount expected back is far below the principal: check it")
    rec |= {"status": "WITHDRAWAL_REQUESTED", "withdrawal": {"requested_at": _now(), "requested_by": by, "expected_minor": exp}}
    rec.setdefault("reference", reference(rec))
    how = {"PORTAL": f"in {p['name']}'s portal", "EMAIL": f"by email to {p.get('redeem_contact') or p['name']}",
           "API": f"through {p['name']}'s API", "AIRWALLEX_APP": "in the Airwallex app (Yield → Withdraw)"}[p["redeem_by"]]
    rec["withdrawal"]["instruction"] = (f"Redeem {rec['currency']} {exp / 100:,.2f} of placement {rec['reference']}"
                                        + (f" (our reference {p['our_reference']})" if p.get("our_reference") else "")
                                        + (f" {how}; it comes back to the Airwallex wallet, then close it with the amount back."
                                           if p["redeem_by"] == "AIRWALLEX_APP" else
                                           f" {how}, paid to the company's Airwallex Global Account with the reference {rec['reference']}."))
    _hist(rec, by, "withdrawal requested: " + rec["withdrawal"]["instruction"])
    org._put("placement", rec, by)
    return rec


def estimate(rec: dict, p: dict) -> int:
    """Principal plus the indicative return for the days held (simple interest): what VENTHORA expects back."""
    start = dt.date.fromisoformat((rec.get("placed_at") or rec.get("decided_at") or _now())[:10])
    days = max((dt.date.today() - start).days, p.get("tenor_days") or 0) if p["kind"] == "TERM_DEPOSIT" else (dt.date.today() - start).days
    return rec["amount_minor"] + int(rec["amount_minor"] * p.get("indicative_rate_pct", 0) / 100 * days / 365)


def candidates(org, rec: dict, deposits: list[dict]) -> list[dict]:
    """Deposits that may be this placement coming back: settled, same currency, not matched to another, the
    reference in the payer's text or the amount within 5% of what is expected."""
    taken = {r.get("closing", {}).get("deposit_id") for r in org.list("placement") if r.get("closing")}
    exp = (rec.get("withdrawal") or {}).get("expected_minor") or rec["amount_minor"]
    out = []
    for d in deposits:
        if d.get("id") in taken or str(d.get("currency", "")).upper() != rec["currency"] or str(d.get("status", "")).upper() not in ("SETTLED", "SUCCEEDED", "COMPLETED"):
            continue
        amt = _minor(d.get("amount"))
        payer = (d.get("payer") or {}).get("name") if isinstance(d.get("payer"), dict) else d.get("payer_name")  # Airwallex: payer.name
        text = " ".join(str(x or "") for x in (d.get("reference"), payer, d.get("description"), d.get("statement_ref"))).upper()
        by_ref = rec.get("reference", "~") in text
        if by_ref or abs(amt - exp) <= exp * 0.05:
            out.append({"deposit_id": d.get("id"), "amount_minor": amt, "currency": rec["currency"], "payer": payer,
                        "by_reference": by_ref, "created_at": d.get("created_at")})
    return sorted(out, key=lambda c: (not c["by_reference"], abs(c["amount_minor"] - exp)))


def _minor(v) -> int:
    from decimal import Decimal

    return int((Decimal(str(v or 0)) * 100).to_integral_value())


def close(org, rec: dict, *, received_minor: int, deposit_id: str | None, by: str) -> dict:
    if rec.get("status") not in ("WITHDRAWAL_REQUESTED", "DONE", "PLACED"):
        raise ValueError(f"a {rec.get('status', '').lower()} placement cannot be closed")
    if deposit_id and any((r.get("closing") or {}).get("deposit_id") == deposit_id for r in org.list("placement") if r["_id"] != rec["_id"]):
        raise ValueError("that deposit is already matched to another placement")
    ret = received_minor - rec["amount_minor"]
    rec |= {"status": "CLOSED", "closing": {"deposit_id": deposit_id, "received_minor": received_minor, "return_minor": ret, "closed_at": _now(), "closed_by": by}}
    _hist(rec, by, f"closed: {rec['currency']} {received_minor / 100:,.2f} back ({'+' if ret >= 0 else ''}{ret / 100:,.2f} {'return' if ret >= 0 else 'loss'})"
          + (f", deposit {deposit_id}" if deposit_id else ""))
    org._put("placement", rec, by)
    return rec


def _fail(org, rec: dict, by: str, why: str) -> dict:
    rec["status"] = "FAILED"
    _hist(rec, by, why)
    org._put("placement", rec, by)
    return rec


def _hist(rec: dict, by: str, what: str) -> None:
    rec.setdefault("history", []).append({"at": _now(), "by": by, "what": what})
