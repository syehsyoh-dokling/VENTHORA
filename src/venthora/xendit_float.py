# ruff: noqa: E501
"""The Xendit float: the company's rupiah at Xendit that staff draws (staff_wallets.py) are paid from at once.

The FCO sets a low mark, a target and a daily cap once. When the float is below the low mark, VENTHORA tops it up to
the target by an Airwallex IDR transfer (funded from the base currency: Airwallex converts inside the transfer) to
the company's Xendit top-up account, a bank virtual account Xendit gives the business. That account passes the bank
check like any payee. A top-up is cash moving between two company accounts: never expense.

On the simulator, the PAID Airwallex transfer is what credits the simulated Xendit balance; on live, Xendit credits
the balance itself when its virtual account receives the money.
"""
from __future__ import annotations

import datetime as dt
import uuid

from . import bankcheck

KIND = "xendit_float"
CHECK_KEY = "float:xendit-topup"
DEFAULT = {"low_minor": 20_000_000, "target_minor": 50_000_000, "daily_cap_minor": 200_000_000, "auto": True}


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def policy(org) -> dict:
    rec = org.get(KIND, "policy") or {}
    return DEFAULT | {k: rec[k] for k in DEFAULT if k in rec} | {"topup_account": rec.get("topup_account"), "beneficiary_id": rec.get("beneficiary_id")}


def payload(acct: dict) -> dict:
    from .masterdata import BankDetails, beneficiary_payload

    bank = BankDetails.model_validate(acct["bank"] | {"account_currency": "IDR", "bank_country_code": "ID"}).model_dump()
    return beneficiary_payload({"name": acct["bank"]["account_name"], "entity_type": "COMPANY", "payout_method": "LOCAL",
                                "transfer_reason": "business_expenses", "bank": bank, "address": acct.get("address") or {}})


async def save_policy(org, bank, xendit, body: dict, by: str) -> dict:
    low, target, cap = int(body.get("low_minor", DEFAULT["low_minor"])), int(body.get("target_minor", DEFAULT["target_minor"])), int(body.get("daily_cap_minor", DEFAULT["daily_cap_minor"]))
    if not 0 < low < target:
        raise ValueError("the low mark is above zero and below the target")
    if cap < target - low:
        raise ValueError("the daily cap must allow at least one top-up from the low mark to the target")
    old = org.get(KIND, "policy") or {}
    rec = {"_id": "policy", "low_minor": low, "target_minor": target, "daily_cap_minor": cap, "auto": bool(body.get("auto", True)),
           "topup_account": old.get("topup_account"), "beneficiary_id": old.get("beneficiary_id"), "updated_by": by, "updated_at": _now()}
    acct = body.get("topup_account")
    if acct:
        company = org.standards().get("company_name", "")
        p = payload(acct)
        res = await bankcheck.run(bank, p, xendit)
        if res["verdict"] in bankcheck.REFUSED:
            raise ValueError(f"the Xendit top-up account is refused ({res['verdict']}): {res['detail']}")
        bankcheck.save(org, CHECK_KEY, res, by)
        if acct != old.get("topup_account"):
            rec["beneficiary_id"] = None
        rec["topup_account"] = acct
        rec["topup_check"] = {k: res.get(k) for k in ("verdict", "detail", "at")}
        rec["company_name"] = company
    org._put(KIND, rec, by)
    return policy(org)


def refills(org) -> list[dict]:
    return sorted((r for r in org.list(KIND) if r["_id"] != "policy"), key=lambda r: r["at"], reverse=True)


def _today_total(org) -> int:
    d = dt.date.today().isoformat()
    return sum(r["amount_minor"] for r in refills(org) if r["at"][:10] == d and r["status"] != "FAILED")


async def refill(org, bank, xendit, by: str, force: bool = False) -> dict | None:
    """Top the float up to the target when it is below the low mark (or `force`). None when nothing is needed."""
    from .gateways.airwallex import AirwallexError

    pol = policy(org)
    if not pol["topup_account"]:
        raise ValueError("no Xendit top-up account yet: set it in Platform Setup → Xendit (Indonesia)")
    if any(r["status"] == "SENT" for r in refills(org)):
        return None  # one top-up on its way at a time
    bal = await xendit.balance()
    if bal >= pol["low_minor"] and not force:
        return None
    amount = pol["target_minor"] - bal
    if amount <= 0:
        return None
    room = pol["daily_cap_minor"] - _today_total(org)
    if room <= 0:
        raise ValueError("today's float top-ups reached the daily cap set by the FCO")
    amount = min(amount, room)
    p = payload(pol["topup_account"])
    ok, why = bankcheck.passed(org, CHECK_KEY, p)
    if not ok:
        raise ValueError(f"the Xendit top-up account is not checked: {why}")
    rec = {"_id": "FT-" + uuid.uuid4().hex[:8].upper(), "at": _now(), "by": by, "amount_minor": amount, "float_before_minor": bal,
           "request_id": str(uuid.uuid4()), "status": "PENDING"}
    org._put(KIND, rec, by)
    try:
        if not pol["beneficiary_id"]:
            await bank.validate_beneficiary(p["payload"])
            pol_rec = org.get(KIND, "policy")
            pol_rec["beneficiary_id"] = await bank.create_beneficiary(p["payload"])
            org._put(KIND, pol_rec, by)
            pol = policy(org)
        base = org.standards().get("base_currency", "USD")
        t = await bank.create_transfer(request_id=rec["request_id"], beneficiary_id=pol["beneficiary_id"], currency="IDR", amount_minor=amount,
                                       transfer_method="LOCAL", reference=f"VENTHORA float {rec['_id']}", reason="business_expenses",
                                       source_currency=base if base != "IDR" else None)
    except AirwallexError as exc:
        rec |= {"status": "FAILED", "why": str(exc)}
        org._put(KIND, rec, by)
        return rec
    rec |= {"status": "SENT", "transfer_id": t.transfer_id, "transfer_status": t.status, "source_currency": t.source_currency,
            "source_minor": t.funded_minor, "fee_minor": t.fee_minor}
    org._put(KIND, rec, by)
    return rec


async def refresh(org, bank, xendit, by: str) -> list[dict]:
    """Read every top-up on its way back from Airwallex; on the simulator a PAID one credits the simulated float."""
    out = []
    for r in refills(org):
        if r["status"] != "SENT":
            continue
        t = await bank.get_transfer(r["transfer_id"])
        r["transfer_status"] = t.status
        if t.status == "PAID":
            r["status"] = "CREDITED"
            r["credited_at"] = _now()
            if xendit.simulated:
                r["float_after_minor"] = xendit.sim_topup(r["amount_minor"])
        elif t.status in ("FAILED", "CANCELLED"):
            r["status"] = "FAILED"
            r["why"] = t.failure_reason
        org._put(KIND, r, by)
        out.append(r)
    return out
