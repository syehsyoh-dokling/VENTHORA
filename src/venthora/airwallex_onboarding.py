# ruff: noqa: E501
"""Open the company's Airwallex account from VENTHORA, asking only for what VENTHORA does not already know.

Every field Airwallex needs to open and verify a company account (connected account: POST /accounts/create, then
/update with the KYB details, then /submit) is listed here with where VENTHORA finds its value:

  Finance standards   the registered company name, the base currency (the revenue currency)
  Head office unit    the country the company is registered in
  People & roles      the FCO (Super admin): the primary contact's email, and by default the director's name
  Company profile     what the FCO typed the first time (registration number, address, industry, director ...):
                      kept, so it is never asked again

The FCO sees every value with its source before Create; nothing is sent until they confirm. The two Airwallex
agreements are never prefilled: they are ticked each time.
"""
from __future__ import annotations

import re

PROFILE = ("company_profile", "profile")
PLACEHOLDER_NAMES = {"acme group", "head office", ""}

# key, label, section, required
FIELDS = [
    ("business_name", "Registered company name", "Company", True),
    ("country_code", "Country of registration", "Company", True),
    ("business_structure", "Business structure", "Company", True),
    ("registration_number", "Registration number (UEN, NIB, SSM, CRN ...)", "Company", True),
    ("industry_code", "Industry (Airwallex category)", "Company", True),
    ("description", "What the business sells", "Company", True),
    ("address_line1", "Street address", "Registered address", True),
    ("suburb", "City / suburb", "Registered address", True),
    ("state", "State / province", "Registered address", True),
    ("postcode", "Postcode", "Registered address", True),
    ("monthly_revenue", "Estimated monthly revenue (major units)", "Business activity", True),
    ("revenue_currency", "Revenue currency", "Business activity", True),
    ("contact_email", "Primary contact email (the FCO)", "Primary contact", True),
    ("contact_mobile", "Primary contact mobile", "Primary contact", False),
    ("director_first_name", "Director first name", "Director (authorised person, beneficial owner)", True),
    ("director_last_name", "Director last name", "Director (authorised person, beneficial owner)", True),
    ("director_date_of_birth", "Director date of birth (YYYY-MM-DD)", "Director (authorised person, beneficial owner)", True),
    ("director_nationality", "Director nationality (2-letter)", "Director (authorised person, beneficial owner)", True),
    ("director_id_type", "Director ID type", "Director (authorised person, beneficial owner)", True),
]
CHOICES = {"business_structure": ["COMPANY", "PARTNERSHIP", "SOLE_PROPRIETOR", "LIMITED_LIABILITY_COMPANY", "TRUST", "OTHER"],
           "director_id_type": ["PASSPORT", "DRIVERS_LICENSE", "PERSONAL_ID"]}


def _fco(org) -> dict:
    users = [u for u in org.list("user") if u.get("role") == "super_admin" and u.get("active", True)]
    return users[0] if users else {}


def _hq(org) -> dict:
    units = org.list("unit")
    return next((u for u in units if u.get("unit_id") == "hq"), None) or next((u for u in units if not u.get("parent_id")), {}) or {}


def prefill(org) -> dict:
    """Every field with its value and where it came from; `missing` lists the required ones still empty."""
    st, hq, fco = org.standards(), _hq(org), _fco(org)
    prof = org.get(*PROFILE) or {}
    name = (fco.get("name") or "").strip()
    first, _, last = name.partition(" ")
    real_name = name.lower() not in ("super admin", "")
    company = st.get("company_name", "")
    found = {
        "business_name": (company, "Finance standards") if company.strip().lower() not in PLACEHOLDER_NAMES else (None, None),
        "country_code": (hq.get("country"), "Head office unit") if hq.get("country") else (None, None),
        "business_structure": ("COMPANY", "default for a company"),
        "revenue_currency": (st.get("base_currency"), "Finance standards (base currency)"),
        "contact_email": (fco.get("email"), "People & roles (FCO)") if fco.get("email") and not fco["email"].endswith(".example") else (None, None),
        "director_first_name": (first, "People & roles (the FCO, as director)") if real_name else (None, None),
        "director_last_name": (last or first, "People & roles (the FCO, as director)") if real_name else (None, None),
        "director_id_type": ("PASSPORT", "default"),
    }
    out, missing = [], []
    for key, label, section, required in FIELDS:
        if prof.get(key) not in (None, ""):
            val, src = prof[key], "Company profile (entered before)"
        else:
            val, src = found.get(key, (None, None))
        if key == "director_nationality" and val in (None, "") and found["country_code"][0]:
            val, src = found["country_code"][0], "Head office unit (country)"
        row = {"key": key, "label": label, "section": section, "required": required, "value": val, "source": src}
        if key in CHOICES:
            row["choices"] = CHOICES[key]
        out.append(row)
        if required and val in (None, ""):
            missing.append(key)
    return {"fields": out, "missing": missing, "fco": {"name": fco.get("name"), "email": fco.get("email")}}


