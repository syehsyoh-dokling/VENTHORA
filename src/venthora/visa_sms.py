# ruff: noqa: E501
"""Visa Supplier Matching Service: does this vendor accept Visa cards? (sandbox, X-Pay token)

VENTHORA asks Visa before it decides how to pay a vendor. A match ("Yes", with Visa's confidence) means the supplier
is a Visa merchant: the bill can be paid with a VENTHORA virtual card (Airwallex Issuing, Visa network), locked to the
merchant category code (MCC) Visa reports. No match means a bank transfer. Visa also says whether the supplier can
take Level 2 / Level 3 data (richer invoice data on the card payment) and fleet cards.

  POST https://sandbox.api.visa.com/visasuppliermatchingservice/v1/search?supplierName=...&supplierCountryCode=840...
  X-Pay token over the query string with its parameters SORTED by name (apikey included), percent-encoded (%20, not
  +), and the body "{}" (verified 6 Nov 2026: any other order or a + gives 9159 "Token Validation Failed").
  The sandbox answers with test data (every supplier "Yes", High, MCC 3501); the page says so.

Results are kept for 30 days per vendor (org store "visa_sms"), so a check costs one call a month per vendor.
"""
from __future__ import annotations

import datetime as dt
import urllib.parse

import httpx

from . import secret_values, visa_vic

PATH = "/visasuppliermatchingservice/v1/search"
KEEP_DAYS = 30
# ISO 3166-1 alpha-2 -> numeric, the form Visa expects in supplierCountryCode
ISO_COUNTRY = {"US": "840", "GB": "826", "DE": "276", "FR": "250", "NL": "528", "BE": "056", "IE": "372", "ES": "724", "IT": "380", "PT": "620",
               "AT": "040", "CH": "756", "SE": "752", "NO": "578", "DK": "208", "FI": "246", "PL": "616", "CZ": "203", "CA": "124", "MX": "484",
               "BR": "076", "AU": "036", "NZ": "554", "SG": "702", "HK": "344", "JP": "392", "KR": "410", "CN": "156", "IN": "356", "ID": "360",
               "MY": "458", "TH": "764", "PH": "608", "VN": "704", "AE": "784", "SA": "682", "IL": "376", "ZA": "710", "NG": "566", "KE": "404"}


class SMSError(RuntimeError):
    pass


def _country(code: str | None) -> str | None:
    c = (code or "").strip().upper()
    if c.isdigit() and len(c) == 3:
        return c
    return ISO_COUNTRY.get(c)


def rail(m: dict | None) -> dict:
    """How VENTHORA should pay: by Visa virtual card when Visa matches the supplier with high or medium confidence."""
    if not m or not m.get("ok"):
        return {"rail": "transfer", "why": "Visa could not be asked; pay by bank transfer"}
    if m["accepts_visa"] and m.get("confidence") in ("High", "Medium"):
        return {"rail": "card", "why": f"Visa matches the supplier ({m['confidence']} confidence{', MCC ' + m['mcc'] if m.get('mcc') else ''}): pay with a Visa virtual card locked to that merchant category"}
    if m["accepts_visa"]:
        return {"rail": "transfer", "why": f"Visa's match is only {m.get('confidence') or 'unknown'} confidence: pay by bank transfer unless the vendor confirms it takes cards"}
    return {"rail": "transfer", "why": "Visa does not know the supplier as a Visa merchant: pay by bank transfer"}


