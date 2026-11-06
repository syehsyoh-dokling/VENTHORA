# ruff: noqa: E501
"""Bank details are checked before VENTHORA keeps them, and again before it pays: a wrong account never reaches
an Airwallex transfer.

Two checks, both on Airwallex, neither creating anything there:
  1. format   POST /beneficiaries/validate: the account number, IBAN and bank codes are right for the corridor (every
              country Airwallex pays to).
  2. account  POST /beneficiaries/verify_account: the account exists and is open, and the holder's name matches the
              name VENTHORA has (the banks' Confirmation / Verification of Payee). Only where Airwallex covers it:
              the United Kingdom, the SEPA countries and Viet Nam, by LOCAL transfer.
  3. Indonesia the holder's name of an Indonesian account, by Xendit's name check (gateways/xendit.py) when it is
              connected: Airwallex has no name check there. The payment itself still goes through Airwallex.

Verdicts:
  PASSED_NAME_MATCH  format right, the bank confirms the account is open and the name is a full match
  PASSED_FORMAT      format right; the country has no bank-side name check on Airwallex, so the call-back on the
                     registered number is what confirms the holder
  FORMAT_INVALID     refused: a field is wrong (named)
  ACCOUNT_INVALID    refused: the account does not exist or is closed
  NAME_MISMATCH      refused: the bank names another holder (the name it gives is shown)
  CANNOT_VERIFY      not passed: the bank could not check it (outside coverage, holder opted out, service down)
  UNCHECKED          not passed yet: no Airwallex account was connected when the details were saved

Only PASSED_* lets a payment go. A check belongs to the exact bank details it was made on (their fingerprint): a
change of bank details needs a new check.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

SEPA = {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IS", "IE", "IT", "LV", "LI", "LT",
        "LU", "MT", "NL", "NO", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "CH", "MC", "SM", "AD", "VA"}
NAME_CHECK = SEPA | {"GB", "VN"}
PASSED = ("PASSED_NAME_MATCH", "PASSED_FORMAT")
REFUSED = ("FORMAT_INVALID", "ACCOUNT_INVALID", "NAME_MISMATCH")
KIND = "bank_check"


def fingerprint(payload: dict | None) -> str | None:
    """The bank details of a beneficiary payload, hashed: a check is valid for these details only."""
    bd = (((payload or {}).get("payload") or payload or {}).get("beneficiary") or {}).get("bank_details")
    if not bd:
        return None
    return hashlib.sha256(json.dumps(bd, sort_keys=True).encode()).hexdigest()[:24]


def _verify_body(payload: dict) -> dict:
    b = payload["payload"]["beneficiary"]
    bd = b["bank_details"]
    keep = ("account_name", "account_number", "iban", "account_routing_type1", "account_routing_value1", "swift_code", "bank_country_code")
    return {"transfer_method": "LOCAL", "entity_type": "COMPANY" if b.get("entity_type") == "COMPANY" else "PERSONAL",
            "bank_details": {k: bd[k] for k in keep if bd.get(k)}}


async def _indonesian_name(xendit, bd: dict, base: dict) -> dict | None:
    """Indonesia has no name check on Airwallex: Xendit's name check fills that gap (the money still goes through
    Airwallex). None when Xendit cannot check this bank or has no name check active: the format verdict stands."""
    from .gateways.xendit import SWIFT_TO_CHANNEL, XenditError

    ch = SWIFT_TO_CHANNEL.get((bd.get("swift_code") or "")[:8].upper())
    if ch is None or xendit.mode == "off":
        return None
    try:
        res = await xendit.check_name(ch, bd.get("account_number") or "", bd.get("account_name") or "")
    except XenditError:
        return None
    if not res.get("available"):
        return None
    tag = {"name_check_by": "xendit" + (" (simulated)" if res.get("simulated") else ""), "resolved_account_name": res.get("resolved_name")}
    if res["result"] == "MATCH":
        return base | tag | {"verdict": "PASSED_NAME_MATCH", "passed": True, "name_match": "FULL_MATCH",
                             "detail": "format accepted by Airwallex; Xendit's name check confirms the account is in this name"}
    if res["result"] == "MISMATCH":
        return base | tag | {"verdict": "NAME_MISMATCH", "passed": False, "name_match": "NO_MATCH",
                             "detail": f"the bank holds this account in the name '{res.get('resolved_name')}' (Xendit's name check): correct the name or ask the vendor"}
    return base | tag | {"verdict": "ACCOUNT_INVALID", "passed": False, "detail": "Xendit's name check finds no account with this number at this bank"}


async def run(bank, payload: dict | None, name_checker=None) -> dict:
    """Check one beneficiary payload (masterdata.beneficiary_payload) on Airwallex. `bank` None: not connected.
    `name_checker`: the Xendit client, for the holder's name of an Indonesian account (Airwallex has none there)."""
    from .gateways.airwallex import AirwallexError

    at = datetime.now(UTC).isoformat(timespec="seconds")
    if payload is None:
        return {"verdict": "FORMAT_INVALID", "passed": False, "detail": "no bank details", "at": at, "fingerprint": None}
    fp = fingerprint(payload)
    base = {"fingerprint": fp, "at": at}
    if bank is None:
        return base | {"verdict": "UNCHECKED", "passed": False, "detail": "no Airwallex account connected yet: press Verify account once it is"}
    try:
        await bank.validate_beneficiary(payload["payload"])
    except AirwallexError as exc:
        return base | {"verdict": "FORMAT_INVALID", "passed": False, "detail": str(exc)[:400], "fields": getattr(exc, "fields", [])}
    bd = payload["payload"]["beneficiary"]["bank_details"]
    country, method = bd.get("bank_country_code"), (payload["payload"].get("transfer_methods") or ["LOCAL"])[0]
    if country == "ID" and method == "LOCAL" and name_checker is not None:
        held = await _indonesian_name(name_checker, bd, base)
        if held is not None:
            return held
    if country not in NAME_CHECK or method != "LOCAL":
        return base | {"verdict": "PASSED_FORMAT", "passed": True,
                       "detail": f"bank details accepted by Airwallex; {country} has no bank-side name check on Airwallex, so the call-back confirms the holder"}
    try:
        res = await bank.verify_account(_verify_body(payload))
    except AirwallexError as exc:
        return base | {"verdict": "FORMAT_INVALID", "passed": False, "detail": str(exc)[:400], "fields": getattr(exc, "fields", [])}
    code, det = res.get("code"), res.get("details") or {}
    named = {"resolved_account_name": det.get("resolved_account_name"), "resolved_bank_name": det.get("resolved_bank_name"),
             "name_match": det.get("account_name_match_result")}
    if code == "VERIFIED" and det.get("account_name_match_result") == "FULL_MATCH":
        return base | named | {"verdict": "PASSED_NAME_MATCH", "passed": True, "detail": "the bank confirms the account is open and in this name"}
    if code == "VERIFIED":
        return base | named | {"verdict": "NAME_MISMATCH", "passed": False,
                               "detail": f"the bank holds this account in the name '{det.get('resolved_account_name') or '?'}' ({det.get('account_name_match_result')}): correct the name or ask the vendor"}
    if code == "INVALID":
        return base | {"verdict": "ACCOUNT_INVALID", "passed": False, "detail": "the bank says this account does not exist or is closed"}
    return base | {"verdict": "CANNOT_VERIFY", "passed": False,
                   "detail": f"the bank could not verify this account ({code}): retry later, or ask the vendor for another account"}


def save(org, payee_id: str, result: dict, by: str) -> dict:
    org._put(KIND, {"_id": payee_id} | result, by)
    return result


def passed(org, payee_id: str, payload: dict | None) -> tuple[bool, str]:
    """May VENTHORA pay to these bank details? Only after a passed check on exactly these details."""
    rec = org.get(KIND, payee_id) or {}
    if not rec:
        return False, "the bank details were never checked: press Verify account"
    if rec.get("fingerprint") != fingerprint(payload):
        return False, "the bank details changed since they were checked: press Verify account"
    if rec.get("verdict") not in PASSED:
        return False, f"bank check {rec.get('verdict')}: {rec.get('detail')}"
    return True, rec["verdict"]