def validate(values: dict) -> dict:
    v = {k: (str(values.get(k)).strip() if values.get(k) is not None else "") for k, *_ in FIELDS}
    errs = [label for key, label, _s, req in FIELDS if req and not v[key]]
    if errs:
        raise ValueError("still needed: " + ", ".join(errs))
    v["country_code"] = v["country_code"].upper()
    v["director_nationality"] = v["director_nationality"].upper()
    v["revenue_currency"] = v["revenue_currency"].upper()
    checks = [(re.fullmatch(r"[A-Z]{2}", v["country_code"]), "the country is a 2-letter code"),
              (re.fullmatch(r"[A-Z]{2}", v["director_nationality"]), "the nationality is a 2-letter code"),
              (re.fullmatch(r"[A-Z]{3}", v["revenue_currency"]), "the revenue currency is a 3-letter code"),
              (re.fullmatch(r"\d{4}-\d{2}-\d{2}", v["director_date_of_birth"]), "the date of birth is YYYY-MM-DD"),
              (re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v["contact_email"]), "the contact email is not an email address"),
              (v["monthly_revenue"].replace(".", "", 1).isdigit(), "the monthly revenue is a number"),
              (v["business_structure"] in CHOICES["business_structure"], "unknown business structure"),
              (v["director_id_type"] in CHOICES["director_id_type"], "unknown ID type")]
    bad = [msg for ok, msg in checks if not ok]
    if bad:
        raise ValueError("; ".join(bad))
    return v


def create_body(v: dict) -> dict:
    contact = {"email": v["contact_email"]} | ({"mobile": v["contact_mobile"]} if v.get("contact_mobile") else {})
    return {"account_details": {"legal_entity_type": "BUSINESS", "business_details": {"business_name": v["business_name"],
                                                                                    "registration_address": {"country_code": v["country_code"]}}},
            "primary_contact": contact, "customer_agreements": {"agreed_to_terms_and_conditions": True, "agreed_to_data_usage": True}}


def details_body(v: dict) -> dict:
    cc = v["country_code"]
    addr = {"address_line1": v["address_line1"], "suburb": v["suburb"], "state": v["state"], "postcode": v["postcode"], "country_code": cc}
    return {"account_details": {"legal_entity_type": "BUSINESS", "business_details": {
        "business_name": v["business_name"], "business_structure": v["business_structure"],
        "description_of_goods_or_services": v["description"], "industry_category_code": v["industry_code"],
        "operating_country": [cc], "registration_address": addr,
        "business_identifiers": [{"number": v["registration_number"], "type": "BRN", "country_code": cc}],
        "account_usage": {"product_reference": ["MAKE_TRANSFERS"],
                          "estimated_monthly_revenue": {"currency": v["revenue_currency"], "amount": v["monthly_revenue"]}}},
        "business_person_details": [{
            "first_name": v["director_first_name"], "last_name": v["director_last_name"], "date_of_birth": v["director_date_of_birth"],
            "nationality": v["director_nationality"], "roles": ["DIRECTOR", "AUTHORISED_PERSON", "BENEFICIAL_OWNER"], "residential_address": addr,
            "identifications": {"primary": {"identification_type": v["director_id_type"], "issuing_country_code": v["director_nationality"]}}}]}}


def keep(org, v: dict, by: str) -> None:
    """What the FCO entered is kept, so it is not asked again; the company name and country also go where VENTHORA
    reads them (Finance standards, the head office) when those still hold a placeholder."""
    org._put(PROFILE[0], {"_id": PROFILE[1]} | {k: v[k] for k, *_ in FIELDS}, by)
    st = org.get("standards", "standards") or {}
    if (st.get("company_name") or "Acme Group").strip().lower() in PLACEHOLDER_NAMES:
        org._put("standards", st | {"_id": "standards", "company_name": v["business_name"]}, by)
    hq = _hq(org)
    if hq and not hq.get("country"):
        org._put("unit", hq | {"country": v["country_code"]}, by)