async def search(*, name: str, country: str, city: str = "", state: str = "", postal: str = "", street: str = "", phone: str = "", tax_id: str = "",
                 client: httpx.AsyncClient | None = None) -> dict:
    """One Supplier Matching search."""
    key = secret_values.get(visa_vic.API_KEY, visa_vic.ENV[visa_vic.API_KEY])
    secret = secret_values.get(visa_vic.SHARED_SECRET, visa_vic.ENV[visa_vic.SHARED_SECRET])
    if not key or not secret:
        return {"ok": False, "detail": "enter the Visa project's API key and shared secret first (Platform Setup → Visa Intelligent Commerce)"}
    cc = _country(country)
    if not name.strip() or not cc:
        return {"ok": False, "detail": f"a supplier name and a known country are needed (got {country!r})"}
    params = {"supplierName": name.strip()[:100], "supplierCountryCode": cc, "supplierCity": city, "supplierState": state, "supplierPostalCode": postal,
              "supplierStreetAddress": street, "supplierPhoneNumber": phone, "supplierTaxId": tax_id, "apikey": key}
    # sorted by name and percent-encoded (%20, not +), signed exactly as sent: the only form Visa's X-Pay check accepts
    query = urllib.parse.urlencode(sorted((k, str(v)) for k, v in params.items() if v), quote_via=urllib.parse.quote)
    body = "{}"
    headers = {"x-pay-token": visa_vic.xpay_token(PATH.split("/", 2)[2], query, body, secret), "accept": "application/json", "content-type": "application/json"}
    url = f"{visa_vic.SANDBOX}{PATH}?{query}"
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=30) as c:
                res = await c.post(url, content=body, headers=headers)
        else:
            res = await client.post(url, content=body, headers=headers)
    except httpx.HTTPError as exc:
        return {"ok": False, "detail": f"Visa did not answer: {type(exc).__name__}"}
    try:
        out = res.json()
    except ValueError:
        out = {}
    if res.status_code not in (200, 201):
        err = (out.get("responseStatus") or out) if isinstance(out, dict) else out
        return {"ok": False, "status": res.status_code, "detail": str(err)[:300]}
    det = out.get("matchDetails") or {}
    status = (out.get("matchStatus") or "").strip()
    return {"ok": True, "status": res.status_code, "match_status": status or "Unknown", "accepts_visa": status.lower() == "yes",
            "confidence": out.get("matchConfidence") or None, "mcc": det.get("mcc") or None, "level2": det.get("l2") == "Y",
            "level3": det.get("l3s") == "Y" or det.get("l3li") == "Y", "fleet": det.get("fleetInd") == "Y",
            "visa_status": (out.get("status") or {}).get("statusCode"), "at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "environment": "sandbox"}


def _vendor_query(vid: str, v: dict, ben: dict | None = None) -> dict:
    """The search fields from the vendor record, the Airwallex beneficiary's address (data/beneficiaries.json or the
    master data) filling what the vendor record lacks."""
    b = ((ben or {}).get("payload") or ben or {}).get("beneficiary") or {}
    addr = v.get("address") or b.get("address") or {}
    bank = v.get("bank") or b.get("bank_details") or {}
    name = v.get("name") or b.get("company_name") or vid
    return {"name": name, "country": addr.get("country_code") or bank.get("bank_country_code") or "",
            "city": addr.get("city") or "", "state": addr.get("state") or "", "postal": addr.get("postcode") or "",
            "street": addr.get("street_address") or "", "phone": v.get("registered_phone") or (v.get("lifecycle") or {}).get("registered_phone") or "",
            "tax_id": v.get("tax_id") or ""}


def stored(org, vid: str) -> dict | None:
    rec = org.get("visa_sms", vid)
    if not rec or not rec.get("at"):
        return None
    age = dt.datetime.now(dt.UTC) - dt.datetime.fromisoformat(rec["at"])
    return rec if age.days < KEEP_DAYS else None


async def check_vendor(org, vid: str, vendor: dict, *, ben: dict | None = None, fresh: bool = False, client: httpx.AsyncClient | None = None) -> dict:
    """Visa's answer for one registered vendor, kept for 30 days."""
    if not fresh:
        hit = stored(org, vid)
        if hit:
            return hit | {"cached": True}
    q = _vendor_query(vid, vendor, ben)
    if not q["country"]:
        return {"_id": vid, "vendor_id": vid, "name": q["name"], "ok": False, "detail": "the vendor has no country on file"} | {"rail": rail(None)}
    m = await search(client=client, **q)
    rec = {"_id": vid, "vendor_id": vid, "name": q["name"], "country": q["country"]} | m
    rec["rail"] = rail(m)
    if m.get("ok"):
        org._put("visa_sms", rec, "agent: Visa Supplier Matching")
    return rec


async def check_all(org, payees: dict, *, beneficiaries=None, fresh: bool = False, client: httpx.AsyncClient | None = None) -> list[dict]:
    """Every vendor and consultant (not staff accounts): may VENTHORA pay it by Visa card?"""
    out = []
    for vid, v in payees.items():
        if v.get("kind") == "employee" or v.get("budget_line") == "payroll":
            continue
        ben = beneficiaries.get(vid) if beneficiaries is not None else None
        out.append(await check_vendor(org, vid, v, ben=ben, fresh=fresh, client=client))
    return sorted(out, key=lambda r: (not r.get("accepts_visa"), r.get("name") or ""))
